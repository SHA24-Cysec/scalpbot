from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from .security import (
    RedactingFormatter,
    ensure_private_dir,
    ensure_private_file,
    redact,
)


def clean_message(text: str) -> str:
    return redact(text)


_audit_log = logging.getLogger("scalpbot.events.audit")
_audit_log.propagate = False
_audit_log.setLevel(logging.WARNING)
if not _audit_log.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s | %(message)s"))
    _audit_log.addHandler(handler)


@dataclass(slots=True)
class Event:
    ts: float
    level: str
    source: str
    message: str

    def as_json(self) -> str:
        d = asdict(self)
        d["message"] = clean_message(self.message)
        d["iso"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.ts))
        return json.dumps(d, ensure_ascii=False)


class EventLog:
    def __init__(
        self,
        capacity: int = 400,
        file_path: Optional[str] = None,
        max_bytes: int = 2_000_000,
        console: bool = False,
        mode: str = "",
    ) -> None:
        self._events: deque[Event] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._file = Path(file_path) if file_path else None
        self._max_bytes = max(1024, max_bytes)
        self.mode = clean_message(str(mode).strip().lower())
        self.console = console
        self.last_error = ""
        if self._file:
            ensure_private_dir(self._file.parent)

    def add(self, level: str, source: str, message: str) -> Event:
        ev = Event(
            ts=time.time(),
            level=clean_message(str(level).upper()),
            source=clean_message(str(source)),
            message=clean_message(message),
        )
        with self._lock:
            self._events.append(ev)
            if self._file:
                self._write(ev)
            if self.console:
                stamp = time.strftime("%H:%M:%S", time.localtime(ev.ts))
                tag = f"{self.mode.upper():<7} " if self.mode else ""
                print(
                    f"{stamp} {ev.level:<7} {tag}{ev.source:<12} {ev.message}",
                    file=sys.stderr,
                    flush=True,
                )
        return ev

    def _write(self, ev: Event) -> None:
        if self._file is None:
            raise RuntimeError("event log tanpa path file")
        try:
            if self._file.exists() and self._file.stat().st_size > self._max_bytes:
                rotated = self._file.with_suffix(self._file.suffix + ".1")
                if rotated.exists():
                    rotated.unlink()
                self._file.rename(rotated)
                ensure_private_file(rotated)
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self._file, flags, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(ev.as_json() + "\n")
            ensure_private_file(self._file)
            self.last_error = ""
        except (OSError, ValueError) as exc:
            self.last_error = clean_message(str(exc))
            _audit_log.error("event log gagal ditulis: %s", exc)

    def tail(self, n: int = 10) -> list[Event]:
        with self._lock:
            items = list(self._events)
        return items[-n:]

    def count(self, level: Optional[str] = None) -> int:
        with self._lock:
            if level is None:
                return len(self._events)
            return sum(1 for e in self._events if e.level == level.upper())


class EventLogHandler(logging.Handler):
    def __init__(self, event_log: EventLog) -> None:
        super().__init__()
        self.event_log = event_log

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.event_log.add(record.levelname, record.name.replace("scalpbot.", ""), record.getMessage())
        except Exception:
            self.handleError(record)
