import json
import math
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scalpbot.broker import ProtectionOrders
from scalpbot.config import Config, ConfigError, load_config, validate_config
from scalpbot.events import redact
from scalpbot.risk import (
    SymbolFilters,
    plan_stops,
    round_step,
    size_position,
    wants_stop_update,
)
from scalpbot.trader import ManagedPosition, Trader

ROOT = Path(__file__).resolve().parents[1]


def filters(**kw):
    base = dict(
        symbol="TESTUSDT",
        tick_size=0.01,
        step_size=0.001,
        min_qty=0.001,
        max_qty=1000.0,
        min_notional=5.0,
        qty_precision=3,
        price_precision=2,
    )
    base.update(kw)
    return SymbolFilters(**base)


class TestConfigHardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"

    def write_raw(self, text):
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def test_nan_constant_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write_raw('{"risk": {"risk_pct": NaN}}'))

    def test_infinity_constant_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write_raw('{"risk": {"risk_pct": Infinity}}'))

    def test_overflow_float_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write_raw('{"risk": {"risk_pct": 1e999}}'))

    def test_wrong_type_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write_raw('{"market": {"warmup_bars": "1000"}}'))
        with self.assertRaises(ConfigError):
            load_config(self.write_raw('{"risk": {"leverage": true}}'))
        with self.assertRaises(ConfigError):
            load_config(self.write_raw('{"market": {"symbols": "BTCUSDT"}}'))

    def test_unvalidated_ranges_rejected(self):
        for payload in (
            {"api": {"timeout_s": 0}},
            {"api": {"max_retries": 0}},
            {"api": {"base_url_live": "http://fapi.binance.com"}},
            {"api": {"ws_live": "ws://fstream.binance.com"}},
            {"net": {"proxy": "ftp://localhost"}},
            {"orderbook": {"max_age_s": 0}},
            {"dashboard": {"log_lines": 0}},
            {"logging": {"max_bytes": 10}},
            {"risk": {"max_bump_factor": 0.5}},
            {"execution": {"max_slippage_bps": 0}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ConfigError):
                cfg = load_config(self.write_raw(json.dumps(payload)))
                validate_config(cfg)

    def test_live_config_needs_live_flag(self):
        cfg = load_config(self.write_raw('{"mode": "live"}'))
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        warnings = validate_config(cfg, live_requested=True)
        self.assertTrue(any("MODE LIVE" in w for w in warnings))

    def test_paper_balance_nan_rejected_by_cli(self):
        cfg_path = self.write_raw("{}")
        result = subprocess.run(  # noqa: RUF100, S603
            [
                sys.executable,
                "-m",
                "scalpbot",
                "run",
                "--paper",
                "--paper-balance",
                "nan",
                "-c",
                str(cfg_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("paper-balance", result.stdout)


class TestSecretRedaction(unittest.TestCase):
    def test_all_secret_shapes(self):
        self.assertIn('api_key="***"', redact('api_key="REALVALUE"'))
        self.assertIn('secret="***"', redact('secret="REALVALUE"'))
        self.assertEqual(
            redact("signature=" + "a" * 64),
            "signature=***",
        )
        self.assertEqual(redact("Bearer " + "Z" * 70), "Bearer ***")


class TestRiskEdgeCases(unittest.TestCase):
    def test_non_finite_sizing_rejected(self):
        cfg = Config().risk
        self.assertFalse(size_position(float("nan"), 100.0, 99.0, cfg, filters()).ok)
        self.assertFalse(size_position(1000.0, 100.0, float("inf"), cfg, filters()).ok)
        self.assertEqual(round_step(float("nan"), 0.001), 0.0)

    def test_max_qty_clamp_rechecks_min_notional(self):
        cfg = Config().risk
        cfg.risk_pct = 5.0
        cfg.leverage = 10
        cfg.max_notional_frac_equity = 20.0
        flt = filters(max_qty=0.01, min_notional=5.0)
        result = size_position(100_000.0, 100.0, 99.0, cfg, flt)
        self.assertFalse(result.ok)

    def test_infinite_atr_falls_back(self):
        cfg = Config().risk
        cfg.sl_mode = "atr"
        plan = plan_stops("LONG", 100.0, float("inf"), 101.0, 99.0, cfg, filters())
        self.assertTrue(math.isfinite(plan.stop))
        self.assertTrue(math.isfinite(plan.target))
        self.assertLess(plan.stop, 100.0)

    def test_breakeven_bypasses_trail_step(self):
        cfg = Config().risk
        cfg.trail_step_bps = 50.0
        self.assertFalse(wants_stop_update("LONG", 99.0, 99.001, 100.0, cfg))
        self.assertTrue(wants_stop_update("LONG", 99.0, 99.001, 100.0, cfg, apply_step=False))


class StubBroker:
    name = "stub"

    def __init__(self, positions=None):
        self.positions = positions or {}
        self.cancels = []

    def equity(self):
        return 1000.0, 1000.0

    def open_positions(self):
        return dict(self.positions)

    def cancel(self, symbol, order_id):
        self.cancels.append((symbol, order_id))

    def replace_stop(self, *args, **kwargs):
        return ProtectionOrders(stop_id="stub")

    def cancel_all(self, symbol):
        return None

    def emergency_close(self, *args, **kwargs):
        import time as _time

        from scalpbot.broker import Fill
        return Fill(order_id="em", avg_price=100.0, qty=1.0, ts=_time.time())

    def client(self):
        return None


class TestSyncAndMultiPosition(unittest.TestCase):
    def _trader(self, broker, cfg=None):
        cfg = cfg or Config()
        cfg.mode = "testnet"
        cfg.orderbook.enabled = False
        from scalpbot.events import EventLog
        from scalpbot.orderbook import OrderbookFilter

        return Trader(
            cfg,
            broker,
            {"TESTUSDT": filters()},
            EventLog(capacity=50),
            OrderbookFilter(cfg.orderbook, lambda s, lim: None),
            300,
        )

    def _position(self):
        return ManagedPosition(
            symbol="TESTUSDT",
            side="LONG",
            qty=1.0,
            entry=100.0,
            stop=99.0,
            target=101.0,
            initial_stop=99.0,
            risk_dist=1.0,
            atr=1.0,
            opened_ts=time.time(),
            signal_ts=0,
            stop_order_id="s1",
            tp_order_id="t1",
            key="k1",
        )

    def test_external_close_uses_fill_price_and_cancels_both_orders(self):
        broker = StubBroker()
        trader = self._trader(broker)
        pos = self._position()
        trader.positions["TESTUSDT"] = [pos]
        trader.set_mark("TESTUSDT", 100.0)
        trader.record_external_fill("TESTUSDT", 101.0)
        trader.sync_with_exchange(True)
        self.assertEqual(len(trader.trades), 1)
        self.assertAlmostEqual(trader.trades[0].exit, 101.0)
        self.assertEqual(trader.trades[0].reason, "EXCHANGE")
        self.assertEqual(len(broker.cancels), 2)
        self.assertEqual(trader.position_count(), 0)

    def test_tiny_entry_drift_does_not_reset_risk_dist(self):
        broker = StubBroker(
            {"TESTUSDT": {"qty": 1.0, "entry": 100.0000001, "side": "LONG", "mark": 100.0, "unrealized": 0.0}}
        )
        trader = self._trader(broker)
        pos = self._position()
        trader.positions["TESTUSDT"] = [pos]
        trader.sync_with_exchange(True)
        self.assertAlmostEqual(pos.risk_dist, 1.0)
        self.assertEqual(trader.position_count(), 1)

    def test_max_open_per_symbol_above_one(self):
        from scalpbot.broker import PaperBroker
        from scalpbot.strategy import Signal

        cfg = Config()
        cfg.mode = "paper"
        cfg.risk.max_open_positions = 2
        cfg.risk.max_open_per_symbol = 2
        cfg.risk.cooldown_bars = 0
        cfg.risk.sl_atr_mult = 1.0
        cfg.orderbook.enabled = False
        from scalpbot.events import EventLog
        from scalpbot.orderbook import OrderbookFilter

        broker = PaperBroker(
            cfg,
            filters_provider=lambda s: filters(),
            price_provider=lambda s: 100.0,
            start_balance=1000.0,
        )
        trader = Trader(
            cfg,
            broker,
            {"TESTUSDT": filters()},
            EventLog(capacity=50),
            OrderbookFilter(cfg.orderbook, lambda s, lim: None),
            300,
        )
        trader.equity = 1000.0
        trader.available = 1000.0
        trader.day.start_equity = 1000.0
        trader.set_mark("TESTUSDT", 100.0)

        def sig():
            return Signal(
                symbol="TESTUSDT",
                side="LONG",
                ts=1_700_000_000_000,
                price=100.0,
                atr=1.0,
                pac_center=100.0,
                pac_upper=101.0,
                pac_lower=99.0,
                trend=1,
                bars_since_opposite_pac=1,
                fast_ema=102.0,
                medium_ema=101.0,
                ha_close=100.0,
            )

        first = trader.try_open(sig(), filters(), bar_idx=10)
        second = trader.try_open(sig(), filters(), bar_idx=11)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(trader.position_count(), 2)
        self.assertEqual(len(trader.positions["TESTUSDT"]), 2)
        trader.close_position(first, "TP", 102.0)
        self.assertEqual(trader.position_count(), 1)
        self.assertIs(trader.positions["TESTUSDT"][0], second)


if __name__ == "__main__":
    unittest.main()
