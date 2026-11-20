"""Regresi untuk perbaikan temuan Tahap 3.

Semua uji di sini offline: tidak ada koneksi Binance, tidak ada config.json
yang dibuat, dan cache selalu diarahkan ke direktori sementara.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import io
import json
import os
import pickle
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scalpbot.broker import PaperBroker, TestnetBroker as LiveBroker
from scalpbot.config import Config, ConfigError
from scalpbot.exchange import BinanceFuturesClient
from scalpbot.indicators import Candle
from scalpbot.risk import SymbolFilters, net_pnl, size_position
from scalpbot.strategy import Signal
from tools import backtest, tune
from tools.backtest import BacktestResult, Trade, combine, default_warmup, fill_price, simulate


def make_filters(symbol: str = "BTCUSDT") -> SymbolFilters:
    return SymbolFilters(
        symbol=symbol,
        tick_size=0.1,
        step_size=0.001,
        min_qty=0.001,
        max_qty=1000.0,
        min_notional=5.0,
        qty_precision=3,
        price_precision=1,
    )


def fixture_candles() -> list[Candle]:
    raw = json.loads((ROOT / "tests" / "fixtures" / "btc_5m.json").read_text(encoding="utf-8"))
    return [
        Candle(ts=int(row[0]), open=row[1], high=row[2], low=row[3], close=row[4], volume=0.0)
        for row in raw
    ]


def base_cfg() -> Config:
    cfg = Config()
    cfg.mode = "paper"
    cfg.strategy.regime_min_slope_bps = 0.0
    cfg.risk.sl_atr_mult = 1.0
    cfg.risk.sl_min_bps = 5.0
    cfg.risk.sl_max_bps = 500.0
    cfg.risk.tp_rr = 1.0
    cfg.risk.be_trigger_r = 0.0
    cfg.risk.trail_trigger_r = 0.0
    cfg.risk.trail_atr_mult = 0.0
    cfg.risk.cooldown_bars = 0
    cfg.risk.slippage_bps = 0.0
    cfg.risk.max_notional_frac_equity = 1.0
    return cfg


def synthetic_candles(count: int, start_ts: int = 1_700_000_000_000, step_ms: int = 300_000) -> list[Candle]:
    return [
        Candle(
            ts=start_ts + i * step_ms,
            open=100.0 + i * 0.01,
            high=100.1 + i * 0.01,
            low=99.9 + i * 0.01,
            close=100.0 + i * 0.01,
            volume=1.0,
        )
        for i in range(count)
    ]


class FakeStrategy:
    """Strategi deterministik untuk menguji ATR entry, throttle, dan urutan exit."""

    def __init__(self, symbol: str, cfg, atr_len: int = 14) -> None:
        self.symbol = symbol
        self.cfg = cfg
        self.atr_len = atr_len
        self.idx = -1

    def on_bar(self, candle: Candle):
        self.idx += 1
        return SimpleNamespace(
            signal="LONG" if self.idx == 0 else None,
            atr=1.0 if self.idx == 0 else 10.0,
        )

    def build_signal(self, state) -> Signal:
        return Signal(
            symbol=self.symbol,
            side="LONG",
            ts=0,
            price=100.0,
            atr=1.0,
            pac_center=100.0,
            pac_upper=101.0,
            pac_lower=99.0,
            trend=1,
            bars_since_opposite_pac=0,
            fast_ema=100.0,
            medium_ema=100.0,
            ha_close=100.0,
        )

    def regime_allows(self, state) -> bool:
        return True


def trailing_candles() -> list[Candle]:
    return [
        Candle(ts=1_000, open=100.0, high=100.1, low=99.9, close=100.0, volume=1.0),
        Candle(ts=1_300, open=100.0, high=102.0, low=99.9, close=102.0, volume=1.0),
        Candle(ts=1_600, open=102.0, high=103.0, low=98.9, close=103.0, volume=1.0),
    ]


def trailing_cfg(throttle_seconds: float) -> Config:
    cfg = Config()
    cfg.mode = "paper"
    cfg.risk.sl_mode = "percent"
    cfg.risk.sl_percent = 1.0
    cfg.risk.tp_rr = 100.0
    cfg.risk.be_trigger_r = 0.0
    cfg.risk.be_buffer_bps = 2.0
    cfg.risk.trail_trigger_r = 1.0
    cfg.risk.trail_atr_mult = 1.2
    cfg.risk.trail_step_bps = 0.0
    cfg.risk.fee_rate = 0.0
    cfg.risk.slippage_bps = 0.0
    cfg.risk.cooldown_bars = 0
    cfg.risk.max_hold_minutes = 0
    cfg.risk.max_notional_frac_equity = 1.0
    cfg.execution.cancel_replace_min_s = throttle_seconds
    return cfg


class PagedIncomeClient:
    def __init__(self) -> None:
        self.pages: list[int] = []

    def income_history(
        self,
        income_type="REALIZED_PNL",
        start_ms=None,
        end_ms=None,
        limit=1000,
        page=None,
    ):
        self.pages.append(int(page or 1))
        if income_type != "REALIZED_PNL":
            return []
        if int(page or 1) == 1:
            return [
                {
                    "incomeType": income_type,
                    "income": "-0.01",
                    "symbol": "BTCUSDT",
                    "time": 1_000 + i,
                    "tranId": i + 1,
                }
                for i in range(1000)
            ]
        return [
            {
                "incomeType": income_type,
                "income": "-0.01",
                "symbol": "BTCUSDT",
                "time": 2_000,
                "tranId": 1001,
            }
        ]


class TruncatedIncomeClient:
    def income_history(
        self,
        income_type="REALIZED_PNL",
        start_ms=None,
        end_ms=None,
        limit=1000,
        page=None,
    ):
        return [{"incomeType": income_type, "income": "-0.01", "symbol": "BTCUSDT"}] * 1000


class TestPrioritasPertama(unittest.TestCase):
    def test_tr01_tune_help_tidak_crash(self):
        proc = subprocess.run(
            [sys.executable, "-m", "tools.tune", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("65%", proc.stdout)
        self.assertNotIn("TypeError", proc.stderr)

    def test_tr02_cache_bukan_pickle_dan_tolak_payload_berbahaya(self):
        source = (ROOT / "tools" / "tune.py").read_text(encoding="utf-8")
        self.assertNotIn("import pickle", source)
        self.assertNotIn("pickle.loads", source)
        self.assertNotIn("/tmp/bt", source)
        self.assertIn("json.loads", source)
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker"
            cache = Path(tmp) / "cache.json"

            class Payload:
                def __reduce__(self):
                    return (os.system, (f"printf PWNED > {marker}",))

            cache.write_bytes(pickle.dumps(Payload()))
            with patch.object(tune, "CACHE", cache):
                result = tune._read_cache("5m", ["BTCUSDT"], "testnet", "https://x", 1, 1000, 3, 3600)
            self.assertIsNone(result)
            self.assertFalse(marker.exists())
            cache.write_text('{"schema":"x","bad":NaN}', encoding="utf-8")
            with patch.object(tune, "CACHE", cache):
                result = tune._read_cache("5m", ["BTCUSDT"], "testnet", "https://x", 1, 1000, 3, 3600)
            self.assertIsNone(result)

    def test_tr03_fee_entry_tidak_dihitung_dua_kali(self):
        cfg = base_cfg()
        res = simulate("BTCUSDT", fixture_candles(), cfg, make_filters(), equity=100.0, warmup=650)
        self.assertGreater(res.n, 0)
        self.assertAlmostEqual(res.equity_end - res.equity_start, sum(t.pnl for t in res.trades), places=8)
        for trade in res.trades:
            expected = net_pnl(trade.side, trade.entry, trade.exit, trade.qty, cfg.risk)
            self.assertAlmostEqual(trade.pnl, expected, places=8)

    def test_tr04_sizing_memakai_size_position(self):
        cfg = base_cfg()
        res = simulate("BTCUSDT", fixture_candles(), cfg, make_filters(), equity=100.0, warmup=650)
        self.assertGreater(res.n, 0)
        first = res.trades[0]
        live = size_position(100.0, first.entry, first.stop, cfg.risk, make_filters())
        self.assertTrue(live.ok)
        self.assertAlmostEqual(first.qty, live.qty, places=8)

    def test_tr12_income_history_mendukung_page_dan_paginasi(self):
        cfg = Config()
        client = BinanceFuturesClient(cfg)
        captured = {}

        def fake_request(method, path, params=None, signed=False, retries=1):
            captured.update(params or {})
            return []

        with patch.object(client, "_request", side_effect=fake_request):
            client.income_history("COMMISSION", start_ms=1, end_ms=2, limit=500, page=2)
        client.close()
        self.assertEqual(captured["page"], 2)
        self.assertEqual(captured["limit"], 500)

        rows_client = PagedIncomeClient()
        broker = LiveBroker(rows_client, Config())
        info = broker.income_today()
        self.assertIsNotNone(info)
        self.assertFalse(info["truncated"])
        self.assertEqual(info["counts"]["REALIZED_PNL"], 1001)
        self.assertAlmostEqual(info["total"], -10.01, places=6)
        self.assertIn(2, rows_client.pages)
        self.assertIsNotNone(broker.realized_since(0))
        self.assertAlmostEqual(broker.realized_since(0), -10.01, places=6)

    def test_tr12_halaman_penuh_tanpa_id_dianggap_terpotong(self):
        broker = LiveBroker(TruncatedIncomeClient(), Config())
        info = broker.income_today()
        self.assertTrue(info["truncated"])
        self.assertIsNone(broker.realized_today())
        self.assertIsNone(broker.realized_since(0))
        main_source = (ROOT / "scalpbot" / "__main__.py").read_text(encoding="utf-8")
        self.assertIn('bot_info.get("truncated")', main_source)


class TestPrioritasKedua(unittest.TestCase):
    def test_tr05_slippage_masuk_harga_fill(self):
        cfg0 = base_cfg()
        cfg100 = base_cfg()
        cfg100.risk.slippage_bps = 100.0
        self.assertEqual(fill_price("LONG", 100.0, cfg100.risk, True), 101.0)
        self.assertEqual(fill_price("SHORT", 100.0, cfg100.risk, True), 99.0)
        self.assertEqual(fill_price("LONG", 100.0, cfg100.risk, False), 99.0)
        res0 = simulate("BTCUSDT", fixture_candles(), cfg0, make_filters(), equity=100.0, warmup=650)
        res100 = simulate("BTCUSDT", fixture_candles(), cfg100, make_filters(), equity=100.0, warmup=650)
        self.assertNotAlmostEqual(res0.equity_end, res100.equity_end, places=4)
        paper = PaperBroker(cfg100, lambda s: make_filters(s), lambda s: 100.0, start_balance=100.0)
        self.assertAlmostEqual(paper.place_market("BTCUSDT", "LONG", 1.0, "x").avg_price, 101.0, places=8)

    def test_tr06_trailing_memakai_atr_entry_dan_throttle(self):
        with patch.object(backtest, "ScalpPullbackStrategy", FakeStrategy):
            res = simulate(
                "BTCUSDT",
                trailing_candles(),
                trailing_cfg(0.0),
                make_filters(),
                equity=100.0,
                warmup=0,
                interval_seconds=300,
            )
            slow = simulate(
                "BTCUSDT",
                trailing_candles(),
                trailing_cfg(600.0),
                make_filters(),
                equity=100.0,
                warmup=0,
                interval_seconds=300,
            )
        self.assertEqual(res.n, 1)
        self.assertEqual(slow.n, 1)
        self.assertGreater(res.trades[0].stop, 100.5)
        self.assertAlmostEqual(slow.trades[0].stop, 99.0, places=6)
        source = inspect.getsource(simulate)
        self.assertIn("trailing_stop(side, price, entry_atr", source)
        self.assertIn("bars_held - last_trail_bar >= throttle_bars", source)
        self.assertIn("mark_stop", source)

    def test_tr07_warmup_tidak_hardcode_800(self):
        cfg = Config()
        self.assertEqual(default_warmup(cfg), max(cfg.market.warmup_bars, 808))
        cfg.market.warmup_bars = 1200
        cfg.strategy.regime_min_slope_bps = 0.0
        self.assertEqual(default_warmup(cfg), 1200)
        self.assertIsNone(inspect.signature(simulate).parameters["warmup"].default)
        self.assertIsNone(inspect.signature(tune.evaluate).parameters["warmup"].default)
        self.assertIsNone(inspect.signature(tune.walk_forward).parameters["warmup"].default)
        for path in (ROOT / "tools" / "tune.py", ROOT / "tools" / "backtest.py"):
            self.assertNotIn("warmup: int = 800", path.read_text(encoding="utf-8"))

    def test_tr08_seleksi_grid_hanya_pada_data_latih(self):
        data = {
            "candles": {"BTCUSDT": synthetic_candles(10)},
            "filters": {"BTCUSDT": make_filters()},
        }
        train, _ = tune.split_data(data)
        captured = []
        metrics = {"n": 1, "win": 100.0, "net": 1.0, "expR": 1.0, "pf": 9.99}

        def fake_evaluate(part, cfg, warmup=None, interval_s=300):
            captured.append(part)
            return metrics

        grid = {"risk__sl_atr_mult": [5.0]}
        with (
            patch.object(tune, "load_data", return_value=data),
            patch.object(tune, "GRIDS", {"quick": grid, "cost": grid}),
            patch.object(tune, "evaluate", side_effect=fake_evaluate),
            patch.object(tune, "walk_forward", return_value=(1.0, 0.5)),
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = tune.main(["--days", "1", "--symbols", "BTCUSDT", "--walk-forward"])
        self.assertEqual(code, 0)
        self.assertEqual(len(captured), 1)
        self.assertEqual(len(captured[0]["candles"]["BTCUSDT"]), len(train["candles"]["BTCUSDT"]))
        self.assertIn("seleksi hanya pada 65% pertama", out.getvalue())
        source = inspect.getsource(tune.main)
        self.assertLess(source.index("rows.sort"), source.index("if args.walk_forward"))
        self.assertIn("evaluate(train,", source)

    def test_tr08_walk_forward_memakai_warmup_sama(self):
        data = {
            "candles": {"BTCUSDT": synthetic_candles(20)},
            "filters": {"BTCUSDT": make_filters()},
        }
        cfg = tune.make_cfg()
        calls = []
        warmup = default_warmup(cfg)

        def fake_simulate(symbol, candles, config, filters, **kwargs):
            calls.append(kwargs.get("warmup"))
            trade = Trade(symbol, "LONG", 100, 101, 99, 102, "TP", 1, 1.0, 1.0, 1, 2, 1.0)
            return BacktestResult(trades=[trade], equity_start=100.0, equity_end=101.0)

        with patch.object(tune, "simulate", side_effect=fake_simulate):
            ins, oos = tune.walk_forward(data, cfg, interval_s=300)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls, [warmup, warmup])
        self.assertAlmostEqual(ins, 1.0)
        self.assertAlmostEqual(oos, 1.0)

    def test_tr09_interval_dan_metadata_cache(self):
        self.assertIn("2h", tune.INTERVAL_SECONDS)
        data = {
            "candles": {"BTCUSDT": synthetic_candles(10)},
            "filters": {"BTCUSDT": make_filters()},
        }
        grid = {"risk__sl_atr_mult": [5.0]}
        with (
            patch.object(tune, "load_data", return_value=data) as loader,
            patch.object(tune, "GRIDS", {"quick": grid, "cost": grid}),
            patch.object(tune, "evaluate", return_value={"n": 0, "win": 0, "net": 0, "expR": 0, "pf": 0}),
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = tune.main(["--days", "1", "--interval", "2h", "--symbols", "BTCUSDT"])
        self.assertEqual(code, 0)
        self.assertEqual(loader.call_args.args[2], "2h")

        cfg = Config()
        cfg.mode = "paper"
        days = 1
        interval = "5m"
        need, warmup, _ = tune._required_bars(days, interval, cfg)
        candles = synthetic_candles(need + 5)
        filters = {"BTCUSDT": make_filters()}
        payload = tune._cache_payload(
            {"BTCUSDT": candles},
            filters,
            ["BTCUSDT"],
            interval,
            days,
            warmup,
            need,
            cfg,
            cfg.base_url().rstrip("/"),
        )
        args = ("5m", ["BTCUSDT"], cfg.mode, cfg.base_url().rstrip("/"), days, warmup, need, 3600)
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache.json"
            with patch.object(tune, "CACHE", cache):
                tune._write_cache(payload)
                self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
                parsed = tune._read_cache(*args)
                self.assertIsNotNone(parsed)
                self.assertGreaterEqual(len(parsed[0]["BTCUSDT"]), need)
                wrong = ("15m",) + args[1:]
                self.assertIsNone(tune._read_cache(*wrong))
                self.assertIsNone(tune._read_cache(*args[:-1], 0))
                payload["fetched_at"] = int(time.time() * 1000) - 10_000_000
                tune._write_cache(payload)
                self.assertIsNone(tune._read_cache(*args))
                link = Path(tmp) / "link.json"
                link.symlink_to(cache)
                with patch.object(tune, "CACHE", link):
                    self.assertIsNone(tune._read_cache(*args))

    def test_tr09_load_data_memakai_cache_tanpa_jaringan(self):
        cfg = Config()
        days = 1
        interval = "5m"
        need, warmup, _ = tune._required_bars(days, interval, cfg)
        candles = synthetic_candles(need + 10)
        filters = {"BTCUSDT": make_filters()}
        payload = tune._cache_payload(
            {"BTCUSDT": candles},
            filters,
            ["BTCUSDT"],
            interval,
            days,
            warmup,
            need,
            cfg,
            cfg.base_url().rstrip("/"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache.json"
            with patch.object(tune, "CACHE", cache):
                tune._write_cache(payload)
                with patch.object(tune, "BinanceFuturesClient", side_effect=AssertionError("jaringan terlarang")):
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        data = tune.load_data(days, ["BTCUSDT"], interval)
        self.assertEqual(len(data["candles"]["BTCUSDT"]), need)
        self.assertIsInstance(data["filters"]["BTCUSDT"], SymbolFilters)

    def test_tr10_make_cfg_menolak_typo_dan_nilai_invalid(self):
        cfg = tune.make_cfg(risk__sl_atr_mult=3.0)
        self.assertEqual(cfg.risk.sl_atr_mult, 3.0)
        with self.assertRaises(ValueError):
            tune.make_cfg(risk__sl_atr_mult_typo=9.9)
        with self.assertRaises(ConfigError):
            tune.make_cfg(risk__risk_pct=500.0)

    def test_tr11_default_sama_dengan_contoh(self):
        cfg = Config()
        example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg.risk.max_open_positions, example["risk"]["max_open_positions"])
        self.assertEqual(cfg.risk.max_notional_frac_equity, example["risk"]["max_notional_frac_equity"])
        self.assertEqual((cfg.risk.max_open_positions, cfg.risk.max_notional_frac_equity), (3, 1.0))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("REALIZED_PNL + COMMISSION + FUNDING_FEE", readme)
        self.assertIn("00:00 WIB", readme)
        self.assertIn("arsip", readme.lower())


class TestPrioritasKetiga(unittest.TestCase):
    def test_tr13_combine_kronologis_dan_bootstrap_blok(self):
        def tr(ts: int, pnl: float) -> Trade:
            return Trade("X", "LONG", 100.0, 100.0, 99.0, 101.0, "SL", 1, pnl / 10.0, pnl, ts, ts + 1, 1.0)

        r1 = BacktestResult(trades=[tr(1, -10.0), tr(3, 20.0)], equity_start=100.0, equity_end=110.0)
        r2 = BacktestResult(trades=[tr(2, -30.0)], equity_start=100.0, equity_end=70.0)
        combined = combine([r1, r2])
        self.assertEqual([t.ts_entry for t in combined.trades], [1, 2, 3])
        self.assertGreater(combined.max_drawdown_pct(), 39.0)
        empty = combine([])
        self.assertEqual(empty.equity_end, empty.equity_start)
        rs = [Trade("X", "LONG", 100, 101, 99, 102, "TP", 1, (i % 3) - 1.0, 1.0, i, i + 1, 1.0) for i in range(40)]
        result = BacktestResult(trades=rs)
        low, high = result.expectancy_ci(samples=400, seed=11, block_size=5)
        self.assertLessEqual(low, high)
        low2, high2 = result.expectancy_ci(samples=400, seed=11, block_size=5)
        self.assertEqual((low, high), (low2, high2))
        source = inspect.getsource(BacktestResult.expectancy_ci)
        self.assertIn("block_size", source)
        self.assertIn("sorted(self.trades", source)

    def test_tr14_tools_bebas_pola_berisiko_dan_modul_tercakup(self):
        tune_tree = ast.parse((ROOT / "tools" / "tune.py").read_text(encoding="utf-8"))
        backtest_tree = ast.parse((ROOT / "tools" / "backtest.py").read_text(encoding="utf-8"))
        banned = {"pickle", "random", "subprocess", "marshal", "shelve"}
        for tree in (tune_tree, backtest_tree):
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertFalse(imported & banned, f"impor terlarang: {imported & banned}")
        self.assertNotIn("/tmp/", (ROOT / "tools" / "tune.py").read_text(encoding="utf-8"))
        self.assertNotIn("eval(", (ROOT / "tools" / "tune.py").read_text(encoding="utf-8"))
        self.assertNotIn("exec(", (ROOT / "tools" / "tune.py").read_text(encoding="utf-8"))
        self.assertTrue(hasattr(tune, "load_data"))
        self.assertTrue(hasattr(backtest, "simulate"))


if __name__ == "__main__":
    unittest.main()
