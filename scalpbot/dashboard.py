from __future__ import annotations

import ctypes
import math
import os
import shutil
import signal
import sys
import time
from dataclasses import replace
from typing import Any, Iterable, Optional

from .config import DashboardConfig
from .events import Event
from .security import clean_message
from .timeutil import format_ts

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
GREEN = "\x1b[32m"
BRIGHT_GREEN = "\x1b[92m"
RED = "\x1b[31m"
BRIGHT_RED = "\x1b[91m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
BRIGHT_BLUE = "\x1b[94m"
CYAN = "\x1b[36m"
MAGENTA = "\x1b[35m"
GRAY = "\x1b[90m"
WHITE = "\x1b[97m"
BG_RED = "\x1b[41m\x1b[97m"

LEVEL_COLOR = {
    "DEBUG": GRAY,
    "INFO": CYAN,
    "WARNING": YELLOW,
    "ERROR": BRIGHT_RED,
    "CRITICAL": BG_RED,
}


def _clean_state(value: Any) -> Any:
    if isinstance(value, Event):
        return replace(
            value,
            source=clean_message(value.source),
            message=clean_message(value.message),
        )
    if isinstance(value, str):
        return clean_message(value)
    if isinstance(value, dict):
        return {
            clean_message(k) if isinstance(k, str) else k: _clean_state(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_clean_state(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_clean_state(v) for v in value)
    return value


def _visible(text: str) -> int:
    length = 0
    in_escape = False
    for ch in text:
        if in_escape:
            if ch == "m":
                in_escape = False
            continue
        if ch == "\x1b":
            in_escape = True
            continue
        length += 1
    return length


def fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    vis = _visible(text)
    if vis > width:
        plain = []
        in_escape = False
        count = 0
        for ch in text:
            if in_escape:
                plain.append(ch)
                if ch == "m":
                    in_escape = False
                continue
            if ch == "\x1b":
                in_escape = True
                plain.append(ch)
                continue
            if count >= width - 1:
                break
            plain.append(ch)
            count += 1
        return "".join(plain) + "…" + RESET
    return text + " " * (width - vis)


def pad(text: str, width: int, align: str = "<") -> str:
    vis = _visible(text)
    if vis >= width:
        return text
    gap = width - vis
    if align == ">":
        return " " * gap + text
    if align == "^":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def cell(text: str, width: int, align: str = "<", color: str = "") -> str:
    body = pad(text, width, align)
    return f"{color}{body}{RESET}" if color else body


def _enable_windows_vt() -> bool:
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def fmt_dur(seconds: float) -> str:
    if not math.isfinite(seconds):
        return "-"
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def fmt_num(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "-"
    if abs(value) >= 1000:
        return f"{value:,.{digits}f}"
    if abs(value) >= 1:
        return f"{value:.{digits}f}"
    return f"{value:.{max(digits, 4)}f}"


def fmt_px(value: Optional[float]) -> str:
    if value is None or value == 0:
        return "-"
    if abs(value) >= 100:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


def color_for_pnl(value: float) -> str:
    if value > 0:
        return BRIGHT_GREEN
    if value < 0:
        return BRIGHT_RED
    return GRAY


def trend_label(trend: int) -> tuple[str, str]:
    if trend > 0:
        return "UP  ", GREEN
    if trend < 0:
        return "DOWN", RED
    return "FLAT", YELLOW


class Dashboard:
    def __init__(
        self,
        cfg: DashboardConfig,
        live: bool = False,
        tty: Optional[bool] = None,
        timezone_name: str = "WIB",
    ) -> None:
        self.cfg = cfg
        self.timezone_name = timezone_name
        self.live = live
        self.enabled = cfg.enabled and (tty if tty is not None else sys.stdout.isatty())
        if self.enabled and not _enable_windows_vt():
            self.enabled = False
        self._winch_sig = getattr(signal, "SIGWINCH", None)
        self._started = False
        self._resized = True
        self._last_render = 0.0
        self._last_width = 0
        self._last_height = 0
        self._prev_handler: Any = None

    def start(self) -> None:
        if not self.enabled or self._started:
            return
        out = sys.stdout
        out.write("\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H")
        out.flush()
        self._started = True
        try:
            if self._winch_sig is not None:
                self._prev_handler = signal.getsignal(self._winch_sig)
                signal.signal(self._winch_sig, self._on_resize)
        except (ValueError, OSError, AttributeError):
            self._prev_handler = None

    def _on_resize(self, signum: int, frame: Any) -> None:
        self._resized = True

    def stop(self) -> None:
        if not self._started:
            return
        try:
            if self._winch_sig is not None:
                signal.signal(self._winch_sig, self._prev_handler or signal.SIG_DFL)
        except (ValueError, OSError, TypeError, AttributeError):
            pass
        out = sys.stdout
        out.write(f"{RESET}\x1b[?25h\x1b[?1049l\r\x1b[K")
        out.flush()
        self._started = False

    def terminal_size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size(fallback=(100, 32))
        return max(40, size.columns), max(12, size.lines)

    def render(self, state: dict[str, Any], force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.time()
        if not force and (now - self._last_render) * 1000.0 < self.cfg.refresh_ms:
            return
        width, height = self.terminal_size()
        if (width, height) != (self._last_width, self._last_height):
            self._resized = True
            self._last_width, self._last_height = width, height
        self._last_render = now
        frame = self.build_frame(_clean_state(state), width, height)
        prefix = "\x1b[2J\x1b[H" if self._resized else "\x1b[H"
        sys.stdout.write(prefix + frame + RESET + "\x1b[J")
        sys.stdout.flush()
        self._resized = False

    SPLIT_MIN_WIDTH = 108
    GROUP_ORDER = ("SIAP", "DEKAT", "TREN", "FLAT")
    GROUP_LABEL = {
        "SIAP": "SIAP EKSEKUSI",
        "DEKAT": "DEKAT PAC",
        "TREN": "TREN JELAS",
        "FLAT": "FLAT",
    }
    GROUP_COLOR = {
        "SIAP": BRIGHT_GREEN,
        "DEKAT": CYAN,
        "TREN": BLUE,
        "FLAT": GRAY,
    }
    SYM_W = {"sym": 5, "price": 10, "trend": 4, "meter": 10, "dpac": 6, "zone": 3, "sig": 6}

    def _frame_top(self, title: str, width: int) -> str:
        inner = max(0, width - 2)
        label = f" {title} "
        fill = max(0, inner - _visible(label))
        return f"{GRAY}\u250c{RESET}{BOLD}{WHITE}{label}{RESET}{GRAY}{'\u2500' * fill}\u2510{RESET}"

    def _frame_mid(self, width: int) -> str:
        return f"{GRAY}\u251c{'\u2500' * max(0, width - 2)}\u2524{RESET}"

    def _frame_bot(self, width: int) -> str:
        return f"{GRAY}\u2514{'\u2500' * max(0, width - 2)}\u2518{RESET}"

    def _frame_row(self, content: str, width: int) -> str:
        inner = max(0, width - 2)
        vis = _visible(content)
        if vis > inner:
            content = fit(content, inner)
            vis = _visible(content)
        return f"{GRAY}\u2502{RESET}{content}{' ' * max(0, inner - vis)}{GRAY}\u2502{RESET}"

    def _meter(self, dd: float, thr: float, width: int = 11) -> str:
        center = width // 2
        span = max(1, width - center - 1)
        n = min(int(round(min(abs(dd) / 3.0, 1.0) * center)), span)
        if abs(dd) <= thr:
            color = YELLOW
        elif dd > 0:
            color = BRIGHT_BLUE
        else:
            color = BRIGHT_RED
        if dd < 0:
            left = f"{color}{'\u2588' * n}{RESET}{GRAY}{'\u2500' * (center - n)}{RESET}"
            right = f"{GRAY}{'\u2500' * (width - center - 1)}{RESET}"
        elif dd > 0:
            left = f"{GRAY}{'\u2500' * center}{RESET}"
            right = f"{color}{'\u2588' * n}{RESET}{GRAY}{'\u2500' * (width - center - 1 - n)}{RESET}"
        else:
            left = f"{GRAY}{'\u2500' * center}{RESET}"
            right = f"{GRAY}{'\u2500' * (width - center - 1)}{RESET}"
        return f"{left}{GRAY}\u253c{RESET}{right}"

    def _age_short(self, seconds: float) -> str:
        if seconds <= 0:
            return ""
        if seconds < 60:
            return f"{int(seconds)}s"
        if seconds < 3600:
            return f"{int(seconds // 60)}m"
        return f"{int(seconds // 3600)}h"

    def _dpac(self, row: dict) -> float:
        price = safe_number(row.get("price"))
        pac = safe_number(row.get("pac_center"))
        if price <= 0 or pac <= 0:
            return 0.0
        return (price - pac) / pac * 100.0

    def _near_thr(self, row: dict) -> float:
        atr_pct = safe_number(row.get("atr_pct"))
        if atr_pct <= 0:
            return 0.05
        return min(0.60, max(0.05, atr_pct * 0.5))

    def _classify(self, row: dict) -> str:
        if row.get("armed_long") or row.get("armed_short"):
            return "SIAP"
        if int(safe_number(row.get("trend"))) == 0:
            return "FLAT"
        dd = self._dpac(row)
        return "DEKAT" if abs(dd) <= self._near_thr(row) else "TREN"

    def _group_symbols(self, rows: Iterable[dict]) -> list[tuple[str, list[dict]]]:
        buckets: dict[str, list[dict]] = {key: [] for key in self.GROUP_ORDER}
        for row in rows:
            buckets[self._classify(row)].append(row)
        for key in ("SIAP", "DEKAT", "TREN"):
            buckets[key].sort(key=lambda r: abs(self._dpac(r)))
        buckets["FLAT"].sort(key=lambda r: str(r.get("symbol", "")))
        return [(key, buckets[key]) for key in self.GROUP_ORDER]

    def _symbol_header(self, width: int) -> str:
        w = self.SYM_W
        body = " ".join(
            [
                pad("SIMB", w["sym"]),
                pad("PRICE", w["price"], ">"),
                pad("TREN", w["trend"]),
                pad("PAC", w["meter"], "^"),
                pad("D%", w["dpac"], ">"),
                pad("ZN", w["zone"]),
                pad("SIG", w["sig"]),
            ]
        )
        return self._frame_row(f"{DIM}{body}{RESET}", width)

    def _symbol_row(self, row: dict, width: int) -> str:
        w = self.SYM_W
        thr = self._near_thr(row)
        dd = self._dpac(row)
        symbol = str(row.get("symbol", ""))
        if symbol.endswith("USDT"):
            symbol = symbol[:-4]
        price = safe_number(row.get("price"))
        trend = int(safe_number(row.get("trend")))
        label, tcolor = trend_label(trend)
        zone = str(row.get("zone", "-"))
        zone_txt = {"above": "abv", "below": "blw", "inside": "in"}.get(zone, "-")
        if zone == "above":
            zone_color = BRIGHT_BLUE
        elif zone == "below":
            zone_color = BRIGHT_RED
        elif zone == "inside":
            zone_color = YELLOW
        else:
            zone_color = DIM
        sig, sig_color = "-", DIM
        if row.get("armed_long"):
            sig, sig_color = "LONG", BOLD + BRIGHT_GREEN
        elif row.get("armed_short"):
            sig, sig_color = "SHRT", BOLD + BRIGHT_RED
        elif row.get("last_signal") in ("LONG", "SHORT"):
            age = self._age_short(safe_number(row.get("age")))
            sig = f"{'L' if row['last_signal'] == 'LONG' else 'S'}{age}"
            sig_color = GRAY
        dd_txt = f"{dd:+.2f}" if abs(dd) < 100 else f"{dd:+.0f}"
        body = " ".join(
            [
                cell(symbol[: w["sym"]], w["sym"]),
                cell(fmt_px(price)[: w["price"]], w["price"], ">"),
                cell(label.strip()[:4], w["trend"], "<", tcolor),
                self._meter(dd, thr, w["meter"]),
                cell(dd_txt[: w["dpac"]], w["dpac"], ">", GRAY),
                cell(zone_txt[: w["zone"]], w["zone"], "<", zone_color),
                cell(sig[: w["sig"]], w["sig"], "<", sig_color),
            ]
        )
        return self._frame_row(body, width)

    def _group_line(self, key: str, count: int, width: int) -> str:
        label = f" > {self.GROUP_LABEL[key]}"
        pad_to = 17
        gap = max(1, pad_to - _visible(label))
        body = (
            f"{self.GROUP_COLOR[key]}{BOLD}{label}{RESET}"
            f"{' ' * gap}{BOLD}{count}{RESET} {DIM}simbol{RESET}"
        )
        return self._frame_row(body, width)

    def _symbol_panel(self, rows: list[dict], width: int, budget: int) -> list[str]:
        inner = max(0, width - 2)
        if budget < 4 or inner < 46:
            return []
        groups = dict(self._group_symbols(rows))
        head = [self._frame_top("SIMBOL / SINYAL", width), self._symbol_header(width), self._frame_mid(width)]
        avail = budget - len(head) - 1
        if avail <= 0:
            return (head + [self._frame_bot(width)])[: max(0, budget)]

        out: list[str] = []
        room = avail
        hidden = 0
        for key in self.GROUP_ORDER:
            items = groups[key]
            if not items:
                continue
            if room <= 0:
                hidden += len(items)
                continue
            out.append(self._group_line(key, len(items), width))
            room -= 1
            take = min(len(items), max(0, room))
            for row in items[:take]:
                out.append(self._symbol_row(row, width))
            room -= take
            hidden += len(items) - take
        if hidden:
            if room > 0:
                out.append(self._frame_row(f"{DIM}   +{hidden} simbol lain{RESET}", width))
                room -= 1
            else:
                out = out[: avail - 1] + [
                    self._frame_row(f"{DIM}   +{hidden + 1} simbol lain{RESET}", width)
                ]
        while len(out) < avail:
            out.append(self._frame_row("", width))
        return head + out[:avail] + [self._frame_bot(width)]

    def _positions_box(self, positions: list[dict], max_open: int, width: int, budget: int, hint: str) -> list[str]:
        inner = max(0, width - 2)
        if budget < 3:
            return []
        top = self._frame_top(f"POSISI TERBUKA {len(positions)}/{max_open}", width)
        if not positions:
            body = [self._frame_row(f" {DIM}(tidak ada posisi){RESET}", width)]
            if budget >= 4 and hint:
                body.append(self._frame_row(f" {DIM}{hint}{RESET}", width))
            return [top, *body[: max(0, budget - 2)], self._frame_bot(width)][:budget]
        wide = inner >= 56
        mid = inner >= 44
        cols: list[tuple[str, int, str]] = [("SYM", 8, "<"), ("S", 1, "<"), ("R", 6, ">"), ("PNL", 8, ">")]
        if mid:
            cols += [("SL", 10, ">"), ("TP", 10, ">")]
        if wide:
            cols.append(("STATE", 6, "<"))
        header = " " + " ".join(pad(name, size, align) for name, size, align in cols)
        lines = [top, self._frame_row(f"{DIM}{header}{RESET}", width)]
        room = max(0, budget - 3)
        for pos in positions[:room]:
            side = str(pos.get("side", ""))
            side_color = BRIGHT_GREEN if side == "LONG" else BRIGHT_RED
            parts = [
                cell(str(pos.get("symbol", ""))[:8], 8),
                cell(side[:1], 1, "<", side_color),
                cell(f"{safe_number(pos.get('r')):+.2f}", 6, ">", color_for_pnl(safe_number(pos.get("r")))),
                cell(fmt_num(safe_number(pos.get("pnl"))), 8, ">", color_for_pnl(safe_number(pos.get("pnl")))),
            ]
            if mid:
                parts += [
                    cell(fmt_px(pos.get("sl"))[:10], 10, ">"),
                    cell(fmt_px(pos.get("tp"))[:10], 10, ">"),
                ]
            if wide:
                parts.append(cell(str(pos.get("state", ""))[:6], 6, "<", DIM))
            lines.append(self._frame_row(" " + " ".join(parts), width))
        if len(positions) > room:
            lines.append(self._frame_row(f"{DIM} +{len(positions) - room} posisi lain{RESET}", width))
        return lines[: budget - 1] + [self._frame_bot(width)]

    def _entry_box(self, entries: list[dict], width: int, budget: int) -> list[str]:
        if budget < 3:
            return []
        top = self._frame_top("RIWAYAT ENTRY", width)
        inner = max(0, width - 2)
        if not entries:
            return [
                top,
                self._frame_row(f" {DIM}(belum ada entry / penolakan){RESET}", width),
                self._frame_bot(width),
            ][:budget]
        lines = [top]
        room = max(0, budget - 2)
        for row in entries[-room:] if room else []:
            status = str(row.get("status", ""))
            if status == "OPEN":
                color = CYAN
            elif status == "TOLAK":
                color = GRAY
            elif status in {"TP", "TRAIL"}:
                color = GREEN
            elif status == "SL":
                color = RED
            elif status == "BE":
                color = YELLOW
            else:
                color = YELLOW
            stamp = format_ts(safe_number(row.get("time")), self.timezone_name, "%H:%M")
            symbol = str(row.get("symbol", "")).replace("USDT", "")
            side = str(row.get("side", ""))
            price = safe_number(row.get("price"))
            detail = str(row.get("detail", ""))
            body = (
                f" {DIM}{stamp}{RESET} {WHITE}{symbol:<5}{RESET}"
                f" {color}{side[:1]}{RESET} {color}{status[:5]:<5}{RESET}"
                f" {DIM}{fmt_px(price) if price else '-':<10}{RESET}"
                f" {detail}"
            )
            lines.append(self._frame_row(fit(body, inner) if _visible(body) > inner else body, width))
        return lines[: budget - 1] + [self._frame_bot(width)]

    def _events_box(self, events: list[Event], width: int, budget: int, fill: bool = False) -> list[str]:
        inner = max(0, width - 2)
        if budget < 3:
            return []
        top = self._frame_top("EVENT LOG", width)
        room = max(0, budget - 2)
        if not events:
            return [top, self._frame_row(f" {DIM}(belum ada event){RESET}", width)][:budget] + (
                [self._frame_bot(width)] if budget >= 3 else []
            )
        lines = [top]
        for ev in events[-room:] if room else []:
            color = LEVEL_COLOR.get(ev.level, GRAY)
            stamp = format_ts(ev.ts, self.timezone_name, "%H:%M:%S")
            body = (
                f" {DIM}{stamp}{RESET} {color}{ev.level[:4]:<4}{RESET}"
                f" {DIM}{ev.source[:10]:<10}{RESET} {ev.message}"
            )
            lines.append(self._frame_row(fit(body, inner) if _visible(body) > inner else body, width))
        while fill and len(lines) < budget - 1:
            lines.append(self._frame_row("", width))
        return lines[: budget - 1] + [self._frame_bot(width)]

    def _join(self, left: list[str], right: list[str], left_w: int, width: int) -> list[str]:
        height = max(len(left), len(right))
        out: list[str] = []
        for i in range(height):
            lft = left[i] if i < len(left) else ""
            rgt = right[i] if i < len(right) else ""
            out.append(fit(lft, left_w) + ("  " + rgt if rgt else ""))
        return out

    def _positions_hint(self, rows: list[dict]) -> str:
        near = sorted(
            [r for r in rows if safe_number(r.get("pac_center")) > 0],
            key=lambda r: abs(self._dpac(r)),
        )[:3]
        if not near:
            return ""
        names = []
        for row in near:
            symbol = str(row.get("symbol", "")).replace("USDT", "")
            names.append(f"{symbol} {self._dpac(row):+.2f}%")
        return "terdekat PAC: " + " | ".join(names)

    def build_frame(self, state: dict[str, Any], width: int, height: int) -> str:
        if width < self.cfg.min_width or height < self.cfg.min_height:
            message = (
                f" Terminal {width}x{height} terlalu kecil. "
                f"Minimal {self.cfg.min_width}x{self.cfg.min_height}. Perbesar jendela."
            )
            return "\n".join(
                [
                    fit(BOLD + RED + message + RESET, width),
                    fit(DIM + " Bot tetap berjalan; tampilan kembali saat ukuran cukup." + RESET, width),
                ]
            )
        lines: list[str] = []
        mode = str(state.get("mode", "?")).upper()
        title = f" SCALPTOOL  PAC PULLBACK  |  {mode} "
        badge = (
            f"{BG_RED}{BOLD} LIVE {RESET}"
            if state.get("mode") == "live"
            else f"{GREEN}{BOLD} SAFE {RESET}"
        )
        lines.append(fit(f"{BOLD}{WHITE}{title}{RESET} {badge}", width))

        ws_ok = state.get("ws_ok", False)
        ip = state.get("ip") or "unknown"
        lines.append(
            fit(
                f" {GREEN if ws_ok else RED}WS:{'OK' if ws_ok else 'DOWN'}{RESET}"
                f" {DIM}{safe_number(state.get('ws_age')):4.1f}s{RESET}"
                f" {GRAY}|{RESET} {DIM}w:{state.get('weight', 0)}{RESET}"
                f" {GRAY}|{RESET} {DIM}lat:{safe_number(state.get('latency_ms')):.0f}ms{RESET}"
                f" {GRAY}|{RESET} IP {CYAN}{ip}{RESET} {DIM}({fmt_dur(safe_number(state.get('ip_age')))}){RESET}"
                f" {GRAY}|{RESET} {DIM}up {fmt_dur(safe_number(state.get('uptime_s')))}{RESET}",
                width,
            )
        )

        day_pnl = safe_number(state.get("day_pnl"))
        day_pct = safe_number(state.get("day_pnl_pct"))
        discovery_raw = str(state.get("discovery", "top_volume"))
        tag = (
            f"{MAGENTA}auto{RESET}"
            if discovery_raw.startswith("top_volume")
            else f"{MAGENTA}static{RESET}"
        )
        halted = str(state.get("halted_reason", "") or "")
        adopted = int(safe_number(state.get("adopted")))
        adopt_tag = f" {MAGENTA}{adopted} adopsi{RESET}" if adopted > 0 else ""
        acct = (
            f" EQUITY {BOLD}{fmt_num(safe_number(state.get('equity')))}{RESET}"
            f"   AVAIL {fmt_num(safe_number(state.get('available')))}"
            f"   DAY {color_for_pnl(day_pnl)}{day_pnl:+.2f} ({day_pct:+.2f}%){RESET}"
            f"   POS {state.get('open_count', 0)}/{state.get('max_open', 0)}{adopt_tag}"
            f"   RISK {safe_number(state.get('risk_pct')):.2f}%"
            f"   LEV {state.get('leverage', 0)}x"
        )
        if halted:
            acct += f"  {BG_RED} HALT {halted} {RESET}"
        lines.append(fit(acct, width))

        lines.append(
            fit(
                f" {DIM}UNIVERSE{RESET} {tag}"
                f" {DIM}{state.get('symbols', 0)} simbol{RESET}"
                f" {GRAY}|{RESET} {DIM}tiap {state.get('refresh_s', 0) or '-'}s{RESET}"
                f" {GRAY}|{RESET} {DIM}min {safe_number(state.get('min_vol_m')):.0f}M{RESET}",
                width,
            )
        )
        lines.append(GRAY + "\u2500" * width + RESET)

        rows = list(state.get("rows", []))
        positions = list(state.get("positions", []))
        events = list(state.get("events", []))
        show_hist = self.cfg.history_lines > 0
        entries = list(state.get("entries", [])) if show_hist else []
        max_open = int(safe_number(state.get("max_open")))
        hint = self._positions_hint(rows)
        budget = height - len(lines) - 1

        if width >= self.SPLIT_MIN_WIDTH and budget >= 8:
            left_w = min(62, max(52, int(width * 0.42)))
            right_w = width - left_w - 2
            if len(positions) <= 1:
                pos_budget = 4
            else:
                pos_budget = min(budget - 8, 2 + len(positions) + 1)
            pos_budget = max(3, pos_budget)
            left = self._symbol_panel(rows, left_w, budget)
            pos = self._positions_box(positions, max_open, right_w, pos_budget, hint)
            hist_room = max(0, budget - len(pos) - 8) if show_hist else 0
            hist = (
                self._entry_box(entries, right_w, min(3 + min(len(entries), 4), hist_room))
                if hist_room >= 3
                else []
            )
            ev_budget = budget - len(pos) - len(hist)
            if ev_budget < 4:
                hist = hist[: max(0, len(hist) - (4 - ev_budget))]
                ev_budget = budget - len(pos) - len(hist)
            if ev_budget < 4:
                pos = pos[: max(0, budget - len(hist) - 4)]
                ev_budget = budget - len(pos) - len(hist)
            right = pos + hist + self._events_box(events, right_w, max(3, ev_budget), fill=True)
            lines.extend(self._join(left, right, left_w, width))
        else:
            pos = self._positions_box(positions, max_open, width, 4, hint)
            # sisakan >=4 baris simbol dan >=4 baris event log sebelum memberi ruang ke riwayat
            hist_room = max(0, budget - len(pos) - 8) if show_hist else 0
            hist = (
                self._entry_box(entries, width, min(3 + min(len(entries), 3), hist_room))
                if hist_room >= 3
                else []
            )
            sym_budget = max(4, budget - len(pos) - len(hist) - 4)
            sym = self._symbol_panel(rows, width, sym_budget)
            used = len(sym) + len(pos) + len(hist)
            lines.extend(sym)
            lines.extend(pos)
            lines.extend(hist)
            ev_budget = budget - used
            if ev_budget >= 4:
                lines.extend(self._events_box(events, width, ev_budget))

        lines.append(
            fit(
                f" {DIM}Ctrl+C keluar{RESET}"
                f" {GRAY}|{RESET} {DIM}sinyal{RESET} {state.get('signals_today', 0)}"
                f" {DIM}trade{RESET} {state.get('trades_today', 0)}"
                f" {DIM}error{RESET} {color_for_pnl(-safe_number(state.get('errors')))}"
                f"{state.get('errors', 0)}{RESET}",
                width,
            )
        )
        return "\n".join(fit(line, width) for line in lines[:height])


def safe_number(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def fmt_duration_long(seconds: float) -> str:
    if not math.isfinite(seconds):
        return "-"
    total = max(0, round(seconds))
    if total < 60:
        return f"{total}s"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        human = f"{days}d {hours:02d}h {minutes:02d}m {secs:02d}s"
    elif hours:
        human = f"{hours}h {minutes:02d}m {secs:02d}s"
    else:
        human = f"{minutes}m {secs:02d}s"
    return f"{human} ({total:,}s)"


def _wrap_plain(text: str, width: int) -> list[str]:
    width = max(8, width)
    raw = str(text)
    if len(raw) <= width:
        return [raw]
    chunks: list[str] = []
    rest = raw
    while len(rest) > width:
        window = rest[: width + 1]
        cut = window.rfind(" ")
        if cut < width // 3:
            slash = max(window[:width].rfind("/"), window[:width].rfind("\\"))
            cut = slash + 1 if slash >= width // 3 else width
            chunks.append(rest[:cut].rstrip())
            rest = rest[cut:].lstrip()
        else:
            chunks.append(rest[:cut].rstrip())
            rest = rest[cut + 1 :].lstrip()
    if rest or not chunks:
        chunks.append(rest)
    return chunks


def format_session_summary(
    summary: dict[str, Any],
    width: int | None = None,
    color: bool | None = None,
) -> str:
    data = _clean_state(dict(summary))
    if color is None:
        use_color = (
            sys.stdout.isatty()
            and not bool(os.environ.get("NO_COLOR"))
            and _enable_windows_vt()
        )
    else:
        use_color = bool(color)

    def style(code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if use_color and code and text else text

    mode = str(data.get("mode") or "?").upper()
    tz = str(data.get("timezone_label") or "WIB")
    fatal_error = str(data.get("fatal_error") or "").strip()
    stop_reason = str(data.get("stop_reason") or "").strip()
    halted_reason = str(data.get("halted_reason") or "").strip()

    sections: list[list[tuple[str, str, str, str, str]]] = []
    sec_info: list[tuple[str, str, str, str, str]] = []
    if fatal_error:
        sec_info.append(("Status", "Berhenti karena error", BOLD + BRIGHT_RED, "", ""))
        sec_info.append(("Penyebab", fatal_error, BRIGHT_RED, "", ""))
    elif stop_reason:
        sec_info.append(("Status", f"Dihentikan dengan rapi ({stop_reason})", GREEN, "", ""))
    else:
        sec_info.append(("Status", "Selesai", GREEN, "", ""))
    if halted_reason:
        sec_info.append(("Guard aktif", halted_reason, YELLOW, "", ""))
    sec_info.append(("Durasi", fmt_duration_long(safe_number(data.get("duration_s"))), WHITE, "", ""))
    tz_note = (
        " (mode paper, tanpa order nyata)"
        if mode == "PAPER"
        else " (acuan hari bot; aplikasi Binance tetap UTC)"
    )
    sec_info.append(("Zona waktu", tz, CYAN, tz_note, DIM))
    sections.append(sec_info)

    if data.get("has_trader"):
        sec_perf: list[tuple[str, str, str, str, str]] = []
        equity = safe_number(data.get("equity"))
        realized = safe_number(data.get("realized"))
        start_eq = safe_number(data.get("start_equity")) or equity
        pct_txt = f" ({realized / start_eq * 100.0:+.2f}%)" if start_eq > 0 else ""
        pnl_note = " [sesi paper]" if mode == "PAPER" else f" [hari bot {tz}]"
        sec_perf.append(("Equity akhir", f"{fmt_num(equity)} USDT", BOLD + WHITE, "", ""))
        sec_perf.append(
            ("Realized hari", f"{realized:+,.2f} USDT{pct_txt}", color_for_pnl(realized), pnl_note, DIM)
        )
        trades = int(safe_number(data.get("trades")))
        wins = int(safe_number(data.get("wins")))
        losses = int(safe_number(data.get("losses")))
        if trades > 0:
            wr = wins / trades * 100.0
            sec_perf.append(("Trade selesai", str(trades), WHITE, f" (W{wins} / L{losses} | WR {wr:.1f}%)", DIM))
        else:
            sec_perf.append(("Trade selesai", "0", GRAY, f" (W{wins} / L{losses})", DIM))
        signals = int(safe_number(data.get("signals")))
        sec_perf.append(("Sinyal lolos", str(signals), WHITE if signals > 0 else GRAY, "", ""))

        raw_rejects = data.get("reject_stats") if isinstance(data.get("reject_stats"), dict) else {}
        book_items = {
            str(k): int(safe_number(v))
            for k, v in raw_rejects.items()
            if k != "regime" and int(safe_number(v)) > 0
        }
        if book_items:
            ringkas = ", ".join(f"{k} {v}" for k, v in sorted(book_items.items()))
            sec_perf.append(("Tolak entry", ringkas, YELLOW, " (orderbook)", DIM))
        regime_cnt = int(safe_number(raw_rejects.get("regime")))
        if regime_cnt > 0:
            slope = safe_number(data.get("regime_min_slope_bps"))
            sec_perf.append(("Tolak rezim", f"{regime_cnt} sinyal", YELLOW, f" (slope < {slope:g} bps)", DIM))

        open_count = int(safe_number(data.get("open_count")))
        raw_open = data.get("open_symbols") if isinstance(data.get("open_symbols"), list) else []
        open_syms = [str(item) for item in raw_open if str(item)]
        if open_count > 0:
            shown = ", ".join(open_syms[:3])
            extra = f", +{len(open_syms) - 3}" if len(open_syms) > 3 else ""
            detail = f" ({shown}{extra})" if shown else ""
            sec_perf.append(("Posisi terbuka", str(open_count), YELLOW, detail, DIM))
            if mode != "PAPER":
                sec_perf.append(("Proteksi", "Order SL/TP tetap aktif di server Binance", CYAN, "", ""))
        else:
            sec_perf.append(("Posisi terbuka", "0", GRAY, "", ""))

        errors = int(safe_number(data.get("errors")))
        sec_perf.append(("Error", str(errors), BRIGHT_RED if errors > 0 else GRAY, "", ""))
        sections.append(sec_perf)

    sec_files: list[tuple[str, str, str, str, str]] = [
        ("Log teknis", str(data.get("log_file") or "dimatikan"), "", "", ""),
        ("Log event", str(data.get("events_file") or "dimatikan"), "", "", ""),
        ("State", str(data.get("state_file") or "dimatikan"), "", "", ""),
    ]
    sections.append(sec_files)

    term_w = width if width is not None else shutil.get_terminal_size(fallback=(88, 24)).columns
    term_w = max(44, int(term_w))
    label_w = 14
    prefix_len = 1 + label_w + 3
    natural_inner = max(
        prefix_len + len(val) + len(sfx) + 1
        for sec in sections
        for _, val, _, sfx, _ in sec
    )
    head_plain = f" RINGKASAN SESI | {mode} "
    live_title = " RINGKASAN SESI "
    live_badge = " LIVE "
    min_box = min(term_w, 68)
    max_box = min(term_w, 92)
    wanted = max(natural_inner + 2, len(head_plain) + 6)
    box_w = max(min_box, min(wanted, max_box))
    inner_w = box_w - 2
    val_room = max(8, inner_w - prefix_len - 1)

    if mode == "LIVE" and use_color and len(live_title) + len(live_badge) + 1 <= inner_w:
        fill = max(0, inner_w - len(live_title) - len(live_badge) - 1)
        top = (
            style(GRAY, "\u250c")
            + style(BOLD + WHITE, live_title)
            + style(BG_RED + BOLD, live_badge)
            + style(GRAY, " " + "\u2500" * fill + "\u2510")
        )
    else:
        clipped_head = head_plain[:inner_w]
        fill = max(0, inner_w - len(clipped_head))
        top = (
            style(GRAY, "\u250c")
            + style(BOLD + WHITE, clipped_head)
            + style(GRAY, "\u2500" * fill + "\u2510")
        )

    mid = style(GRAY, "\u251c" + "\u2500" * inner_w + "\u2524")
    bot = style(GRAY, "\u2514" + "\u2500" * inner_w + "\u2518")
    lbar = style(GRAY, "\u2502")
    rbar = style(GRAY, "\u2502")
    cont_pfx = " " * prefix_len

    out_lines: list[str] = [top]
    for sec_idx, sec in enumerate(sections):
        if sec_idx > 0:
            out_lines.append(mid)
        for label, val, vcol, sfx, scol in sec:
            pfx_styled = f" {style(DIM, f'{label:<{label_w}}')} {style(GRAY, ':')} "
            if len(val) + len(sfx) <= val_room:
                body = style(vcol, val) + style(scol, sfx)
                pad_r = " " * max(0, inner_w - prefix_len - len(val) - len(sfx))
                out_lines.append(f"{lbar}{pfx_styled}{body}{pad_r}{rbar}")
            else:
                wrapped = _wrap_plain(val + sfx, val_room)
                for idx, part in enumerate(wrapped):
                    lead = pfx_styled if idx == 0 else cont_pfx
                    pad_r = " " * max(0, inner_w - prefix_len - len(part))
                    out_lines.append(f"{lbar}{lead}{style(vcol, part)}{pad_r}{rbar}")
    out_lines.append(bot)
    return "\n".join(out_lines)

