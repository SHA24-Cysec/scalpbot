import unittest

from scalpbot.orderbook import OrderbookFilter, analyze_depth
from scalpbot.config import OrderbookConfig


def book(bids, asks):
    return {
        "bids": [[str(p), str(q)] for p, q in bids],
        "asks": [[str(p), str(q)] for p, q in asks],
    }


class TestAnalyzeDepth(unittest.TestCase):
    def test_balanced(self):
        snap = analyze_depth("TESTUSDT", book([(100, 1), (99, 1)], [(101, 1), (102, 1)]), 20)
        self.assertIsNotNone(snap)
        self.assertAlmostEqual(snap.imbalance, 0.0, places=6)
        self.assertAlmostEqual(snap.spread_bps, 99.5, places=1)
        self.assertAlmostEqual(snap.mid, 100.5)

    def test_bid_heavy(self):
        snap = analyze_depth("TESTUSDT", book([(100, 10)], [(101, 1)]), 20)
        self.assertGreater(snap.imbalance, 0.7)

    def test_ask_heavy(self):
        snap = analyze_depth("TESTUSDT", book([(100, 1)], [(101, 10)]), 20)
        self.assertLess(snap.imbalance, -0.7)

    def test_weights_prefer_top_levels(self):
        flat = analyze_depth("TESTUSDT", book([(100, 1), (99, 5)], [(101, 1), (102, 1)]), 20, weights=False)
        weighted = analyze_depth("TESTUSDT", book([(100, 1), (99, 5)], [(101, 1), (102, 1)]), 20, weights=True)
        self.assertNotAlmostEqual(flat.imbalance, weighted.imbalance, places=3)

    def test_invalid_payloads(self):
        self.assertIsNone(analyze_depth("X", {}, 20))
        self.assertIsNone(analyze_depth("X", {"bids": [], "asks": []}, 20))
        self.assertIsNone(analyze_depth("X", {"bids": [["bad", "1"]], "asks": [["1", "1"]]}, 20))
        self.assertIsNone(analyze_depth("X", book([(100, 1)], [(99, 1)]), 20))

    def test_depth_limit(self):
        snap = analyze_depth("X", book([(100, 1), (99, 100)], [(101, 1), (102, 1)]), 1)
        self.assertAlmostEqual(snap.imbalance, 0.0, places=6)
        self.assertEqual(snap.levels, 1)


class TestOrderbookFilter(unittest.TestCase):
    def setUp(self):
        self.cfg = OrderbookConfig(enabled=True, min_imbalance=0.15, max_spread_bps=5.0)
        self.book = book([(100, 10)], [(100.01, 1)])

    def _filter(self, payload=None):
        return OrderbookFilter(self.cfg, lambda s, lim: payload if payload is not None else self.book)

    def test_pass_long(self):
        f = self._filter()
        snap = f.snapshot("TESTUSDT", force=True)
        ok, reason = f.check("LONG", snap)
        self.assertTrue(ok, reason)

    def test_reject_long_on_ask_heavy(self):
        f = self._filter(book([(100, 1)], [(100.01, 10)]))
        snap = f.snapshot("TESTUSDT", force=True)
        ok, _ = f.check("LONG", snap)
        self.assertFalse(ok)

    def test_reject_wide_spread(self):
        f = self._filter(book([(100, 10)], [(101, 1)]))
        snap = f.snapshot("TESTUSDT", force=True)
        ok, reason = f.check("LONG", snap)
        self.assertFalse(ok)
        self.assertIn("spread", reason)

    def test_disabled_filter_passes(self):
        cfg = OrderbookConfig(enabled=False)
        f = OrderbookFilter(cfg, lambda s, lim: self.book)
        self.assertIsNone(f.snapshot("TESTUSDT", force=True))
        ok, _ = f.check("LONG", None)
        self.assertTrue(ok)

    def test_stale_snapshot_rejected(self):
        f = self._filter()
        snap = f.snapshot("TESTUSDT", force=True)
        snap.ts -= 60.0
        ok, reason = f.check("LONG", snap)
        self.assertFalse(ok)
        self.assertIn("terlalu lama", reason)

    def test_failed_refresh_does_not_return_stale_snapshot(self):
        cfg = OrderbookConfig(enabled=True, max_age_s=1.0)
        f = OrderbookFilter(cfg, lambda s, lim: self.book)
        snap = f.snapshot("TESTUSDT", force=True)
        self.assertIsNotNone(snap)
        snap.ts -= 10.0
        f._fetch = lambda s, lim: None
        self.assertIsNone(f.snapshot("TESTUSDT", force=True))

    def test_missing_fetch_falls_back(self):
        f = OrderbookFilter(self.cfg, lambda s, lim: None)
        self.assertIsNone(f.snapshot("TESTUSDT", force=True))
        ok, reason = f.check("SHORT", None)
        self.assertTrue(ok)
        self.assertIn("tidak tersedia", reason)


if __name__ == "__main__":
    unittest.main()
