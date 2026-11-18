"""Backtest kecil untuk memilih parameter.

Memakai logika yang sama dengan bot (strategy.ScalpPullbackStrategy,
risk.plan_stops/trailing_stop/breakeven_price), ditambah simulasi intrabar
konservatif: kalau SL dan TP tersentuh pada bar yang sama, SL dianggap kena
lebih dulu.

Bukan pengganti uji testnet. Tujuannya memisahkan parameter yang buruk dari
yang wajar sebelum uang dipertaruhkan.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Iterable

from scalpbot.config import Config
from scalpbot.indicators import Candle
from scalpbot.risk import (
    SymbolFilters,
    breakeven_price,
    plan_stops,
    round_tick,
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
    # Waktu bar (ms) masuk dan keluar; dipakai analisis per hari (mis. kill-switch harian).
    ts_entry: int = 0
    ts_exit: int = 0


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

    def expectancy_ci(self, samples: int = 4000, seed: int = 7) -> tuple[float, float]:
        """Selang kepercayaan 95% untuk rata-rata R (bootstrap).

        Kalau angka bawah <= 0, hasil backtest belum bisa dibedakan dari nol.
        """
        import random

        rs = [t.r for t in self.trades]
        if len(rs) < 20:
            return (0.0, 0.0)
        rng = random.Random(seed)
        means = []
        for _ in range(samples):
            pick = [rs[rng.randrange(len(rs))] for _ in range(len(rs))]
            means.append(statistics.fmean(pick))
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


def simulate(
    symbol: str,
    candles: list[Candle],
    cfg: Config,
    filters: SymbolFilters,
    *,
    equity: float = 100.0,
    warmup: int = 1200,
    max_hold_bars: int = 0,
) -> BacktestResult:
    """Jalankan satu simbol, satu posisi pada satu waktu (sama seperti max_open_per_symbol=1)."""
    risk = cfg.risk
    strat = ScalpPullbackStrategy(symbol, cfg.strategy, risk.atr_len)
    res = BacktestResult(equity_start=equity, equity_end=equity)
    cash = equity

    in_pos = False
    side = ""
    entry = stop = target = risk_dist = 0.0
    qty = 0.0
    risk_usd = 0.0
    entry_fee = 0.0
    be_done = trail_active = False
    bars_held = 0
    entry_ts = 0
    cooldown_until = 0

    for i, candle in enumerate(candles):
        state = strat.on_bar(candle)
        if i < warmup:
            continue

        if in_pos:
            bars_held += 1
            price = candle.close

            # 1) konservatif: SL dulu bila keduanya kena di bar yang sama
            hit_stop = (side == "LONG" and candle.low <= stop) or (side == "SHORT" and candle.high >= stop)
            hit_tp = (side == "LONG" and candle.high >= target) or (side == "SHORT" and candle.low <= target)

            if hit_stop:
                exit_price = stop
                gross = (exit_price - entry) * qty if side == "LONG" else (entry - exit_price) * qty
                fee = exit_price * qty * risk.fee_rate
                net = gross - fee - entry_fee
                reason = "TRAIL" if trail_active else "BE" if be_done else "SL"
                cash += net
                res.trades.append(
                    Trade(symbol, side, entry, exit_price, stop, target, reason, bars_held,
                          net / max(risk_usd, 1e-9), net, entry_ts, int(candle.ts))
                )
                in_pos = False
                cooldown_until = i + max(0, risk.cooldown_bars)
                continue
            if hit_tp:
                exit_price = target
                gross = (exit_price - entry) * qty if side == "LONG" else (entry - exit_price) * qty
                fee = exit_price * qty * risk.fee_rate
                net = gross - fee - entry_fee
                cash += net
                res.trades.append(
                    Trade(symbol, side, entry, exit_price, stop, target, "TP", bars_held,
                          net / max(risk_usd, 1e-9), net, entry_ts, int(candle.ts))
                )
                in_pos = False
                cooldown_until = i + max(0, risk.cooldown_bars)
                continue

            # 2) kelola BE & trailing memakai harga close
            r_now = ((price - entry) if side == "LONG" else (entry - price)) / risk_dist if risk_dist > 0 else 0.0
            if risk.be_trigger_r > 0 and not be_done and r_now >= risk.be_trigger_r:
                be = breakeven_price(side, entry, risk)
                if wants_stop_update(side, stop, be, price, risk, apply_step=False):
                    stop = round_tick(be, filters.tick_size, "down" if side == "LONG" else "up")
                    be_done = True
            if (
                risk.trail_trigger_r > 0
                and risk.trail_atr_mult > 0
                and r_now >= risk.trail_trigger_r
                and state.atr
            ):
                floor = breakeven_price(side, entry, risk)
                cand = trailing_stop(side, price, state.atr, stop, risk, filters, floor)
                if wants_stop_update(side, stop, cand, price, risk):
                    stop = cand
                    trail_active = True

            if max_hold_bars > 0 and bars_held >= max_hold_bars:
                gross = (price - entry) * qty if side == "LONG" else (entry - price) * qty
                fee = price * qty * risk.fee_rate
                net = gross - fee - entry_fee
                cash += net
                res.trades.append(
                    Trade(symbol, side, entry, price, stop, target, "TIMEOUT", bars_held,
                          net / max(risk_usd, 1e-9), net, entry_ts, int(candle.ts))
                )
                in_pos = False
                cooldown_until = i + max(0, risk.cooldown_bars)
            continue

        # 3) entry
        sig = strat.build_signal(state) if state.signal else None
        if sig is None or i < cooldown_until:
            continue
        res.signals += 1
        if not strat.regime_allows(state):
            res.skipped["regime"] = res.skipped.get("regime", 0) + 1
            continue
        plan = plan_stops(sig.side, sig.price, sig.atr, sig.pac_upper, sig.pac_lower, risk, filters)
        equity_now = cash
        risk_usd = equity_now * risk.risk_pct / 100.0
        if plan.risk_dist <= 0:
            continue
        qty_raw = risk_usd / plan.risk_dist
        notional = qty_raw * plan.entry
        cap = equity_now * risk.max_notional_frac_equity
        if notional > cap and cap > 0:
            qty_raw = cap / plan.entry
        qty = round_tick(qty_raw, filters.step_size, "down") if filters.step_size else qty_raw
        if qty <= 0 or qty * plan.entry < filters.min_notional:
            # bump ke minimum notional bila risiko masih wajar
            bumped = filters.min_notional / plan.entry
            bumped = round_tick(bumped, filters.step_size, "up") if filters.step_size else bumped
            if bumped * plan.risk_dist > risk_usd * risk.max_bump_factor:
                res.skipped["min_notional"] = res.skipped.get("min_notional", 0) + 1
                continue
            qty = bumped
        entry = plan.entry
        stop = plan.stop
        target = plan.target
        risk_dist = plan.risk_dist
        risk_usd = risk_dist * qty
        entry_fee = entry * qty * risk.fee_rate
        cash -= entry_fee
        in_pos = True
        side = sig.side
        be_done = trail_active = False
        bars_held = 0
        entry_ts = int(candle.ts)

    # posisi yang masih terbuka pada bar terakhir: tutup di harga terakhir
    if in_pos and candles:
        price = candles[-1].close
        gross = (price - entry) * qty if side == "LONG" else (entry - price) * qty
        fee = price * qty * risk.fee_rate
        net = gross - fee - entry_fee
        cash += net
        res.trades.append(
            Trade(symbol, side, entry, price, stop, target, "OPEN_END", bars_held,
                  net / max(risk_usd, 1e-9), net, entry_ts, int(candles[-1].ts))
        )

    res.equity_end = cash
    return res


def combine(results: Iterable[BacktestResult]) -> BacktestResult:
    total = BacktestResult()
    equity = 0.0
    for first, res in enumerate(results):
        if first == 0:
            equity = res.equity_start
            total.equity_start = res.equity_start
        total.trades.extend(res.trades)
        total.signals += res.signals
        for k, v in res.skipped.items():
            total.skipped[k] = total.skipped.get(k, 0) + v
        equity += res.equity_end - res.equity_start
    total.equity_end = equity
    total.trades.sort(key=lambda t: 0)
    return total
