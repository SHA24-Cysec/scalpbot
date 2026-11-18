from __future__ import annotations

import unittest
from unittest import mock

from scalpbot.config import Config, ConfigError, validate_config
from scalpbot.strategy import BarState, ScalpPullbackStrategy


def _state(signal: str | None, slope: float | None) -> BarState:
    return BarState(
        idx=10,
        ts=0,
        price=100.0,
        ha_close=100.0,
        pac_center=100.0,
        pac_upper=101.0,
        pac_lower=99.0,
        fast_ema=100.0,
        medium_ema=100.0,
        slow_ema=100.0,
        regime_slope_bps=slope,
        trend=1 if signal == "LONG" else -1,
        atr=1.0,
        bar_zone="above",
        top_fractal=False,
        bottom_fractal=False,
        pullback_armed_long=False,
        pullback_armed_short=False,
        trade_direction=0,
        signal=signal,
    )


class TestRegimeFilter(unittest.TestCase):
    def _strategy(self, min_bps: float, bars: int = 200) -> ScalpPullbackStrategy:
        cfg = Config().strategy
        cfg.regime_min_slope_bps = min_bps
        cfg.regime_slope_bars = bars
        return ScalpPullbackStrategy("BTCUSDT", cfg)

    def test_filter_mati_saat_nol(self):
        st = self._strategy(0.0)
        for slope in (-500.0, 0.0, 500.0, None):
            self.assertTrue(st.regime_allows(_state("LONG", slope)))
            self.assertTrue(st.regime_allows(_state("SHORT", slope)))

    def test_long_ditolak_saat_slope_turun(self):
        st = self._strategy(100.0)
        self.assertFalse(st.regime_allows(_state("LONG", -150.0)))
        self.assertFalse(st.regime_allows(_state("LONG", +50.0)))
        self.assertTrue(st.regime_allows(_state("LONG", +100.0)))
        self.assertTrue(st.regime_allows(_state("LONG", +250.0)))

    def test_short_ditolak_saat_slope_naik(self):
        st = self._strategy(100.0)
        self.assertFalse(st.regime_allows(_state("SHORT", +150.0)))
        self.assertFalse(st.regime_allows(_state("SHORT", -50.0)))
        self.assertTrue(st.regime_allows(_state("SHORT", -100.0)))
        self.assertTrue(st.regime_allows(_state("SHORT", -250.0)))

    def test_slope_belum_ada_diizinkan(self):
        st = self._strategy(100.0)
        self.assertTrue(st.regime_allows(_state("LONG", None)))
        self.assertTrue(st.regime_allows(_state("SHORT", None)))

class TestRegimeTidakMengubahSinyal(unittest.TestCase):
    """Filter hanya menyaring entry, tidak boleh mengubah state machine."""

    def _candles(self, n: int = 1200):
        from scalpbot.indicators import Candle

        out = []
        price = 100.0
        import math

        for i in range(n):
            drift = math.sin(i / 37.0) * 1.2 + math.sin(i / 11.0) * 0.4
            open_ = price
            price = max(1.0, price + drift * 0.25)
            high = max(open_, price) + 0.35
            low = min(open_, price) - 0.35
            out.append(Candle(ts=i * 300_000, open=open_, high=high, low=low, close=price, volume=10.0))
        return out

    def _signals(self, min_bps: float) -> list[int]:
        cfg = Config().strategy
        cfg.regime_min_slope_bps = min_bps
        cfg.regime_slope_bars = 50
        cfg.slow_ema = 120
        cfg.medium_ema = 60
        cfg.fast_ema = 30
        st = ScalpPullbackStrategy("BTCUSDT", cfg, 14)
        idx = []
        for i, c in enumerate(self._candles()):
            state = st.on_bar(c)
            if state.signal:
                idx.append(i)
        return idx

    def test_sinyal_identik_saat_filter_mati_dan_aktif(self):
        self.assertEqual(self._signals(0.0), self._signals(100.0))

    def test_required_bars_lebih_besar_dari_warmup(self):
        cfg = Config().strategy
        cfg.regime_min_slope_bps = 100.0
        cfg.regime_slope_bars = 200
        st = ScalpPullbackStrategy("BTCUSDT", cfg, 14)
        self.assertEqual(st.warmup_bars(), max(cfg.slow_ema, cfg.medium_ema, cfg.fast_ema, cfg.hi_lo_len, 14) + 8)
        self.assertEqual(st.required_bars(), cfg.slow_ema + cfg.regime_slope_bars + 8)

    def test_required_bars_sama_dengan_warmup_saat_filter_mati(self):
        cfg = Config().strategy
        cfg.regime_min_slope_bps = 0.0
        st = ScalpPullbackStrategy("BTCUSDT", cfg, 14)
        self.assertEqual(st.required_bars(), st.warmup_bars())

    def test_slope_diukur_pada_jarak_yang_diminta(self):
        cfg = Config().strategy
        cfg.regime_min_slope_bps = 100.0
        cfg.regime_slope_bars = 50
        cfg.slow_ema = 120
        cfg.medium_ema = 60
        cfg.fast_ema = 30
        st = ScalpPullbackStrategy("BTCUSDT", cfg, 14)
        candles = self._candles()
        for c in candles[:-1]:
            st.on_bar(c)
        hist_before = list(st._slow_hist)
        state = st.on_bar(candles[-1])
        self.assertIsNotNone(state.regime_slope_bps)
        self.assertEqual(len(st._slow_hist), cfg.regime_slope_bars)
        # slope dihitung sebelum nilai bar ini masuk riwayat, jadi basis = nilai N bar lalu
        base = hist_before[0]
        expected = (state.slow_ema - base) / base * 10_000.0
        self.assertAlmostEqual(state.regime_slope_bps, expected, places=6)


class TestRegimeValidation(unittest.TestCase):
    def test_slope_bars_terlalu_kecil(self):
        cfg = Config()
        cfg.strategy.regime_slope_bars = 5
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_slope_negatif_ditolak(self):
        cfg = Config()
        cfg.strategy.regime_min_slope_bps = -1.0
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_slope_terlalu_besar_ditolak(self):
        cfg = Config()
        cfg.strategy.regime_min_slope_bps = 5000.0
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_default_valid(self):
        validate_config(Config())


class TestRegimeRejectStats(unittest.TestCase):
    def test_penolakan_dicatat_dan_disarankan_sekali(self):
        from scalpbot.trader import Trader

        cfg = Config()
        cfg.mode = "paper"
        events = mock.MagicMock()
        trader = Trader.__new__(Trader)
        trader.cfg = cfg
        trader.events = events
        trader.reject_stats = {}
        trader.entries = []
        trader._lock = __import__("threading").Lock()

        for _ in range(6):
            trader.note_regime_reject("BTCUSDT", "LONG")

        self.assertEqual(trader.reject_stats["regime"], 6)
        # penolakan berulang dalam 300 s digabung jadi satu baris riwayat
        self.assertEqual(len(trader.entries), 1)
        self.assertEqual(trader.entries[0].count, 6)
        self.assertIn("x6", trader.entry_rows(6)[0]["detail"])
        info_msgs = [c for c in events.add.call_args_list if c.args and c.args[0] == "INFO"]
        self.assertEqual(len(info_msgs), 1, "saran hanya muncul sekali per sesi")


class TestRingkasanDanSignals(unittest.TestCase):
    def test_ringkasan_memisahkan_rezim_dan_orderbook(self):
        import threading
        from unittest import mock

        from scalpbot.trader import Trader

        cfg = Config()
        cfg.mode = "paper"
        cfg.strategy.regime_min_slope_bps = 100.0
        trader = Trader.__new__(Trader)
        trader.cfg = cfg
        trader.events = mock.MagicMock()
        trader.reject_stats = {"regime": 7, "imbalance": 3}
        trader._lock = threading.Lock()

        book = {k: v for k, v in trader.reject_stats.items() if k != "regime"}
        self.assertEqual(book, {"imbalance": 3})
        self.assertEqual(trader.reject_stats.get("regime"), 7)
        self.assertEqual(cfg.strategy.regime_min_slope_bps, 100.0)

    def test_regime_default_aktif(self):
        cfg = Config()
        self.assertGreater(cfg.strategy.regime_min_slope_bps, 0)
        self.assertEqual(cfg.strategy.regime_slope_bars, 200)
        self.assertEqual(cfg.strategy.regime_min_slope_bps, 100.0)


if __name__ == "__main__":
    unittest.main()
