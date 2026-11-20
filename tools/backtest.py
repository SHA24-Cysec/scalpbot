"""Backtest kecil untuk memilih parameter.

Memakai fungsi strategi dan risiko yang sama dengan bot, termasuk
risk.size_position, ditambah simulasi intrabar konservatif: kalau SL dan TP
tersentuh pada bar yang sama, SL dianggap kena lebih dulu. Biaya entry, biaya
exit, dan slippage dua sisi diterapkan pada harga fill.

Bukan pengganti uji testnet. Tujuannya memisahkan parameter yang buruk dari
yang wajar sebelum uang dipertaruhkan.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Iterable

from scalpbot.config import Config, RiskConfig
from scalpbot.indicators import Candle
from scalpbot.risk import (
    SymbolFilters,
    breakeven_price,
    plan_stops,
    round_tick,
    size_position,
    trailing_stop,
    wants_stop_update,
)
from scalpbot.strategy import ScalpPullbackStrategy


@dataclass(slots=True)
class Trade:
    symbol: str
    side: str
    entry: float
    exit: float
    stop: float
    target: float
    reason: str
    bars_held: int
    r: float
    pnl: float
    # Waktu bar (ms) masuk dan keluar; dipakai analisis per hari dan urutan gabungan.
    ts_entry: int = 0
    ts_exit: int = 0
    qty: float = 0.0


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_start: float = 100.0
    equity_end: float = 100.0
    signals: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def win_rate(self) -> float:
        return self.wins / self.n * 100.0 if self.n else 0.0

    @property
    def net_pct(self) -> float:
        return (self.equity_end / self.equity_start - 1.0) * 100.0 if self.equity_start else 0.0

    @property
    def expectancy_r(self) -> float:
        return statistics.fmean([t.r for t in self.trades]) if self.trades else 0.0

    def expectancy_ci(self, samples: int = 4000, seed: int = 7, block_size: int = 20) -> tuple[float, float]:
        """Selang kepercayaan 95% dari bootstrap blok atas trade kronologis.

        Blok berurutan dipakai supaya korelasi waktu tidak hilang. Angka ini tetap
        indikasi, bukan bukti signifikansi untuk data pasar yang tidak stasioner.
        """
        ordered = sorted(self.trades, key=lambda t: (t.ts_entry, t.ts_exit, t.symbol))
        rs = [t.r for t in ordered]
        n = len(rs)
        if n < 20 or samples < 100:
            return (0.0, 0.0)
        width = max(1, min(int(block_size), n))
        rng = _BootstrapRng(seed)
        means = []
        for _ in range(samples):
            picked = []
            while len(picked) < n:
                start = rng.randrange(n)
                for offset in range(width):
                    picked.append(rs[(start + offset) % n])
            means.append(statistics.fmean(picked[:n]))
        means.sort()
        return means[int(0.025 * samples)], means[int(0.975 * samples)]

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = -sum(t.pnl for t in self.trades if t.pnl < 0)
        if losses <= 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    def max_drawdown_pct(self) -> float:
        equity = self.equity_start
        worst = 0.0
        peak = equity
        for t in self.trades:
            equity += t.pnl
            peak = max(peak, equity)
            dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
            worst = max(worst, dd)
        return worst

    def reasons(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.trades:
            out[t.reason] = out.get(t.reason, 0) + 1
        return out


class _BootstrapRng:
    def __init__(self, seed: int) -> None:
        self._state = seed & 0xFFFFFFFFFFFFFFFF

    def randrange(self, n: int) -> int:
        self._state = (self._state * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        return (self._state >> 33) % n


def fill_price(side: str, price: float, risk: RiskConfig, entering: bool) -> float:
    slip = abs(price) * risk.slippage_bps / 10_000.0
    if entering:
        return price + slip if side == "LONG" else price - slip
    return price - slip if side == "LONG" else price + slip


def default_warmup(cfg: Config) -> int:
    strat = ScalpPullbackStrategy("-", cfg.strategy, cfg.risk.atr_len)
    return max(cfg.market.warmup_bars, strat.required_bars())


def _close_trade(
    res: BacktestResult,
    cash: float,
    symbol: str,
    side: str,
    entry: float,
    exit_marker: float,
    stop: float,
    target: float,
    reason: str,
    bars_held: int,
    qty: float,
    risk_usd: float,
    entry_fee: float,
    risk: RiskConfig,
    entry_ts: int,
    exit_ts: int,
    use_marker_price: bool,
) -> tuple[float, bool]:
    exit_price = fill_price(side, exit_marker, risk, False)
    gross = (exit_price - entry) * qty if side == "LONG" else (entry - exit_price) * qty
    fee = exit_price * qty * risk.fee_rate
    net = gross - fee - entry_fee
    cash += net
    res.trades.append(
        Trade(
            symbol,
            side,
            entry,
            exit_price,
            stop,
            target,
            reason,
            bars_held,
            net / max(risk_usd, 1e-9),
            net,
            entry_ts,
            exit_ts,
            qty,
        )
    )
    return cash, bool(use_marker_price)


def simulate(
    symbol: str,
    candles: list[Candle],
    cfg: Config,
    filters: SymbolFilters,
    *,
    equity: float = 100.0,
    warmup: int | None = None,
    max_hold_bars: int | None = None,
    interval_seconds: int = 300,
) -> BacktestResult:
    risk = cfg.risk
    strat = ScalpPullbackStrategy(symbol, cfg.strategy, risk.atr_len)
    res = BacktestResult(equity_start=equity, equity_end=equity)
    cash = equity
    if warmup is None:
        warmup = default_warmup(cfg)
    if max_hold_bars is None:
        max_hold_bars = (
            math.ceil(risk.max_hold_minutes * 60.0 / max(1, int(interval_seconds)))
            if risk.max_hold_minutes > 0
            else 0
        )
    throttle_bars = max(1, math.ceil(cfg.execution.cancel_replace_min_s / max(1, int(interval_seconds))))

    in_pos = False
    side = ""
    entry = stop = target = risk_dist = 0.0
    qty = 0.0
    risk_usd = 0.0
    entry_fee = 0.0
    entry_atr = 0.0
    be_done = trail_active = False
    bars_held = 0
    entry_ts = 0
    cooldown_until = 0
    last_trail_bar = 0

    for i, candle in enumerate(candles):
        state = strat.on_bar(candle)
        if i < warmup:
            continue

        if in_pos:
            bars_held += 1
            price = candle.close
            hit_stop = (side == "LONG" and candle.low <= stop) or (side == "SHORT" and candle.high >= stop)
            hit_tp = (side == "LONG" and candle.high >= target) or (side == "SHORT" and candle.low <= target)
            if hit_stop:
                cash, _ = _close_trade(
                    res, cash, symbol, side, entry, stop, stop, target,
                    "TRAIL" if trail_active else "BE" if be_done else "SL",
                    bars_held, qty, risk_usd, entry_fee, risk, entry_ts, int(candle.ts), False,
                )
                in_pos = False
                cooldown_until = i + max(0, risk.cooldown_bars)
                continue
            if hit_tp:
                cash, _ = _close_trade(
                    res, cash, symbol, side, entry, target, stop, target, "TP",
                    bars_held, qty, risk_usd, entry_fee, risk, entry_ts, int(candle.ts), False,
                )
                in_pos = False
                cooldown_until = i + max(0, risk.cooldown_bars)
                continue

            r_now = ((price - entry) if side == "LONG" else (entry - price)) / risk_dist if risk_dist > 0 else 0.0
            if risk.be_trigger_r > 0 and not be_done and r_now >= risk.be_trigger_r:
                be_px = breakeven_price(side, entry, risk)
                be_px = round_tick(be_px, filters.tick_size, "down" if side == "LONG" else "up")
                gap = filters.tick_size * 2
                if side == "LONG":
                    be_px = min(be_px, round_tick(price - gap, filters.tick_size, "down"))
                else:
                    be_px = max(be_px, round_tick(price + gap, filters.tick_size, "up"))
                if wants_stop_update(side, stop, be_px, price, risk, apply_step=False):
                    stop = be_px
                    be_done = True
                    last_trail_bar = bars_held
                elif (side == "LONG" and stop >= be_px) or (side == "SHORT" and stop > 0 and stop <= be_px):
                    be_done = True

            if (
                risk.trail_trigger_r > 0
                and risk.trail_atr_mult > 0
                and r_now >= risk.trail_trigger_r
                and entry_atr > 0
                and bars_held - last_trail_bar >= throttle_bars
            ):
                floor = breakeven_price(side, entry, risk)
                cand = trailing_stop(side, price, entry_atr, stop, risk, filters, floor)
                if wants_stop_update(side, stop, cand, price, risk):
                    stop = cand
                    trail_active = True
                    last_trail_bar = bars_held

            mark_stop = (side == "LONG" and price <= stop) or (side == "SHORT" and price >= stop)
            mark_tp = (side == "LONG" and price >= target) or (side == "SHORT" and price <= target)
            if mark_stop:
                cash, _ = _close_trade(
                    res, cash, symbol, side, entry, price, stop, target,
                    "TRAIL" if trail_active else "BE" if be_done else "SL",
                    bars_held, qty, risk_usd, entry_fee, risk, entry_ts, int(candle.ts), True,
                )
                in_pos = False
                cooldown_until = i + max(0, risk.cooldown_bars)
                continue
            if mark_tp:
                cash, _ = _close_trade(
                    res, cash, symbol, side, entry, price, stop, target, "TP",
                    bars_held, qty, risk_usd, entry_fee, risk, entry_ts, int(candle.ts), True,
                )
                in_pos = False
                cooldown_until = i + max(0, risk.cooldown_bars)
                continue

            if max_hold_bars > 0 and bars_held >= max_hold_bars:
                cash, _ = _close_trade(
                    res, cash, symbol, side, entry, price, stop, target, "TIMEOUT",
                    bars_held, qty, risk_usd, entry_fee, risk, entry_ts, int(candle.ts), True,
                )
                in_pos = False
                cooldown_until = i + max(0, risk.cooldown_bars)
            continue

        sig = strat.build_signal(state) if state.signal else None
        if sig is None or i < cooldown_until:
            continue
        res.signals += 1
        if not strat.regime_allows(state):
            res.skipped["regime"] = res.skipped.get("regime", 0) + 1
            continue
        raw_entry = fill_price(sig.side, sig.price, risk, True)
        plan = plan_stops(sig.side, raw_entry, sig.atr, sig.pac_upper, sig.pac_lower, risk, filters)
        sizing = size_position(cash, plan.entry, plan.stop, risk, filters)
        if not sizing.ok:
            key = "min_notional" if "minimum" in sizing.reason else "sizing"
            res.skipped[key] = res.skipped.get(key, 0) + 1
            continue
        entry = plan.entry
        qty = sizing.qty
        risk_usd = sizing.risk_usd
        entry_fee = entry * qty * risk.fee_rate
        entry_atr = sig.atr
        stop = plan.stop
        target = plan.target
        risk_dist = plan.risk_dist
        in_pos = True
        side = sig.side
        be_done = trail_active = False
        bars_held = 0
        entry_ts = int(candle.ts)
        last_trail_bar = 0

    if in_pos and candles:
        price = candles[-1].close
        cash, _ = _close_trade(
            res, cash, symbol, side, entry, price, stop, target, "OPEN_END",
            bars_held, qty, risk_usd, entry_fee, risk, entry_ts, int(candles[-1].ts), True,
        )

    res.equity_end = cash
    return res


def combine(results: Iterable[BacktestResult]) -> BacktestResult:
    total = BacktestResult()
    equity = total.equity_start
    seen = False
    for res in results:
        if not seen:
            total.equity_start = res.equity_start
            equity = res.equity_start
            seen = True
        total.trades.extend(res.trades)
        total.signals += res.signals
        for k, v in res.skipped.items():
            total.skipped[k] = total.skipped.get(k, 0) + v
        equity += res.equity_end - res.equity_start
    total.equity_end = equity
    total.trades.sort(key=lambda t: (t.ts_entry, t.ts_exit, t.symbol))
    return total
