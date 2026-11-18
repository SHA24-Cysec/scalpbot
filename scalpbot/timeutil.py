"""Zona waktu tampilan dan batas harian.

Ada dua batas hari yang sengaja dipisahkan di sini, karena menjawab pertanyaan berbeda:

1. **Hari akun exchange** (`/fapi/v1/income`) selalu dimulai 00:00 **UTC**. Itu ketentuan
   Binance, bukan pilihan bot. Angka ini dipakai untuk menampilkan PnL versi Binance
   (`pnl`) supaya cocok dengan aplikasi Binance.

2. **Hari bot** — dasar batas rugi harian (`daily_loss_limit_pct`) dan rollover —
   mengikuti `logging.timezone`, sama dengan jam di log. Dengan WIB, hari dimulai
   00:00 WIB = 17:00 UTC.

**Kenapa hari bot tidak memakai jendela UTC.** Jendela 00:00-07:00 WIB adalah bagian dari
hari UTC sebelumnya. Kalau batas rugi harian memakai jendela UTC sementara rollover
memakai WIB, bot di awal hari WIB masih menghitung rugi dari 7 jam terakhir hari kemarin.
Akibatnya bot bisa langsung berhenti di 00:05 WIB gara-gara rugi yang sudah "lunas" di
hari sebelumnya, dan angka PnL hariannya tidak pernah cocok dengan jam yang ditampilkan.

Karena itu baseline rugi harian sekarang dihitung dari **trade bot sendiri** (yang
memang punya cap waktu) plus, kalau perlu, `/fapi/v1/income` yang dipotong ke batas WIB.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python lama
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment]


class TimezoneError(ValueError):
    pass


# Offset tetap untuk nama yang lazim dipakai. Dicoba lebih dulu supaya bot tetap jalan
# di Windows tanpa paket tzdata.
_FIXED_OFFSETS = {
    "WIB": 7,
    "UTC": 0,
    "GMT": 0,
    "WITA": 8,
    "WIT": 9,
}

DEFAULT_TZ = "WIB"


def _fixed(name: str) -> Optional[timezone]:
    hours = _FIXED_OFFSETS.get(name.upper())
    if hours is None:
        return None
    return timezone(timedelta(hours=hours), name.upper())


def parse_timezone(name: str) -> timezone | "ZoneInfo":
    """Ubah nama zona waktu jadi objek tzinfo, atau TimezoneError."""
    raw = str(name or "").strip()
    if not raw:
        raise TimezoneError("zona waktu tidak boleh kosong")
    fixed = _fixed(raw)
    if fixed is not None:
        return fixed
    if ZoneInfo is None:
        raise TimezoneError(
            f"zona waktu {raw!r} butuh Python 3.9+ (zoneinfo). Pakai offset tetap seperti "
            f"WIB/UTC, atau tambahkan paket 'tzdata' di Windows"
        )
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise TimezoneError(
            f"zona waktu tidak dikenal: {raw!r} ({exc}). Contoh: WIB, Asia/Jakarta, UTC, WITA"
        ) from exc


def tz_label(name: str) -> str:
    """Label pendek untuk ditampilkan, mis. 'WIB' atau 'Asia/Jakarta'."""
    raw = str(name or "").strip()
    return raw.upper() if raw.upper() in _FIXED_OFFSETS else raw


def now_local(name: str = DEFAULT_TZ) -> datetime:
    return datetime.now(timezone.utc).astimezone(parse_timezone(name))


def format_ts(ts_seconds: float, name: str = DEFAULT_TZ, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format timestamp epoch (detik) menurut zona waktu tampilan."""
    if not ts_seconds or ts_seconds <= 0:
        return "-"
    return datetime.fromtimestamp(float(ts_seconds), tz=timezone.utc).astimezone(
        parse_timezone(name)
    ).strftime(fmt)


def day_key(name: str = DEFAULT_TZ) -> str:
    """Tanggal menurut zona waktu tampilan, dipakai untuk label dan rollover harian bot."""
    return now_local(name).strftime("%Y-%m-%d")


def tz_abbrev(name: str = DEFAULT_TZ, at: Optional[float] = None) -> str:
    """Singkatan zona pada waktu tertentu, mis. 'WIB'."""
    try:
        zone = parse_timezone(name)
    except TimezoneError:
        return "?"
    moment = datetime.fromtimestamp(at, tz=timezone.utc) if at else datetime.now(timezone.utc)
    return moment.astimezone(zone).tzname() or tz_label(name)


def utc_day_start_ms(now: Optional[datetime] = None) -> int:
    """Awal hari UTC (00:00) dalam epoch ms, untuk query /fapi/v1/income."""
    moment = now or datetime.now(timezone.utc)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def utc_day_label(now: Optional[datetime] = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%d")


def day_start_ms(name: str = DEFAULT_TZ, now: Optional[datetime] = None) -> int:
    """Awal hari menurut zona waktu tampilan, dalam epoch ms.

    Ini **berbeda** dari `utc_day_start_ms`. Dipakai untuk mengukur PnL harian yang
    dihitung bot sendiri (`day.realized`), supaya batas 00:00 yang dipakai untuk
    rollover dan yang dipakai untuk menjumlah rugi adalah batas yang sama.

    Contoh WIB: hari 18 Nov dimulai 17 Nov 17:00 UTC (= 18 Nov 00:00 WIB).
    """
    moment = now or datetime.now(timezone.utc)
    local = moment.astimezone(parse_timezone(name)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int(local.timestamp() * 1000)


def day_start_ms_from_label(day: str, name: str = DEFAULT_TZ) -> int:
    """Awal hari dari label 'YYYY-MM-DD' menurut zona tampilan, dalam epoch ms."""
    zone = parse_timezone(name)
    local = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=zone)
    return int(local.timestamp() * 1000)


def describe_boundary(name: str = DEFAULT_TZ) -> str:
    """Keterangan jam berapa batas hari exchange terjadi menurut zona tampilan."""
    zone = parse_timezone(name)
    utc_midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    local = utc_midnight.astimezone(zone)
    return local.strftime("%H:%M")


def monotonic_stamp() -> str:
    """Stempel waktu UTC untuk metadata berkas state (tidak untuk tampilan pengguna)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
