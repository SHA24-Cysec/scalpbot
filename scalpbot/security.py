from __future__ import annotations

import builtins
import logging
import logging.handlers
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

ALLOWED_LIVE_REST_HOSTS = frozenset({"fapi.binance.com"})
ALLOWED_TESTNET_REST_HOSTS = frozenset({"testnet.binancefuture.com", "demo-fapi.binance.com"})
ALLOWED_LIVE_WS_HOSTS = frozenset({"fstream.binance.com"})
ALLOWED_TESTNET_WS_HOSTS = frozenset({"fstream.binancefuture.com", "demo-fstream.binance.com"})
ALLOWED_IP_HOSTS = frozenset({"api.ipify.org", "ifconfig.me"})
ALLOWED_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})

_SENSITIVE_LABEL = r"[A-Za-z0-9_.-]*(?:api[_-]?key|secret|token|password|passwd|signature|listen[_-]?key|authorization)[A-Za-z0-9_.-]*"
_QUOTED_SECRET = re.compile(
    r"(?i)(?P<prefix>\b" + _SENSITIVE_LABEL + r"\s*[:=]\s*)(?P<q>['\"])(?P<value>.*?)(?P=q)"
)
_UNQUOTED_SECRET = re.compile(
    r"(?i)(?P<prefix>\b" + _SENSITIVE_LABEL + r"\s*[:=]\s*)(?!['\"])(?P<value>[^\s,;&]+)"
)
_AUTH_ASSIGN = re.compile(
    r"(?i)(?P<prefix>\b(?:proxy-)?authorization\s*[:=]\s*)(?P<value>[^\r\n]+)"
)
_PROXY_CREDENTIALS = re.compile(
    r"(?i)(?P<prefix>(?:https?|socks5h?)://)(?P<user>[^/\s:@]+):(?P<password>[^\s/]+)@(?P<host>[^/\s]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_JWT = re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9]{64,}\b")
_OSC = re.compile(r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)")
_ANSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def sanitize_control(text: str) -> str:
    return _CONTROL.sub("", _ANSI.sub("", _OSC.sub("", str(text))))


def redact(text: str) -> str:
    out = str(text)
    out = _QUOTED_SECRET.sub(lambda m: m.group("prefix") + m.group("q") + "***" + m.group("q"), out)
    out = _BEARER.sub("Bearer ***", out)
    out = _AUTH_ASSIGN.sub(lambda m: m.group("prefix") + "***", out)
    out = _UNQUOTED_SECRET.sub(lambda m: m.group("prefix") + "***", out)
    out = _PROXY_CREDENTIALS.sub(lambda m: m.group("prefix") + "***:***@" + m.group("host"), out)
    out = _JWT.sub("***", out)
    out = _LONG_TOKEN.sub("***", out)
    return sanitize_control(out)


def clean_message(text: str) -> str:
    return redact(text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        clone = logging.makeLogRecord(record.__dict__)
        clone.msg = clean_message(record.getMessage())
        clone.message = clone.msg
        clone.args = ()
        clone.exc_info = None
        clone.exc_text = None
        clone.stack_info = None
        return super().format(clone)


def clean_multiline(text: str) -> str:
    normalized = str(text).replace("\r\n", "\n").replace("\r", "")
    return "\n".join(clean_message(line) for line in normalized.split("\n"))


def safe_print(*values: object, **kwargs: Any) -> None:
    cleaned = [clean_multiline(v) if isinstance(v, str) else v for v in values]
    if isinstance(kwargs.get("sep"), str):
        kwargs["sep"] = clean_multiline(kwargs["sep"])
    if isinstance(kwargs.get("end"), str):
        kwargs["end"] = clean_multiline(kwargs["end"])
    builtins.print(*cleaned, **kwargs)


class PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    def _open(self):
        path = Path(self.baseFilename)
        ensure_private_dir(path.parent)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        if os.name == "nt":
            flags |= getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags, 0o600)
        ensure_private_file(path)
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)

    def doRollover(self) -> None:
        super().doRollover()
        for i in range(1, max(1, self.backupCount) + 1):
            ensure_private_file(Path(f"{self.baseFilename}.{i}"))


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def ensure_private_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def resolve_under(raw: str | Path, base: str | Path | None = None) -> Path:
    if sanitize_control(str(raw)) != str(raw):
        raise ValueError(f"path memuat karakter kontrol: {raw}")
    root = Path.cwd() if base is None else Path(base).resolve()
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path di luar direktori kerja: {raw}")
    return resolved


def validate_url(
    url: str,
    label: str,
    scheme: str,
    allowed_hosts: frozenset[str],
    allow_path: bool = False,
) -> None:
    if sanitize_control(url) != url:
        raise ValueError(f"{label} memuat karakter kontrol")
    parsed = urlparse(url)
    if parsed.scheme.lower() != scheme:
        raise ValueError(f"{label} harus {scheme}")
    host = (parsed.hostname or "").lower()
    if not host or host not in allowed_hosts:
        raise ValueError(f"{label} host tidak diizinkan: {host or '-'}")
    if parsed.username or parsed.password or parsed.port is not None:
        raise ValueError(f"{label} tidak boleh memuat kredensial atau port")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{label} tidak boleh memuat query atau fragment")
    if not allow_path and parsed.path not in ("", "/"):
        raise ValueError(f"{label} tidak boleh memuat path: {parsed.path}")


_LISTEN_KEY = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def harden_session(session: Any, proxy: str = "") -> None:
    session.trust_env = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
    if ca_bundle and sanitize_control(ca_bundle) == ca_bundle:
        session.verify = ca_bundle


def validate_listen_key(key: str) -> str:
    raw = str(key)
    if sanitize_control(raw) != raw or not _LISTEN_KEY.fullmatch(raw):
        raise ValueError("listenKey tidak valid")
    return raw


def parse_proxy(proxy: str) -> dict[str, Any]:
    raw = proxy if "://" in proxy else f"http://{proxy}"
    if sanitize_control(raw) != raw:
        raise ValueError("proxy memuat karakter kontrol")
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "http").lower()
    if scheme not in ALLOWED_PROXY_SCHEMES:
        raise ValueError(f"skema proxy tidak didukung: {scheme}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("proxy tanpa hostname")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("proxy tidak boleh memuat path, query, atau fragment")
    try:
        port = parsed.port or (1080 if scheme.startswith("socks5") else 80)
    except ValueError as exc:
        raise ValueError("port proxy tidak valid") from exc
    if not 1 <= port <= 65535:
        raise ValueError("port proxy harus 1..65535")
    username = unquote(parsed.username) if parsed.username else ""
    password = unquote(parsed.password) if parsed.password else ""
    if sanitize_control(username) != username or sanitize_control(password) != password:
        raise ValueError("kredensial proxy memuat karakter kontrol")
    netloc = ""
    if parsed.username:
        netloc += parsed.username
        if parsed.password is not None:
            netloc += ":" + parsed.password
        netloc += "@"
    netloc += host + f":{port}"
    normalized = f"{scheme}://{netloc}"
    return {
        "raw": raw,
        "normalized": normalized,
        "host": host,
        "port": port,
        "type": "socks5" if scheme.startswith("socks5") else "http",
        "username": username,
        "password": password,
    }
