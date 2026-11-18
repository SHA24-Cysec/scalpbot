"""Uji paritas sinyal terhadap transkripsi literal Pine "Scalping PullBack Tool R1.1".

Setiap test membandingkan sinyal bot dengan `tools/pine_reference.py` yang ditulis
mengikuti script Pine apa adanya. Yang diperiksa hanya bar setelah warmup, karena
EMA Pine bernilai na sebelum seed SMA-nya penuh sehingga sinyal awal tidak bermakna.
"""

from __future__ import annotations

import json
import math
import unittest
from pathlib import Path

from scalpbot.config import Config
from scalpbot.indicators import Candle
from scalpbot.strategy import ScalpPullbackStrategy
from tools.pine_reference import PineScalpTool


FIXTURE = Path(__file__).parent / "fixtures" / "btc_5m.json"


def _candles(n: int | None = None) -> list[Candle]:
    """Data BTCUSDT 5m sungguhan dari Binance testnet (fixture, tanpa jaringan)."""
    raw = json.loads(FIXTURE.read_text())
    rows = raw if n is None else raw[-n:]
    return [Candle(ts=int(r[0]), open=r[1], high=r[2], low=r[3], close=r[4], volume=0.0) for r in rows]


def _strategies(candles: list[Candle], **overrides):
    cfg = Config().strategy
    cfg.slow_ema = 160
    cfg.medium_ema = 100
    cfg.fast_ema = 50
    cfg.hi_lo_len = 34
    cfg.regime_min_slope_bps = 0.0
    for key, value in overrides.items():
        setattr(cfg, key, value)
    bot = ScalpPullbackStrategy("BTCUSDT", cfg, 14)
    ref = PineScalpTool(
        hi_lo_len=cfg.hi_lo_len,
        fast_len=cfg.fast_ema,
        medium_len=cfg.medium_ema,
        slow_len=cfg.slow_ema,
        lookback=cfg.lookback,
        use_ha=cfg.use_ha,
        filter_bw=cfg.filter_bw_fractals,
        delay=0,
        haopen_mode="cur",
    )
    return bot, ref


def _compare(candles: list[Candle], **overrides) -> tuple[int, int]:
    bot, ref = _strategies(candles, **overrides)
    warm = bot.warmup_bars()
    diffs = []
    total = 0
    for i, candle in enumerate(candles):
        want = ref.on_bar(candle)["signal"]
        got = bot.on_bar(candle).signal
        if want:
            total += 1
        if i >= warm and want != got:
            diffs.append((i, want, got))
    return total, len(diffs)


class TestParityDenganPine(unittest.TestCase):
    def test_sinyal_identik_setelah_warmup(self):
        candles = _candles()
        total, diffs = _compare(candles)
        self.assertGreater(total, 0, "data uji harus menghasilkan sinyal agar test bermakna")
        self.assertEqual(diffs, 0, "sinyal bot harus sama dengan transkripsi Pine")

    def test_identik_saat_heikin_ashi_dimatikan(self):
        total, diffs = _compare(_candles(), use_ha=False)
        self.assertGreater(total, 0)
        self.assertEqual(diffs, 0)

    def test_identik_saat_fractal_ideal(self):
        total, diffs = _compare(_candles(), filter_bw_fractals=True)
        self.assertGreater(total, 0)
        self.assertEqual(diffs, 0)

    def test_identik_untuk_beberapa_lookback(self):
        for lookback in (1, 2, 5):
            with self.subTest(lookback=lookback):
                total, diffs = _compare(_candles(), lookback=lookback)
                self.assertEqual(diffs, 0)

    def test_identik_untuk_kombinasi_ema_lain(self):
        for pac, fast, medium, slow in ((21, 55, 144, 377), (13, 34, 89, 233)):
            with self.subTest(kombinasi=(pac, fast, medium, slow)):
                total, diffs = _compare(
                    _candles(), hi_lo_len=pac, fast_ema=fast, medium_ema=medium, slow_ema=slow
                )
                self.assertEqual(diffs, 0)

    def test_identifier_chevron_selalu_searah_tren(self):
        candles = _candles()
        bot, ref = _strategies(candles)
        for candle in candles:
            state = bot.on_bar(candle)
            if state.signal == "LONG":
                self.assertEqual(state.trend, 1)
            elif state.signal == "SHORT":
                self.assertEqual(state.trend, -1)


class TestEmasSamaDenganPine(unittest.TestCase):
    def test_seed_dan_alpha_sama(self):
        from scalpbot.indicators import EMA
        from tools.pine_reference import PineEMA

        values = [100.0 + math.sin(i / 5.0) * 3 for i in range(200)]
        for length in (3, 14, 34, 89):
            with self.subTest(length=length):
                a = EMA(length)
                b = PineEMA(length)
                for v in values:
                    self.assertAlmostEqual(a.update(v) or 0.0, b.update(v) or 0.0, places=10)

    def test_trend_direction_sama(self):
        import itertools
        import random

        from scalpbot.indicators import trend_direction

        rng = random.Random(3)
        for _ in range(500):
            fast, medium, pac_l, pac_u = (rng.uniform(90, 110) for _ in range(4))
            want = (
                1
                if fast > medium and pac_l > medium
                else -1
                if fast < medium and pac_u < medium
                else 0
            )
            self.assertEqual(trend_direction(fast, medium, pac_l, pac_u), want)
        for fast, medium, pac_l, pac_u in itertools.product((100.0,), (100.0,), (99.0,), (101.0,)):
            self.assertEqual(trend_direction(fast, medium, pac_l, pac_u), 0)


class TestKlaimScript(unittest.TestCase):
    """Klaim yang tertulis di komentar script Pine, diuji langsung."""

    def test_slow_ema_tidak_mempengaruhi_sinyal(self):
        candles = _candles()
        slow_a, _ = _strategies(candles)
        cfg_a = slow_a.cfg
        signals_a = [slow_a.on_bar(c).signal for c in candles]

        cfg_b = Config().strategy
        cfg_b.slow_ema = cfg_a.slow_ema
        cfg_b.medium_ema = cfg_a.medium_ema
        cfg_b.fast_ema = cfg_a.fast_ema
        cfg_b.hi_lo_len = cfg_a.hi_lo_len
        cfg_b.regime_min_slope_bps = 0.0
        slow_b = ScalpPullbackStrategy("BTCUSDT", cfg_b, 14)
        signals_b = [slow_b.on_bar(c).signal for c in candles]
        self.assertEqual(signals_a, signals_b)

    def test_slow_ema_berbeda_hanya_berpengaruh_sebelum_warmup(self):
        import copy

        candles = _candles()
        base = Config().strategy
        base.slow_ema = 160
        base.medium_ema = 100
        base.fast_ema = 50
        base.regime_min_slope_bps = 0.0

        out = {}
        for slow in (160, 300):
            cfg = copy.deepcopy(base)
            cfg.slow_ema = slow
            st = ScalpPullbackStrategy("BTCUSDT", cfg, 14)
            out[slow] = ([st.on_bar(c).signal for c in candles], st.warmup_bars())

        warm = max(out[160][1], out[300][1])
        for i in range(warm, len(candles)):
            self.assertEqual(
                out[160][0][i], out[300][0][i], f"slow_ema tidak boleh mengubah sinyal di bar {i}"
            )

    def test_fractal_tidak_mempengaruhi_sinyal(self):
        candles = _candles()
        _, diffs_default = _compare(candles, filter_bw_fractals=False)
        _, diffs_regular = _compare(candles, filter_bw_fractals=True)
        self.assertEqual(diffs_default, 0)
        self.assertEqual(diffs_regular, 0)


class TestPineFractal(unittest.TestCase):
    def test_bw_fractal_dan_regular_sama(self):
        from scalpbot.indicators import is_bw_fractal_top, is_regular_fractal_top
        from tools.pine_reference import PineScalpTool

        def ref_top(highs: list[float], regular: bool) -> bool:
            st = PineScalpTool(use_ha=False, filter_bw=regular)
            st._high = list(highs)
            st._low = list(highs)
            return st._is_regular_fractal(1) if regular else st._is_bw_fractal(1)

        import random

        rng = random.Random(11)
        for _ in range(300):
            highs = [round(rng.uniform(99, 101), 4) for _ in range(5)]
            self.assertEqual(is_bw_fractal_top(highs), ref_top(highs, False))
            self.assertEqual(is_regular_fractal_top(highs), ref_top(highs, True))


if __name__ == "__main__":
    unittest.main()
