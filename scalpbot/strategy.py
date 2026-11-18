from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .config import StrategyConfig
from .indicators import (
    ATR,
    EMA,
    Candle,
    fractal_bottom,
    fractal_top,
    heikin_ashi_step,
    trend_direction,
)


@dataclass(slots=True)
class BarState:
    idx: int
    ts: int
    price: float
    ha_close: float
    pac_center: Optional[float]
    pac_upper: Optional[float]
    pac_lower: Optional[float]
    fast_ema: Optional[float]
    medium_ema: Optional[float]
    slow_ema: Optional[float]
    regime_slope_bps: Optional[float]
    trend: int
    atr: Optional[float]
    bar_zone: str
    top_fractal: bool
    bottom_fractal: bool
    pullback_armed_long: bool
    pullback_armed_short: bool
    trade_direction: int
    signal: Optional[str] = None


@dataclass(slots=True)
class Signal:
    symbol: str
    side: str
    ts: int
    price: float
    atr: float
    pac_center: float
    pac_upper: float
    pac_lower: float
    trend: int
    bars_since_opposite_pac: int
    fast_ema: float
    medium_ema: float
    ha_close: float
    notes: list[str] = field(default_factory=list)


class ScalpPullbackStrategy:
    def __init__(self, symbol: str, cfg: StrategyConfig, atr_len: int = 14) -> None:
        self.symbol = symbol
        self.cfg = cfg
        self.atr_len = atr_len
        self.idx = -1
        self.trade_direction = 0
        self._ha_open: Optional[float] = None
        self._ha_close: Optional[float] = None
        self._ema_fast = EMA(cfg.fast_ema)
        self._ema_medium = EMA(cfg.medium_ema)
        self._ema_slow = EMA(cfg.slow_ema)
        self._ema_pac_c = EMA(cfg.hi_lo_len)
        self._ema_pac_l = EMA(cfg.hi_lo_len)
        self._ema_pac_u = EMA(cfg.hi_lo_len)
        self._atr = ATR(atr_len)
        self._raw_highs: deque[float] = deque(maxlen=5)
        self._raw_lows: deque[float] = deque(maxlen=5)
        self._pivot_highs: deque[float] = deque(maxlen=3)
        self._pivot_lows: deque[float] = deque(maxlen=3)
        self._last_below_pac_c: Optional[int] = None
        self._last_above_pac_c: Optional[int] = None
        self._pending_pivot_high: deque[float] = deque(maxlen=3)
        self._pending_pivot_low: deque[float] = deque(maxlen=3)
        self.last_bar: Optional[BarState] = None
        self.signal_count = 0
        self._slow_hist: deque[float] = deque(maxlen=max(2, cfg.regime_slope_bars))

    def warmup_bars(self) -> int:
        """Warmup indikator; menentukan kapan sinyal boleh diterbitkan."""
        return max(self.cfg.slow_ema, self.cfg.medium_ema, self.cfg.fast_ema, self.cfg.hi_lo_len, self.atr_len) + 8

    def required_bars(self) -> int:
        """Bar yang perlu diunduh. Lebih besar dari warmup bila filter rezim aktif."""
        need = self.warmup_bars()
        if self.cfg.regime_min_slope_bps > 0:
            need = max(need, self.cfg.slow_ema + self.cfg.regime_slope_bars + 8)
        return need

    def _update_pivots(self, top_fractal: bool, bottom_fractal: bool, high: float, low: float) -> None:
        if top_fractal and self._pending_pivot_high:
            self._pivot_highs.append(self._pending_pivot_high[0])
        if bottom_fractal and self._pending_pivot_low:
            self._pivot_lows.append(self._pending_pivot_low[0])

    def on_bar(self, candle: Candle) -> BarState:
        self.idx += 1
        raw_high, raw_low = candle.high, candle.low

        ha_open, ha_high, ha_low, ha_close = heikin_ashi_step(
            self._ha_open, self._ha_close, candle.open, candle.high, candle.low, candle.close
        )
        if self.cfg.use_ha:
            self._ha_open, self._ha_close = ha_open, ha_close
            src_close, src_high, src_low = ha_close, ha_high, ha_low
        else:
            self._ha_open, self._ha_close = candle.open, candle.close
            src_close, src_high, src_low = candle.close, candle.high, candle.low

        fast = self._ema_fast.update(src_close)
        medium = self._ema_medium.update(src_close)
        slow = self._ema_slow.update(src_close)
        pac_c = self._ema_pac_c.update(src_close)
        pac_l = self._ema_pac_l.update(src_low)
        pac_u = self._ema_pac_u.update(src_high)
        atr = self._atr.update(candle.high, candle.low, candle.close)

        self._raw_highs.append(raw_high)
        self._raw_lows.append(raw_low)
        self._pending_pivot_high.append(raw_high)
        self._pending_pivot_low.append(raw_low)

        top_fractal = fractal_top(list(self._raw_highs), self.cfg.filter_bw_fractals)
        bottom_fractal = fractal_bottom(list(self._raw_lows), self.cfg.filter_bw_fractals)
        self._update_pivots(top_fractal, bottom_fractal, raw_high, raw_low)

        armed_long = False
        armed_short = False
        trend = 0
        signal: Optional[str] = None
        regime_slope: Optional[float] = None
        if slow is not None:
            if len(self._slow_hist) >= self.cfg.regime_slope_bars:
                base = self._slow_hist[0]
                if base:
                    regime_slope = (slow - base) / base * 10_000.0
            self._slow_hist.append(slow)

        if pac_c is not None and pac_u is not None and pac_l is not None:
            if src_close < pac_c:
                self._last_below_pac_c = self.idx
            if src_close > pac_c:
                self._last_above_pac_c = self.idx

        if None not in (fast, medium, pac_c, pac_l, pac_u) and atr is not None:
            trend = trend_direction(fast, medium, pac_l, pac_u)
            bars_since_below = self.idx - self._last_below_pac_c if self._last_below_pac_c is not None else None
            bars_since_above = self.idx - self._last_above_pac_c if self._last_above_pac_c is not None else None

            pac_exit_u = (
                self._ha_open is not None
                and self._ha_open < pac_u
                and src_close > pac_u
                and bars_since_below is not None
                and bars_since_below <= self.cfg.lookback
            )
            pac_exit_l = (
                self._ha_open is not None
                and self._ha_open > pac_l
                and src_close < pac_l
                and bars_since_above is not None
                and bars_since_above <= self.cfg.lookback
            )

            armed_long = trend == 1 and pac_exit_u
            armed_short = trend == -1 and pac_exit_l

            prev_dir = self.trade_direction
            if prev_dir == 1 and src_close < pac_c:
                self.trade_direction = 0
            elif prev_dir == -1 and src_close > pac_c:
                self.trade_direction = 0
            elif prev_dir == 0 and armed_long:
                self.trade_direction = 1
            elif prev_dir == 0 and armed_short:
                self.trade_direction = -1

            if prev_dir == 0 and self.trade_direction == 1:
                signal = "LONG"
            elif prev_dir == 0 and self.trade_direction == -1:
                signal = "SHORT"

            if self.idx < self.warmup_bars():
                signal = None

            if signal:
                self.signal_count += 1
        bar_zone = "na"
        if pac_u is not None and pac_l is not None:
            if src_close > pac_u:
                bar_zone = "above"
            elif src_close < pac_l:
                bar_zone = "below"
            else:
                bar_zone = "inside"

        state = BarState(
            idx=self.idx,
            ts=candle.ts,
            price=candle.close,
            ha_close=src_close,
            pac_center=pac_c,
            pac_upper=pac_u,
            pac_lower=pac_l,
            fast_ema=fast,
            medium_ema=medium,
            slow_ema=slow,
            regime_slope_bps=regime_slope,
            trend=trend,
            atr=atr,
            bar_zone=bar_zone,
            top_fractal=top_fractal,
            bottom_fractal=bottom_fractal,
            pullback_armed_long=armed_long,
            pullback_armed_short=armed_short,
            trade_direction=self.trade_direction,
            signal=signal,
        )
        self.last_bar = state
        return state

    def regime_allows(self, state: BarState) -> bool:
        """Filter rezim: tolak sinyal yang melawan arah EMA lambat.

        `regime_min_slope_bps` 0 berarti filter mati.
        """
        if self.cfg.regime_min_slope_bps <= 0:
            return True
        slope = state.regime_slope_bps
        if slope is None or state.signal is None:
            return True
        if state.signal == "LONG":
            return slope >= self.cfg.regime_min_slope_bps
        return slope <= -self.cfg.regime_min_slope_bps

    def build_signal(self, state: BarState) -> Optional[Signal]:
        if not state.signal or state.atr is None:
            return None
        if state.pac_center is None or state.pac_upper is None or state.pac_lower is None:
            return None
        if state.fast_ema is None or state.medium_ema is None:
            return None
        bars_since = (
            state.idx - self._last_below_pac_c
            if state.signal == "LONG" and self._last_below_pac_c is not None
            else state.idx - self._last_above_pac_c
            if self._last_above_pac_c is not None
            else 0
        )
        return Signal(
            symbol=self.symbol,
            side=state.signal,
            ts=state.ts,
            price=state.price,
            atr=state.atr,
            pac_center=state.pac_center,
            pac_upper=state.pac_upper,
            pac_lower=state.pac_lower,
            trend=state.trend,
            bars_since_opposite_pac=bars_since,
            fast_ema=state.fast_ema,
            medium_ema=state.medium_ema,
            ha_close=state.ha_close,
        )

    def reset(self) -> None:
        self.__init__(self.symbol, self.cfg, self.atr_len)
