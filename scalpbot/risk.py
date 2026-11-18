from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional

from .config import RiskConfig


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    symbol: str
    tick_size: float
    step_size: float
    min_qty: float
    max_qty: float
    min_notional: float
    qty_precision: int
    price_precision: int


@dataclass(frozen=True, slots=True)
class Sizing:
    ok: bool
    qty: float = 0.0
    notional: float = 0.0
    risk_usd: float = 0.0
    risk_price_dist: float = 0.0
    bumped: bool = False
    capped: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class StopPlan:
    side: str
    entry: float
    stop: float
    target: float
    risk_dist: float
    risk_bps: float


def dec(value: float | str) -> Decimal:
    return Decimal(str(value))


def round_step(value: float, step: float) -> float:
    if not math.isfinite(value) or not math.isfinite(step) or step <= 0:
        return 0.0 if not math.isfinite(value) else value
    d = dec(value)
    s = dec(step)
    return float((d / s).to_integral_value(rounding=ROUND_DOWN) * s)


def round_tick(price: float, tick: float, side: Optional[str] = None) -> float:
    if not math.isfinite(price) or not math.isfinite(tick) or tick <= 0:
        return price if math.isfinite(price) else 0.0
    d = dec(price)
    t = dec(tick)
    if side == "down":
        return float((d / t).to_integral_value(rounding=ROUND_DOWN) * t)
    if side == "up":
        return float((d / t).to_integral_value(rounding="ROUND_CEILING") * t)
    return float((d / t).to_integral_value(rounding=ROUND_HALF_UP) * t)


def clamp_stop_distance(
    side: str, entry: float, raw_stop: float, cfg: RiskConfig, filters: SymbolFilters
) -> float:
    if entry <= 0 or not math.isfinite(entry):
        return raw_stop
    min_tick_dist = max(filters.tick_size * 2, entry * cfg.sl_min_bps / 10_000.0)
    max_dist = max(entry * cfg.sl_max_bps / 10_000.0, min_tick_dist)
    if side == "LONG":
        dist = entry - raw_stop
        dist = min(max(dist, min_tick_dist), max_dist)
        stop = round_tick(entry - dist, filters.tick_size, "down")
        if stop >= entry:
            stop = round_tick(entry - min_tick_dist, filters.tick_size, "down")
        return stop
    dist = raw_stop - entry
    dist = min(max(dist, min_tick_dist), max_dist)
    stop = round_tick(entry + dist, filters.tick_size, "up")
    if stop <= entry:
        stop = round_tick(entry + min_tick_dist, filters.tick_size, "up")
    return stop


def plan_stops(
    side: str,
    entry: float,
    atr: float,
    pac_upper: float,
    pac_lower: float,
    cfg: RiskConfig,
    filters: SymbolFilters,
) -> StopPlan:
    if cfg.sl_mode == "atr" and math.isfinite(atr) and atr > 0:
        raw_dist = atr * cfg.sl_atr_mult
        raw_stop = entry - raw_dist if side == "LONG" else entry + raw_dist
    elif cfg.sl_mode == "pac":
        raw_stop = (
            min(pac_lower, entry - atr * 0.3)
            if side == "LONG"
            else max(pac_upper, entry + atr * 0.3)
        )
    else:
        raw_dist = entry * cfg.sl_percent / 100.0
        raw_stop = entry - raw_dist if side == "LONG" else entry + raw_dist

    stop = clamp_stop_distance(side, entry, raw_stop, cfg, filters)
    risk_dist = abs(entry - stop)
    if risk_dist <= 0:
        risk_dist = entry * cfg.sl_min_bps / 10_000.0
    target_raw = (
        entry + risk_dist * cfg.tp_rr if side == "LONG" else entry - risk_dist * cfg.tp_rr
    )
    target = round_tick(target_raw, filters.tick_size, "up" if side == "LONG" else "down")
    if side == "LONG" and target - entry < filters.tick_size:
        target = round_tick(entry + filters.tick_size * 2, filters.tick_size, "up")
    if side == "SHORT" and entry - target < filters.tick_size:
        target = round_tick(entry - filters.tick_size * 2, filters.tick_size, "down")
    risk_bps = risk_dist / entry * 10_000.0
    return StopPlan(
        side=side,
        entry=entry,
        stop=stop,
        target=target,
        risk_dist=risk_dist,
        risk_bps=risk_bps,
    )


def breakeven_price(side: str, entry: float, cfg: RiskConfig) -> float:
    cost = 2.0 * cfg.fee_rate + cfg.slippage_bps / 10_000.0 + cfg.be_buffer_bps / 10_000.0
    if side == "LONG":
        return entry * (1.0 + cost)
    return entry * (1.0 - cost)


def size_position(
    equity: float,
    entry: float,
    stop: float,
    cfg: RiskConfig,
    filters: SymbolFilters,
) -> Sizing:
    if not all(math.isfinite(v) for v in (equity, entry, stop)) or equity <= 0 or entry <= 0:
        return Sizing(False, reason="equity/entry/stop tidak valid")
    risk_dist = abs(entry - stop)
    if risk_dist <= 0:
        return Sizing(False, reason="jarak SL nol")
    risk_usd = equity * cfg.risk_pct / 100.0
    raw_qty = risk_usd / risk_dist
    qty = round_step(raw_qty, filters.step_size)
    bumped = False
    min_notional = max(filters.min_notional, filters.min_qty * entry)
    max_notional = equity * cfg.leverage * min(cfg.max_notional_frac_equity, 1.0)
    hard_max_notional = equity * cfg.leverage * cfg.max_notional_frac_equity

    if qty * entry < min_notional:
        if not cfg.min_notional_bump:
            return Sizing(False, reason=f"notional < minimum {min_notional:.2f} USDT")
        needed = round_step(
            min_notional / entry * 1.001 + filters.step_size, filters.step_size
        )
        bump_risk = needed * risk_dist
        if bump_risk > risk_usd * cfg.max_bump_factor:
            return Sizing(
                False,
                reason=(
                    f"qty minimum butuh risk {bump_risk:.2f} USDT > "
                    f"{cfg.max_bump_factor:.1f}x batas {risk_usd:.2f} USDT"
                ),
            )
        if needed * entry > hard_max_notional:
            return Sizing(False, reason="notional minimum > batas leverage/modal")
        qty = needed
        bumped = True

    capped = False
    intended_qty = qty
    if qty * entry > max_notional:
        qty = round_step(max_notional / entry, filters.step_size)
        capped = intended_qty > qty
        if qty * entry < min_notional:
            return Sizing(False, reason="batas notional terlalu kecil untuk minimum exchange")

    if qty <= 0 or qty * entry < min_notional:
        return Sizing(False, reason="qty nol setelah pembulatan step size")
    if qty > filters.max_qty:
        qty = round_step(filters.max_qty, filters.step_size)
    if qty <= 0 or qty * entry < min_notional:
        return Sizing(False, reason="qty tidak memenuhi minimum setelah batas max_qty")
    if qty * entry > hard_max_notional:
        return Sizing(False, reason="notional melebihi batas keras")

    actual_risk = qty * risk_dist
    reason = "ok"
    if capped:
        reason = (
            f"qty dipotong batas notional {max_notional:.2f} USDT: risiko terpasang "
            f"{actual_risk:.2f} USDT ({actual_risk / equity * 100:.2f}% equity), "
            f"bukan {risk_usd:.2f} USDT ({cfg.risk_pct:g}%)"
        )
    return Sizing(
        ok=True,
        qty=qty,
        notional=qty * entry,
        risk_usd=actual_risk,
        risk_price_dist=risk_dist,
        bumped=bumped,
        capped=capped,
        reason=reason,
    )


def r_now(side: str, entry: float, price: float, risk_dist: float) -> float:
    if risk_dist <= 0:
        return 0.0
    delta = price - entry if side == "LONG" else entry - price
    return delta / risk_dist


def unrealized_pnl(side: str, entry: float, price: float, qty: float) -> float:
    return (price - entry) * qty if side == "LONG" else (entry - price) * qty


def trailing_stop(
    side: str,
    price: float,
    atr: float,
    current_stop: float,
    cfg: RiskConfig,
    filters: SymbolFilters,
    be_ref: float,
) -> float:
    dist = atr * cfg.trail_atr_mult
    if dist <= 0:
        return current_stop
    if side == "LONG":
        candidate = round_tick(price - dist, filters.tick_size, "down")
        candidate = max(candidate, be_ref)
        return candidate if candidate > current_stop else current_stop
    candidate = round_tick(price + dist, filters.tick_size, "up")
    candidate = min(candidate, be_ref) if be_ref > 0 else candidate
    return candidate if candidate < current_stop else current_stop


def wants_stop_update(
    side: str,
    current_stop: float,
    new_stop: float,
    price: float,
    cfg: RiskConfig,
    apply_step: bool = True,
) -> bool:
    if not all(math.isfinite(v) for v in (current_stop, new_stop, price)):
        return False
    if abs(new_stop - current_stop) <= 0:
        return False
    if current_stop <= 0:
        return True
    if apply_step:
        step = max(price * cfg.trail_step_bps / 10_000.0, 0.0)
        if abs(new_stop - current_stop) < step:
            return False
    if side == "LONG":
        return new_stop > current_stop and new_stop < price
    return new_stop < current_stop and new_stop > price


def estimate_fees(entry: float, exit_price: float, qty: float, cfg: RiskConfig) -> float:
    return (entry + exit_price) * qty * cfg.fee_rate


def net_pnl(side: str, entry: float, exit_price: float, qty: float, cfg: RiskConfig) -> float:
    gross = unrealized_pnl(side, entry, exit_price, qty)
    return gross - estimate_fees(entry, exit_price, qty, cfg)
