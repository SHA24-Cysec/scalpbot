from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from .risk import SymbolFilters

SELECTED = "selected"
SKIP_VOLUME = "volume_di_bawah_minimum"
SKIP_NOTIONAL = "min_notional_tidak_muat"
SKIP_DUPLICATE = "duplikat"
SKIP_LIMIT = "dipotong_top_n"
SKIP_NOT_TRADABLE = "tidak_tradable"
SKIP_EXCLUDED = "dikecualikan"
SKIP_DATA = "data_tidak_valid"


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    quote_volume: float
    min_notional: float
    status: str


@dataclass(frozen=True, slots=True)
class Selection:
    symbols: list[str]
    candidates: list[Candidate]

    def skipped(self) -> list[Candidate]:
        return [c for c in self.candidates if c.status != SELECTED]

    def summary(self, limit: int = 6) -> str:
        top = [c for c in self.candidates if c.status == SELECTED][:limit]
        parts = [f"{c.symbol}({c.quote_volume / 1_000_000:.0f}M)" for c in top]
        return ", ".join(parts) if parts else "-"


def _to_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def select_symbols(
    tickers: Iterable[Mapping[str, Any]],
    tradable: Iterable[str],
    filters: Mapping[str, SymbolFilters],
    exclude: Iterable[str] = (),
    min_quote_volume: float = 0.0,
    top_n: int = 5,
    equity: float = 0.0,
    leverage: int = 1,
    max_notional_frac: float = 1.0,
    require_affordable: bool = True,
) -> Selection:
    allowed = {s.upper() for s in tradable}
    blocked = {s.upper() for s in exclude}
    cap = (
        equity * max(1, leverage) * max(0.1, max_notional_frac)
        if require_affordable and equity > 0
        else None
    )

    candidates: list[Candidate] = []
    seen: set[str] = set()
    for ticker in tickers:
        symbol = str(ticker.get("symbol", "")).upper()
        if not symbol:
            continue
        if symbol in seen:
            candidates.append(Candidate(symbol, 0.0, 0.0, SKIP_DUPLICATE))
            continue
        seen.add(symbol)
        flt = filters.get(symbol)
        min_notional = flt.min_notional if flt else 0.0
        quote_volume = _to_float(ticker.get("quoteVolume")) or 0.0
        if symbol in blocked:
            candidates.append(Candidate(symbol, quote_volume, min_notional, SKIP_EXCLUDED))
            continue
        if symbol not in allowed or flt is None:
            candidates.append(Candidate(symbol, quote_volume, min_notional, SKIP_NOT_TRADABLE))
            continue
        if quote_volume <= 0:
            candidates.append(Candidate(symbol, quote_volume, min_notional, SKIP_DATA))
            continue
        if quote_volume < min_quote_volume:
            candidates.append(Candidate(symbol, quote_volume, min_notional, SKIP_VOLUME))
            continue
        if cap is not None and min_notional > cap:
            candidates.append(Candidate(symbol, quote_volume, min_notional, SKIP_NOTIONAL))
            continue
        candidates.append(Candidate(symbol, quote_volume, min_notional, SELECTED))

    eligible = [c for c in candidates if c.status == SELECTED]
    eligible.sort(key=lambda c: (-c.quote_volume, c.symbol))
    limit = max(1, min(int(top_n), 25))
    chosen = eligible[:limit]
    chosen_symbols = [c.symbol for c in chosen]
    chosen_set = set(chosen_symbols)

    final: list[Candidate] = []
    for candidate in candidates:
        if candidate.status == SELECTED and candidate.symbol not in chosen_set:
            final.append(Candidate(candidate.symbol, candidate.quote_volume, candidate.min_notional, SKIP_LIMIT))
        else:
            final.append(candidate)
    final.sort(key=lambda c: (c.status != SELECTED, -c.quote_volume, c.symbol))
    return Selection(symbols=chosen_symbols, candidates=final)
