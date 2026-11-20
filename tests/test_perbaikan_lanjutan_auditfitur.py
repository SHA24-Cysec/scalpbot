import json
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scalpbot.__main__ import _state_summary
from scalpbot.config import Config
from scalpbot.dashboard import Dashboard, DashboardConfig
from scalpbot.events import EventLog
from scalpbot.indicators import Candle
from scalpbot.orderbook import OrderbookFilter
from scalpbot.risk import SymbolFilters
from scalpbot.state import (
    LOSS_RULE,
    MAX_LOSS_SYMBOL_ENTRIES,
    VERSION,
    StateStore,
)
from scalpbot.strategy import Signal
from scalpbot.timeutil import format_ts
from scalpbot.trader import ManagedPosition, Trader
from tools import backtest, tune


def filters(symbol):
    return SymbolFilters(
        symbol=symbol,
        tick_size=0.01,
        step_size=0.001,
        min_qty=0.001,
        max_qty=1000.0,
        min_notional=5.0,
        qty_precision=3,
        price_precision=2,
    )


class StubBroker:
    name = "paper"

    def equity(self):
        return (100.0, 100.0)

    def open_positions(self):
        return {}

    def open_protection(self, symbol):
        return [
            {"side": "SELL", "type": "STOP_MARKET", "trigger": 99.0, "id": "sl-1"},
            {"side": "SELL", "type": "TAKE_PROFIT_MARKET", "trigger": 101.0, "id": "tp-1"},
        ]


def make_trader(tmp, timezone="WIB"):
    cfg = Config()
    cfg.mode = "paper"
    cfg.logging.persist_state = True
    cfg.logging.timezone = timezone
    cfg.risk.cooldown_bars = 0
    cfg.risk.daily_loss_limit_pct = 5.0
    cfg.risk.max_open_per_symbol = 2
    cfg.risk.loss_symbol_day_block = True
    cfg.orderbook.enabled = False
    events = EventLog(capacity=200)
    trader = Trader(
        cfg,
        StubBroker(),
        {"BTCUSDT": filters("BTCUSDT"), "ETHUSDT": filters("ETHUSDT")},
        events,
        OrderbookFilter(cfg.orderbook, lambda symbol, limit: None),
        300,
    )
    trader.equity = trader.available = 100.0
    trader.day.start_equity = 100.0
    store = StateStore(Path(tmp) / "state.json")
    trader.attach_state(store)
    return trader, store


def make_position(symbol="BTCUSDT"):
    return ManagedPosition(
        symbol=symbol,
        side="LONG",
        qty=1.0,
        entry=100.0,
        stop=99.0,
        target=104.0,
        initial_stop=99.0,
        risk_dist=1.0,
        atr=0.2,
        opened_ts=time.time() - 60,
        signal_ts=1_700_000_000_000,
    )


def messages(trader):
    return [event.message for event in trader.events.tail(200)]


class TrackingRLock:
    def __init__(self):
        self._lock = threading.RLock()
        self.held = False

    def __enter__(self):
        self._lock.acquire()
        self.held = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.held = False
        self._lock.release()


class TrackingDays(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock = None
        self.get_held = None

    def get(self, key, default=None):
        self.get_held = bool(self.lock and self.lock.held)
        return super().get(key, default)


class AlternatingStrategy:
    def __init__(self, symbol: str, cfg, atr_len: int = 14) -> None:
        self.symbol = symbol
        self.idx = -1

    def on_bar(self, candle):
        self.idx += 1
        return SimpleNamespace(
            signal="LONG" if self.idx in (0, 3, 289) else None,
            atr=1.0,
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


def alternating_candles():
    base_ms = int(
        datetime(2026, 11, 18, 16, 55, tzinfo=UTC).timestamp() * 1000
    )
    rows = []
    for i in range(291):
        if i == 0:
            row = (100.0, 100.1, 99.9, 100.0)
        elif i == 1:
            row = (100.0, 100.1, 98.9, 99.0)
        elif i == 3:
            row = (100.0, 100.1, 99.9, 100.0)
        elif i == 4:
            row = (100.0, 102.1, 99.9, 102.0)
        elif i == 289:
            row = (100.0, 100.1, 99.9, 100.0)
        elif i == 290:
            row = (100.0, 102.1, 99.9, 102.0)
        else:
            row = (100.0, 100.1, 99.9, 100.0)
        rows.append(
            Candle(
                ts=base_ms + i * 300_000,
                open=row[0],
                high=row[1],
                low=row[2],
                close=row[3],
                volume=1.0,
            )
        )
    return rows


def alternating_cfg(on):
    cfg = Config()
    cfg.mode = "paper"
    cfg.risk.sl_mode = "percent"
    cfg.risk.sl_percent = 1.0
    cfg.risk.sl_min_bps = 5.0
    cfg.risk.sl_max_bps = 500.0
    cfg.risk.tp_rr = 2.0
    cfg.risk.be_trigger_r = 0.0
    cfg.risk.trail_trigger_r = 0.0
    cfg.risk.trail_atr_mult = 0.0
    cfg.risk.cooldown_bars = 0
    cfg.risk.slippage_bps = 0.0
    cfg.risk.fee_rate = 0.0
    cfg.risk.max_hold_minutes = 0
    cfg.risk.max_notional_frac_equity = 1.0
    cfg.risk.loss_symbol_day_block = on
    return cfg


class TestPerbaikanLanjutanAuditFitur(unittest.TestCase):
    def test_af06_parser_menolak_tanggal_dan_simbol_tidak_kanonik(self):
        blob = {
            "version": VERSION,
            "loss_symbol_days": {
                "BTCUSDT": "2026-11-19",
                "ETHUSDT": "2026-1-9",
                "btcusdt": "2026-11-19",
                "BAD SYMBOL": "2026-11-19",
                "TOOLONGSYMBOLUSDTXXXXX": "2026-11-19",
            },
            "loss_symbol_ts": {"BTCUSDT": 1_700_000_000, 99: 1_700_000_000},
        }
        days = StateStore.loss_days_from_state(blob)
        stamps = StateStore.loss_timestamps_from_state(blob)
        self.assertEqual(days, {"BTCUSDT": "2026-11-19"})
        self.assertEqual(list(stamps), ["BTCUSDT"])

    def test_af06_batas_entri_state_tetap_berlaku(self):
        blob = {
            "version": VERSION,
            "loss_symbol_days": {
                f"S{i:05d}USDT": "2026-11-19"
                for i in range(MAX_LOSS_SYMBOL_ENTRIES + 10)
            },
        }
        days = StateStore.loss_days_from_state(blob)
        self.assertEqual(len(days), MAX_LOSS_SYMBOL_ENTRIES)

    def test_af07_event_buka_blokir_hanya_untuk_hari_aktif(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader, _ = make_trader(tmp)
            with trader._lock:
                trader._loss_symbol_days["BTCUSDT"] = "2001-01-01"
            pos = make_position()
            trader.positions[pos.symbol] = [pos]
            trader._record_close(pos, "TP", 101.0)
            self.assertFalse(
                any("blokir entry dibuka kembali" in text for text in messages(trader))
            )

    def test_af08_penanda_aturan_disimpan_dan_state_lama_diperingatkan(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader, store = make_trader(tmp)
            trader._persist_state()
            data = json.loads(Path(store.path).read_text(encoding="utf-8"))
            self.assertEqual(data["loss_rule"], LOSS_RULE)
            self.assertNotIn("tanpa penanda", "\n".join(messages(trader)))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": VERSION,
                        "tz": "WIB",
                        "loss_symbol_days": {"BTCUSDT": "2026-11-19"},
                        "loss_symbol_ts": {"BTCUSDT": 1_700_000_000},
                    }
                ),
                encoding="utf-8",
            )
            trader, _ = make_trader(tmp)
            self.assertIn("tanpa penanda aturan", "\n".join(messages(trader)))

    def test_af09_adopsi_posisi_terblokir_tetap_protektif_dengan_peringatan(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader, _ = make_trader(tmp)
            trader._loss_symbol_days["BTCUSDT"] = trader._today()
            pos = trader.adopt_position("BTCUSDT", "LONG", 1.0, 100.0)
            self.assertIsNotNone(pos)
            self.assertIn("BTCUSDT", trader.positions)
            self.assertIn("tetap diadopsi", "\n".join(messages(trader)))
            ok, reason = trader.guard_entry("BTCUSDT", 10, 0)
            self.assertFalse(ok)
            self.assertIn("trade terakhir rugi", reason)

    def test_af09_adopsi_membaca_status_blokir_di_bawah_kunci(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader, _ = make_trader(tmp)
            tracker = TrackingRLock()
            days = TrackingDays({"BTCUSDT": trader._today()})
            days.lock = tracker
            trader._lock = tracker
            trader._loss_symbol_days = days
            pos = trader.adopt_position("BTCUSDT", "LONG", 1.0, 100.0)
            self.assertIsNotNone(pos)
            self.assertTrue(days.get_held)
            self.assertFalse(tracker.held)
            self.assertIn("tetap diadopsi", "\n".join(messages(trader)))

    def test_af10_cli_state_bukan_object_ditolak(self):
        with self.assertRaises(TypeError):
            _state_summary([])

    def test_af10_cli_dan_dashboard_menampilkan_detail_blokir(self):
        blob = {
            "version": VERSION,
            "loss_symbol_days": {"BTCUSDT": "2026-11-19", "ETHUSDT": "2026-11-19"},
            "loss_symbol_ts": {"BTCUSDT": 1_700_000_000, "ETHUSDT": 1_700_000_000},
            "cooldown": {"BTCUSDT": 1},
            "entries": ["a"],
            "loss_rule": LOSS_RULE,
            "saved_at": "2026-11-19 00:00:00 WIB",
        }
        info = _state_summary(blob)
        self.assertIn("2 blokir rugi", info)
        self.assertIn("BTCUSDT", info)
        self.assertIn(LOSS_RULE, info)
        frame = Dashboard(DashboardConfig(enabled=True), tty=False).build_frame(
            {"loss_blocked": 2, "loss_blocked_symbols": ["BTCUSDT", "ETHUSDT"]},
            100,
            30,
        )
        self.assertIn("BLOK", frame)
        self.assertIn("ETHUSDT", frame)

    def test_af11_backtest_memodelkan_blokir_dan_reset_hari(self):
        with patch.object(backtest, "ScalpPullbackStrategy", AlternatingStrategy):
            blocked = backtest.simulate(
                "BTCUSDT",
                alternating_candles(),
                alternating_cfg(True),
                filters("BTCUSDT"),
                equity=100.0,
                warmup=0,
                interval_seconds=300,
            )
            plain = backtest.simulate(
                "BTCUSDT",
                alternating_candles(),
                alternating_cfg(False),
                filters("BTCUSDT"),
                equity=100.0,
                warmup=0,
                interval_seconds=300,
            )
        self.assertLess(blocked.n, plain.n)
        self.assertGreaterEqual(blocked.skipped.get("loss_block", 0), 1)
        self.assertEqual(plain.skipped.get("loss_block", 0), 0)
        self.assertEqual(
            format_ts(blocked.trades[-1].ts_exit / 1000.0, "WIB", "%Y-%m-%d"),
            "2026-11-20",
        )

    def test_af11_trade_untung_membuka_blokir_pada_helper(self):
        self.assertEqual(
            backtest.loss_block_day_after_close(1.0, 1_700_000_000_000, "WIB"), ""
        )
        self.assertEqual(
            backtest.loss_block_day_after_close(0.0, 1_700_000_000_000, "WIB"),
            "2023-11-15",
        )
        self.assertEqual(
            backtest.loss_block_day_after_close(-1.0, 1_700_000_000_000, "WIB"),
            "2023-11-15",
        )

    def test_af11_tune_flag_mematikan_blokir_pada_konfigurasi(self):
        seen = []

        def fake_evaluate(data, cfg, warmup=None, interval_s=300):
            seen.append(cfg.risk.loss_symbol_day_block)
            return {"n": 0, "win": 0.0, "net": 0.0, "expR": 0.0, "pf": 0.0}

        with patch.object(tune, "load_data", return_value={"candles": {}, "filters": {}}), \
             patch.object(tune, "split_data", side_effect=lambda d: (d, d)), \
             patch.object(tune, "evaluate", fake_evaluate), \
             patch.object(tune, "GRIDS", {"quick": {"risk__sl_atr_mult": [1.0]}}):
            self.assertEqual(tune.main(["--tanpa-blokir-rugi"]), 0)
        self.assertEqual(seen, [False])


if __name__ == "__main__":
    unittest.main()
