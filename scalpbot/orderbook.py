from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .config import OrderbookConfig


@dataclass(slots=True)
class BookSnapshot:
    symbol: str
    ts: float
    bid: float
    ask: float
    spread_bps: float
    bid_vol: float
    ask_vol: float
    imbalance: float
    depth_limit: int
    levels: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "bid": self.bid,
            "ask": self.ask,
            "spread_bps": round(self.spread_bps, 2),
            "imbalance": round(self.imbalance, 3),
            "bid_vol": round(self.bid_vol, 4),
            "ask_vol": round(self.ask_vol, 4),
        }


def analyze_depth(
    symbol: str, payload: dict[str, Any], depth_limit: int, weights: bool = True
) -> Optional[BookSnapshot]:
    bids = payload.get("bids") or []
    asks = payload.get("asks") or []
    if not bids or not asks:
        return None
    try:
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
    except (TypeError, ValueError, IndexError):
        return None
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None
    bid_vol = 0.0
    ask_vol = 0.0
    for i, level in enumerate(bids[:depth_limit]):
        try:
            qty = float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        bid_vol += qty * (1.0 / (1.0 + i * 0.15) if weights else 1.0)
    for i, level in enumerate(asks[:depth_limit]):
        try:
            qty = float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        ask_vol += qty * (1.0 / (1.0 + i * 0.15) if weights else 1.0)
    total = bid_vol + ask_vol
    imbalance = (bid_vol - ask_vol) / total if total > 0 else 0.0
    mid = (best_bid + best_ask) / 2.0
    spread_bps = (best_ask - best_bid) / mid * 10_000.0 if mid > 0 else 0.0
    return BookSnapshot(
        symbol=symbol,
        ts=time.time(),
        bid=best_bid,
        ask=best_ask,
        spread_bps=spread_bps,
        bid_vol=bid_vol,
        ask_vol=ask_vol,
        imbalance=imbalance,
        depth_limit=depth_limit,
        levels=min(len(bids), len(asks), depth_limit),
    )


def classify_reason(reason: str) -> str:
    """Petakan alasan penolakan ke kategori: spread, imbalance, stale, atau other."""
    text = reason.strip().lower()
    if text.startswith("spread"):
        return "spread"
    if text.startswith("imbalance"):
        return "imbalance"
    if "terlalu lama" in text:
        return "stale"
    return "other"


class OrderbookFilter:
    def __init__(self, cfg: OrderbookConfig, fetch_depth: Callable[[str, int], Optional[dict]]) -> None:
        self.cfg = cfg
        self._fetch = fetch_depth
        self._cache: dict[str, BookSnapshot] = {}
        self._last_fetch: dict[str, float] = {}

    def snapshot(self, symbol: str, force: bool = False) -> Optional[BookSnapshot]:
        if not self.cfg.enabled:
            return None
        interval = float(self.cfg.periodic_snapshot_s or 0)
        now = time.time()
        cached = self._cache.get(symbol)
        if not force and interval > 0 and cached and now - self._last_fetch.get(symbol, 0.0) < interval:
            return cached
        if not force and interval <= 0 and cached and now - self._last_fetch.get(symbol, 0.0) < 1.5:
            return cached
        payload = self._fetch(symbol, self.cfg.depth_limit)
        if not payload:
            return cached if self._fresh(cached) else None
        snap = analyze_depth(symbol, payload, self.cfg.depth_limit)
        if snap is None:
            return cached if self._fresh(cached) else None
        self._cache[symbol] = snap
        self._last_fetch[symbol] = now
        return snap

    def _fresh(self, snap: Optional[BookSnapshot]) -> bool:
        if snap is None:
            return False
        return (time.time() - snap.ts) <= max(0.1, self.cfg.max_age_s)

    def check(self, side: str, snap: Optional[BookSnapshot]) -> tuple[bool, str]:
        if not self.cfg.enabled:
            return True, "orderbook filter off"
        if snap is None:
            return True, "orderbook tidak tersedia"
        if not self._fresh(snap):
            return False, f"snapshot orderbook terlalu lama (>{self.cfg.max_age_s:.0f}s)"
        if snap.spread_bps > self.cfg.max_spread_bps:
            return False, f"spread {snap.spread_bps:.1f}bps > {self.cfg.max_spread_bps:.1f}bps"
        if side == "LONG" and snap.imbalance < self.cfg.min_imbalance:
            return False, f"imbalance {snap.imbalance:+.2f} < +{self.cfg.min_imbalance:.2f}"
        if side == "SHORT" and snap.imbalance > -self.cfg.min_imbalance:
            return False, f"imbalance {snap.imbalance:+.2f} > -{self.cfg.min_imbalance:.2f}"
        return True, f"imbalance {snap.imbalance:+.2f}"

    def cached(self, symbol: str) -> Optional[BookSnapshot]:
        return self._cache.get(symbol)
