from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True, slots=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class EMA:
    __slots__ = ("length", "alpha", "value", "_seed", "_count")

    def __init__(self, length: int) -> None:
        if length < 1:
            raise ValueError("EMA length minimal 1")
        self.length = int(length)
        self.alpha = 2.0 / (self.length + 1.0)
        self.value: float | None = None
        self._seed = 0.0
        self._count = 0

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed += x
            self._count += 1
            if self._count >= self.length:
                self.value = self._seed / self.length
            return self.value
        self.value = self.alpha * x + (1.0 - self.alpha) * self.value
        return self.value

    @property
    def ready(self) -> bool:
        return self.value is not None


class ATR:
    __slots__ = ("length", "value", "_trs", "_prev_close")

    def __init__(self, length: int) -> None:
        if length < 1:
            raise ValueError("ATR length minimal 1")
        self.length = int(length)
        self.value: float | None = None
        self._trs: list[float] = []
        self._prev_close: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        if self.value is None:
            self._trs.append(tr)
            if len(self._trs) >= self.length:
                self.value = sum(self._trs) / len(self._trs)
            return self.value
        self.value = (self.value * (self.length - 1) + tr) / self.length
        return self.value


def heikin_ashi_step(
    prev_ha_open: float | None,
    prev_ha_close: float | None,
    o: float,
    h: float,
    lo: float,
    c: float,
) -> tuple[float, float, float, float]:
    ha_close = (o + h + lo + c) / 4.0
    if prev_ha_open is None or prev_ha_close is None:
        ha_open = (o + c) / 2.0
    else:
        ha_open = (prev_ha_open + prev_ha_close) / 2.0
    ha_high = max(h, ha_open, ha_close)
    ha_low = min(lo, ha_open, ha_close)
    return ha_open, ha_high, ha_low, ha_close


def heikin_ashi_series(candles: Sequence[Candle]) -> list[Candle]:
    out: list[Candle] = []
    po: float | None = None
    pc: float | None = None
    for k in candles:
        o, h, lo, c = heikin_ashi_step(po, pc, k.open, k.high, k.low, k.close)
        po, pc = o, c
        out.append(Candle(k.ts, o, h, lo, c, k.volume))
    return out


def ema_series(values: Sequence[float], length: int) -> list[float | None]:
    e = EMA(length)
    return [e.update(v) for v in values]


def atr_series(candles: Sequence[Candle], length: int) -> list[float | None]:
    a = ATR(length)
    return [a.update(k.high, k.low, k.close) for k in candles]


def is_regular_fractal_top(highs: Sequence[float]) -> bool:
    if len(highs) < 5:
        return False
    h0, h1, h2, h3, h4 = highs[-5:]
    return h0 < h1 < h2 and h2 > h3 > h4


def is_regular_fractal_bottom(lows: Sequence[float]) -> bool:
    if len(lows) < 5:
        return False
    l0, l1, l2, l3, l4 = lows[-5:]
    return l0 > l1 > l2 and l2 < l3 < l4


def is_bw_fractal_top(highs: Sequence[float]) -> bool:
    if len(highs) < 5:
        return False
    h0, h1, h2, h3, h4 = highs[-5:]
    return h4 < h2 and h3 <= h2 and h2 >= h1 and h2 > h0


def is_bw_fractal_bottom(lows: Sequence[float]) -> bool:
    if len(lows) < 5:
        return False
    l0, l1, l2, l3, l4 = lows[-5:]
    return l4 > l2 and l3 >= l2 and l2 <= l1 and l2 < l0


def fractal_top(highs: Sequence[float], regular_only: bool) -> bool:
    return is_regular_fractal_top(highs) if regular_only else is_bw_fractal_top(highs)


def fractal_bottom(lows: Sequence[float], regular_only: bool) -> bool:
    return is_regular_fractal_bottom(lows) if regular_only else is_bw_fractal_bottom(lows)


def trend_direction(fast_ema: float, medium_ema: float, pac_lower: float, pac_upper: float) -> int:
    if fast_ema > medium_ema and pac_lower > medium_ema:
        return 1
    if fast_ema < medium_ema and pac_upper < medium_ema:
        return -1
    return 0


def pct_diff(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0


def bps_diff(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b * 10_000.0


def last_n(seq: Iterable[float], n: int) -> list[float]:
    items = list(seq)
    return items[-n:] if len(items) >= n else items
