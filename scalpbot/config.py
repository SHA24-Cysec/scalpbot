from __future__ import annotations

import importlib.util
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints
from urllib.parse import urlparse

from .security import (
    ALLOWED_IP_HOSTS,
    ALLOWED_LIVE_REST_HOSTS,
    ALLOWED_LIVE_WS_HOSTS,
    ALLOWED_TESTNET_REST_HOSTS,
    ALLOWED_TESTNET_WS_HOSTS,
    parse_proxy,
    resolve_under,
    validate_url,
)

INTERVALS = {
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h", "1d",
}
MODES = {"testnet", "live", "paper"}
DISCOVERY_MODES = {"static", "top_volume"}
MARGIN_TYPES = {"ISOLATED", "CROSSED"}
SL_MODES = {"atr", "pac", "percent"}
ALLOWED_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")

SYMBOL_ALIASES = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "XRP": "XRPUSDT", "DOGE": "DOGEUSDT", "BNB": "BNBUSDT",
    "ADA": "ADAUSDT", "LINK": "LINKUSDT", "AVAX": "AVAXUSDT",
}


class ConfigError(ValueError):
    pass


@dataclass
class ApiConfig:
    key_env: str = "BINANCE_API_KEY"
    secret_env: str = "BINANCE_API_SECRET"  # noqa: RUF100, S105
    recv_window_ms: int = 5000
    base_url_testnet: str = "https://testnet.binancefuture.com"
    base_url_live: str = "https://fapi.binance.com"
    ws_testnet: str = "wss://fstream.binancefuture.com"
    ws_live: str = "wss://fstream.binance.com"
    timeout_s: float = 10.0
    max_retries: int = 3


@dataclass
class MarketConfig:
    symbols: list[str] = field(default_factory=lambda: [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    ])
    interval: str = "5m"
    warmup_bars: int = 1000
    discovery: str = "top_volume"
    discovery_top_n: int = 5
    discovery_refresh_s: int = 1800
    discovery_quote_volume_min: float = 50_000_000.0
    discovery_exclude: list[str] = field(default_factory=list)
    discovery_require_affordable: bool = True


@dataclass
class StrategyConfig:
    hi_lo_len: int = 34
    fast_ema: int = 89
    medium_ema: int = 200
    slow_ema: int = 600
    lookback: int = 3
    filter_bw_fractals: bool = False
    use_ha: bool = True
    # Filter rezim: sinyal hanya diterima kalau EMA lambat bergerak cukup jauh
    # ke arah yang sama. 0 berarti filter mati (perilaku lama).
    regime_slope_bars: int = 200
    regime_min_slope_bps: float = 100.0


@dataclass
class RiskConfig:
    risk_pct: float = 1.5
    leverage: int = 10
    margin_type: str = "ISOLATED"
    max_open_positions: int = 5
    max_open_per_symbol: int = 1
    max_notional_frac_equity: float = 3.0
    min_notional_bump: bool = True
    max_bump_factor: float = 2.0
    daily_loss_limit_pct: float = 5.0
    sl_mode: str = "atr"
    atr_len: int = 14
    sl_atr_mult: float = 5.0
    sl_percent: float = 0.35
    sl_min_bps: float = 8.0
    sl_max_bps: float = 250.0
    tp_rr: float = 4.0
    be_trigger_r: float = 1.0
    be_buffer_bps: float = 2.0
    trail_trigger_r: float = 2.5
    trail_atr_mult: float = 1.2
    trail_step_bps: float = 5.0
    fee_rate: float = 0.0004
    slippage_bps: float = 1.0
    cooldown_bars: int = 1
    max_hold_minutes: int = 0
    adopt_existing: bool = True


@dataclass
class OrderbookConfig:
    enabled: bool = True
    depth_limit: int = 20
    min_imbalance: float = 0.05
    max_spread_bps: float = 5.0
    periodic_snapshot_s: int = 0
    max_age_s: float = 15.0


@dataclass
class ExecutionConfig:
    working_type: str = "MARK_PRICE"
    place_sl: bool = True
    place_tp: bool = True
    position_poll_s: int = 5
    reconcile_s: int = 30
    cancel_replace_min_s: float = 2.0
    max_slippage_bps: float = 25.0


@dataclass
class NetConfig:
    ip_check: bool = True
    ip_check_interval_s: int = 600
    ip_url: str = "https://api.ipify.org"
    ip_fallback: bool = False
    proxy: str = ""


@dataclass
class DashboardConfig:
    enabled: bool = True
    refresh_ms: int = 500
    log_lines: int = 8
    history_lines: int = 5
    min_width: int = 72
    min_height: int = 22


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/scalpbot.log"
    max_bytes: int = 2_000_000
    backups: int = 3
    events_file: str = "logs/events.jsonl"
    state_file: str = "logs/state.json"
    persist_state: bool = True
    timezone: str = "WIB"
    mode_subdir: bool = True

    def file_for(self, mode: str) -> str:
        return _mode_path(self.file, mode, self.mode_subdir)

    def events_for(self, mode: str) -> str:
        return _mode_path(self.events_file, mode, self.mode_subdir)

    def state_for(self, mode: str) -> str:
        return _mode_path(self.state_file, mode, self.mode_subdir)


def _mode_path(path: str, mode: str, use_subdir: bool) -> str:
    p = Path(str(path))
    if not use_subdir:
        return str(p)
    tag = str(mode).strip().lower()
    if tag not in MODES:
        raise ConfigError(f"mode tidak valid untuk folder log: {mode!r}")
    return str(p.parent / tag / p.name)


@dataclass
class Config:
    mode: str = "testnet"
    api: ApiConfig = field(default_factory=ApiConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    orderbook: OrderbookConfig = field(default_factory=OrderbookConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    net: NetConfig = field(default_factory=NetConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def base_url(self) -> str:
        return self.api.base_url_live if self.mode == "live" else self.api.base_url_testnet

    def ws_url(self) -> str:
        return self.api.ws_live if self.mode == "live" else self.api.ws_testnet

    def api_key(self) -> str:
        return os.environ.get(self.api.key_env, "").strip()

    def api_secret(self) -> str:
        return os.environ.get(self.api.secret_env, "").strip()

    def needs_credentials(self) -> bool:
        return self.mode in {"testnet", "live"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_urls(cfg: Config) -> None:
    try:
        validate_url(cfg.api.base_url_live, "api.base_url_live", "https", ALLOWED_LIVE_REST_HOSTS)
        validate_url(
            cfg.api.base_url_testnet, "api.base_url_testnet", "https", ALLOWED_TESTNET_REST_HOSTS
        )
        validate_url(cfg.api.ws_live, "api.ws_live", "wss", ALLOWED_LIVE_WS_HOSTS)
        validate_url(cfg.api.ws_testnet, "api.ws_testnet", "wss", ALLOWED_TESTNET_WS_HOSTS)
        validate_url(cfg.net.ip_url, "net.ip_url", "https", ALLOWED_IP_HOSTS, allow_path=True)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def assert_mode_hosts(cfg: Config) -> None:
    validate_urls(cfg)
    rest = (urlparse(cfg.base_url()).hostname or "").lower()
    ws = (urlparse(cfg.ws_url()).hostname or "").lower()
    if cfg.mode == "live":
        if rest not in ALLOWED_LIVE_REST_HOSTS or ws not in ALLOWED_LIVE_WS_HOSTS:
            raise ConfigError("mode live wajib memakai host live resmi")
    elif rest not in ALLOWED_TESTNET_REST_HOSTS or ws not in ALLOWED_TESTNET_WS_HOSTS:
        raise ConfigError("mode testnet/paper tidak boleh memakai host live")


_SECTIONS = {
    "api": ApiConfig,
    "market": MarketConfig,
    "strategy": StrategyConfig,
    "risk": RiskConfig,
    "orderbook": OrderbookConfig,
    "execution": ExecutionConfig,
    "net": NetConfig,
    "dashboard": DashboardConfig,
    "logging": LoggingConfig,
}


def _norm_symbol(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ConfigError(f"simbol harus string, ditemukan {type(raw).__name__}")
    sym = raw.strip().upper()
    if not sym:
        raise ConfigError("simbol kosong")
    if sym not in SYMBOL_ALIASES and not SYMBOL_RE.match(sym):
        raise ConfigError(f"simbol tidak valid: {raw!r}")
    return SYMBOL_ALIASES.get(sym, sym)


_TYPE_HINTS: dict[Any, dict[str, Any]] = {}


def _hints(cls: Any) -> dict[str, Any]:
    if cls not in _TYPE_HINTS:
        _TYPE_HINTS[cls] = get_type_hints(cls)
    return _TYPE_HINTS[cls]


def _coerce(section: str, key: str, expected: Any, value: Any) -> Any:
    label = f"{section}.{key}"
    if expected is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{label} harus boolean")
        return value
    if expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{label} harus integer")
        return value
    if expected is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{label} harus angka")
        out = float(value)
        if not math.isfinite(out):
            raise ConfigError(f"{label} harus angka hingga (bukan NaN/Infinity)")
        return out
    if expected is str:
        if not isinstance(value, str):
            raise ConfigError(f"{label} harus string")
        return value
    if get_origin(expected) is list:
        if not isinstance(value, list):
            raise ConfigError(f"{label} harus array")
        args = get_args(expected)
        if args and args[0] is str and not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{label} harus array string")
        return value
    return value


def _bad_constant(value: str) -> None:
    raise ConfigError(f"JSON mengandung nilai tidak valid: {value}")


def _build_section(name: str, cls: Any, data: Any) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"bagian '{name}' harus object JSON")
    valid = {f.name for f in fields(cls)}
    unknown = set(data) - valid
    if unknown:
        raise ConfigError(f"kunci tidak dikenal di '{name}': {sorted(unknown)}")
    hints = _hints(cls)
    clean = {k: _coerce(name, k, hints.get(k, Any), v) for k, v in data.items()}
    return cls(**clean)


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"file config tidak ditemukan: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"), parse_constant=_bad_constant)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"JSON tidak valid pada {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config harus object JSON")

    known = {"mode", *_SECTIONS}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"kunci top-level tidak dikenal: {sorted(unknown)}")

    cfg = Config()
    mode = raw.get("mode", cfg.mode)
    if not isinstance(mode, str):
        raise ConfigError("mode harus string")
    cfg.mode = mode.strip().lower()
    for name, cls in _SECTIONS.items():
        if name in raw:
            setattr(cfg, name, _build_section(name, cls, raw[name]))

    cfg.market.symbols = [_norm_symbol(s) for s in cfg.market.symbols]
    cfg.market.symbols = list(dict.fromkeys(cfg.market.symbols))
    cfg.market.discovery = str(cfg.market.discovery).strip().lower()
    cfg.market.discovery_exclude = [
        _norm_symbol(s) for s in cfg.market.discovery_exclude
    ]
    cfg.market.discovery_exclude = list(dict.fromkeys(cfg.market.discovery_exclude))
    cfg.market.interval = str(cfg.market.interval).strip().lower()
    cfg.risk.margin_type = str(cfg.risk.margin_type).strip().upper()
    cfg.risk.sl_mode = str(cfg.risk.sl_mode).strip().lower()
    cfg.execution.working_type = str(cfg.execution.working_type).strip().upper()
    cfg.logging.level = str(cfg.logging.level).strip().upper()
    cfg.logging.timezone = str(cfg.logging.timezone).strip()
    # Validasi lebih awal supaya salah tulis ketahuan saat load, bukan saat bot jalan.
    from .timeutil import TimezoneError, parse_timezone

    try:
        parse_timezone(cfg.logging.timezone)
    except TimezoneError as exc:
        raise ConfigError(f"logging.timezone: {exc}") from exc
    return cfg


def validate_config(cfg: Config, live_requested: bool = False) -> list[str]:
    warnings: list[str] = []
    if cfg.mode not in MODES:
        raise ConfigError(f"mode harus salah satu dari {sorted(MODES)}")
    if live_requested and cfg.mode != "live":
        raise ConfigError("flag --live dipakai tapi config mode bukan 'live'")
    if cfg.mode == "live" and not live_requested:
        raise ConfigError("mode live butuh flag --live dan konfirmasi eksplisit")
    assert_mode_hosts(cfg)

    if cfg.market.discovery not in DISCOVERY_MODES:
        raise ConfigError(f"market.discovery harus {sorted(DISCOVERY_MODES)}")
    if not cfg.market.symbols and cfg.market.discovery == "static":
        raise ConfigError("market.symbols kosong")
    if len(cfg.market.symbols) > 25 and cfg.market.discovery == "static":
        raise ConfigError("market.symbols maksimal 25")
    if not 1 <= cfg.market.discovery_top_n <= 25:
        raise ConfigError("market.discovery_top_n harus 1..25")
    if cfg.market.discovery_refresh_s < 300:
        raise ConfigError("market.discovery_refresh_s minimal 300 detik")
    if cfg.market.discovery_quote_volume_min < 0:
        raise ConfigError("market.discovery_quote_volume_min tidak boleh negatif")
    if cfg.market.discovery_top_n > 10:
        warnings.append("discovery_top_n > 10 memperbanyak stream dan beban rate limit")
    if cfg.market.interval not in INTERVALS:
        raise ConfigError(f"interval tidak didukung: {cfg.market.interval}")
    if not 200 <= cfg.market.warmup_bars <= 1500:
        raise ConfigError("warmup_bars harus 200..1500 (batas klines Binance)")

    s = cfg.strategy
    if min(s.hi_lo_len, s.fast_ema, s.medium_ema, s.slow_ema) < 2:
        raise ConfigError("panjang EMA/PAC minimal 2")
    if s.lookback < 1:
        raise ConfigError("strategy.lookback minimal 1")
    if not (s.hi_lo_len < s.fast_ema < s.medium_ema < s.slow_ema):
        warnings.append("urutan EMA tidak naik (PAC < fast < medium < slow), sinyal bisa tidak wajar")
    if s.regime_slope_bars < 10:
        raise ConfigError("strategy.regime_slope_bars minimal 10")
    if not 0.0 <= s.regime_min_slope_bps <= 1000.0:
        raise ConfigError("strategy.regime_min_slope_bps harus 0..1000 (0 = filter mati)")
    if s.regime_min_slope_bps > 0 and s.regime_slope_bars + s.slow_ema > 1500:
        warnings.append(
            "regime_slope_bars + slow_ema melebihi batas klines (1500 bar); filter rezim butuh warmup panjang"
        )

    r = cfg.risk
    if not 0.01 <= r.risk_pct <= 100.0:
        raise ConfigError("risk.risk_pct harus 0.01..100")
    if not 1 <= r.leverage <= 50:
        raise ConfigError("risk.leverage harus 1..50")
    if r.margin_type not in MARGIN_TYPES:
        raise ConfigError(f"risk.margin_type harus {sorted(MARGIN_TYPES)}")
    if r.max_open_positions < 1:
        raise ConfigError("risk.max_open_positions minimal 1")
    if r.max_open_per_symbol < 1:
        raise ConfigError("risk.max_open_per_symbol minimal 1")
    if not 0.1 <= r.max_notional_frac_equity <= 20:
        raise ConfigError("risk.max_notional_frac_equity harus 0.1..20")
    if r.sl_mode not in SL_MODES:
        raise ConfigError(f"risk.sl_mode harus {sorted(SL_MODES)}")
    if r.atr_len < 2:
        raise ConfigError("risk.atr_len minimal 2")
    if not 0.2 <= r.sl_atr_mult <= 10:
        raise ConfigError("risk.sl_atr_mult harus 0.2..10")
    if not 0.05 <= r.sl_percent <= 10:
        raise ConfigError("risk.sl_percent harus 0.05..10")
    if not (0 < r.sl_min_bps < r.sl_max_bps <= 2000):
        raise ConfigError("butuh 0 < sl_min_bps < sl_max_bps <= 2000")
    if not 0.2 <= r.tp_rr <= 10:
        raise ConfigError("risk.tp_rr harus 0.2..10")
    if not 0 <= r.be_trigger_r <= 10:
        raise ConfigError("risk.be_trigger_r harus 0..10")
    if not 0 <= r.trail_trigger_r <= 20:
        raise ConfigError("risk.trail_trigger_r harus 0..20")
    if not 0 <= r.trail_atr_mult <= 10:
        raise ConfigError("risk.trail_atr_mult harus 0..10")
    if r.trail_trigger_r > 0 and r.trail_atr_mult <= 0:
        raise ConfigError("trail_atr_mult harus > 0 bila trailing aktif")
    if not 0 <= r.fee_rate <= 0.002:
        raise ConfigError("risk.fee_rate harus 0..0.002 (0.2%)")
    if not 0 <= r.slippage_bps <= 100:
        raise ConfigError("risk.slippage_bps harus 0..100")
    if r.cooldown_bars < 0:
        raise ConfigError("risk.cooldown_bars tidak boleh negatif")
    if not isinstance(r.adopt_existing, bool):
        raise ConfigError("risk.adopt_existing harus true/false")
    if r.max_hold_minutes < 0:
        raise ConfigError("risk.max_hold_minutes tidak boleh negatif")

    if not 5 <= cfg.orderbook.depth_limit <= 500:
        raise ConfigError("orderbook.depth_limit harus 5..500")
    if not 0 <= cfg.orderbook.min_imbalance <= 0.95:
        raise ConfigError("orderbook.min_imbalance harus 0..0.95")
    if cfg.orderbook.max_spread_bps <= 0:
        raise ConfigError("orderbook.max_spread_bps harus > 0")

    if cfg.execution.working_type not in {"MARK_PRICE", "CONTRACT_PRICE"}:
        raise ConfigError("execution.working_type harus MARK_PRICE atau CONTRACT_PRICE")
    if cfg.execution.position_poll_s < 1 or cfg.execution.position_poll_s > 60:
        raise ConfigError("execution.position_poll_s harus 1..60")
    if cfg.execution.reconcile_s < 5:
        raise ConfigError("execution.reconcile_s minimal 5")
    if cfg.execution.cancel_replace_min_s < 1:
        raise ConfigError("execution.cancel_replace_min_s minimal 1")

    if cfg.net.ip_check_interval_s < 60:
        raise ConfigError("net.ip_check_interval_s minimal 60 detik")
    if not isinstance(cfg.net.ip_fallback, bool):
        raise ConfigError("net.ip_fallback harus true/false")

    a = cfg.api
    if not 0 < a.timeout_s <= 120:
        raise ConfigError("api.timeout_s harus lebih dari 0 dan maksimal 120 detik")
    if not 1 <= a.max_retries <= 10:
        raise ConfigError("api.max_retries harus 1..10")
    if not 1 <= a.recv_window_ms <= 60_000:
        raise ConfigError("api.recv_window_ms harus 1..60000")
    if not a.key_env.strip() or not a.secret_env.strip():
        raise ConfigError("nama env API key/secret tidak boleh kosong")
    if cfg.net.proxy:
        try:
            proxy = parse_proxy(cfg.net.proxy)
        except ValueError as exc:
            raise ConfigError(f"net.proxy: {exc}") from exc
        cfg.net.proxy = proxy["normalized"]
        if proxy["type"] == "socks5" and importlib.util.find_spec("socks") is None:
            raise ConfigError("proxy SOCKS5 butuh PySocks; install requirements.txt lalu coba lagi")
    if cfg.net.ip_check_interval_s > 86_400:
        raise ConfigError("net.ip_check_interval_s maksimal 86400 detik")
    if cfg.orderbook.periodic_snapshot_s < 0:
        raise ConfigError("orderbook.periodic_snapshot_s tidak boleh negatif")
    if cfg.orderbook.max_age_s <= 0:
        raise ConfigError("orderbook.max_age_s harus > 0")
    if cfg.execution.reconcile_s > 3600:
        raise ConfigError("execution.reconcile_s maksimal 3600 detik")
    if cfg.execution.cancel_replace_min_s > 60:
        raise ConfigError("execution.cancel_replace_min_s maksimal 60 detik")
    if cfg.execution.max_slippage_bps <= 0:
        raise ConfigError("execution.max_slippage_bps harus > 0")
    if cfg.dashboard.refresh_ms < 200:
        raise ConfigError("dashboard.refresh_ms minimal 200")
    if not 1 <= cfg.dashboard.log_lines <= 50:
        raise ConfigError("dashboard.log_lines harus 1..50")
    if not 0 <= cfg.dashboard.history_lines <= 20:
        raise ConfigError("dashboard.history_lines harus 0..20")
    if not isinstance(cfg.logging.persist_state, bool):
        raise ConfigError("logging.persist_state harus true/false")
    if not str(cfg.logging.state_file).strip():
        raise ConfigError("logging.state_file tidak boleh kosong")
    if not 40 <= cfg.dashboard.min_width <= 300:
        raise ConfigError("dashboard.min_width harus 40..300")
    if not 12 <= cfg.dashboard.min_height <= 100:
        raise ConfigError("dashboard.min_height harus 12..100")
    if cfg.logging.max_bytes < 1024:
        raise ConfigError("logging.max_bytes minimal 1024")
    if not 0 <= cfg.logging.backups <= 20:
        raise ConfigError("logging.backups harus 0..20")
    if not cfg.logging.file.strip() or not cfg.logging.events_file.strip():
        raise ConfigError("path logging.file/events_file tidak boleh kosong")
    paths = (
        ("logging.file", cfg.logging.file),
        ("logging.events_file", cfg.logging.events_file),
        ("logging.state_file", cfg.logging.state_file),
        ("logging.file_for", cfg.logging.file_for(cfg.mode)),
        ("logging.events_for", cfg.logging.events_for(cfg.mode)),
        ("logging.state_for", cfg.logging.state_for(cfg.mode)),
    )
    for label, raw in paths:
        try:
            resolve_under(raw)
        except (ValueError, OSError) as exc:
            raise ConfigError(f"{label}: {exc}") from exc
    log_file = Path(cfg.logging.file_for(cfg.mode))
    log_events = Path(cfg.logging.events_for(cfg.mode))
    if log_file == log_events:
        raise ConfigError("logging.file dan logging.events_file tidak boleh sama")
    if log_file.name in {"", ".", ".."} or log_events.name in {"", ".", ".."}:
        raise ConfigError("nama file log tidak valid")
    if cfg.logging.level not in ALLOWED_LEVELS:
        raise ConfigError(f"logging.level harus {sorted(ALLOWED_LEVELS)}")
    if r.be_buffer_bps < 0 or r.be_buffer_bps > 100:
        raise ConfigError("risk.be_buffer_bps harus 0..100")
    if r.trail_step_bps < 0 or r.trail_step_bps > 100:
        raise ConfigError("risk.trail_step_bps harus 0..100")
    if r.daily_loss_limit_pct < 0 or r.daily_loss_limit_pct > 50:
        raise ConfigError("risk.daily_loss_limit_pct harus 0..50")
    if not 1 <= r.max_bump_factor <= 10:
        raise ConfigError("risk.max_bump_factor harus 1..10")
    if r.max_open_per_symbol > r.max_open_positions:
        warnings.append("max_open_per_symbol > max_open_positions, batas global yang berlaku")
    if cfg.market.warmup_bars < cfg.strategy.slow_ema + 20:
        warnings.append("warmup_bars lebih kecil dari slow_ema + 20, bot akan menambah bar otomatis")
    if s.regime_min_slope_bps > 0:
        need = s.slow_ema + s.regime_slope_bars + 8
        if need > 1500:
            warnings.append(
                f"filter rezim butuh {need} bar warmup, melebihi batas klines 1500; "
                f"turunkan slow_ema atau regime_slope_bars"
            )
        elif cfg.market.warmup_bars < need:
            warnings.append(
                f"warmup_bars {cfg.market.warmup_bars} < kebutuhan filter rezim {need}, "
                f"bot menambah bar otomatis"
            )

    if cfg.mode in {"testnet", "live"} and not cfg.api_key():
        warnings.append(f"env {cfg.api.key_env} belum diset")
    if cfg.mode in {"testnet", "live"} and not cfg.api_secret():
        warnings.append(f"env {cfg.api.secret_env} belum diset")
    if cfg.api.recv_window_ms > 10000:
        warnings.append("recv_window_ms besar memperlebar jendela replay request")
    if r.risk_pct > 3:
        warnings.append("risk_pct > 3% per trade sangat agresif untuk modal kecil")
    if r.risk_pct >= 25:
        warnings.append(
            f"risk_pct {r.risk_pct:g}%: sekitar {max(1, int(100 // max(1, r.risk_pct)))} SL beruntun "
            f"menghabiskan akun, dan batas notional/leverage bisa memotong qty sebelum SL tercapai"
        )
    if r.leverage > 20:
        warnings.append("leverage > 20x rentan likuidasi pada scalping")
    if cfg.mode == "live":
        warnings.append("MODE LIVE: order nyata akan dikirim ke Binance")
    return warnings


def confirmation_from_env() -> bool:
    return os.environ.get("SCALPBOT_LIVE_CONFIRM", "").strip().upper() == "YES-LIVE"
