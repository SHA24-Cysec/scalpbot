"""Optimasi parameter exit memakai arsip klines resmi Binance.

Mengunduh data bulanan 5m dari data.binance.vision, menjalankan simulator
`tools.backtest`, lalu memilih parameter exit yang positif di dua jendela data
terpisah (in-sample dan out-of-sample). Bobot biaya mengikuti config bot:
fee taker dan slippage dua sisi.

Contoh:
    python3 -m tools.optimize_exit --months 13
    python3 -m tools.optimize_exit --months 13 --symbols BTCUSDT,ETHUSDT --quick
"""

from __future__ import annotations

import argparse
import csv
import io
import itertools
import json
import os
import stat
import statistics
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalpbot.config import SYMBOL_RE, Config
from scalpbot.indicators import Candle
from scalpbot.risk import SymbolFilters
from tools.backtest import default_warmup, simulate

ARCHIVE = "https://data.binance.vision/data/futures/um/monthly/klines"
CACHE = Path(os.environ.get("SCALPBOT_ARCHIVE_CACHE", str(Path.home() / ".cache" / "scalpbot" / "archive")))
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BCHUSDT", "XRPUSDT", "DOGEUSDT"]
FALLBACK_FILTERS = {
    "BTCUSDT": (0.1, 0.001, 0.001, 1000.0, 5.0, 3, 1),
    "ETHUSDT": (0.01, 0.001, 0.001, 10000.0, 5.0, 3, 2),
    "BCHUSDT": (0.01, 0.001, 0.001, 10000.0, 5.0, 3, 2),
    "XRPUSDT": (0.0001, 1.0, 1.0, 10000000.0, 5.0, 0, 4),
    "DOGEUSDT": (0.00001, 1.0, 1.0, 10000000.0, 5.0, 0, 5),
    "SOLUSDT": (0.01, 0.01, 0.01, 100000.0, 5.0, 2, 2),
    "ADAUSDT": (0.0001, 1.0, 1.0, 10000000.0, 5.0, 0, 4),
    "LINKUSDT": (0.001, 0.01, 0.01, 1000000.0, 5.0, 2, 3),
}
QUICK_GRID = {
    "sl": [3.0, 5.0, 6.0, 8.0, 10.0],
    "tp": [2.0, 3.0, 3.5, 4.0, 6.0],
}
FULL_GRID = {
    "sl": [1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0],
    "tp": [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 6.0],
}


def filters_for(symbol: str) -> SymbolFilters:
    row = FALLBACK_FILTERS.get(symbol)
    if row is None:
        raise SystemExit(f"filter exchange untuk {symbol} tidak dikenal; tambahkan ke FALLBACK_FILTERS")
    tick, step, min_qty, max_qty, min_notional, qty_prec, price_prec = row
    return SymbolFilters(
        symbol=symbol,
        tick_size=tick,
        step_size=step,
        min_qty=min_qty,
        max_qty=max_qty,
        min_notional=min_notional,
        qty_precision=qty_prec,
        price_precision=price_prec,
    )


def months_back(count: int, today: date | None = None) -> list[str]:
    today = today or date.today()
    year, month = today.year, today.month
    out = []
    for _ in range(count):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
        out.append(f"{year:04d}-{month:02d}")
    return sorted(out)


def _cache_path(symbol: str, month: str) -> Path:
    return CACHE / f"{symbol}-5m-{month}.zip"


def fetch_month(symbol: str, month: str, retries: int = 3) -> list[Candle]:
    path = _cache_path(symbol, month)
    if not path.is_file():
        CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
        url = f"{ARCHIVE}/{symbol}/5m/{symbol}-5m-{month}.zip"
        last: Exception | None = None
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(url, timeout=60) as resp:
                    blob = resp.read()
                tmp = path.with_suffix(".part")
                tmp.write_bytes(blob)
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
                tmp.replace(path)
                last = None
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return []
                last = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
            time.sleep(1.5 * (attempt + 1))
        if last is not None:
            raise SystemExit(f"gagal mengunduh {symbol} {month}: {last}")
    return parse_zip(path.read_bytes())


def parse_zip(blob: bytes) -> list[Candle]:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = zf.namelist()[0]
        text = zf.read(name).decode("utf-8")
    out: list[Candle] = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row or row[0] == "open_time":
            continue
        try:
            candle = Candle(
                ts=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
        except (IndexError, TypeError, ValueError):
            continue
        if candle.close <= 0 or candle.high < candle.low:
            continue
        out.append(candle)
    return out


def load_series(symbol: str, months: Iterable[str]) -> list[Candle]:
    seen: dict[int, Candle] = {}
    for month in months:
        for candle in fetch_month(symbol, month):
            seen.setdefault(candle.ts, candle)
    return [seen[ts] for ts in sorted(seen)]


def make_cfg(sl: float, tp: float, be: float, trail_trigger: float, trail_mult: float,
             fee: float, slippage: float) -> Config:
    cfg = Config()
    cfg.mode = "paper"
    cfg.risk.sl_atr_mult = sl
    cfg.risk.tp_rr = tp
    cfg.risk.be_trigger_r = be
    cfg.risk.trail_trigger_r = trail_trigger
    cfg.risk.trail_atr_mult = trail_mult
    cfg.risk.fee_rate = fee
    cfg.risk.slippage_bps = slippage
    return cfg


def evaluate(data: dict[str, list[Candle]], cfg: Config) -> dict:
    warmup = default_warmup(cfg)
    trades = []
    net = 0.0
    for symbol, candles in data.items():
        if len(candles) <= warmup + 50:
            continue
        res = simulate(symbol, candles, cfg, filters_for(symbol), equity=100.0,
                       warmup=warmup, interval_seconds=300)
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
        "trades": trades,
    }


def ci95(trades, samples: int = 4000, seed: int = 7, block: int = 20) -> tuple[float, float]:
    from tools.backtest import _BootstrapRng

    rs = [t.r for t in trades]
    n = len(rs)
    if n < 20:
        return (0.0, 0.0)
    width = max(1, min(block, n))
    rng = _BootstrapRng(seed)
    means = []
    for _ in range(samples):
        picked: list[float] = []
        while len(picked) < n:
            start = rng.randrange(n)
            for offset in range(width):
                picked.append(rs[(start + offset) % n])
        means.append(statistics.fmean(picked[:n]))
    means.sort()
    return means[int(0.025 * samples)], means[int(0.975 * samples)]


def split_months(months: list[str], ratio: float) -> tuple[list[str], list[str]]:
    cut = max(1, min(len(months) - 1, int(round(len(months) * ratio))))
    return months[:cut], months[cut:]


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("harus bilangan bulat positif") from None
    if parsed < 4:
        raise argparse.ArgumentTypeError("minimal 4 bulan supaya in-sample dan out-of-sample terisi")
    return parsed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="optimize_exit", description="Optimasi exit dari arsip Binance")
    ap.add_argument("--months", type=positive_int, default=13, help="jumlah bulan arsip (selesai bulan lalu)")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--quick", action="store_true", help="grid kecil 25 kombinasi")
    ap.add_argument("--split", type=float, default=0.5, help="porsi bulan awal untuk in-sample")
    ap.add_argument("--fee", type=float, default=0.0004, help="fee taker per sisi")
    ap.add_argument("--slippage-bps", type=float, default=1.0)
    ap.add_argument("--json", default="", help="simpan hasil lengkap ke path JSON")
    args = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    for symbol in symbols:
        if not SYMBOL_RE.fullmatch(symbol):
            ap.error(f"simbol tidak valid: {symbol}")
        if symbol not in FALLBACK_FILTERS:
            ap.error(f"filter untuk {symbol} belum ada di FALLBACK_FILTERS")
    if not 0.2 <= args.split <= 0.8:
        ap.error("--split harus 0.2..0.8")

    months = months_back(args.months)
    is_months, oos_months = split_months(months, args.split)
    print(f"bulan tersedia: {months[0]} .. {months[-1]}")
    print(f"in-sample: {is_months[0]}..{is_months[-1]} | out-of-sample: {oos_months[0]}..{oos_months[-1]}")

    is_data: dict[str, list[Candle]] = {}
    oos_data: dict[str, list[Candle]] = {}
    for symbol in symbols:
        candles = load_series(symbol, months)
        cut = _month_start(oos_months[0])
        is_data[symbol] = [c for c in candles if c.ts < cut]
        oos_data[symbol] = [c for c in candles if c.ts >= cut]
        print(f"  {symbol}: {len(candles)} bar, IS {len(is_data[symbol])}, OOS {len(oos_data[symbol])}")

    full_data = {s: is_data[s] + oos_data[s] for s in symbols}

    def run(data, sl, tp, be=0.0, trg=0.0, trm=0.0) -> dict:
        cfg = make_cfg(sl, tp, be, trg, trm, args.fee, args.slippage_bps)
        return evaluate(data, cfg)

    baseline = run(full_data, 5.0, 4.0, 1.0, 2.5, 1.2)
    print(f"\nbaseline config repo (SL 5, TP 4, BE 1, trail 2.5/1.2): "
          f"n={baseline['n']} net={baseline['net']:+.1f}% expR={baseline['expR']:+.3f} PF={baseline['pf']:.2f}")

    grid = QUICK_GRID if args.quick else FULL_GRID
    combos = list(itertools.product(grid["sl"], grid["tp"]))
    print(f"\nfase 1: {len(combos)} kombinasi SL/TP tanpa BE dan tanpa trailing")
    print(f"{'sl':>5}{'tp':>6}{'IS n':>7}{'IS expR':>9}{'IS net':>8}{'OOS n':>7}{'OOS expR':>9}{'OOS net':>8}{'full expR':>10}")
    rows = []
    for sl, tp in combos:
        a = run(is_data, sl, tp)
        b = run(oos_data, sl, tp)
        c = run(full_data, sl, tp)
        rows.append({"sl": sl, "tp": tp, "is": _metrics(a), "oos": _metrics(b), "full": _metrics(c)})
        print(f"{sl:>5}{tp:>6}{a['n']:>7}{a['expR']:>+9.3f}{a['net']:>+8.1f}"
              f"{b['n']:>7}{b['expR']:>+9.3f}{b['net']:>+8.1f}{c['expR']:>+10.3f}")
    robust = [r for r in rows if r["is"]["expR"] > 0 and r["oos"]["expR"] > 0]
    ranked = sorted(robust or rows, key=lambda r: min(r["is"]["expR"], r["oos"]["expR"]), reverse=True)
    best = ranked[0]
    print(f"\nkandidat terbaik: SL {best['sl']}xATR, TP {best['tp']}R, tanpa BE/trail"
          f" ({len(robust)} dari {len(rows)} kombinasi positif di kedua jendela)")

    print("\nfase 2: uji apakah BE dan trailing menambah atau mengurangi hasil")
    print(f"{'varian':<34}{'n':>6}{'expR':>8}{'net%':>8}{'PF':>7}")
    variants = [
        ("tanpa BE, tanpa trail", 0.0, 0.0, 0.0),
        ("BE 1R lalu trail 2.5R/1.2ATR", 1.0, 2.5, 1.2),
        ("BE 1R saja", 1.0, 0.0, 0.0),
        ("trail 2.5R/1.2ATR saja", 0.0, 2.5, 1.2),
        ("BE 2R saja", 2.0, 0.0, 0.0),
    ]
    for label, be, trg, trm in variants:
        m = run(full_data, best["sl"], best["tp"], be, trg, trm)
        print(f"{label:<34}{m['n']:>6}{m['expR']:>+8.3f}{m['net']:>+8.1f}{m['pf']:>7.2f}")

    final = run(full_data, best["sl"], best["tp"])
    ci = ci95(final["trades"])
    print(f"\nhasil akhir seluruh bulan: n={final['n']} win={final['win']:.1f}% net={final['net']:+.1f}% "
          f"expR={final['expR']:+.3f} PF={final['pf']:.2f}")
    print(f"bootstrap blok CI95 expR: [{ci[0]:+.3f}, {ci[1]:+.3f}]")

    if args.json:
        payload = {
            "generated_at": int(time.time()),
            "months": months,
            "symbols": symbols,
            "fee_rate": args.fee,
            "slippage_bps": args.slippage_bps,
            "baseline": _metrics(baseline),
            "rows": rows,
            "best": best,
            "ci95_final": [round(ci[0], 4), round(ci[1], 4)],
        }
        Path(args.json).write_text(json.dumps(payload, indent=1), encoding="utf-8")
        print(f"hasil JSON: {args.json}")
    return 0


def _metrics(m: dict) -> dict:
    return {k: round(v, 4) for k, v in m.items() if k != "trades"}


def _month_start(month: str) -> int:
    from datetime import datetime, timezone

    year, mon = (int(x) for x in month.split("-"))
    return int(datetime(year, mon, 1, tzinfo=timezone.utc).timestamp() * 1000)


if __name__ == "__main__":
    raise SystemExit(main())
