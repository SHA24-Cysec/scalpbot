from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

SECRET_PATTERNS = [
    re.compile(r'(?i)(api[_-]?key"?\s*[:=]\s*")[^"]+'),
    re.compile(r'(?i)(secret"?\s*[:=]\s*")[^"]+'),
    re.compile(r"(?i)(signature=)[a-f0-9]{32,}"),
    re.compile(r'(?i)(x-mbx-apikey"?\s*[:=]\s*)([a-z0-9]{16,})'),
    re.compile(r"\b[A-Za-z0-9]{64,}\b"),
]


def redact(text: str) -> str:
    out = str(text)
    for pat in SECRET_PATTERNS[:4]:
        out = pat.sub(lambda m: m.group(1) + "***", out)
    return SECRET_PATTERNS[4].sub("***", out)


@dataclass(slots=True)
class Event:
    ts: float
    level: str
    source: str
    message: str

    def as_json(self) -> str:
        d = asdict(self)
        d["message"] = redact(str(self.message))
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
        self.mode = str(mode).strip().lower()
        self.console = console
        if self._file:
            self._file.parent.mkdir(parents=True, exist_ok=True)

    def add(self, level: str, source: str, message: str) -> Event:
        ev = Event(ts=time.time(), level=level.upper(), source=source, message=redact(str(message)))
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
        assert self._file is not None
        try:
            if self._file.exists() and self._file.stat().st_size > self._max_bytes:
                rotated = self._file.with_suffix(self._file.suffix + ".1")
                if rotated.exists():
                    rotated.unlink()
                self._file.rename(rotated)
            with self._file.open("a", encoding="utf-8") as fh:
                fh.write(ev.as_json() + "\n")
        except OSError:
            pass

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
