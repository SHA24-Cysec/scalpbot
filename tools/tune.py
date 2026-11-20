"""Tuning parameter memakai data Binance nyata.

Menjalankan backtest grid + walk-forward, lalu mencetak tabel yang bisa
dipakai untuk memilih parameter. Data diambil sekali dan disimpan di cache
JSON privat supaya percobaan berikutnya cepat. Cache memuat metadata sumber,
interval, mode, simbol, hari, warmup, dan waktu pengambilan.

Contoh:
    python -m tools.tune --days 30 --symbols BTCUSDT,ETHUSDT
    python -m tools.tune --days 30 --grid quick
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import statistics
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalpbot.config import SYMBOL_RE, Config, validate_config
from scalpbot.exchange import BinanceFuturesClient
from scalpbot.indicators import Candle
from scalpbot.risk import SymbolFilters
from tools.backtest import default_warmup, simulate

CACHE = Path(
    os.environ.get(
        "SCALPBOT_TUNE_CACHE",
        str(Path.home() / ".cache" / "scalpbot" / "tune-cache.json"),
    )
)
CACHE_SCHEMA = "scalpbot-tune-cache-v1"
CACHE_SOURCE = "fapi/v1/klines"
CACHE_MAX_AGE_S = 24 * 3600
SPLIT_RATIO = 0.65
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BCHUSDT", "XRPUSDT", "DOGEUSDT"]
INTERVAL_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "3d": 259200,
    "1w": 604800,
    "1M": 2592000,
}


def _reject_constant(value: str) -> None:
    raise ValueError(f"konstanta JSON tidak valid: {value}")


def _parse_candles(raw: Any) -> dict[str, list[Candle]] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, list[Candle]] = {}
    for sym, rows in raw.items():
        if not isinstance(sym, str) or not SYMBOL_RE.fullmatch(sym) or not isinstance(rows, list):
            return None
        parsed: list[Candle] = []
        last_ts = -1
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 6:
                return None
            try:
                candle = Candle(
                    ts=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            except (TypeError, ValueError, OverflowError):
                return None
            values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
            if (
                not all(math.isfinite(v) for v in values)
                or candle.close <= 0
                or candle.ts <= last_ts
                or candle.low > min(candle.open, candle.close)
                or candle.high < max(candle.open, candle.close)
            ):
                return None
            last_ts = candle.ts
            parsed.append(candle)
        out[sym] = parsed
    return out


def _parse_filters(raw: Any) -> dict[str, SymbolFilters] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, SymbolFilters] = {}
    for sym, row in raw.items():
        if not isinstance(sym, str) or not SYMBOL_RE.fullmatch(sym) or not isinstance(row, dict):
            return None
        try:
            item = SymbolFilters(
                symbol=str(row["symbol"]),
                tick_size=float(row["tick_size"]),
                step_size=float(row["step_size"]),
                min_qty=float(row["min_qty"]),
                max_qty=float(row["max_qty"]),
                min_notional=float(row["min_notional"]),
                qty_precision=int(row["qty_precision"]),
                price_precision=int(row["price_precision"]),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        values = (
            item.tick_size,
            item.step_size,
            item.min_qty,
            item.max_qty,
            item.min_notional,
        )
        if (
            item.symbol != sym
            or not all(math.isfinite(v) for v in values)
            or min(item.tick_size, item.step_size, item.min_qty, item.min_notional) <= 0
            or item.max_qty <= 0
            or item.qty_precision < 0
            or item.price_precision < 0
        ):
            return None
        out[sym] = item
    return out


def _read_cache(
    interval: str,
    symbols: list[str],
    mode: str,
    base_url: str,
    days: int,
    warmup_bars: int,
    need: int,
    max_age_s: int,
) -> tuple[dict[str, list[Candle]], dict[str, SymbolFilters]] | None:
    if CACHE.is_symlink() or CACHE.parent.is_symlink() or not CACHE.is_file():
        return None
    try:
        blob = json.loads(CACHE.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(blob, dict):
        return None
    expected = {
        "schema": CACHE_SCHEMA,
        "source": CACHE_SOURCE,
        "interval": interval,
        "interval_seconds": INTERVAL_SECONDS[interval],
        "mode": mode,
        "base_url": base_url,
        "days": days,
        "warmup_bars": warmup_bars,
        "need": need,
        "split_ratio": SPLIT_RATIO,
    }
    if any(blob.get(k) != v for k, v in expected.items()):
        return None
    try:
        fetched_at = int(blob.get("fetched_at") or 0)
        age_ms = int(time.time() * 1000) - fetched_at
    except (TypeError, ValueError, OverflowError):
        return None
    if fetched_at <= 0 or age_ms < -60_000 or age_ms > max(0, int(max_age_s)) * 1000:
        return None
    cached_symbols = blob.get("symbols")
    if not isinstance(cached_symbols, list) or not all(
        isinstance(s, str) and SYMBOL_RE.fullmatch(s) for s in cached_symbols
    ):
        return None
    if not all(s in cached_symbols for s in symbols):
        return None
    candles = _parse_candles(blob.get("candles"))
    filters = _parse_filters(blob.get("filters"))
    if candles is None or filters is None:
        return None
    if not all(s in candles and s in filters and len(candles[s]) >= need for s in symbols):
        return None
    return candles, filters


def _write_cache(payload: dict[str, Any]) -> None:
    if CACHE.is_symlink() or CACHE.parent.is_symlink():
        raise OSError("path cache tidak boleh symlink")
    CACHE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(CACHE.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(CACHE, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), allow_nan=False)
    os.chmod(CACHE, 0o600)


def _cache_payload(
    candles: dict[str, list[Candle]],
    filters: dict[str, SymbolFilters],
    symbols: list[str],
    interval: str,
    days: int,
    warmup_bars: int,
    need: int,
    cfg: Config,
    base_url: str,
) -> dict[str, Any]:
    selected = sorted(set(symbols))
    return {
        "schema": CACHE_SCHEMA,
        "source": CACHE_SOURCE,
        "interval": interval,
        "interval_seconds": INTERVAL_SECONDS[interval],
        "mode": cfg.mode,
        "base_url": base_url,
        "symbols": selected,
        "days": days,
        "warmup_bars": warmup_bars,
        "need": need,
        "split_ratio": SPLIT_RATIO,
        "fetched_at": int(time.time() * 1000),
        "candles": {
            sym: [[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in candles.get(sym, [])]
            for sym in selected
        },
        "filters": {sym: asdict(filters[sym]) for sym in selected if sym in filters},
    }


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("harus bilangan bulat positif") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("harus bilangan bulat positif")
    return parsed


def split_data(data: dict, ratio: float = SPLIT_RATIO) -> tuple[dict, dict]:
    train: dict = {"candles": {}, "filters": data["filters"]}
    test: dict = {"candles": {}, "filters": data["filters"]}
    for sym, candles in data["candles"].items():
        cut = int(len(candles) * ratio)
        train["candles"][sym] = candles[:cut]
        test["candles"][sym] = candles[cut:]
    return train, test


def _required_bars(days: int, interval: str, cfg: Config) -> tuple[int, int, int]:
    secs = INTERVAL_SECONDS[interval]
    requested = max(1, int(days * 86400 / secs))
    warmup = default_warmup(cfg)
    min_oos = math.ceil((warmup + 1) / (1.0 - SPLIT_RATIO)) + requested
    return max(requested + warmup, min_oos), warmup, requested


def load_data(
    days: int,
    symbols: list[str],
    interval: str = "5m",
    *,
    refresh: bool = False,
    max_age_s: int = CACHE_MAX_AGE_S,
) -> dict:
    if days < 1:
        raise ValueError("days minimal 1")
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"interval tidak didukung: {interval}")
    if not symbols or not all(isinstance(s, str) and SYMBOL_RE.fullmatch(s) for s in symbols):
        raise ValueError("symbols tidak valid")
    cfg = Config()
    base_url = cfg.base_url().rstrip("/")
    need, warmup, _ = _required_bars(days, interval, cfg)
    if not refresh:
        cached = _read_cache(
            interval, symbols, cfg.mode, base_url, days, warmup, need, max_age_s
        )
        if cached is not None:
            candles, filters = cached
            print(f"cache: {CACHE} ({min(len(candles[s]) for s in symbols)} bar tersedia)")
            return {
                "candles": {s: candles[s][-need:] for s in symbols},
                "filters": {s: filters[s] for s in symbols},
            }

    client = BinanceFuturesClient(cfg)
    print(f"mengambil {need} bar {interval} untuk {len(symbols)} simbol...")
    try:
        candles: dict[str, list[Candle]] = {}
        for sym in symbols:
            end = None
            got: list[Candle] = []
            while len(got) < need:
                page = client.klines(sym, interval, limit=1500, end_time=end)
                if not page:
                    break
                got = page + got
                end = page[0].ts - 1
            candles[sym] = got[-need:] if len(got) > need else got
            print(f"  {sym}: {len(candles[sym])} bar")
        filters = client.load_all_filters()
        missing = [s for s in symbols if s not in filters]
        if missing:
            raise ValueError(f"filter exchange tidak tersedia untuk {', '.join(missing)}")
        payload = _cache_payload(
            candles, filters, symbols, interval, days, warmup, need, cfg, base_url
        )
    finally:
        client.close()
    try:
        _write_cache(payload)
    except OSError as exc:
        print(f"peringatan: cache tidak ditulis ({exc})")
    return {"candles": candles, "filters": {s: filters[s] for s in symbols}}


def make_cfg(**overrides) -> Config:
    cfg = Config()
    cfg.mode = "paper"
    for key, value in overrides.items():
        if "__" not in key:
            raise ValueError(f"override tidak valid: {key}")
        section, field = key.split("__", 1)
        target = getattr(cfg, section, None)
        if (
            target is None
            or not hasattr(target, "__dataclass_fields__")
            or field not in target.__dataclass_fields__
        ):
            raise ValueError(f"override tidak dikenal: {key}")
        setattr(target, field, value)
    validate_config(cfg)
    return cfg


def evaluate(data: dict, cfg: Config, warmup: int | None = None, interval_s: int = 300) -> dict:
    if warmup is None:
        warmup = default_warmup(cfg)
    trades = []
    net = 0.0
    for sym, candles in data["candles"].items():
        res = simulate(
            sym,
            candles,
            cfg,
            data["filters"][sym],
            equity=100.0,
            warmup=warmup,
            interval_seconds=interval_s,
        )
        trades.extend(res.trades)
        net += res.net_pct
    if not trades:
        return {"n": 0, "win": 0.0, "net": 0.0, "expR": 0.0, "pf": 0.0}
    gains = sum(t.pnl for t in trades if t.pnl > 0)
    losses = -sum(t.pnl for t in trades if t.pnl < 0)
    return {
        "n": len(trades),
        "win": sum(1 for t in trades if t.pnl > 0) / len(trades) * 100.0,
        "net": net,
        "expR": statistics.fmean(t.r for t in trades),
        "pf": gains / losses if losses > 0 else 9.99,
    }


def walk_forward(data: dict, cfg: Config, warmup: int | None = None, interval_s: int = 300) -> tuple[float, float]:
    if warmup is None:
        warmup = default_warmup(cfg)
    train, test = split_data(data)

    def mean_r(part: dict) -> float:
        values = []
        for sym, candles in part["candles"].items():
            res = simulate(
                sym,
                candles,
                cfg,
                part["filters"][sym],
                equity=100.0,
                warmup=warmup,
                interval_seconds=interval_s,
            )
            values.extend(t.r for t in res.trades)
        return statistics.fmean(values) if values else 0.0

    return mean_r(train), mean_r(test)


GRIDS = {
    "quick": {
        "risk__sl_atr_mult": [1.5, 3.0, 5.0, 8.0],
        "risk__tp_rr": [1.5, 2.5, 4.0],
        "risk__be_trigger_r": [0.5, 1.0, 2.0],
        "risk__trail_trigger_r": [1.0, 2.5],
        "risk__trail_atr_mult": [1.2, 4.0],
    },
    "cost": {
        "risk__fee_rate": [0.0004, 0.0002, 0.0001, 0.0],
        "risk__sl_atr_mult": [1.5, 5.0],
        "risk__tp_rr": [1.5, 4.0],
    },
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tune", description="Tuning parameter dari data Binance nyata")
    ap.add_argument("--days", type=positive_int, default=45, help="panjang data historis")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--interval", default="5m", choices=sorted(INTERVAL_SECONDS))
    ap.add_argument("--grid", default="quick", choices=sorted(GRIDS))
    ap.add_argument("--top", type=positive_int, default=15)
    ap.add_argument("--walk-forward", action="store_true", help="uji 65%% data pertama vs sisanya untuk 3 teratas")
    ap.add_argument("--refresh-cache", action="store_true", help="abaikan cache lalu unduh ulang")
    ap.add_argument(
        "--cache-max-age-hours",
        type=positive_int,
        default=CACHE_MAX_AGE_S // 3600,
        help="umur maksimum cache JSON dalam jam",
    )
    args = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    for sym in symbols:
        if not SYMBOL_RE.fullmatch(sym):
            ap.error(f"simbol tidak valid: {sym}")
    interval_s = INTERVAL_SECONDS[args.interval]
    data = load_data(
        args.days,
        symbols,
        args.interval,
        refresh=args.refresh_cache,
        max_age_s=args.cache_max_age_hours * 3600,
    )
    train, _ = split_data(data)
    grid = GRIDS[args.grid]
    keys = list(grid)
    combos = list(itertools.product(*(grid[k] for k in keys)))
    print(f"\n{len(combos)} kombinasi diuji pada {len(symbols)} simbol ({args.days} hari, seleksi 65% pertama)\n")
    print(f"{'sl_atr':>7}{'tp_rr':>7}{'be_r':>6}{'trail_r':>8}{'trail_atr':>10}{'n':>7}{'win%':>7}{'net%':>9}{'expR':>8}{'PF':>7}")
    rows = []
    for combo in combos:
        overrides = dict(zip(keys, combo))
        metrics = evaluate(train, make_cfg(**overrides), warmup=None, interval_s=interval_s)
        rows.append((metrics["expR"], overrides, metrics))
    rows.sort(key=lambda r: r[0], reverse=True)
    for exp_r, overrides, m in rows[: args.top]:
        print(
            f"{overrides.get('risk__sl_atr_mult', '-'):>7}{overrides.get('risk__tp_rr', '-'):>7}"
            f"{overrides.get('risk__be_trigger_r', 0):>6}{overrides.get('risk__trail_trigger_r', 0):>8}"
            f"{overrides.get('risk__trail_atr_mult', 0):>10}{m['n']:>7}{m['win']:>7.1f}"
            f"{m['net']:>+9.1f}{m['expR']:>+8.3f}{m['pf']:>7.2f}"
        )
    if args.walk_forward:
        print("\nwalk-forward (65% pertama vs 35% terakhir, seleksi hanya pada 65% pertama):")
        print(f"{'overrides':<60}{'IS expR':>9}{'OOS expR':>10}")
        for exp_r, overrides, _ in rows[:3]:
            ins, oos = walk_forward(data, make_cfg(**overrides), warmup=None, interval_s=interval_s)
            label = ", ".join(f"{k.split('__')[1]}={v}" for k, v in overrides.items())
            print(f"{label:<60}{ins:>+9.3f}{oos:>+10.3f}")
    print("\nCatatan: expR = rata-rata R per trade. Positif saja belum cukup; lihat juga PF dan walk-forward.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
