"""Transkripsi literal Pine Script "Scalping PullBack Tool R1.1 by JustUncleL".

Tujuan berkas ini HANYA sebagai pembanding untuk verifikasi. Tidak dipakai bot.
Ditulis mengikuti script Pine baris per baris, tanpa optimasi, supaya selisih
perilaku dengan bot bisa dideteksi.
"""

from __future__ import annotations

from typing import Optional, Sequence

from scalpbot.indicators import Candle


class PineEMA:
    """ema() Pine v4: seed = SMA, lalu alpha = 2/(len+1). Nilai awal na."""

    def __init__(self, length: int) -> None:
        self.length = int(length)
        self.alpha = 2.0 / (self.length + 1.0)
        self.value: Optional[float] = None
        self._sum = 0.0
        self._n = 0

    def update(self, x: float) -> Optional[float]:
        if self.value is None:
            self._sum += x
            self._n += 1
            if self._n >= self.length:
                self.value = self._sum / self.length
            return self.value
        self.value = self.alpha * x + (1.0 - self.alpha) * self.value
        return self.value


def _nz(v, default=0):
    return default if v is None else v


class PineScalpTool:
    """Pembanding literal. Sinyal dihitung dengan aturan script apa adanya."""

    def __init__(
        self,
        hi_lo_len: int = 34,
        fast_len: int = 89,
        medium_len: int = 200,
        slow_len: int = 600,
        lookback: int = 3,
        use_ha: bool = True,
        filter_bw: bool = False,
        delay: int = 0,
        haopen_mode: str = "cur",
    ) -> None:
        self.hi_lo_len = hi_lo_len
        self.fast_len = fast_len
        self.medium_len = medium_len
        self.slow_len = slow_len
        self.lookback = lookback
        self.use_ha = use_ha
        self.filter_bw = filter_bw
        self.delay = delay
        self.haopen_mode = haopen_mode

        self._ema_fast = PineEMA(fast_len)
        self._ema_med = PineEMA(medium_len)
        self._ema_slow = PineEMA(slow_len)
        self._ema_pac_c = PineEMA(hi_lo_len)
        self._ema_pac_l = PineEMA(hi_lo_len)
        self._ema_pac_u = PineEMA(hi_lo_len)

        self._ha_open: Optional[float] = None
        self._ha_close: Optional[float] = None

        # series yang perlu diakses dengan index (high[i], low[i], haOpen[i])
        self._high: list[float] = []
        self._low: list[float] = []
        self._ha_open_hist: list[float] = []

        self._last_below_pac_c: Optional[int] = None
        self._last_above_pac_c: Optional[int] = None

        self.idx = -1
        self.trade_direction = 0
        self._prev_dirs: list[int] = []

    # --- helper index-akses ala Pine -------------------------------------
    @staticmethod
    def _at(series: Sequence[float], i: int) -> Optional[float]:
        """series[i] dengan semantik Pine: i=0 bar terkini."""
        if not series:
            return None
        pos = len(series) - 1 - i
        if pos < 0:
            return None
        return series[pos]

    def _ha_step(self, o: float, h: float, lo: float, c: float):
        ha_close = (o + h + lo + c) / 4.0
        if self._ha_open is None or self._ha_close is None:
            ha_open = (o + c) / 2.0
        else:
            ha_open = (self._ha_open + self._ha_close) / 2.0
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(lo, ha_open, ha_close)
        return ha_open, ha_high, ha_low, ha_close

    # --- fractal literal --------------------------------------------------
    def _is_regular_fractal(self, mode: int) -> bool:
        h = [self._at(self._high, i) for i in range(5)]
        lo = [self._at(self._low, i) for i in range(5)]
        if any(v is None for v in h) or any(v is None for v in lo):
            return False
        h4, h3, h2, h1, h0 = h  # high[4] ... high[0]
        l4, l3, l2, l1, l0 = lo
        if mode == 1:
            return h4 < h3 and h3 < h2 and h2 > h1 and h1 > h0
        if mode == -1:
            return l4 > l3 and l3 > l2 and l2 < l1 and l1 < l0
        return False

    def _is_bw_fractal(self, mode: int) -> bool:
        h = [self._at(self._high, i) for i in range(5)]
        lo = [self._at(self._low, i) for i in range(5)]
        if any(v is None for v in h) or any(v is None for v in lo):
            return False
        h4, h3, h2, h1, h0 = h
        l4, l3, l2, l1, l0 = lo
        if mode == 1:
            return h4 < h2 and h3 <= h2 and h2 >= h1 and h2 > h0
        if mode == -1:
            return l4 > l2 and l3 >= l2 and l2 <= l1 and l2 < l0
        return False

    def on_bar(self, candle: Candle) -> dict:
        self.idx += 1

        ha_open, ha_high, ha_low, ha_close = self._ha_step(
            candle.open, candle.high, candle.low, candle.close
        )
        if self.use_ha:
            self._ha_open, self._ha_close = ha_open, ha_close
            src_close, src_high, src_low = ha_close, ha_high, ha_low
        else:
            self._ha_open, self._ha_close = candle.open, candle.close
            src_close, src_high, src_low = candle.close, candle.high, candle.low

        self._high.append(candle.high)
        self._low.append(candle.low)
        self._ha_open_hist.append(self._ha_open)

        fast = self._ema_fast.update(src_close)
        med = self._ema_med.update(src_close)
        slow = self._ema_slow.update(src_close)
        pac_c = self._ema_pac_c.update(src_close)
        pac_l = self._ema_pac_l.update(src_low)
        pac_u = self._ema_pac_u.update(src_high)

        trend = None
        if None not in (fast, med, pac_c, pac_l, pac_u):
            if fast > med and pac_l > med:
                trend = 1
            elif fast < med and pac_u < med:
                trend = -1
            else:
                trend = 0

        # filteredtopf / filteredbotf
        if self.filter_bw:
            topf = self._is_regular_fractal(1)
            botf = self._is_regular_fractal(-1)
        else:
            topf = self._is_bw_fractal(1)
            botf = self._is_bw_fractal(-1)

        # barssince
        if pac_c is not None:
            if src_close < pac_c:
                self._last_below_pac_c = self.idx
            if src_close > pac_c:
                self._last_above_pac_c = self.idx
        bars_since_below = (
            None if self._last_below_pac_c is None else self.idx - self._last_below_pac_c
        )
        bars_since_above = (
            None if self._last_above_pac_c is None else self.idx - self._last_above_pac_c
        )

        # Varian haOpen: "prev" = haOpen[1], "cur" = haOpen bar ini (literal Pine live)
        ha_open_prev = self._at(self._ha_open_hist, 1 if self.haopen_mode == "prev" else 0)
        signal = None
        buy = sell = False
        pac_exit_u = pac_exit_l = False
        if None not in (fast, med, pac_c, pac_l, pac_u, ha_open_prev) and trend is not None:
            pac_exit_u = (
                ha_open_prev < pac_u
                and src_close > pac_u
                and bars_since_below is not None
                and bars_since_below <= self.lookback
            )
            pac_exit_l = (
                ha_open_prev > pac_l
                and src_close < pac_l
                and bars_since_above is not None
                and bars_since_above <= self.lookback
            )
            buy = trend == 1 and pac_exit_u
            sell = trend == -1 and pac_exit_l

            prev = self.trade_direction
            if prev == 1 and src_close < pac_c:
                self.trade_direction = 0
            elif prev == -1 and src_close > pac_c:
                self.trade_direction = 0
            elif prev == 0 and buy:
                self.trade_direction = 1
            elif prev == 0 and sell:
                self.trade_direction = -1

            if prev == 0 and self.trade_direction == 1:
                signal = "LONG"
            elif prev == 0 and self.trade_direction == -1:
                signal = "SHORT"

        self._prev_dirs.append(self.trade_direction)

        # plotarrow dengan Delay, sinyal TAMPIL pada bar idx + Delay
        arrow = None
        if self.delay > 0:
            d = self.delay
            past = self._at(self._prev_dirs, 1 + d)
            now = self._at(self._prev_dirs, d)
            if _nz(past) == 0 and _nz(now) != 0:
                arrow = "LONG" if now == 1 else "SHORT"
        else:
            arrow = signal

        return {
            "idx": self.idx,
            "signal": signal,
            "arrow": arrow,
            "trend": trend,
            "direction": self.trade_direction,
            "fast": fast,
            "medium": med,
            "slow": slow,
            "pac_c": pac_c,
            "pac_u": pac_u,
            "pac_l": pac_l,
            "top_fractal": topf,
            "bottom_fractal": botf,
            "buy": buy,
            "sell": sell,
            "ha_open": ha_open_prev,
            "ha_close": src_close,
        }


def run_pine(candles: Sequence[Candle], **kwargs) -> list[dict]:
    st = PineScalpTool(**kwargs)
    return [st.on_bar(c) for c in candles]
