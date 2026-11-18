"""Render satu frame dashboard dari data contoh, untuk dokumentasi dan uji layout."""

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalpbot.config import DashboardConfig
from scalpbot.dashboard import Dashboard
from scalpbot.events import Event


def sample_state() -> dict:
    return {
        "mode": "testnet",
        "ip": "103.28.14.77",
        "ip_age": 42.0,
        "ws_ok": True,
        "ws_age": 0.1,
        "weight": 37,
        "latency_ms": 61.0,
        "uptime_s": 5400.0,
        "symbols": 5,
        "discovery": "top_volume/5",
        "equity": 48.62,
        "available": 33.40,
        "day_pnl": 1.84,
        "day_pnl_pct": 3.93,
        "open_count": 2,
        "max_open": 3,
        "risk_pct": 1.5,
        "leverage": 10,
        "halted_reason": "",
        "signals_today": 6,
        "trades_today": 3,
        "errors": 0,
        "positions": [
            {"symbol": "SOLUSDT", "side": "LONG", "qty": 0.71, "entry": 70.42, "mark": 70.88,
             "r": 1.12, "sl": 70.55, "tp": 71.30, "pnl": 0.33, "state": "TRAIL"},
            {"symbol": "DOGEUSDT", "side": "SHORT", "qty": 860, "entry": 0.05842, "mark": 0.05828,
             "r": 0.62, "sl": 0.05846, "tp": 0.05820, "pnl": 0.12, "state": "BE"},
        ],
        "rows": [
            {"symbol": "BTCUSDT", "price": 64057.5, "trend": 0, "pac_center": 64050.1,
             "zone": "inside", "armed_long": False, "armed_short": False, "imbalance": -0.21,
             "spread": 0.2, "atr_pct": 0.29, "last_signal": "-", "age": 0.0},
            {"symbol": "ETHUSDT", "price": 1792.17, "trend": -1, "pac_center": 1802.37,
             "zone": "below", "armed_long": False, "armed_short": False, "imbalance": -0.08,
             "spread": 0.4, "atr_pct": 0.21, "last_signal": "SHORT", "age": 780.0},
            {"symbol": "SOLUSDT", "price": 70.88, "trend": 1, "pac_center": 70.55,
             "zone": "above", "armed_long": True, "armed_short": False, "imbalance": 0.34,
             "spread": 0.1, "atr_pct": 0.24, "last_signal": "LONG", "age": 240.0},
            {"symbol": "XRPUSDT", "price": 0.9478, "trend": -1, "pac_center": 0.9516,
             "zone": "below", "armed_long": False, "armed_short": False, "imbalance": 0.05,
             "spread": 0.3, "atr_pct": 0.22, "last_signal": "SHORT", "age": 1500.0},
            {"symbol": "DOGEUSDT", "price": 0.05828, "trend": 1, "pac_center": 0.05830,
             "zone": "inside", "armed_long": False, "armed_short": False, "imbalance": -0.12,
             "spread": 0.6, "atr_pct": 0.26, "last_signal": "-", "age": 0.0},
            {"symbol": "BNBUSDT", "price": 587.39, "trend": 1, "pac_center": 582.10,
             "zone": "above", "armed_long": False, "armed_short": False, "imbalance": 0.18,
             "spread": 0.3, "atr_pct": 0.31, "last_signal": "-", "age": 0.0},
            {"symbol": "BCHUSDT", "price": 204.93, "trend": 0, "pac_center": 206.71,
             "zone": "below", "armed_long": False, "armed_short": False, "imbalance": -0.04,
             "spread": 0.5, "atr_pct": 0.38, "last_signal": "-", "age": 0.0},
            {"symbol": "FXSUSDT", "price": 0.3035, "trend": 1, "pac_center": 0.3010,
             "zone": "above", "armed_long": False, "armed_short": False, "imbalance": 0.09,
             "spread": 0.8, "atr_pct": 0.65, "last_signal": "-", "age": 0.0},
        ],
        "entries": [
            {"time": time.time() - 1500, "symbol": "XRPUSDT", "side": "SHORT", "price": 0.9478,
             "status": "SL", "detail": "-0.71$ -1.00R"},
            {"time": time.time() - 900, "symbol": "ETHUSDT", "side": "SHORT", "price": 1802.37,
             "status": "TOLAK", "detail": "book: imbalance"},
            {"time": time.time() - 420, "symbol": "BCHUSDT", "side": "LONG", "price": 0.0,
             "status": "TOLAK", "detail": "rezim x4"},
            {"time": time.time() - 210, "symbol": "SOLUSDT", "side": "LONG", "price": 70.42,
             "status": "OPEN", "detail": "R=52bps"},
            {"time": time.time() - 45, "symbol": "DOGEUSDT", "side": "SHORT", "price": 0.05842,
             "status": "OPEN", "detail": "R=38bps"},
        ],
        "events": [
            Event(time.time() - 210, "INFO", "trade",
                  "OPEN LONG SOLUSDT qty=0.71 @70.42 SL=70.05 TP=71.30 risk=0.74$ R=52bps imb=+0.34"),
            Event(time.time() - 180, "INFO", "manage", "SOLUSDT BE dipasang @70.44 (R=+0.63)"),
            Event(time.time() - 120, "INFO", "manage", "SOLUSDT trailing SL -> 70.55 (peak R=+1.05)"),
            Event(time.time() - 90, "INFO", "trade", "CLOSE SHORT BTCUSDT TP @63980 PnL +0.91$ (+1.50R)"),
            Event(time.time() - 60, "INFO", "signal",
                  "SHORT DOGEUSDT @0.05842 PAC=0.05855 ATR=0.000146 trend=-1"),
            Event(time.time() - 45, "INFO", "trade",
                  "OPEN SHORT DOGEUSDT qty=860 @0.05842 SL=0.05864 TP=0.05820 risk=0.73$ R=38bps imb=-0.12"),
            Event(time.time() - 20, "INFO", "manage", "DOGEUSDT BE dipasang @0.05838 (R=+0.55)"),
        ],
        "footer": "Ctrl+C = keluar | order proteksi tetap aktif di exchange",
        "refresh_s": 1800,
        "min_vol_m": 50.0,
    }


SIZES = (("wide", 150, 44), ("standard", 120, 40))


def main() -> None:
    dash = Dashboard(DashboardConfig(enabled=True, log_lines=8), live=False, tty=False)
    out_dir = Path(__file__).resolve().parents[1] / "docs"
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered = {}
    for name, width, height in SIZES:
        frame = dash.build_frame(sample_state(), width, height)
        rendered[name] = frame
        (out_dir / f"dashboard-sample-{name}.txt").write_text(frame, encoding="utf-8")
        (out_dir / f"dashboard-sample-{name}-plain.txt").write_text(
            re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", frame), encoding="utf-8"
        )
    plain = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", rendered["wide"])
    (out_dir / "dashboard-sample.txt").write_text(rendered["wide"], encoding="utf-8")
    (out_dir / "dashboard-sample-plain.txt").write_text(plain, encoding="utf-8")
    print(plain)


if __name__ == "__main__":
    main()
