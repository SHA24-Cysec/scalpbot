from __future__ import annotations

import ipaddress
import logging
import threading
import time
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from .security import ALLOWED_IP_HOSTS, clean_message, harden_session, parse_proxy

log = logging.getLogger("scalpbot.netinfo")


class IpDetector:
    def __init__(
        self,
        url: str = "https://api.ipify.org",
        interval_s: int = 600,
        proxy: str = "",
        fallback: bool = True,
    ) -> None:
        self.url = url
        self.interval_s = max(60, interval_s)
        self.proxy = proxy
        self.fallback = fallback
        self.ip: Optional[str] = None
        self.last_check: float = 0.0
        self.last_error: str = ""
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def _urls(self) -> list[str]:
        urls = [self.url]
        if self.fallback:
            for extra in ("https://ifconfig.me/ip", "https://api.ipify.org"):
                if extra not in urls:
                    urls.append(extra)
        return urls

    @staticmethod
    def _host_ok(url: Any) -> bool:
        try:
            return (urlparse(str(url or "")).hostname or "").lower() in ALLOWED_IP_HOSTS
        except (ValueError, TypeError):
            return False

    def fetch_once(self, timeout: float = 6.0) -> Optional[str]:
        proxy = ""
        if self.proxy:
            try:
                proxy = parse_proxy(self.proxy)["normalized"]
            except ValueError as exc:
                self.last_error = clean_message(str(exc))
                return None
        session = requests.Session()
        harden_session(session, proxy)
        try:
            for url in self._urls():
                if not self._host_ok(url):
                    continue
                try:
                    resp = session.get(
                        url,
                        timeout=timeout,
                        headers={"User-Agent": "curl/8", "Accept": "text/plain"},
                        allow_redirects=False,
                    )
                    if resp.status_code != 200 or not self._host_ok(resp.url):
                        continue
                    lines = resp.text.strip().splitlines()
                    text = lines[0].strip() if lines else ""
                    if not text:
                        continue
                    ipaddress.ip_address(text)
                    with self._lock:
                        self.ip = text
                        self.last_check = time.time()
                        self.last_error = ""
                    return text
                except (requests.RequestException, ValueError, OSError, IndexError, AttributeError) as exc:
                    with self._lock:
                        self.last_error = clean_message(f"{type(exc).__name__}: {exc}")
                    continue
        finally:
            session.close()
        return None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ipdetect", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.fetch_once()
            if self._stop.wait(self.interval_s):
                break

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> tuple[str, float]:
        with self._lock:
            if self.ip is None:
                return "unknown", 0.0
            return self.ip, max(0.0, time.time() - self.last_check)
