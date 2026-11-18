import random
import unittest

from scalpbot.config import StrategyConfig
from scalpbot.indicators import Candle
from scalpbot.strategy import ScalpPullbackStrategy


def synthetic_candles(count=1600, drift=-0.0005, seed=11):
    rng = random.Random(seed)
    price = 100.0
    candles = []
    for i in range(count):
        o = price
        price = price * (1 + drift) * (1 + rng.gauss(0, 0.0012))
        high = max(o, price) * (1 + abs(rng.gauss(0, 0.0005)))
        low = min(o, price) * (1 - abs(rng.gauss(0, 0.0005)))
        candles.append(Candle(i * 300_000, o, high, low, price, 1.0))
    return candles


class TestStrategyWarmup(unittest.TestCase):
    def test_no_signal_before_warmup(self):
        cfg = StrategyConfig()
        strat = ScalpPullbackStrategy("TESTUSDT", cfg, 14)
        for candle in synthetic_candles(400):
            state = strat.on_bar(candle)
            self.assertIsNone(state.signal)

    def test_pac_values_absent_early(self):
        strat = ScalpPullbackStrategy("TESTUSDT", StrategyConfig(), 14)
        state = strat.on_bar(Candle(0, 1, 2, 0.5, 1.5))
        self.assertIsNone(state.pac_center)
        self.assertEqual(state.trend, 0)
        self.assertIsNone(state.signal)


class TestStrategySignals(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig()
        self.candles = synthetic_candles()
        self.strat = ScalpPullbackStrategy("TESTUSDT", self.cfg, 14)
        self.states = [self.strat.on_bar(c) for c in self.candles]

    def test_signal_present(self):
        signals = [s for s in self.states if s.signal]
        self.assertGreaterEqual(len(signals), 1)

    def test_signal_invariants(self):
        prev_direction = 0
        warmup = self.strat.warmup_bars()
        for state in self.states:
            if state.signal == "LONG":
                self.assertEqual(state.trend, 1)
                self.assertEqual(prev_direction, 0)
                self.assertEqual(state.trade_direction, 1)
                self.assertGreater(state.idx, warmup)
                self.assertGreater(state.price, 0)
                self.assertIsNotNone(state.pac_upper)
                self.assertGreater(state.atr or 0, 0)
            elif state.signal == "SHORT":
                self.assertEqual(state.trend, -1)
                self.assertEqual(prev_direction, 0)
                self.assertEqual(state.trade_direction, -1)
                self.assertGreater(state.idx, warmup)
                self.assertLess(state.price, state.pac_lower or 0)
            prev_direction = state.trade_direction

    def test_reset_after_opposite_pac_close(self):
        cfg = StrategyConfig()
        strat = ScalpPullbackStrategy("TESTUSDT", cfg, 14)
        for candle in synthetic_candles(800, drift=0.0006, seed=21):
            strat.on_bar(candle)
        strat.trade_direction = -1
        last_close = strat.last_bar.ha_close
        pac_center = strat.last_bar.pac_center or 0.0
        target = pac_center + max(pac_center * 0.005, 0.01)
        candle = Candle(999_999, last_close, target * 1.01, last_close * 0.99, target, 1.0)
        state = strat.on_bar(candle)
        self.assertGreater(state.ha_close, state.pac_center or 0)
        self.assertEqual(state.trade_direction, 0)

    def test_reset_keeps_direction_inside_pac(self):
        cfg = StrategyConfig()
        strat = ScalpPullbackStrategy("TESTUSDT", cfg, 14)
        for candle in synthetic_candles(800, drift=-0.0006, seed=23):
            strat.on_bar(candle)
        strat.trade_direction = 1
        last_close = strat.last_bar.ha_close
        pac_center = strat.last_bar.pac_center or 0.0
        target = pac_center + max(pac_center * 0.001, 0.001)
        candle = Candle(999_999, last_close, target * 1.001, last_close * 0.999, target, 1.0)
        state = strat.on_bar(candle)
        if state.ha_close >= (state.pac_center or 0):
            self.assertEqual(state.trade_direction, 1)

    def test_build_signal_fields(self):
        target = next(s for s in self.states if s.signal)
        signal = self.strat.build_signal(target)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "TESTUSDT")
        self.assertIn(signal.side, ("LONG", "SHORT"))
        self.assertGreater(signal.atr, 0)
        self.assertGreaterEqual(signal.bars_since_opposite_pac, 0)

    def test_build_signal_none_without_signal(self):
        flat = next(s for s in self.states if s.signal is None)
        self.assertIsNone(self.strat.build_signal(flat))

    def test_zone_classification(self):
        for state in self.states[-50:]:
            if state.ha_close > (state.pac_upper or 0):
                self.assertEqual(state.bar_zone, "above")
            elif state.ha_close < (state.pac_lower or 0):
                self.assertEqual(state.bar_zone, "below")
            else:
                self.assertEqual(state.bar_zone, "inside")

    def test_use_ha_toggle_changes_values(self):
        cfg = StrategyConfig(use_ha=False)
        strat = ScalpPullbackStrategy("TESTUSDT", cfg, 14)
        states = [strat.on_bar(c) for c in self.candles[:200]]
        self.assertNotEqual(states[-1].ha_close, self.candles[199].close) if cfg.use_ha else None
        self.assertAlmostEqual(states[-1].ha_close, self.candles[199].close)


class TestStrategyReset(unittest.TestCase):
    def test_reset_clears_state(self):
        strat = ScalpPullbackStrategy("TESTUSDT", StrategyConfig(), 14)
        for c in synthetic_candles(700):
            strat.on_bar(c)
        strat.reset()
        self.assertEqual(strat.idx, -1)
        self.assertEqual(strat.trade_direction, 0)
        self.assertIsNone(strat.last_bar)


if __name__ == "__main__":
    unittest.main()
