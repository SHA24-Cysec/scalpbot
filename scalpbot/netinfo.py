from __future__ import annotations

import ipaddress
import logging
import threading
import time
from typing import Optional

import requests

log = logging.getLogger("scalpbot.netinfo")


class IpDetector:
    def __init__(self, url: str = "https://api.ipify.org", interval_s: int = 600, proxy: str = "") -> None:
        self.url = url
        self.interval_s = max(60, interval_s)
        self.proxy = proxy
        self.ip: Optional[str] = None
        self.last_check: float = 0.0
        self.last_error: str = ""
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def fetch_once(self, timeout: float = 6.0) -> Optional[str]:
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        for url in (self.url, "https://ifconfig.me/ip", "https://api.ipify.org"):
            try:
                resp = requests.get(
                    url,
                    timeout=timeout,
                    proxies=proxies,
                    headers={"User-Agent": "curl/8", "Accept": "text/plain"},
                )
                if resp.status_code == 200:
                    text = resp.text.strip().splitlines()[0].strip()
                    try:
                        ipaddress.ip_address(text)
                        valid = True
                    except ValueError:
                        valid = False
                    if valid:
                        with self._lock:
                            self.ip = text
                            self.last_check = time.time()
                            self.last_error = ""
                        return text
            except requests.RequestException as exc:
                with self._lock:
                    self.last_error = type(exc).__name__
                continue
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
