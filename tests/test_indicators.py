import unittest

from scalpbot.indicators import (
    ATR,
    EMA,
    Candle,
    atr_series,
    ema_series,
    fractal_bottom,
    fractal_top,
    heikin_ashi_series,
    heikin_ashi_step,
    is_bw_fractal_bottom,
    is_bw_fractal_top,
    is_regular_fractal_bottom,
    is_regular_fractal_top,
    trend_direction,
)


class TestEMA(unittest.TestCase):
    def test_seed_is_sma_then_recursive(self):
        e = EMA(3)
        self.assertIsNone(e.update(1.0))
        self.assertIsNone(e.update(2.0))
        seeded = e.update(3.0)
        self.assertAlmostEqual(seeded, 2.0)
        nxt = e.update(4.0)
        self.assertAlmostEqual(nxt, 2.0 + (4.0 - 2.0) * 0.5)
        self.assertTrue(e.ready)

    def test_series_matches_manual(self):
        values = [float(i) for i in range(1, 11)]
        series = ema_series(values, 4)
        self.assertIsNone(series[0])
        self.assertAlmostEqual(series[3], sum(values[:4]) / 4)
        alpha = 2 / 5
        expected = series[3]
        for i in range(4, 10):
            expected = alpha * values[i] + (1 - alpha) * expected
            self.assertAlmostEqual(series[i], expected)

    def test_invalid_length(self):
        with self.assertRaises(ValueError):
            EMA(0)


class TestATR(unittest.TestCase):
    def test_wilder_smoothing(self):
        a = ATR(3)
        self.assertIsNone(a.update(10, 8, 9))
        self.assertIsNone(a.update(11, 9, 10))
        seed = a.update(12, 10, 11)
        self.assertAlmostEqual(seed, (2 + 2 + 2) / 3)
        nxt = a.update(13, 11, 12)
        self.assertAlmostEqual(nxt, (seed * 2 + 2) / 3)

    def test_series_len(self):
        candles = [Candle(i, 1, 2, 0.5, 1.5) for i in range(5)]
        self.assertEqual(len(atr_series(candles, 2)), 5)


class TestHeikinAshi(unittest.TestCase):
    def test_first_bar(self):
        o, h, lo, c = heikin_ashi_step(None, None, 10, 12, 9, 11)
        self.assertAlmostEqual(c, 10.5)
        self.assertAlmostEqual(o, 10.5)
        self.assertAlmostEqual(h, 12)
        self.assertAlmostEqual(lo, 9)

    def test_chain(self):
        o, h, lo, c = heikin_ashi_step(10.0, 11.0, 12, 13, 11, 12.5)
        self.assertAlmostEqual(o, 10.5)
        self.assertAlmostEqual(c, 12.125)
        self.assertAlmostEqual(h, 13)
        self.assertAlmostEqual(lo, 10.5)

    def test_series_values(self):
        candles = [
            Candle(0, 10, 12, 9, 11),
            Candle(1, 11, 13, 10, 12),
        ]
        ha = heikin_ashi_series(candles)
        self.assertEqual(len(ha), 2)
        self.assertAlmostEqual(ha[0].close, 10.5)
        self.assertAlmostEqual(ha[1].close, (11 + 13 + 10 + 12) / 4)
        self.assertAlmostEqual(ha[1].open, (10.5 + 10.5) / 2)


class TestFractals(unittest.TestCase):
    def test_bw_top(self):
        highs = [1.0, 2.0, 5.0, 4.0, 3.0]
        self.assertTrue(is_bw_fractal_top(highs))
        self.assertTrue(fractal_top(highs, regular_only=False))
        highs_relaxed = [1.0, 5.0, 5.0, 4.0, 3.0]
        self.assertTrue(fractal_top(highs_relaxed, regular_only=False))
        self.assertFalse(fractal_top(highs_relaxed, regular_only=True))

    def test_bw_top_flat(self):
        highs = [1.0, 5.0, 5.0, 4.0, 3.0]
        self.assertTrue(is_bw_fractal_top(highs))
        highs2 = [1.0, 2.0, 5.0, 5.0, 3.0]
        self.assertTrue(is_bw_fractal_top(highs2))

    def test_regular_top(self):
        highs = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertFalse(is_regular_fractal_top(highs))
        highs = [1.0, 2.0, 9.0, 4.0, 3.0]
        self.assertTrue(is_regular_fractal_top(highs))

    def test_bottom_mirror(self):
        lows = [5.0, 4.0, 1.0, 2.0, 3.0]
        self.assertTrue(is_bw_fractal_bottom(lows))
        self.assertTrue(fractal_bottom(lows, regular_only=False))
        lows_regular = [5.0, 4.0, 1.0, 2.0, 3.0]
        self.assertTrue(is_regular_fractal_bottom(lows_regular))

    def test_short_sequence(self):
        self.assertFalse(is_bw_fractal_top([1.0, 2.0]))
        self.assertFalse(is_regular_fractal_bottom([1.0]))


class TestTrend(unittest.TestCase):
    def test_up(self):
        self.assertEqual(trend_direction(10, 5, 6, 8), 1)

    def test_down(self):
        self.assertEqual(trend_direction(4, 5, 2, 3), -1)

    def test_flat(self):
        self.assertEqual(trend_direction(10, 5, 3, 8), 0)


if __name__ == "__main__":
    unittest.main()
