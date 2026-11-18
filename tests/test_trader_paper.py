import unittest

from scalpbot.broker import PaperBroker
from scalpbot.config import Config, OrderbookConfig
from scalpbot.events import EventLog
from scalpbot.orderbook import OrderbookFilter
from scalpbot.risk import SymbolFilters
from scalpbot.strategy import Signal
from scalpbot.trader import Trader


def make_filters():
    return SymbolFilters(
        symbol="TESTUSDT",
        tick_size=0.01,
        step_size=0.001,
        min_qty=0.001,
        max_qty=10_000.0,
        min_notional=5.0,
        qty_precision=3,
        price_precision=2,
    )


class TraderHarness(unittest.TestCase):
    def _filters_for(self, symbol):
        return self.filters[symbol]

    def _price_for(self, symbol):
        return self.prices.get(symbol)

    def setUp(self):
        cfg = Config()
        cfg.mode = "paper"
        cfg.risk.risk_pct = 2.0
        cfg.risk.leverage = 10
        cfg.risk.sl_atr_mult = 1.0
        cfg.risk.sl_min_bps = 1.0
        cfg.risk.sl_max_bps = 500.0
        cfg.risk.tp_rr = 2.0
        cfg.risk.be_trigger_r = 0.5
        cfg.risk.trail_trigger_r = 1.0
        cfg.risk.trail_atr_mult = 1.0
        cfg.risk.trail_step_bps = 1.0
        cfg.risk.fee_rate = 0.0004
        cfg.risk.slippage_bps = 0.0
        cfg.orderbook.enabled = False
        self.cfg = cfg
        self.filters = {"TESTUSDT": make_filters()}
        self.events = EventLog(capacity=200)
        self.book = OrderbookFilter(OrderbookConfig(enabled=False), lambda symbol, limit: None)
        self.prices = {"TESTUSDT": 100.0}
        self.broker = PaperBroker(
            cfg,
            filters_provider=self._filters_for,
            price_provider=self._price_for,
            start_balance=1000.0,
        )
        self.trader = Trader(
            cfg, self.broker, self.filters, self.events, self.book, interval_seconds=300
        )
        self.trader.equity = 1000.0
        self.trader.available = 1000.0
        self.trader.day.start_equity = 1000.0
        self.trader.set_mark("TESTUSDT", 100.0)

    def signal(self, side="LONG", price=100.0, atr=1.0):
        return Signal(
            symbol="TESTUSDT",
            side=side,
            ts=1_700_000_000_000,
            price=price,
            atr=atr,
            pac_center=price,
            pac_upper=price + 1,
            pac_lower=price - 1,
            trend=1 if side == "LONG" else -1,
            bars_since_opposite_pac=1,
            fast_ema=price + 2,
            medium_ema=price + 1,
            ha_close=price,
        )


class TestOpen(TraderHarness):
    def test_open_long_creates_position(self):
        pos = self.trader.try_open(self.signal(), self.filters["TESTUSDT"], bar_idx=10)
        self.assertIsNotNone(pos)
        self.assertIn("TESTUSDT", self.trader.positions)
        self.assertLess(pos.stop, pos.entry)
        self.assertGreater(pos.target, pos.entry)
        self.assertAlmostEqual(pos.risk_dist, pos.entry - pos.stop)
        self.assertAlmostEqual(round(pos.qty, 3), pos.qty, places=9)

    def test_position_size_respects_risk(self):
        pos = self.trader.try_open(self.signal(), self.filters["TESTUSDT"], bar_idx=10)
        risk_usd = pos.qty * pos.risk_dist
        self.assertLessEqual(risk_usd, 1000.0 * 0.02 * 1.001)

    def test_second_position_blocked_by_default(self):
        self.trader.try_open(self.signal(), self.filters["TESTUSDT"], bar_idx=10)
        self.assertIsNone(self.trader.try_open(self.signal(), self.filters["TESTUSDT"], bar_idx=11))

    def test_cooldown_blocks_reentry_after_close(self):
        self.assertIsNotNone(
            self.trader.try_open(self.signal(), self.filters["TESTUSDT"], bar_idx=10)
        )
        self.prices["TESTUSDT"] = 98.5
        self.trader.set_mark("TESTUSDT", 98.5)
        self.trader.manage_all()
        self.assertNotIn("TESTUSDT", self.trader.positions)
        self.assertIsNone(self.trader.try_open(self.signal(), self.filters["TESTUSDT"], bar_idx=10))

    def test_guard_blocks_when_max_positions_reached(self):
        self.cfg.risk.max_open_positions = 0
        self.assertIsNone(self.trader.try_open(self.signal(), self.filters["TESTUSDT"], bar_idx=1))

    def test_daily_loss_limit_halts(self):
        self.trader.day.realized = -60.0
        ok, reason = self.trader.guard_entry("TESTUSDT", bar_idx=1)
        self.assertFalse(ok)
        self.assertEqual(reason, "daily loss limit")
        self.assertTrue(self.trader.halted_reason)

    def test_or_short_open(self):
        pos = self.trader.try_open(self.signal("SHORT"), self.filters["TESTUSDT"], bar_idx=3)
        self.assertIsNotNone(pos)
        self.assertGreater(pos.stop, pos.entry)
        self.assertLess(pos.target, pos.entry)


class TestManage(TraderHarness):
    def open_long(self):
        pos = self.trader.try_open(self.signal(), self.filters["TESTUSDT"], bar_idx=10)
        self.assertIsNotNone(pos)
        return pos

    def test_breakeven_moves_stop_above_entry(self):
        pos = self.open_long()
        initial_stop = pos.stop
        self.prices["TESTUSDT"] = 100.6
        self.trader.set_mark("TESTUSDT", 100.6)
        self.trader.manage_all()
        self.assertTrue(pos.be_done)
        self.assertGreater(pos.stop, initial_stop)
        self.assertGreater(pos.stop, pos.entry)

    def test_trailing_activates_and_ratchets(self):
        pos = self.open_long()
        self.prices["TESTUSDT"] = 100.6
        self.trader.set_mark("TESTUSDT", 100.6)
        self.trader.manage_all()
        self.assertTrue(pos.be_done)
        self.prices["TESTUSDT"] = 101.2
        self.trader.set_mark("TESTUSDT", 101.2)
        pos.last_replace_ts = 0.0
        self.trader.manage_all()
        stop_after_trigger = pos.stop
        self.assertTrue(pos.trail_active)
        self.prices["TESTUSDT"] = 102.0
        self.trader.set_mark("TESTUSDT", 102.0)
        pos.last_replace_ts = 0.0
        self.trader.manage_all()
        self.assertGreater(pos.stop, stop_after_trigger)

    def test_stop_hit_records_loss(self):
        pos = self.open_long()
        stop = pos.stop
        self.prices["TESTUSDT"] = stop - 0.05
        self.trader.set_mark("TESTUSDT", stop - 0.05)
        self.trader.manage_all()
        self.assertNotIn("TESTUSDT", self.trader.positions)
        self.assertEqual(len(self.trader.trades), 1)
        trade = self.trader.trades[0]
        self.assertLess(trade.pnl_net, 0)
        self.assertLess(trade.r, 0)
        self.assertIn(trade.reason, ("SL", "BE", "TRAIL"))

    def test_target_hit_records_win(self):
        pos = self.open_long()
        target = pos.target
        self.prices["TESTUSDT"] = target + 0.05
        self.trader.set_mark("TESTUSDT", target + 0.05)
        self.trader.manage_all()
        self.assertEqual(len(self.trader.trades), 1)
        self.assertGreater(self.trader.trades[0].pnl_net, 0)
        self.assertEqual(self.trader.trades[0].reason, "TP")
        self.assertEqual(self.trader.day.wins, 1)

    def test_max_hold_forces_exit(self):
        self.cfg.risk.max_hold_minutes = 1
        pos = self.open_long()
        pos.opened_ts -= 120
        self.trader.manage_all()
        self.assertNotIn("TESTUSDT", self.trader.positions)
        self.assertEqual(self.trader.trades[0].reason, "TIMEOUT")

    def test_position_rows_for_dashboard(self):
        self.open_long()
        self.prices["TESTUSDT"] = 100.5
        self.trader.set_mark("TESTUSDT", 100.5)
        self.trader.manage_all()
        rows = self.trader.position_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "TESTUSDT")
        self.assertGreater(rows[0]["pnl"], 0)


if __name__ == "__main__":
    unittest.main()
