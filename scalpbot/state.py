"""Penyimpanan state kecil yang bertahan melewati restart.

Yang disimpan hanya dua hal, keduanya kecil dan murah ditulis:

1. cooldown entry per simbol (waktu bar terakhir yang dipakai untuk entry), dan
2. riwayat entry (maksimal beberapa ratus baris) untuk panel dashboard.

Posisi terbuka TIDAK disimpan di sini; posisi selalu dibaca ulang dari exchange
(`Trader.reconcile_at_start`), karena exchange adalah sumber kebenaran.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from .security import ensure_private_dir, ensure_private_file
from .timeutil import monotonic_stamp

log = logging.getLogger("scalpbot.state")

VERSION = 1


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return 0.0
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


class StateStore:
    """Berkas JSON tunggal; satu berkas per mode karena path sudah memuat nama mode."""

    def __init__(self, path: str | Path, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.last_error = ""

    # -- baca / tulis ------------------------------------------------------

    def load(self) -> dict[str, Any]:
        if not self.enabled:
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            self.last_error = str(exc)
            log.warning("state tidak terbaca: %s", exc)
            return {}
        try:
            data = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (json.JSONDecodeError, ValueError) as exc:
            # Berkas rusak (mis. mati saat menulis): sisihkan, jangan hapus.
            bad = self.path.with_suffix(self.path.suffix + ".corrupt")
            try:
                if bad.exists():
                    bad.unlink()
                self.path.replace(bad)
                ensure_private_file(bad)
            except OSError:
                pass
            self.last_error = f"state rusak: {exc}"
            log.warning("state rusak, dipindahkan ke %s: %s", bad, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        if data.get("version") != VERSION:
            log.info("versi state %r berbeda, diabaikan", data.get("version"))
            return {}
        return data

    def save(self, data: dict[str, Any]) -> bool:
        """Tulis atomik: file sementara lalu rename, supaya tidak setengah jadi."""
        if not self.enabled:
            return False
        payload = dict(data)
        payload["version"] = VERSION
        try:
            ensure_private_dir(self.path.parent)
            fd, tmp = tempfile.mkstemp(
                prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(
                        _json_safe(payload), fh,
                        ensure_ascii=False, allow_nan=False, separators=(",", ":"),
                    )
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
                ensure_private_file(self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except (OSError, ValueError, TypeError) as exc:
            self.last_error = str(exc)
            log.warning("state gagal ditulis: %s", exc)
            return False
        return True

    # -- bagian yang dipakai trader ---------------------------------------

    @staticmethod
    def cooldown_from_state(data: dict[str, Any]) -> dict[str, int]:
        """Ambil `last_trade_bar_ts` per simbol, hanya nilai yang masuk akal."""
        out: dict[str, int] = {}
        raw = data.get("cooldown")
        if not isinstance(raw, dict):
            return out
        for symbol, ts in raw.items():
            if not isinstance(symbol, str):
                continue
            value = int(_finite(ts, 0.0))
            if value > 0:
                out[symbol] = value
        return out

    @staticmethod
    def entries_from_state(data: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        raw = data.get("entries")
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for row in raw[-limit:]:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status", ""))
            if not status:
                continue
            out.append(
                {
                    "time": _finite(row.get("time"), 0.0),
                    "symbol": str(row.get("symbol", "")),
                    "side": str(row.get("side", "")),
                    "price": _finite(row.get("price"), 0.0),
                    "qty": _finite(row.get("qty"), 0.0),
                    "status": status,
                    "detail": str(row.get("detail", "")),
                    "count": max(1, int(_finite(row.get("count"), 1.0))),
                }
            )
        return out

    @staticmethod
    def now_iso() -> str:
        """Stempel waktu UTC (RFC3339) untuk metadata berkas.

        Sengaja UTC, bukan WIB: ini metadata internal supaya urutan waktu antar mesin
        tetap konsisten, bukan sesuatu yang dibaca pengguna.
        """
        return monotonic_stamp()


def resolve_path(cfg: Any, mode: str) -> Optional[str]:
    """Path state per mode, atau None kalau penyimpanan dimatikan."""
    if not cfg.logging.persist_state:
        return None
    return cfg.logging.state_for(mode)
