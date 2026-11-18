import unittest

from scalpbot.config import RiskConfig
from scalpbot.risk import (
    SymbolFilters,
    breakeven_price,
    clamp_stop_distance,
    net_pnl,
    plan_stops,
    r_now,
    round_step,
    round_tick,
    size_position,
    trailing_stop,
    unrealized_pnl,
    wants_stop_update,
)


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


class TestRounding(unittest.TestCase):
    def test_round_step_down(self):
        self.assertAlmostEqual(round_step(1.23456, 0.001), 1.234)
        self.assertAlmostEqual(round_step(0.0009, 0.001), 0.0)

    def test_round_tick_direction(self):
        self.assertAlmostEqual(round_tick(1.005, 0.01), 1.01)
        self.assertAlmostEqual(round_tick(1.009, 0.01, "down"), 1.0)
        self.assertAlmostEqual(round_tick(1.001, 0.01, "up"), 1.01)

    def test_zero_step(self):
        self.assertEqual(round_step(1.5, 0), 1.5)
        self.assertEqual(round_tick(1.5, 0), 1.5)


class TestStops(unittest.TestCase):
    def setUp(self):
        self.cfg = RiskConfig()

    def test_long_atr_stop_below_entry(self):
        plan = plan_stops("LONG", 100.0, 1.0, 101.0, 99.0, self.cfg, filters())
        self.assertLess(plan.stop, 100.0)
        self.assertGreater(plan.target, 100.0)
        self.assertAlmostEqual(plan.risk_dist, 100.0 - plan.stop)
        self.assertAlmostEqual(plan.target - 100.0, plan.risk_dist * self.cfg.tp_rr, places=2)

    def test_min_distance_clamped(self):
        cfg = RiskConfig(sl_min_bps=50.0)
        plan = plan_stops("LONG", 100.0, 0.01, 101.0, 99.0, cfg, filters())
        self.assertGreaterEqual(plan.risk_bps, 49.9)

    def test_max_distance_clamped(self):
        cfg = RiskConfig(sl_max_bps=20.0)
        plan = plan_stops("LONG", 100.0, 50.0, 101.0, 99.0, cfg, filters())
        self.assertLessEqual(plan.risk_bps, 20.1)

    def test_short_direction(self):
        plan = plan_stops("SHORT", 100.0, 1.0, 101.0, 99.0, self.cfg, filters())
        self.assertGreater(plan.stop, 100.0)
        self.assertLess(plan.target, 100.0)

    def test_pac_mode(self):
        cfg = RiskConfig(sl_mode="pac")
        plan = plan_stops("LONG", 100.0, 1.0, 101.0, 98.5, cfg, filters())
        self.assertLessEqual(plan.stop, 98.5)

    def test_percent_mode(self):
        cfg = RiskConfig(sl_mode="percent", sl_percent=1.0)
        plan = plan_stops("LONG", 100.0, 0.5, 101.0, 99.0, cfg, filters())
        self.assertAlmostEqual(plan.risk_dist, 1.0, places=2)

    def test_clamp_helper(self):
        cfg = RiskConfig(sl_min_bps=10.0, sl_max_bps=100.0)
        stop = clamp_stop_distance("LONG", 100.0, 99.999, cfg, filters())
        self.assertLessEqual(stop, 99.9)
        self.assertGreaterEqual(stop, 99.0)


class TestSizing(unittest.TestCase):
    def setUp(self):
        self.cfg = RiskConfig(risk_pct=2.0, leverage=10, min_notional_bump=False)

    def test_simple_sizing(self):
        sizing = size_position(100.0, 100.0, 99.0, self.cfg, filters())
        self.assertTrue(sizing.ok)
        self.assertAlmostEqual(sizing.qty, 2.0)
        self.assertAlmostEqual(sizing.risk_usd, 2.0)
        self.assertAlmostEqual(sizing.notional, 200.0)

    def test_min_notional_reject(self):
        cfg = RiskConfig(risk_pct=0.05, leverage=10, min_notional_bump=False)
        sizing = size_position(50.0, 100.0, 80.0, cfg, filters())
        self.assertFalse(sizing.ok)
        self.assertIn("minimum", sizing.reason)

    def test_min_notional_bump_allowed(self):
        cfg = RiskConfig(risk_pct=1.0, leverage=20, min_notional_bump=True, max_bump_factor=3.0)
        sizing = size_position(50.0, 100.0, 80.0, cfg, filters())
        self.assertTrue(sizing.ok)
        self.assertTrue(sizing.bumped)
        self.assertGreaterEqual(sizing.notional, 5.0)

    def test_min_notional_bump_refused(self):
        cfg = RiskConfig(risk_pct=1.0, leverage=20, min_notional_bump=True, max_bump_factor=1.0)
        sizing = size_position(50.0, 100.0, 80.0, cfg, filters())
        self.assertFalse(sizing.ok)

    def test_notional_cap(self):
        cfg = RiskConfig(risk_pct=5.0, leverage=2, max_notional_frac_equity=1.0, min_notional_bump=False)
        sizing = size_position(100.0, 100.0, 99.0, cfg, filters(min_notional=1.0))
        self.assertTrue(sizing.ok)
        self.assertLessEqual(sizing.notional, 200.0 + 1e-9)

    def test_zero_equity(self):
        self.assertFalse(size_position(0.0, 100.0, 99.0, self.cfg, filters()).ok)

    def test_zero_sl_distance(self):
        self.assertFalse(size_position(100.0, 100.0, 100.0, self.cfg, filters()).ok)


class TestBreakevenAndTrail(unittest.TestCase):
    def setUp(self):
        self.cfg = RiskConfig(fee_rate=0.0004, slippage_bps=1.0, be_buffer_bps=2.0)

    def test_breakeven_covers_costs(self):
        be = breakeven_price("LONG", 100.0, self.cfg)
        self.assertGreater(be, 100.0)
        self.assertAlmostEqual(be, 100.0 * (1 + 0.0008 + 0.0001 + 0.0002), places=4)
        be_short = breakeven_price("SHORT", 100.0, self.cfg)
        self.assertLess(be_short, 100.0)

    def test_trailing_long_only_moves_up(self):
        cfg = RiskConfig(trail_atr_mult=1.0)
        f = filters()
        first = trailing_stop("LONG", 110.0, 1.0, 99.0, cfg, f, 100.5)
        self.assertAlmostEqual(first, 109.0)
        second = trailing_stop("LONG", 108.0, 1.0, first, cfg, f, 100.5)
        self.assertEqual(second, first)

    def test_trailing_respects_be_floor(self):
        cfg = RiskConfig(trail_atr_mult=1.0)
        stop = trailing_stop("LONG", 100.2, 1.0, 99.0, cfg, filters(), 100.5)
        self.assertGreaterEqual(stop, 100.5)

    def test_trailing_short(self):
        cfg = RiskConfig(trail_atr_mult=1.0)
        stop = trailing_stop("SHORT", 90.0, 1.0, 101.0, cfg, filters(), 99.5)
        self.assertAlmostEqual(stop, 91.0)
        stop2 = trailing_stop("SHORT", 92.0, 1.0, stop, cfg, filters(), 99.5)
        self.assertEqual(stop2, stop)

    def test_wants_update_threshold(self):
        cfg = RiskConfig(trail_step_bps=10.0)
        self.assertFalse(wants_stop_update("LONG", 100.0, 100.05, 105.0, cfg))
        self.assertTrue(wants_stop_update("LONG", 100.0, 101.0, 105.0, cfg))
        self.assertFalse(wants_stop_update("LONG", 100.0, 106.0, 105.0, cfg))
        self.assertTrue(wants_stop_update("SHORT", 100.0, 99.0, 95.0, cfg))


class TestPnL(unittest.TestCase):
    def test_unrealized(self):
        self.assertAlmostEqual(unrealized_pnl("LONG", 100.0, 101.0, 2.0), 2.0)
        self.assertAlmostEqual(unrealized_pnl("SHORT", 100.0, 99.0, 2.0), 2.0)

    def test_net_pnl_subtracts_fees(self):
        cfg = RiskConfig(fee_rate=0.0004)
        net = net_pnl("LONG", 100.0, 101.0, 1.0, cfg)
        self.assertAlmostEqual(net, 1.0 - (100.0 + 101.0) * 1.0 * 0.0004, places=6)
        self.assertAlmostEqual(net_pnl("SHORT", 100.0, 99.0, 1.0, cfg), 1.0 - (100.0 + 99.0) * 0.0004, places=6)

    def test_r_multiple(self):
        self.assertAlmostEqual(r_now("LONG", 100.0, 102.0, 2.0), 1.0)
        self.assertAlmostEqual(r_now("SHORT", 100.0, 98.0, 2.0), 1.0)
        self.assertEqual(r_now("LONG", 100.0, 101.0, 0.0), 0.0)


if __name__ == "__main__":
    unittest.main()
