"""Tuning parameter memakai data Binance nyata.

Menjalankan backtest grid + walk-forward, lalu mencetak tabel yang bisa
dipakai untuk memilih parameter. Data diambil sekali dan di-cache di
/tmp/bt/data.pkl supaya percobaan berikutnya cepat.

Contoh:
    python -m tools.tune --days 30 --symbols BTCUSDT,ETHUSDT
    python -m tools.tune --days 30 --grid quick
"""

from __future__ import annotations

import argparse
import itertools
import pickle
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalpbot.config import Config  # noqa: E402
from scalpbot.exchange import BinanceFuturesClient  # noqa: E402
from tools.backtest import simulate  # noqa: E402

CACHE = Path("/tmp/bt/data.pkl")
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BCHUSDT", "XRPUSDT", "DOGEUSDT"]


def load_data(days: int, symbols: list[str], interval: str = "5m") -> dict:
    bars_per_day = {"5m": 288, "15m": 96, "30m": 48, "1h": 24}[interval]
    need = int(days * bars_per_day)
    if CACHE.exists():
        blob = pickle.loads(CACHE.read_bytes())
        have = {k: len(v) for k, v in blob["candles"].items()}
        if all(have.get(s, 0) >= need for s in symbols):
            print(f"cache: {CACHE} ({min(have[s] for s in symbols)} bar tersedia)")
            return {
                "candles": {s: blob["candles"][s][-need:] for s in symbols},
                "filters": {s: blob["filters"][s] for s in symbols},
            }
    cfg = Config()
    client = BinanceFuturesClient(cfg)
    print(f"mengambil {need} bar {interval} untuk {len(symbols)} simbol...")
    candles: dict[str, list] = {}
    for sym in symbols:
        end = None
        got: list = []
        while len(got) < need:
            page = client.klines(sym, interval, limit=1500, end_time=end)
            if not page:
                break
            got = page + got
            end = page[0].ts - 1
        candles[sym] = got[-need:] if len(got) > need else got
        print(f"  {sym}: {len(candles[sym])} bar")
    filters = client.load_all_filters()
    client.close()
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_bytes(pickle.dumps({"candles": candles, "filters": {s: filters[s] for s in symbols}}))
    return {"candles": candles, "filters": {s: filters[s] for s in symbols}}


def make_cfg(**overrides) -> Config:
    cfg = Config()
    cfg.mode = "paper"
    for key, value in overrides.items():
        section, field = key.split("__")
        setattr(getattr(cfg, section), field, value)
    return cfg


def evaluate(data: dict, cfg: Config, warmup: int = 800) -> dict:
    trades = []
    net = 0.0
    for sym, candles in data["candles"].items():
        res = simulate(sym, candles, cfg, data["filters"][sym], equity=100.0, warmup=warmup)
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


def walk_forward(data: dict, cfg: Config, warmup: int = 800) -> tuple[float, float]:
    inside = []
    outside = []
    for sym, candles in data["candles"].items():
        split = int(len(candles) * 0.65)
        a = simulate(sym, candles[:split], cfg, data["filters"][sym], equity=100.0, warmup=warmup)
        b = simulate(sym, candles[split:], cfg, data["filters"][sym], equity=100.0, warmup=warmup)
        inside.extend(t.r for t in a.trades)
        outside.extend(t.r for t in b.trades)
    f = statistics.fmean if inside and outside else (lambda v: 0.0)
    return (f(inside) if inside else 0.0), (f(outside) if outside else 0.0)


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
    ap.add_argument("--days", type=int, default=45, help="panjang data historis")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--grid", default="quick", choices=sorted(GRIDS))
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--walk-forward", action="store_true", help="uji 65% data pertama vs sisanya untuk 3 teratas")
    args = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    data = load_data(args.days, symbols, args.interval)
    grid = GRIDS[args.grid]
    keys = list(grid)
    combos = list(itertools.product(*(grid[k] for k in keys)))
    print(f"\n{len(combos)} kombinasi diuji pada {len(symbols)} simbol ({args.days} hari)\n")
    print(f"{'sl_atr':>7}{'tp_rr':>7}{'be_r':>6}{'trail_r':>8}{'trail_atr':>10}{'n':>7}{'win%':>7}{'net%':>9}{'expR':>8}{'PF':>7}")
    rows = []
    for combo in combos:
        overrides = dict(zip(keys, combo))
        metrics = evaluate(data, make_cfg(**overrides))
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
        print("\nwalk-forward (65% pertama vs 35% terakhir):")
        print(f"{'overrides':<60}{'IS expR':>9}{'OOS expR':>10}")
        for exp_r, overrides, _ in rows[:3]:
            ins, oos = walk_forward(data, make_cfg(**overrides))
            label = ", ".join(f"{k.split('__')[1]}={v}" for k, v in overrides.items())
            print(f"{label:<60}{ins:>+9.3f}{oos:>+10.3f}")
    print("\nCatatan: expR = rata-rata R per trade. Positif saja belum cukup; lihat juga PF dan walk-forward.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
