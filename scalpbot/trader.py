from __future__ import annotations

import itertools
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .broker import PaperBroker, TestnetBroker
from .config import Config
from .events import EventLog
from .exchange import BinanceError
from .orderbook import BookSnapshot, OrderbookFilter, classify_reason
from .state import StateStore
from .timeutil import day_key, day_start_ms, tz_label
from .risk import (
    SymbolFilters,
    breakeven_price,
    plan_stops,
    r_now,
    round_step,
    round_tick,
    size_position,
    trailing_stop,
    unrealized_pnl,
    wants_stop_update,
)
from .strategy import Signal

log = logging.getLogger("scalpbot.trader")


@dataclass(slots=True)
class ManagedPosition:
    symbol: str
    side: str
    qty: float
    entry: float
    stop: float
    target: float
    initial_stop: float
    risk_dist: float
    atr: float
    opened_ts: float
    signal_ts: int
    stop_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    be_done: bool = False
    trail_active: bool = False
    state: str = "open"
    last_replace_ts: float = 0.0
    peak_price: float = 0.0
    entry_fee: float = 0.0
    mark: float = 0.0
    r: float = 0.0
    key: str = ""

    def side_sign(self) -> int:
        return 1 if self.side == "LONG" else -1


@dataclass(slots=True)
class TradeRecord:
    symbol: str
    side: str
    qty: float
    entry: float
    exit: float
    pnl: float
    pnl_net: float
    r: float
    opened_ts: float
    closed_ts: float
    reason: str


@dataclass(slots=True)
class EntryRecord:
    ts: float
    symbol: str
    side: str
    price: float
    qty: float
    status: str
    detail: str = ""
    count: int = 1


@dataclass
class DayStats:
    day: str = ""
    start_equity: float = 0.0
    realized: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    signals: int = 0


class Trader:
    def __init__(
        self,
        cfg: Config,
        broker: TestnetBroker | PaperBroker,
        filters: dict[str, SymbolFilters],
        events: EventLog,
        book_filter: OrderbookFilter,
        interval_seconds: int,
    ) -> None:
        self.cfg = cfg
        self.broker = broker
        self.filters = filters
        self.events = events
        self.book = book_filter
        self.interval_seconds = interval_seconds
        self.poll_s = max(1, cfg.execution.position_poll_s)
        self.positions: dict[str, list[ManagedPosition]] = {}
        self.trades: list[TradeRecord] = []
        self.entries: list[EntryRecord] = []
        self.day = DayStats(day=self._today(), start_equity=0.0)
        self.equity = 0.0
        self.available = 0.0
        self.halted_reason = ""
        self._lock = threading.RLock()
        self._pending_entries = 0
        self._pending_by_symbol: dict[str, int] = {}
        self._position_seq = itertools.count(1)
        self._external_exit: dict[str, float] = {}
        self._last_equity_poll = 0.0
        self._last_trade_bar: dict[str, int] = {}
        # Waktu bar (ms) entry terakhir per simbol; dasar cooldown yang tahan restart.
        self._last_entry_bar_ts: dict[str, int] = {}
        # Penyimpanan state (cooldown + riwayat entry) yang bertahan melewati restart.
        self.state = None
        self._external_warned: set[str] = set()
        self._consecutive_errors = 0
        self.error_count = 0
        self.prices: dict[str, float] = {}
        self.equity_history: list[tuple[float, float]] = []
        # PnL yang tidak berasal dari sesi ini (mis. sebelum bot restart).
        self.day_baseline = 0.0
        self._baseline_day = ""
        self._baseline_try_ts = 0.0
        self._baseline_failed = False
        # Statistik penolakan entry, dipakai untuk saran konfigurasi.
        self.reject_stats: dict[str, int] = {}
        self._book_advice_sent = False
        # Jumlah posisi hasil adopsi saat start (untuk laporan dan batas efektif).
        self.adopted_count = 0
        self.adopt_error = ""
        self._adopt_suspect: set[str] = set()
        # Simbol yang sudah diberi tahu bahwa qty dipotong batas notional.
        self._cap_warned: set[str] = set()

    def _today(self) -> str:
        """Tanggal menurut zona waktu tampilan (default WIB), bukan UTC."""
        return day_key(self.cfg.logging.timezone)

    def _day_start_ms(self) -> int:
        """Awal hari bot (00:00 menurut `logging.timezone`) dalam epoch ms."""
        return day_start_ms(self.cfg.logging.timezone)

    def session_realized(self) -> float:
        """Pnl bersih trade sesi ini **yang jatuh di hari bot yang sedang berjalan**.

        `self.day.realized` adalah akumulator yang direset saat rollover; fungsi ini
        memeriksa ulang dengan cap waktu tutup transaksi supaya selaras dengan jendela
        `daily_loss_limit_pct`. Dipakai hanya sebagai pengaman di `sync_day_baseline`.
        """
        start = self._day_start_ms() / 1000.0
        with self._lock:
            trades = list(self.trades)
        return sum(t.pnl_net for t in trades if t.closed_ts >= start)

    def _all_positions(self) -> list[ManagedPosition]:
        return [pos for group in self.positions.values() for pos in group]

    def price(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self.prices.get(symbol)

    def snapshot_equity(self) -> tuple[float, float]:
        with self._lock:
            return self.equity, self.available

    def stats(self) -> dict[str, float | int | str]:
        with self._lock:
            return {
                "equity": self.equity,
                "available": self.available,
                "realized": self.current_realized(),
                "start_equity": self.day.start_equity,
                "halted_reason": self.halted_reason,
                "signals": self.day.signals,
                "trades": self.day.trades,
                "wins": self.day.wins,
                "losses": self.day.losses,
                "book_rejects": dict(self.reject_stats),
                "errors": self.error_count,
                "open_count": sum(len(g) for g in self.positions.values()),
            }

    def position_count(self) -> int:
        with self._lock:
            return sum(len(g) for g in self.positions.values())

    def position_symbols(self) -> set[str]:
        with self._lock:
            return {symbol for symbol, group in self.positions.items() if group}

    def has_position(self, symbol: str) -> bool:
        with self._lock:
            return bool(self.positions.get(symbol))

    def bump_error(self) -> None:
        with self._lock:
            self.error_count += 1

    def record_external_fill(self, symbol: str, price: float) -> None:
        if not math.isfinite(price) or price <= 0:
            return
        with self._lock:
            self._external_exit[symbol] = price

    def set_mark(self, symbol: str, price: float) -> None:
        if not math.isfinite(price) or price <= 0:
            return
        with self._lock:
            self.prices[symbol] = price
        if isinstance(self.broker, PaperBroker):
            self.broker.mark(symbol, price)

    def refresh_equity(self, force: bool = False) -> None:
        now = time.time()
        with self._lock:
            if not force and now - self._last_equity_poll < self.poll_s:
                return
        try:
            equity, available = self.broker.equity()
        except BinanceError as exc:
            self.bump_error()
            log.warning("gagal ambil equity: %s", exc)
            return
        if not math.isfinite(equity) or not math.isfinite(available):
            self.bump_error()
            log.warning("equity tidak valid: %r / %r", equity, available)
            return
        with self._lock:
            self.equity, self.available = equity, available
            self._last_equity_poll = now
            self.equity_history.append((now, equity))
            if len(self.equity_history) > 2000:
                self.equity_history = self.equity_history[-1000:]
            if self.day.start_equity <= 0:
                self.day = DayStats(day=self._today(), start_equity=equity)
        self.sync_day_baseline()

    def current_realized(self) -> float:
        """Realized hari ini termasuk PnL sebelum bot dijalankan ulang."""
        return self.day_baseline + self.day.realized

    def sync_day_baseline(self) -> bool:
        """Sinkronkan dasar batas rugi harian dengan hari bot (00:00 menurut zona tampilan).

        Yang penting di sini: bot menghitung sendiri PnL hari ini dari trade yang
        ditutupnya (`session_realized`), sehingga batas 00:00 WIB dan batas yang dipakai
        menjumlah rugi selalu sama. `/fapi/v1/income` hanya dipakai sebagai pembanding:
        angkanya diambil dari **awal hari bot** (bukan 00:00 UTC), dan selisihnya
        dilaporkan sebagai trade di luar bot (posisi manual, funding, fee koneksi).

        Dipanggil sekali per hari bot dan setelah rollover. Bila endpoint gagal, bot
        tetap jalan memakai PnL sesi ini saja, dengan peringatan di log.
        """
        today = self._today()
        start_ms = self._day_start_ms()
        reader = getattr(self.broker, "realized_since", None)
        if reader is None:
            return False
        now = time.time()
        with self._lock:
            if self._baseline_day == today:
                return False
            # Hanya saat percobaan sebelumnya gagal: jangan hammering, satu percobaan per menit.
            if self._baseline_failed and now - self._baseline_try_ts < 60.0:
                return False
            self._baseline_try_ts = now
        try:
            value = reader(start_ms)
        except BinanceError as exc:
            with self._lock:
                self._baseline_failed = True
            log.warning("gagal ambil PnL harian dari exchange: %s", exc)
            self.events.add("WARNING", "risk", f"PnL harian exchange gagal dibaca: {exc}")
            return False
        if value is None or not math.isfinite(value):
            return False
        with self._lock:
            if self._baseline_day == today:
                return False
            self._baseline_failed = False
            # PnL bot hari ini sudah masuk day.realized; baseline hanya menampung
            # selisihnya (posisi manual, funding) supaya tidak dihitung dua kali.
            self.day_baseline = value - self.day.realized
            self._baseline_day = today
            total = self.current_realized()
            start_equity = self.day.start_equity
            limit = self.cfg.risk.daily_loss_limit_pct
            outside = self.day_baseline
            tz = self.cfg.logging.timezone
        if abs(outside) >= 0.005:
            self.events.add(
                "INFO",
                "risk",
                (
                    f"PnL di luar bot hari ini {outside:+.2f}$ (posisi manual/funding) "
                    f"ikut dihitung dalam batas rugi harian; hari bot mulai 00:00 {tz_label(tz)}"
                ),
            )
        if limit > 0 and start_equity > 0 and total <= -start_equity * limit / 100.0:
            with self._lock:
                self.halted_reason = "daily loss limit"
            self.events.add(
                "ERROR", "risk", f"halt: rugi hari ini {total:.2f}$ sudah melewati batas {limit:.2f}%"
            )
        return True

    def _roll_day(self) -> None:
        today = self._today()
        with self._lock:
            if today == self.day.day:
                return
            # Trade hari berjalan dipertahankan: kalau proses ini hidup melewati tengah
            # malam, pnl_net-nya masih dibutuhkan `session_realized` sebagai dasar batas
            # rugi harian. Trade dari hari-hari sebelumnya dibuang supaya memori tidak
            # menumpuk; angka hariannya sudah "dibekukan" ke day_baseline saat rollover.
            start_ms = self._day_start_ms() / 1000.0
            kept = [t for t in self.trades if t.closed_ts >= start_ms]
            dropped = len(self.trades) - len(kept)
            log.info(
                "rollover hari %s -> %s: %d trade lama dilepas, %d trade hari ini dipertahankan",
                self.day.day,
                today,
                dropped,
                len(kept),
            )
            self.trades = kept
            self.day_baseline = 0.0
            # Dikosongkan supaya baseline hari baru benar-benar diambil dari exchange.
            self._baseline_day = ""
            # Trade yang dipertahankan tetap menyumbang PnL hari ini, jadi hitungannya
            # dibawa ke DayStats baru, bukan direset 0, agar current_realized() utuh.
            self.day = DayStats(day=today, start_equity=self.equity)
            self.day.realized = sum(t.pnl_net for t in kept)
            self.day.trades = len(kept)
            self.day.wins = sum(1 for t in kept if t.pnl_net >= 0)
            self.day.losses = sum(1 for t in kept if t.pnl_net < 0)
            if self.halted_reason.startswith("daily"):
                self.halted_reason = ""
                self.events.add("INFO", "risk", "halt harian direset untuk hari baru")
        self.sync_day_baseline()

    # Penolakan yang sama (simbol + alasan) dalam jendela ini digabung jadi satu baris.
    ENTRY_MERGE_S = 300.0
    # Batas baris riwayat entry di memori dan di berkas state.
    ENTRY_CAP = 200
    # Diisi lewat attach_state(); None berarti tidak menyimpan state.
    state: Optional[StateStore] = None

    def _note_entry(self, rec: EntryRecord) -> None:
        with self._lock:
            if rec.status == "TOLAK" and self.entries:
                last = self.entries[-1]
                if (
                    last.status == "TOLAK"
                    and last.symbol == rec.symbol
                    and last.detail == rec.detail
                    and rec.ts - last.ts <= self.ENTRY_MERGE_S
                ):
                    last.count += 1
                    last.ts = rec.ts
                    merged = True
                else:
                    merged = False
                if merged:
                    self._persist_state()
                    return
            self.entries.append(rec)
            if len(self.entries) > self.ENTRY_CAP:
                del self.entries[: -self.ENTRY_CAP]
        self._persist_state()

    # -- state yang bertahan melewati restart ---------------------------------

    def attach_state(self, store: Optional[StateStore]) -> int:
        """Pasang penyimpanan state dan muat cooldown + riwayat entry lama.

        Mengembalikan jumlah baris riwayat yang berhasil dimuat.
        """
        self.state = store
        if store is None:
            return 0
        data = store.load()
        if not data:
            return 0
        cooldown = StateStore.cooldown_from_state(data)
        rows = StateStore.entries_from_state(data, self.ENTRY_CAP)
        with self._lock:
            self._last_entry_bar_ts.update(cooldown)
            if rows:
                self.entries = [
                    EntryRecord(
                        ts=row["time"],
                        symbol=row["symbol"],
                        side=row["side"],
                        price=row["price"],
                        qty=row["qty"],
                        status=row["status"],
                        detail=row["detail"],
                        count=row["count"],
                    )
                    for row in rows
                ][-self.ENTRY_CAP :]
        if cooldown:
            symbols = ", ".join(sorted(cooldown)[:5])
            self.events.add(
                "INFO",
                "state",
                f"cooldown dari sesi sebelumnya dimuat untuk {len(cooldown)} simbol ({symbols})",
            )
        if rows:
            self.events.add(
                "INFO", "state", f"riwayat entry sesi sebelumnya dimuat: {len(rows)} baris"
            )
        return len(rows)

    def _persist_state(self) -> None:
        """Simpan cooldown + riwayat entry. Dipanggil hanya saat ada perubahan."""
        store = self.state
        if store is None:
            return
        with self._lock:
            cooldown = dict(self._last_entry_bar_ts)
            entries = [
                {
                    "time": rec.ts,
                    "symbol": rec.symbol,
                    "side": rec.side,
                    "price": rec.price,
                    "qty": rec.qty,
                    "status": rec.status,
                    "detail": rec.detail,
                    "count": rec.count,
                }
                for rec in self.entries
            ]
        store.save(
            {
                "saved_at": StateStore.now_iso(),
                "mode": self.cfg.mode,
                "tz": self.cfg.logging.timezone,
                "symbols": self.cfg.market.symbols,
                "cooldown": cooldown,
                "entries": entries,
            }
        )

    def entry_rows(self, limit: int = 6) -> list[dict]:
        """Riwayat entry terbaru untuk panel dashboard."""
        if limit <= 0:
            return []
        with self._lock:
            items = list(self.entries)[-limit:]
        return [
            {
                "time": r.ts,
                "symbol": r.symbol,
                "side": r.side,
                "price": r.price,
                "status": r.status,
                "detail": r.detail if r.count <= 1 else f"{r.detail} x{r.count}"
            }
            for r in items
        ]

    @property
    def max_open_effective(self) -> int:
        """Batas posisi yang benar-benar berlaku.

        Posisi hasil adopsi tidak boleh membuat bot menutup posisi sendiri, tetapi juga
        tidak boleh dilewati begitu saja: kalau hasil adopsi sudah melebihi
        `max_open_positions`, batas dinaikkan ke jumlah itu supaya tidak ada entry baru
        sama sekali (bukan malah menambah eksposur).
        """
        want = int(self.cfg.risk.max_open_positions)
        with self._lock:
            held = sum(len(g) for g in self.positions.values())
        return max(want, held)

    def adopt_position(self, symbol: str, side: str, qty: float, entry: float) -> Optional[ManagedPosition]:
        """Ambil alih posisi yang sudah ada di exchange supaya ikut dikelola.

        Mengembalikan None bila posisi dilewati (tidak ada order proteksi sama sekali).
        """
        filters = self.filters.get(symbol)
        if filters is None:
            self.events.add("WARNING", "adopt", f"{symbol} dilewati: filter simbol tidak tersedia")
            return None
        prot_rows: list[dict] = []
        try:
            prot_rows = self.broker.open_protection(symbol) or []
        except (BinanceError, AttributeError) as exc:
            self.events.add(
                "WARNING",
                "adopt",
                f"{symbol} order proteksi tidak terbaca ({exc}); SL/TP dipasang ulang dari config",
            )
        stop_row = self._pick_protection(prot_rows, side, "stop")
        tp_row = self._pick_protection(prot_rows, side, "tp")
        price = self.prices.get(symbol) or entry
        synthetic_stop = not (stop_row and float(stop_row.get("trigger", 0)) > 0)
        if not synthetic_stop:
            stop = float(stop_row["trigger"])
        else:
            stop = plan_stops(side, entry, 0.0, entry, entry, self.cfg.risk, filters).stop
            # Jarak SL dari entry bisa saja sudah terlewati harga (posisi sudah rugi jauh).
            # Menaruh SL di sisi harga yang salah = order langsung tereksekusi, jadi SL
            # dihitung ulang dari harga sekarang dengan jarak yang sama.
            dist = abs(entry - stop) or entry * self.cfg.risk.sl_min_bps / 10_000.0
            stop = self._protect_side(side, price, stop, dist, filters)
            if stop <= 0:
                self.events.add(
                    "CRITICAL",
                    "adopt",
                    f"{symbol} tidak punya SL dan SL pengganti tidak bisa ditempatkan aman; pasang SL manual",
                )
                return None
        if tp_row and float(tp_row.get("trigger", 0)) > 0:
            target = float(tp_row["trigger"])
        else:
            risk = abs(entry - stop) or entry * self.cfg.risk.sl_min_bps / 10_000.0
            target = price + risk * self.cfg.risk.tp_rr if side == "LONG" else price - risk * self.cfg.risk.tp_rr
            target = round_tick(target, filters.tick_size, "up" if side == "LONG" else "down")
            if (side == "LONG" and target <= price) or (side == "SHORT" and target >= price):
                self.events.add(
                    "WARNING", "adopt", f"{symbol} TP pengganti tidak masuk akal vs harga {price:.6g}; TP dilewati"
                )
                target = 0.0
        if not stop_row and not tp_row:
            self.events.add(
                "WARNING",
                "adopt",
                f"{symbol} tidak punya SL maupun TP di exchange; posisi dibiarkan terbuka tanpa adopsi",
            )
            return None

        risk_dist = abs(entry - stop)
        if risk_dist <= 0:
            risk_dist = entry * self.cfg.risk.sl_min_bps / 10_000.0
        # Tandai state manajemen yang sudah terlampaui, supaya tidak "memundurkan" SL
        # ke BE saat harga sudah jauh di atas entry.
        r_now_val = (price - entry) / risk_dist * (1 if side == "LONG" else -1)
        be_price = breakeven_price(side, entry, self.cfg.risk)
        be_done = (
            (side == "LONG" and stop >= be_price > 0)
            or (side == "SHORT" and 0 < stop <= be_price)
            or r_now_val >= self.cfg.risk.be_trigger_r > 0
        )
        trail_active = r_now_val >= self.cfg.risk.trail_trigger_r > 0
        pos = ManagedPosition(
            symbol=symbol,
            side=side,
            qty=qty,
            entry=entry,
            stop=stop,
            target=target,
            initial_stop=stop,
            risk_dist=risk_dist,
            # ATR tidak tersimpan di exchange; 0 membuat trailing_stop() mengembalikan SL
            # sekarang, jadi tidak ada SL yang digeser berdasarkan ATR karangan.
            atr=0.0,
            opened_ts=time.time(),
            signal_ts=0,
            stop_order_id=(stop_row or {}).get("id") or None,
            tp_order_id=(tp_row or {}).get("id") or None,
            be_done=bool(be_done),
            trail_active=bool(trail_active),
            state="trail" if trail_active else "be" if be_done else "open",
            peak_price=price if price > 0 else entry,
            entry_fee=entry * qty * self.cfg.risk.fee_rate,
            mark=price,
            key=f"{symbol}-adopt",
        )
        with self._lock:
            self.positions.setdefault(symbol, []).append(pos)
            self.adopted_count += 1
        arah = []
        if stop_row:
            arah.append(f"SL={stop:.6g}")
        if tp_row:
            arah.append(f"TP={target:.6g}")
        self.events.add(
            "WARNING",
            "adopt",
            (
                f"{symbol} {side} {qty:g} @{entry:.6g} diadopsi ({', '.join(arah) or 'tanpa order'}), "
                f"R sekarang {r_now_val:+.2f}, state {pos.state}"
            ),
        )
        if not stop_row:
            self.events.add("WARNING", "adopt", f"{symbol} tidak punya SL; memasang SL di {stop:.6g}")
        if not tp_row:
            self.events.add("INFO", "adopt", f"{symbol} tidak punya TP; memasang TP di {target:.6g}")
        return pos

    @staticmethod
    def _protect_side(side: str, price: float, candidate: float, dist: float, filters: SymbolFilters) -> float:
        """Kembalikan harga SL yang pasti berada di sisi aman terhadap harga sekarang.

        Tanpa ini, SL hasil hitung dari `entry` bisa berada di atas harga untuk LONG
        (posisi sudah rugi melewati SL) sehingga order stop langsung tereksekusi.
        """
        if not (price > 0) or not (dist > 0):
            return 0.0
        gap = max(filters.tick_size * 2, price * 1e-5)
        limit = price - gap if side == "LONG" else price + gap
        if side == "LONG":
            out = min(candidate, limit) if candidate > 0 else limit
            out = min(out, limit)
            return round_tick(out, filters.tick_size, "down") if out > 0 else 0.0
        out = max(candidate, limit) if candidate > 0 else limit
        out = max(out, limit)
        return round_tick(out, filters.tick_size, "up") if out > 0 else 0.0

    @staticmethod
    def _pick_protection(rows: list[dict], side: str, kind: str) -> Optional[dict]:
        """Pilih order proteksi yang berlawanan arah posisi (SL atau TP).

        Arah sengaja dicek supaya TP posisi LONG tidak tertukar dengan SL posisi SHORT
        pada simbol yang sama.
        """
        want_side = "SELL" if side == "LONG" else "BUY"
        stops = ("STOP_MARKET", "STOP")
        tps = ("TAKE_PROFIT_MARKET", "TAKE_PROFIT")
        types = stops if kind == "stop" else tps
        for row in rows:
            row_side = str(row.get("side", "")).upper()
            if str(row.get("type", "")).upper() in types and (not row_side or row_side == want_side):
                return row
        return None

    def mark_signal(self) -> None:
        with self._lock:
            self.day.signals += 1

    def _cooldown_left_bars(self, symbol: str, signal_ts_ms: int) -> int:
        """Sisa cooldown dalam jumlah bar.

        Dihitung dari waktu bar (ms), bukan index bar, supaya tetap benar setelah
        restart: index bar dihitung ulang dari warmup, sedangkan waktu bar tidak.
        Bar lama (signal_ts_ms 0) jatuh ke perbandingan index seperti sebelumnya.
        """
        with self._lock:
            last_ts = self._last_entry_bar_ts.get(symbol, 0)
            last_idx = self._last_trade_bar.get(symbol)
        if signal_ts_ms > 0 and last_ts > 0:
            gap_ms = signal_ts_ms - last_ts
            return int(round(gap_ms / (self.interval_seconds * 1000.0)))
        if last_idx is not None:
            return -1  # penanda: pakai jalur index
        return self.cfg.risk.cooldown_bars

    def guard_entry(
        self, symbol: str, bar_idx: int, signal_ts_ms: int = 0
    ) -> tuple[bool, str]:
        with self._lock:
            if self.halted_reason:
                return False, f"halted: {self.halted_reason}"
            total = sum(len(g) for g in self.positions.values()) + self._pending_entries
            # Batas dihormati walau posisi hasil adopsi membuat total sudah melewatinya.
            limit_total = self.max_open_effective
            if total >= limit_total:
                return False, f"max posisi {limit_total}"
            same = len(self.positions.get(symbol, [])) + self._pending_by_symbol.get(symbol, 0)
            if same >= self.cfg.risk.max_open_per_symbol:
                return False, f"max {self.cfg.risk.max_open_per_symbol} posisi di {symbol}"
            if self.cfg.risk.cooldown_bars > 0:
                left = self._cooldown_left_bars(symbol, signal_ts_ms)
                if left == -1:
                    last_bar = self._last_trade_bar.get(symbol)
                    left = (bar_idx - last_bar) if last_bar is not None else self.cfg.risk.cooldown_bars
                if left < self.cfg.risk.cooldown_bars:
                    return False, f"cooldown {self.cfg.risk.cooldown_bars} bar (sisa {max(0, left)})"
            if self.equity <= 0:
                return False, "equity tidak tersedia"
            if self.day.start_equity > 0 and self.cfg.risk.daily_loss_limit_pct > 0:
                limit = -self.day.start_equity * self.cfg.risk.daily_loss_limit_pct / 100.0
                realized = self.current_realized()
                if realized <= limit:
                    self.halted_reason = "daily loss limit"
                    self.events.add("ERROR", "risk", f"halt: daily loss {realized:.2f} <= {limit:.2f}")
                    return False, "daily loss limit"
            return True, "ok"

    def _note_book_reject(self, symbol: str, reason: str) -> None:
        """Catat alasan penolakan orderbook; beri saran sekali setelah cukup banyak sampel."""
        category = classify_reason(reason)
        with self._lock:
            self.reject_stats[category] = self.reject_stats.get(category, 0) + 1
            total = sum(self.reject_stats.values())
            imbalance_hits = self.reject_stats.get("imbalance", 0)
            advice_pending = not self._book_advice_sent and total >= 5
            if advice_pending:
                self._book_advice_sent = True
        if not advice_pending:
            return
        if imbalance_hits >= max(3, total * 0.6):
            self.events.add(
                "WARNING",
                "orderbook",
                (
                    f"{imbalance_hits} dari {total} entry ditolak karena imbalance (ambang "
                    f"{self.cfg.orderbook.min_imbalance:.2f}). Filter ini menuntut book berat sebelah; "
                    f"turunkan orderbook.min_imbalance (mis. 0.0 = mati) atau matikan orderbook.enabled. "
                    f"Cek angka nyata: python -m scalpbot bookscan"
                ),
            )
        elif self.reject_stats.get("stale", 0) >= max(3, total * 0.6):
            self.events.add(
                "WARNING",
                "orderbook",
                (
                    f"{self.reject_stats['stale']} dari {total} entry ditolak karena snapshot orderbook basi. "
                    f"Naikkan orderbook.max_age_s atau aktifkan orderbook.periodic_snapshot_s."
                ),
            )
        else:
            self.events.add(
                "INFO",
                "orderbook",
                f"{total} entry ditolak filter orderbook: {self.reject_stats}. Cek: python -m scalpbot bookscan",
            )

    def note_regime_reject(self, symbol: str, side: str) -> None:
        """Catat sinyal yang ditolak filter rezim (tren tidak searah)."""
        self._note_entry(
            EntryRecord(
                ts=time.time(), symbol=symbol, side=side, price=0.0, qty=0.0,
                status="TOLAK", detail="rezim",
            )
        )
        with self._lock:
            self.reject_stats["regime"] = self.reject_stats.get("regime", 0) + 1
            total = sum(self.reject_stats.values())
        if total == 5:
            self.events.add(
                "INFO",
                "regime",
                (
                    f"{self.reject_stats['regime']} dari {total} sinyal ditolak filter rezim "
                    f"(min_slope {self.cfg.strategy.regime_min_slope_bps:g} bps / "
                    f"{self.cfg.strategy.regime_slope_bars} bar). "
                    f"Turunkan strategy.regime_min_slope_bps atau set 0 untuk mematikan filter."
                ),
            )

    def try_open(
        self,
        signal: Signal,
        filters: SymbolFilters,
        bar_idx: int,
        snapshot: Optional[BookSnapshot] = None,
    ) -> Optional[ManagedPosition]:
        ok, reason = self.guard_entry(signal.symbol, bar_idx, int(signal.ts or 0))
        if not ok:
            self.events.add("INFO", "guard", f"{signal.symbol} {signal.side} ditolak: {reason}")
            return None
        with self._lock:
            equity = self.equity
            available = self.available
            self._pending_entries += 1
            self._pending_by_symbol[signal.symbol] = self._pending_by_symbol.get(signal.symbol, 0) + 1
        try:
            ok_book, book_reason = self.book.check(signal.side, snapshot)
            if not ok_book:
                self._note_book_reject(signal.symbol, book_reason)
                self._note_entry(
                    EntryRecord(
                        ts=time.time(),
                        symbol=signal.symbol,
                        side=signal.side,
                        price=signal.price,
                        qty=0.0,
                        status="TOLAK",
                        detail=f"book: {book_reason.split(' ')[0]}",
                    )
                )
                self.events.add("INFO", "orderbook", f"{signal.symbol} {signal.side} ditolak: {book_reason}")
                return None
            if snapshot and snapshot.spread_bps > self.cfg.execution.max_slippage_bps:
                self._note_book_reject(signal.symbol, f"spread {snapshot.spread_bps:.1f}bps > slippage")
                self.events.add(
                    "INFO",
                    "orderbook",
                    f"{signal.symbol} {signal.side} ditolak: spread {snapshot.spread_bps:.1f}bps "
                    f"> max_slippage {self.cfg.execution.max_slippage_bps:.1f}bps",
                )
                return None
            plan = plan_stops(
                signal.side,
                signal.price,
                signal.atr,
                signal.pac_upper,
                signal.pac_lower,
                self.cfg.risk,
                filters,
            )
            sizing = size_position(equity, plan.entry, plan.stop, self.cfg.risk, filters)
            if not sizing.ok:
                self.events.add("INFO", "risk", f"{signal.symbol} skip: {sizing.reason}")
                return None
            if sizing.capped and signal.symbol not in self._cap_warned:
                self._cap_warned.add(signal.symbol)
                self.events.add(
                    "WARNING",
                    "risk",
                    (
                        f"{signal.symbol} risiko terpasang "
                        f"{sizing.risk_usd / max(equity, 1e-9) * 100:.2f}% equity "
                        f"({sizing.risk_usd:.2f} USDT), bukan {self.cfg.risk.risk_pct:g}% yang diminta: "
                        f"qty dibatasi notional {sizing.notional:.2f} USDT "
                        f"(leverage {self.cfg.risk.leverage}x, max_notional_frac_equity "
                        f"{self.cfg.risk.max_notional_frac_equity:g}). Naikkan leverage atau "
                        f"max_notional_frac_equity bila memang ingin risiko lebih besar."
                    ),
                )
            margin_needed = sizing.notional / max(1, self.cfg.risk.leverage)
            if available > 0 and margin_needed > available * 0.98:
                self.events.add(
                    "WARNING",
                    "risk",
                    f"{signal.symbol} skip: margin {margin_needed:.2f} > available {available:.2f}",
                )
                return None
            send_qty = round_step(sizing.qty, filters.step_size) or sizing.qty
            try:
                fill = self.broker.place_market(signal.symbol, signal.side, send_qty, "en")
            except BinanceError as exc:
                self.bump_error()
                self.events.add("ERROR", "order", f"{signal.symbol} entry gagal: {exc}")
                return None
            if not math.isfinite(fill.qty) or fill.qty <= 0:
                # Tidak ada qty nyata: jangan kirim close qty=0, cukup laporkan.
                self.bump_error()
                self.events.add(
                    "ERROR", "order", f"{signal.symbol} fill kosong/tidak valid ({fill.qty!r}), tidak ada posisi dibuka"
                )
                return None
            if not math.isfinite(fill.avg_price) or fill.avg_price <= 0:
                self.events.add("ERROR", "order", f"{signal.symbol} harga fill tidak valid: {fill}")
                self._emergency_close(signal.symbol, signal.side, fill.qty, fill.order_id)
                return None
            entry = fill.avg_price
            plan = plan_stops(
                signal.side, entry, signal.atr, signal.pac_upper, signal.pac_lower, self.cfg.risk, filters
            )
            key = str(fill.order_id) if isinstance(self.broker, PaperBroker) else f"{signal.symbol}-{next(self._position_seq)}"
            pos = ManagedPosition(
                symbol=signal.symbol,
                side=signal.side,
                qty=fill.qty,
                entry=entry,
                stop=plan.stop,
                target=plan.target,
                initial_stop=plan.stop,
                risk_dist=plan.risk_dist,
                atr=signal.atr,
                opened_ts=time.time(),
                signal_ts=signal.ts,
                peak_price=entry,
                entry_fee=entry * fill.qty * self.cfg.risk.fee_rate,
                mark=entry,
                key=key,
            )
            try:
                prot = self.broker.place_protection(
                    signal.symbol, signal.side, fill.qty, pos.stop, pos.target, "pr"
                )
                pos.stop_order_id, pos.tp_order_id = prot.stop_id, prot.tp_id
            except BinanceError as exc:
                self.bump_error()
                self.events.add("CRITICAL", "order", f"{signal.symbol} proteksi gagal (algo): {exc}, posisi ditutup")
                self._emergency_close(signal.symbol, signal.side, fill.qty, fill.order_id)
                return None
            with self._lock:
                self.positions.setdefault(signal.symbol, []).append(pos)
                self._last_trade_bar[signal.symbol] = bar_idx
                if signal.ts > 0:
                    self._last_entry_bar_ts[signal.symbol] = int(signal.ts)
            self._persist_state()
            imbal = f"{snapshot.imbalance:+.2f}" if snapshot else "n/a"
            self.events.add(
                "INFO",
                "trade",
                (
                    f"OPEN {signal.side} {signal.symbol} qty={fill.qty:g} @{entry:.6g} "
                    f"SL={pos.stop:.6g} TP={pos.target:.6g} risk={sizing.risk_usd:.2f}$ "
                    f"({sizing.risk_usd / max(equity, 1e-9) * 100:.2f}%"
                    f"{' DIPOTONG' if sizing.capped else ''} dari {self.cfg.risk.risk_pct:g}%) "
                    f"R={plan.risk_bps:.0f}bps imb={imbal}{' BUMP' if sizing.bumped else ''}"
                ),
            )
            self._note_entry(
                EntryRecord(
                    ts=time.time(),
                    symbol=signal.symbol,
                    side=signal.side,
                    price=entry,
                    qty=fill.qty,
                    status="OPEN",
                    detail=f"R={plan.risk_bps:.0f}bps",
                )
            )
            return pos
        finally:
            with self._lock:
                self._pending_entries -= 1
                left = self._pending_by_symbol.get(signal.symbol, 0) - 1
                if left > 0:
                    self._pending_by_symbol[signal.symbol] = left
                else:
                    self._pending_by_symbol.pop(signal.symbol, None)

    def _emergency_close(self, symbol: str, side: str, qty: float, position_key: str) -> None:
        self.bump_error()
        reason = "emergency close"
        self.events.add("CRITICAL", "risk", f"{symbol} close darurat dimulai qty={qty:g}")
        try:
            fill = self.broker.emergency_close(symbol, side, qty, "em", position_key=position_key)
            if not math.isfinite(fill.qty) or fill.qty <= 0:
                raise BinanceError(-1, "fill close darurat tidak valid", 0, "/fapi/v1/order")
        except (BinanceError, AttributeError, TypeError) as exc:
            reason = "emergency close failed"
            self.events.add(
                "CRITICAL",
                "risk",
                f"{symbol} CLOSE DARURAT GAGAL: {exc}; entry baru dihentikan",
            )
            try:
                self.broker.cancel_all(symbol)
            except (BinanceError, OSError, AttributeError, TypeError) as cancel_exc:
                self.events.add("WARNING", "risk", f"{symbol} cancel_all gagal: {cancel_exc}")
        else:
            self.events.add(
                "CRITICAL",
                "risk",
                f"{symbol} close darurat berhasil qty={fill.qty:g}; entry baru dihentikan",
            )
        with self._lock:
            self.halted_reason = reason

    def _replace_stop(self, pos: ManagedPosition, new_stop: float, tag: str) -> bool:
        if not math.isfinite(new_stop) or new_stop <= 0:
            return False
        filters = self.filters.get(pos.symbol)
        if filters is not None and filters.tick_size > 0:
            snapped = round_tick(new_stop, filters.tick_size, "down" if pos.side == "LONG" else "up")
            if snapped > 0:
                new_stop = snapped
        try:
            prot = self.broker.replace_stop(
                pos.symbol, pos.stop_order_id, pos.side, pos.qty, new_stop, tag
            )
        except BinanceError as exc:
            self.bump_error()
            self.events.add("ERROR", "order", f"{pos.symbol} replace SL algo gagal: {exc}")
            return False
        with self._lock:
            pos.stop = new_stop
            pos.stop_order_id = prot.stop_id
            pos.last_replace_ts = time.time()
        return True

    def manage_position(self, pos: ManagedPosition) -> None:
        with self._lock:
            price = self.prices.get(pos.symbol, 0.0)
            if price <= 0:
                return
            filters = self.filters.get(pos.symbol)
            if filters is None:
                return
            pos.mark = price
            pos.r = r_now(pos.side, pos.entry, price, pos.risk_dist)
            if pos.side == "LONG":
                pos.peak_price = max(pos.peak_price, price)
            else:
                pos.peak_price = min(pos.peak_price, price) if pos.peak_price > 0 else price
            now = time.time()
            throttle = self.cfg.execution.cancel_replace_min_s
            be_needed = False
            be_px = 0.0
            if self.cfg.risk.be_trigger_r > 0 and not pos.be_done and pos.r >= self.cfg.risk.be_trigger_r:
                be_px = breakeven_price(pos.side, pos.entry, self.cfg.risk)
                be_px = round_tick(be_px, filters.tick_size, "down" if pos.side == "LONG" else "up")
                gap = filters.tick_size * 2
                if pos.side == "LONG":
                    be_px = min(be_px, round_tick(price - gap, filters.tick_size, "down"))
                else:
                    be_px = max(be_px, round_tick(price + gap, filters.tick_size, "up"))
                if wants_stop_update(pos.side, pos.stop, be_px, price, self.cfg.risk, apply_step=False):
                    be_needed = True
                elif (pos.side == "LONG" and pos.stop >= be_px) or (
                    pos.side == "SHORT" and pos.stop > 0 and pos.stop <= be_px
                ):
                    pos.be_done = True
            trail_needed = False
            trail_px = 0.0
            if (
                self.cfg.risk.trail_trigger_r > 0
                and self.cfg.risk.trail_atr_mult > 0
                and pos.r >= self.cfg.risk.trail_trigger_r
            ):
                floor = breakeven_price(pos.side, pos.entry, self.cfg.risk)
                trail_px = trailing_stop(
                    pos.side, price, pos.atr, pos.stop, self.cfg.risk, filters, floor
                )
                if now - pos.last_replace_ts >= throttle and wants_stop_update(
                    pos.side, pos.stop, trail_px, price, self.cfg.risk
                ):
                    trail_needed = True
            hit_stop = False
            hit_tp = False
            if isinstance(self.broker, PaperBroker):
                hit_stop = (pos.side == "LONG" and price <= pos.stop) or (
                    pos.side == "SHORT" and price >= pos.stop
                )
                hit_tp = (pos.side == "LONG" and price >= pos.target) or (
                    pos.side == "SHORT" and price <= pos.target
                )
            timeout = (
                self.cfg.risk.max_hold_minutes > 0
                and (now - pos.opened_ts) / 60.0 >= self.cfg.risk.max_hold_minutes
            )
        if be_needed and self._replace_stop(pos, be_px, "be"):
            with self._lock:
                pos.be_done = True
                pos.state = "be"
                r_val = pos.r
                stop = pos.stop
            self.events.add("INFO", "manage", f"{pos.symbol} BE dipasang @{stop:.6g} (R={r_val:+.2f})")
        if trail_needed and self._replace_stop(pos, trail_px, "tr"):
            with self._lock:
                pos.trail_active = True
                pos.state = "trail"
                r_val = pos.r
                stop = pos.stop
            self.events.add("INFO", "manage", f"{pos.symbol} trailing SL -> {stop:.6g} (R={r_val:+.2f})")
        if hit_stop:
            self.close_position(pos, "TRAIL" if pos.trail_active else "BE" if pos.be_done else "SL", price)
            return
        if hit_tp:
            self.close_position(pos, "TP", price)
            return
        if timeout:
            self.close_position(pos, "TIMEOUT", price)

    def _cancel_protection(self, pos: ManagedPosition) -> None:
        for order_id in (pos.stop_order_id, pos.tp_order_id):
            if not order_id:
                continue
            try:
                self.broker.cancel(pos.symbol, order_id)
            except BinanceError as exc:
                self.events.add("WARNING", "order", f"{pos.symbol} cancel proteksi: {exc}")

    def close_position(self, pos: ManagedPosition, reason: str, price: Optional[float] = None) -> None:
        with self._lock:
            if pos.state == "closing":
                return
            pos.state = "closing"
        self._cancel_protection(pos)
        fill_price = price or self.price(pos.symbol) or pos.mark or pos.entry
        try:
            fill = self.broker.close_market(
                pos.symbol, pos.side, pos.qty, "cl", position_key=pos.key
            )
            if fill.avg_price > 0:
                fill_price = fill.avg_price
        except BinanceError as exc:
            self.bump_error()
            with self._lock:
                pos.state = "open"
            self.events.add("CRITICAL", "order", f"{pos.symbol} close gagal: {exc}, pasang SL algo ulang")
            try:
                prot = self.broker.replace_stop(pos.symbol, None, pos.side, pos.qty, pos.stop, "rc")
                with self._lock:
                    pos.stop_order_id = prot.stop_id
            except BinanceError as re_exc:
                self.events.add("CRITICAL", "order", f"{pos.symbol} proteksi ulang gagal: {re_exc}")
            return
        self._record_close(pos, reason, fill_price)

    def _record_close(self, pos: ManagedPosition, reason: str, exit_price: float) -> None:
        with self._lock:
            gross = unrealized_pnl(pos.side, pos.entry, exit_price, pos.qty)
            fees = (pos.entry + exit_price) * pos.qty * self.cfg.risk.fee_rate
            net = gross - fees
            r_val = r_now(pos.side, pos.entry, exit_price, pos.risk_dist)
            rec = TradeRecord(
                symbol=pos.symbol,
                side=pos.side,
                qty=pos.qty,
                entry=pos.entry,
                exit=exit_price,
                pnl=gross,
                pnl_net=net,
                r=r_val,
                opened_ts=pos.opened_ts,
                closed_ts=time.time(),
                reason=reason,
            )
            self.trades.append(rec)
            self.day.realized += net
            self.day.trades += 1
            if net >= 0:
                self.day.wins += 1
            else:
                self.day.losses += 1
            group = self.positions.get(pos.symbol)
            if group and pos in group:
                group.remove(pos)
            if group == []:
                self.positions.pop(pos.symbol, None)
                self._external_exit.pop(pos.symbol, None)
            net_value = net
        self.events.add(
            "INFO" if net_value >= 0 else "WARNING",
            "trade",
            f"CLOSE {pos.side} {pos.symbol} {reason} @{exit_price:.6g} PnL {net_value:+.2f}$ ({r_val:+.2f}R)",
        )
        self._note_entry(
            EntryRecord(
                ts=time.time(),
                symbol=pos.symbol,
                side=pos.side,
                price=exit_price,
                qty=pos.qty,
                status=reason,
                detail=f"{net_value:+.2f}$ {r_val:+.2f}R",
            )
        )
        self.refresh_equity(force=True)
        with self._lock:
            start_equity = self.day.start_equity
            realized = self.current_realized()
            limit = self.cfg.risk.daily_loss_limit_pct
        if limit > 0 and start_equity > 0 and realized <= -start_equity * limit / 100.0:
            with self._lock:
                self.halted_reason = "daily loss limit"
            self.events.add("ERROR", "risk", f"halt: daily loss {realized:.2f}$ terpakai")

    def reconcile_at_start(self) -> None:
        """Adopsi posisi yang sudah terbuka di exchange saat bot dinyalakan.

        Tanpa ini bot mengira dirinya flat: posisi lama tidak di-trail, tidak di-BE,
        tidak dihitung di `max_open_positions`, dan tidak punya batas waktu.
        """
        if isinstance(self.broker, PaperBroker):
            return
        if not self.cfg.risk.adopt_existing:
            self.events.add("INFO", "adopt", "adopsi posisi dimatikan (risk.adopt_existing=false)")
            return
        try:
            live = self.broker.open_positions()
        except BinanceError as exc:
            self.adopt_error = str(exc)
            self.bump_error()
            self.events.add(
                "ERROR",
                "adopt",
                (
                    f"gagal membaca posisi terbuka saat start ({exc}). Posisi lama mungkin ada "
                    f"tetapi tidak terpantau; dashboard bisa menampilkan POS 0. Periksa manual di exchange."
                ),
            )
            return
        if not live:
            self.events.add("INFO", "adopt", "tidak ada posisi terbuka di exchange saat start")
            return
        for symbol, info in sorted(live.items()):
            side = str(info.get("side", "")).upper()
            qty = float(info.get("qty", 0) or 0)
            entry = float(info.get("entry", 0) or 0)
            if side not in ("LONG", "SHORT") or qty <= 0 or entry <= 0:
                self.events.add(
                    "WARNING", "adopt", f"{symbol} data posisi tidak lengkap ({side} qty={qty:g} entry={entry:g}), dilewati"
                )
                continue
            with self._lock:
                already = bool(self.positions.get(symbol))
            if already:
                continue
            mark = float(info.get("mark", 0) or 0)
            if mark > 0:
                self.set_mark(symbol, mark)
            # Filter wajib ada sebelum adopsi (dipakai pembulatan tick dan SL).
            # Simbol hasil discovery sebelumnya bisa saja belum termasuk di sini.
            if symbol not in self.filters:
                try:
                    self.filters[symbol] = self.broker.filters(symbol)
                except (BinanceError, AttributeError) as exc:
                    log.debug("filter %s gagal: %s", symbol, exc)
            pos = self.adopt_position(symbol, side, qty, entry)
            if pos is None:
                continue
            if not pos.stop_order_id and self.cfg.execution.place_sl:
                try:
                    prot = self.broker.replace_stop(pos.symbol, None, pos.side, pos.qty, pos.stop, "ad")
                    with self._lock:
                        pos.stop_order_id = prot.stop_id
                except BinanceError as exc:
                    self.bump_error()
                    self.events.add("CRITICAL", "adopt", f"{symbol} gagal pasang SL hasil adopsi: {exc}")
            if not pos.tp_order_id and self.cfg.execution.place_tp and pos.target > 0:
                try:
                    prot = self.broker.place_tp(pos.symbol, pos.side, pos.qty, pos.target, "ad")
                    with self._lock:
                        pos.tp_order_id = prot.tp_id
                except (BinanceError, AttributeError) as exc:
                    self.bump_error()
                    self.events.add("WARNING", "adopt", f"{symbol} gagal pasang TP hasil adopsi: {exc}")
        if self.adopted_count:
            limit = self.cfg.risk.max_open_positions
            extra = ""
            if self.adopted_count >= limit:
                extra = (
                    f". Batas max_open_positions={limit} sudah terpakai, jadi tidak ada entry baru "
                    f"sampai jumlah posisi turun di bawah {limit}"
                )
            self.events.add(
                "WARNING",
                "adopt",
                f"{self.adopted_count} posisi diadopsi dan sekarang dikelola bot{extra}",
            )

    def sync_with_exchange(self, force: bool = False) -> None:
        if isinstance(self.broker, PaperBroker):
            return
        with self._lock:
            local = {symbol: list(group) for symbol, group in self.positions.items() if group}
            if not local:
                return
        try:
            live = self.broker.open_positions()
        except BinanceError as exc:
            self.bump_error()
            log.debug("sync posisi gagal: %s", exc)
            return
        closes: list[tuple[ManagedPosition, str, float]] = []
        unknown: list[str] = []
        with self._lock:
            for symbol, group in local.items():
                info = live.get(symbol)
                if info is None:
                    exit_price = (
                        self._external_exit.get(symbol)
                        or self.prices.get(symbol)
                        or group[0].mark
                        or group[0].entry
                    )
                    self.events.add(
                        "INFO", "sync", f"{symbol} posisi tertutup di exchange (SL/TP), exit ~{exit_price:.6g}"
                    )
                    closes.extend((pos, "EXCHANGE", exit_price) for pos in group)
                    self._external_exit.pop(symbol, None)
                    continue
                if len(group) > 1:
                    self.events.add(
                        "WARNING",
                        "sync",
                        f"{symbol} punya {len(group)} posisi lokal, rekonsiliasi qty dilewati",
                    )
                    continue
                pos = group[0]
                filters = self.filters.get(symbol)
                if abs(info["qty"] - pos.qty) > pos.qty * 0.01:
                    self.events.add(
                        "WARNING",
                        "sync",
                        f"{symbol} qty exchange {info['qty']:g} != lokal {pos.qty:g}, sinkron ke exchange",
                    )
                    step = filters.step_size if filters else 0.0
                    pos.qty = round_step(info["qty"], step) or info["qty"]
                entry = float(info.get("entry", 0) or 0)
                tolerance = max(filters.tick_size * 2, pos.entry * 1e-6) if filters else pos.entry * 1e-6
                if entry > 0 and abs(entry - pos.entry) > tolerance:
                    pos.entry = entry
                    pos.risk_dist = max(abs(entry - pos.initial_stop), entry * 1e-6)
            for symbol in list(self._external_warned):
                if symbol not in live:
                    self._external_warned.discard(symbol)
            for symbol in live:
                if symbol not in self.positions and symbol not in self._external_warned:
                    self._external_warned.add(symbol)
                    self.events.add(
                        "WARNING",
                        "sync",
                        f"{symbol} ada posisi tak dikenal bot (dibuka manual?), tidak dikelola",
                    )
                    unknown.append(symbol)
        # Jangan panggil REST sambil memegang lock: dashboard ikut tertahan.
        for symbol in unknown:
            self._warn_orphan_algos(symbol)
        for pos, reason, exit_price in closes:
            self._cancel_protection(pos)
            self._record_close(pos, reason, exit_price)

    def _warn_orphan_algos(self, symbol: str) -> None:
        """Posisi manual: jangan dibatalkan (bisa jadi SL milik user), cukup dilaporkan."""
        reader = getattr(self.broker, "open_protection", None)
        if reader is None:
            return
        try:
            rows = reader(symbol)
        except BinanceError as exc:
            log.debug("open_algo_orders %s gagal: %s", symbol, exc)
            return
        if not rows:
            return
        ringkas = ", ".join(f"{row['type']}@{row['trigger']:.6g}" for row in rows[:3])
        self.events.add(
            "INFO",
            "sync",
            f"{symbol} ada {len(rows)} order algo di luar bot ({ringkas}), tidak disentuh",
        )

    def check_orphan_protection(self) -> None:
        if isinstance(self.broker, PaperBroker):
            return
        with self._lock:
            positions = self._all_positions()
            if not positions:
                return
            symbols = {p.symbol for p in positions}
        ids: set[str] = set()
        unreadable: set[str] = set()
        for symbol in symbols:
            try:
                ids |= set(self.broker.open_stop_ids(symbol))
            except (BinanceError, AttributeError) as exc:
                # Gagal membaca != tidak ada SL. Simbol ini dilewati supaya SL tidak dipasang dobel.
                unreadable.add(symbol)
                log.debug("open_algo_orders %s gagal: %s", symbol, exc)
                continue
        with self._lock:
            missing = [
                pos
                for pos in positions
                if pos.symbol not in unreadable
                and self.cfg.execution.place_sl
                and pos.stop_order_id not in ids
            ]
        for pos in missing:
            self.events.add("WARNING", "risk", f"{pos.symbol} SL tidak ada di exchange, dipasang ulang")
            try:
                prot = self.broker.replace_stop(pos.symbol, None, pos.side, pos.qty, pos.stop, "rs")
                with self._lock:
                    pos.stop_order_id = prot.stop_id
            except BinanceError as exc:
                self.events.add("CRITICAL", "risk", f"{pos.symbol} gagal pasang SL algo: {exc}")

    def manage_all(self) -> None:
        self._roll_day()
        with self._lock:
            positions = self._all_positions()
        for pos in positions:
            self.manage_position(pos)

    def flatten_all(self, reason: str) -> None:
        with self._lock:
            positions = self._all_positions()
        for pos in positions:
            self.close_position(pos, reason)
        with self._lock:
            self.halted_reason = reason

    def position_rows(self) -> list[dict]:
        with self._lock:
            positions = self._all_positions()
            now = time.time()
            rows = []
            for pos in positions:
                pnl = unrealized_pnl(pos.side, pos.entry, pos.mark or pos.entry, pos.qty)
                rows.append(
                    {
                        "symbol": pos.symbol,
                        "side": pos.side,
                        "qty": pos.qty,
                        "entry": pos.entry,
                        "mark": pos.mark,
                        "r": pos.r,
                        "sl": pos.stop,
                        "tp": pos.target,
                        "pnl": pnl,
                        "state": pos.state,
                        "age": now - pos.opened_ts,
                    }
                )
            return rows
