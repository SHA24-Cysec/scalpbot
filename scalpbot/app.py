from __future__ import annotations

import asyncio
import importlib.util
import itertools
import json
import logging
import logging.handlers
import math
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import websocket

from .broker import PaperBroker, TestnetBroker
from .config import Config, confirmation_from_env
from .dashboard import Dashboard
from .discovery import Selection, select_symbols
from .events import Event, EventLog, EventLogHandler
from .state import StateStore, resolve_path
from .timeutil import tz_label
from .exchange import BinanceError, BinanceFuturesClient, safe_float
from .indicators import Candle
from .netinfo import IpDetector
from .orderbook import OrderbookFilter
from .risk import SymbolFilters
from .strategy import ScalpPullbackStrategy
from .trader import Trader

log = logging.getLogger("scalpbot.app")

INTERVAL_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400,
}


def open_bar_start_ms(now_ms: int, interval_seconds: int) -> int:
    width = max(1, interval_seconds) * 1000
    return (now_ms // width) * width


def ws_proxy_kwargs(proxy: str) -> dict[str, Any]:
    if not proxy:
        return {}
    raw = proxy if "://" in proxy else f"http://{proxy}"
    parsed = urlparse(raw)
    if not parsed.hostname:
        return {}
    scheme = (parsed.scheme or "http").lower()
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        return {}
    proxy_type = "socks5" if scheme.startswith("socks5") else "http"
    if proxy_type == "socks5" and importlib.util.find_spec("socks") is None:
        return {}
    kwargs: dict[str, Any] = {
        "http_proxy_host": parsed.hostname,
        "http_proxy_port": parsed.port or (1080 if proxy_type == "socks5" else 80),
        "proxy_type": proxy_type,
    }
    if parsed.username:
        kwargs["http_proxy_auth"] = (parsed.username, parsed.password or "")
    return kwargs


class WsThread(threading.Thread):
    def __init__(
        self,
        name: str,
        url_provider: Callable[[], Optional[str]],
        on_message: Callable[[dict], None],
        on_connect: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        ping_interval: float = 20.0,
        proxy: str = "",
    ) -> None:
        super().__init__(name=name, daemon=True)
        self._url_provider = url_provider
        self._on_message = on_message
        self._on_connect = on_connect
        self._on_error = on_error
        self._stop = threading.Event()
        self._commands: Queue[dict] = Queue(maxsize=64)
        self._send_lock = threading.Lock()
        self._ws: Optional[websocket.WebSocketApp] = None
        self._last_msg = 0.0
        self._connected = False
        self._ping_interval = ping_interval
        self._proxy_kwargs = ws_proxy_kwargs(proxy)

    def send(self, payload: dict) -> bool:
        ws = self._ws
        if self._connected and ws is not None:
            try:
                with self._send_lock:
                    ws.send(json.dumps(payload))
                return True
            except Exception as exc:
                log.debug("%s command send gagal: %s", self.name, exc)
        try:
            self._commands.put_nowait(payload)
        except Exception:
            return False
        return False

    def age(self) -> float:
        return time.time() - self._last_msg if self._last_msg else float("inf")

    @property
    def connected(self) -> bool:
        return self._connected

    def stop(self) -> None:
        self._stop.set()
        self._close_ws()

    def reconnect(self) -> None:
        self._close_ws()

    def _close_ws(self) -> None:
        ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    def _clear_commands(self) -> None:
        while True:
            try:
                self._commands.get_nowait()
            except Empty:
                return

    def _flush_commands(self, ws: websocket.WebSocketApp) -> None:
        while True:
            try:
                payload = self._commands.get_nowait()
            except Empty:
                return
            try:
                ws.send(json.dumps(payload))
            except Exception as exc:
                log.debug("%s flush command gagal: %s", self.name, exc)
                return

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            url = self._url_provider()
            if not url:
                time.sleep(1.5)
                continue
            self._clear_commands()
            ws = websocket.WebSocketApp(
                url,
                on_open=self._handle_open,
                on_message=self._handle_message,
                on_error=self._handle_error,
                on_close=self._handle_close,
            )
            self._ws = ws
            connected_at = time.time()
            try:
                ws.run_forever(
                    ping_interval=self._ping_interval,
                    ping_timeout=10,
                    skip_utf8_validation=True,
                    **self._proxy_kwargs,
                )
            except Exception as exc:
                if self._on_error:
                    self._on_error(f"{self.name} crash: {type(exc).__name__}")
            connected_for = time.time() - connected_at
            self._connected = False
            self._ws = None
            if self._stop.is_set():
                break
            if connected_for > 30.0:
                backoff = 1.0
            time.sleep(backoff)
            backoff = min(30.0, backoff * 1.8)

    def _handle_open(self, ws: websocket.WebSocketApp) -> None:
        self._connected = True
        self._last_msg = time.time()
        self._flush_commands(ws)
        if self._on_connect:
            self._on_connect()

    def _handle_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        self._last_msg = time.time()
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return
        payload = data.get("data") if isinstance(data, dict) and "data" in data else data
        if not isinstance(payload, dict) or not payload:
            return
        try:
            self._on_message(payload)
        except Exception as exc:
            log.debug("handler error: %s", exc)

    def _handle_error(self, ws: websocket.WebSocketApp, error: Any) -> None:
        if self._on_error:
            self._on_error(f"{self.name}: {error}")

    def _handle_close(self, ws: websocket.WebSocketApp, code: Any, msg: Any) -> None:
        self._connected = False
        self._last_msg = 0.0


@dataclass
class SymbolRuntime:
    symbol: str
    strategy: ScalpPullbackStrategy
    filters: SymbolFilters
    last_bar_ts: int = 0
    last_signal: str = "-"
    last_signal_ts: float = 0.0
    last_price: float = 0.0
    bars: int = 0
    feed_errors: int = 0


class ScalpBotApp:
    def __init__(self, cfg: Config, paper_balance: float = 50.0) -> None:
        self.cfg = cfg
        self.dashboard = Dashboard(
            cfg.dashboard, live=cfg.mode == "live", timezone_name=cfg.logging.timezone
        )
        self.log_file = cfg.logging.file_for(cfg.mode)
        self.events_file = cfg.logging.events_for(cfg.mode)
        self.events = EventLog(
            capacity=500,
            file_path=self.events_file,
            max_bytes=cfg.logging.max_bytes,
            console=not self.dashboard.enabled,
            mode=cfg.mode,
        )
        self.interval_seconds = INTERVAL_SECONDS[cfg.market.interval]
        self.client = BinanceFuturesClient(cfg)
        self.filters: dict[str, SymbolFilters] = {}
        self.symbols: dict[str, SymbolRuntime] = {}
        self.paper_balance = paper_balance
        self.broker: Optional[TestnetBroker | PaperBroker] = None
        self.trader: Optional[Trader] = None
        self.state_file = ""
        self.book: Optional[OrderbookFilter] = None
        self.ip = IpDetector(cfg.net.ip_url, cfg.net.ip_check_interval_s, cfg.net.proxy)
        self.started_at = time.time()
        self._queue: Optional[asyncio.Queue[tuple[str, Any]]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._market_ws: Optional[WsThread] = None
        self._user_ws: Optional[WsThread] = None
        self._listen_key = ""
        self._listen_key_ts = 0.0
        self._listening_marks: set[str] = set()
        self._active_klines: set[str] = set()
        self._url_base_symbols: set[str] = set()
        self._stopping = False
        self._last_discovery = 0.0
        self.discovery_note = ""
        self._last_ws_warn = 0.0
        self._tasks: list[asyncio.Task] = []
        self._universe_lock = threading.RLock()
        self._last_tick_push: dict[str, float] = {}
        self._command_seq = itertools.count(1)

    def setup_logging(self) -> None:
        root = logging.getLogger()
        root.setLevel(getattr(logging, self.cfg.logging.level, logging.INFO))
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        fmt = logging.Formatter(
            "%(asctime)s " + f"[{self.cfg.mode.upper()}] " + "%(levelname)-7s %(name)s | %(message)s",
            "%H:%M:%S",
        )
        self.log_file = self.cfg.logging.file_for(self.cfg.mode)
        self._file_log = logging.getLogger("scalpbot.session")
        for handler in list(self._file_log.handlers):
            self._file_log.removeHandler(handler)
            handler.close()
        self._file_log.propagate = False
        self._file_log.setLevel(logging.INFO)
        if self.log_file:
            path = Path(self.log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=max(1024, self.cfg.logging.max_bytes),
                backupCount=max(0, self.cfg.logging.backups),
                encoding="utf-8",
            )
            fh.setFormatter(fmt)
            root.addHandler(fh)
            self._file_log.addHandler(fh)
        root.addHandler(EventLogHandler(self.events))
        logging.getLogger("websocket").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    def _discovery_equity(self) -> float:
        if self.cfg.mode == "paper":
            return self.paper_balance
        try:
            balance, _ = self.client.usdt_equity()
            return balance
        except BinanceError as exc:
            self.events.add("WARNING", "discovery", f"gagal baca saldo untuk discovery: {exc}")
            return 0.0

    def _select_universe(self, equity: float, force_symbols: Optional[list[str]] = None) -> Selection:
        tickers = self.client.ticker_24hr()
        selection = select_symbols(
            tickers,
            tradable=self.client.tradable_symbols(),
            filters=self.filters,
            exclude=self.cfg.market.discovery_exclude,
            min_quote_volume=self.cfg.market.discovery_quote_volume_min,
            top_n=self.cfg.market.discovery_top_n,
            equity=equity,
            leverage=self.cfg.risk.leverage,
            max_notional_frac=self.cfg.risk.max_notional_frac_equity,
            require_affordable=self.cfg.market.discovery_require_affordable,
        )
        if not selection.symbols and force_symbols:
            self.events.add(
                "WARNING",
                "discovery",
                "tidak ada simbol lolos filter, memakai market.symbols dari config",
            )
            return Selection(symbols=list(force_symbols), candidates=selection.candidates)
        return selection

    def preflight(self) -> None:
        self.client.sync_time()
        self.filters = self.client.load_all_filters()
        if self.cfg.market.discovery == "top_volume":
            equity = self._discovery_equity()
            selection = self._select_universe(equity, force_symbols=self.cfg.market.symbols)
            symbols = selection.symbols
            if not symbols:
                raise BinanceError(-1, "discovery top_volume tidak menghasilkan simbol", 0, "discovery")
            self.cfg.market.symbols = symbols
            self.discovery_note = f"top_volume/{self.cfg.market.discovery_top_n}"
            self.events.add(
                "INFO",
                "discovery",
                (
                    f"top_volume pilih {len(symbols)} simbol: {selection.summary()} "
                    f"(min vol {self.cfg.market.discovery_quote_volume_min / 1_000_000:.0f}M, "
                    f"refresh {self.cfg.market.discovery_refresh_s}s)"
                ),
            )
            for candidate in selection.skipped()[:5]:
                self.events.add(
                    "DEBUG", "discovery", f"lewati {candidate.symbol}: {candidate.status}"
                )
            self._last_discovery = time.time()
        else:
            tradable = set(self.client.tradable_symbols())
            missing = [s for s in self.cfg.market.symbols if s not in tradable]
            if missing:
                raise BinanceError(
                    -1121,
                    (
                        "simbol tidak valid (harus PERPETUAL, TRADING, quote dan margin USDT): "
                        + ", ".join(missing)
                    ),
                    0,
                    "/fapi/v1/exchangeInfo",
                )
        with self._universe_lock:
            for sym in self.cfg.market.symbols:
                self.symbols[sym] = SymbolRuntime(
                    symbol=sym,
                    strategy=ScalpPullbackStrategy(sym, self.cfg.strategy, self.cfg.risk.atr_len),
                    filters=self.filters[sym],
                )
        if self.cfg.mode == "paper":
            self.broker = PaperBroker(
                self.cfg,
                filters_provider=lambda s: self.filters[s],
                price_provider=lambda s: self.trader.price(s) if self.trader else None,
                start_balance=self.paper_balance,
            )
        else:
            self.broker = TestnetBroker(self.client, self.cfg)
            for sym in self.cfg.market.symbols:
                try:
                    self.broker.prepare_symbol(sym)
                except BinanceError as exc:
                    self.events.add("WARNING", "setup", f"{sym} set leverage/margin: {exc}")
        self.book = OrderbookFilter(self.cfg.orderbook, self._fetch_depth)
        self.trader = Trader(
            self.cfg,
            self.broker,
            self.filters,
            self.events,
            self.book,
            self.interval_seconds,
        )
        # State yang bertahan melewati restart: cooldown entry + riwayat entry.
        state_path = resolve_path(self.cfg, self.cfg.mode)
        if state_path:
            store = StateStore(state_path, enabled=True)
            loaded = self.trader.attach_state(store)
            self.state_file = state_path
            self.events.add(
                "INFO",
                "state",
                f"state {'dimuat dari ' + state_path if loaded else 'baru di ' + state_path}",
            )
        else:
            self.state_file = ""
        if isinstance(self.broker, PaperBroker):
            self.trader.equity = self.paper_balance
            self.trader.available = self.paper_balance
            self.trader.day.start_equity = self.paper_balance
            self.trader.day.day = self.trader._today()
        else:
            self.trader.refresh_equity(force=True)
        self.events.add(
            "INFO",
            "boot",
            (
                f"mode={self.cfg.mode} log={self.log_file} events={self.events_file} "
                f"interval={self.cfg.market.interval} "
                f"symbols={len(self.cfg.market.symbols)} ({self.cfg.market.discovery}) "
                f"equity={self.trader.equity:.2f} "
                f"risk={self.cfg.risk.risk_pct}% lev={self.cfg.risk.leverage}x"
            ),
        )

    def _fetch_depth(self, symbol: str, limit: int) -> Optional[dict]:
        try:
            return self.client.depth(symbol, limit) or None
        except BinanceError as exc:
            log.debug("depth %s gagal: %s", symbol, exc)
            return None

    def _warmup_symbol(self, rt: SymbolRuntime) -> None:
        need = min(max(self.cfg.market.warmup_bars, rt.strategy.required_bars()), 1500)
        try:
            candles = self.client.klines(rt.symbol, self.cfg.market.interval, limit=need)
        except BinanceError as exc:
            self.events.add("ERROR", "data", f"{rt.symbol} warmup gagal: {exc}")
            return
        open_ts = open_bar_start_ms(self.client.server_time_ms(), self.interval_seconds)
        closed = [c for c in candles if c.ts < open_ts]
        for c in closed:
            rt.strategy.on_bar(c)
            rt.last_bar_ts = max(rt.last_bar_ts, c.ts)
            rt.bars += 1
        if candles:
            rt.last_price = candles[-1].close
            if self.trader:
                self.trader.set_mark(rt.symbol, rt.last_price)
        bar = rt.strategy.last_bar
        trend = bar.trend if bar else 0
        ready = bar is not None and bar.pac_center is not None and bar.idx >= rt.strategy.warmup_bars()
        self.events.add(
            "INFO",
            "data",
            f"{rt.symbol} warmup {len(closed)} bar, trend={trend}, siap={ready}, price={rt.last_price:.6g}",
        )

    def warmup(self) -> None:
        with self._universe_lock:
            runtimes = list(self.symbols.values())
        for rt in runtimes:
            self._warmup_symbol(rt)

    def _market_streams(self) -> list[str]:
        base = [f"{s.lower()}@kline_{self.cfg.market.interval}" for s in sorted(self.symbols)]
        marks = [f"{s.lower()}@markPrice@1s" for s in sorted(self._listening_marks)]
        return base + marks

    def _market_url(self) -> str:
        with self._universe_lock:
            streams = self._market_streams()
            self._url_base_symbols = set(self.symbols)
            if not streams:
                return ""
            return f"{self.cfg.ws_url()}/stream?streams={'/'.join(streams)}"

    def _push(self, kind: str, payload: Any) -> None:
        loop = self._loop
        queue = self._queue
        if loop and queue is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._try_put, kind, payload)
            except RuntimeError:
                pass

    def _try_put(self, kind: str, payload: Any) -> None:
        queue = self._queue
        if queue is None:
            return
        try:
            queue.put_nowait((kind, payload))
        except asyncio.QueueFull:
            pass

    def _on_market_message(self, payload: dict) -> None:
        if payload.get("e") == "kline" and not (payload.get("k") or {}).get("x"):
            symbol = str(payload.get("s", "")).upper()
            now = time.time()
            if now - self._last_tick_push.get(symbol, 0.0) < 1.0:
                return
            self._last_tick_push[symbol] = now
        self._push("market", payload)

    def _activate_position_symbol(self, symbol: str) -> None:
        """Daftarkan simbol posisi hasil adopsi supaya markPrice dan kline-nya ikut di-stream."""
        flt = self.filters.get(symbol)
        if flt is None:
            try:
                flt = self.client.filters(symbol)
            except BinanceError as exc:
                self.events.add("WARNING", "adopt", f"{symbol} filter gagal dibaca: {exc}")
                return
            self.filters[symbol] = flt
        rt = SymbolRuntime(
            symbol=symbol,
            strategy=ScalpPullbackStrategy(symbol, self.cfg.strategy, self.cfg.risk.atr_len),
            filters=flt,
        )
        self._warmup_symbol(rt)
        with self._universe_lock:
            self.symbols[symbol] = rt
        self.events.add("INFO", "adopt", f"{symbol} ditambahkan ke universe karena ada posisi terbuka")

    def start_threads(self) -> None:
        # Simbol posisi (termasuk hasil adopsi di luar universe) wajib punya sumber harga.
        if self.trader:
            for sym in sorted(self.trader.position_symbols()):
                if sym not in self.symbols:
                    self._activate_position_symbol(sym)
        self._market_ws = WsThread(
            "market-ws",
            url_provider=self._market_url,
            on_message=self._on_market_message,
            on_error=lambda m: self._push("wserr", m),
            on_connect=lambda: self._push("wsopen", "market"),
            proxy=self.cfg.net.proxy,
        )
        self._market_ws.start()
        if self.cfg.mode != "paper":
            self._refresh_listen_key()
            self._user_ws = WsThread(
                "user-ws",
                url_provider=lambda: (
                    f"{self.cfg.ws_url()}/ws/{self._listen_key}" if self._listen_key else None
                ),
                on_message=lambda p: self._push("user", p),
                on_error=lambda m: self._push("wserr", m),
                on_connect=lambda: self._push("wsopen", "user"),
                proxy=self.cfg.net.proxy,
            )
            self._user_ws.start()

    def _refresh_listen_key(self) -> None:
        try:
            if not self._listen_key:
                self._listen_key = self.client.listen_key_create()
            else:
                self.client.listen_key_keepalive(self._listen_key)
            self._listen_key_ts = time.time()
        except BinanceError as exc:
            self.events.add("WARNING", "ws", f"listenKey gagal: {exc}")

    def _recreate_listen_key(self) -> None:
        self._listen_key = ""
        self._refresh_listen_key()
        if self._user_ws:
            self._user_ws.reconnect()

    def _send_stream_command(self, method: str, streams: list[str]) -> bool:
        if not streams or not self._market_ws:
            return False
        return self._market_ws.send(
            {"method": method, "params": streams, "id": next(self._command_seq)}
        )

    def _update_mark_subscriptions(self) -> None:
        if not self.trader or not self._market_ws or not self._market_ws.connected:
            return
        wanted = self.trader.position_symbols()
        with self._universe_lock:
            to_add = wanted - self._listening_marks
            to_del = self._listening_marks - wanted
        if to_add and self._send_stream_command(
            "SUBSCRIBE", [f"{s.lower()}@markPrice@1s" for s in sorted(to_add)]
        ):
            with self._universe_lock:
                self._listening_marks |= to_add
        if to_del and self._send_stream_command(
            "UNSUBSCRIBE", [f"{s.lower()}@markPrice@1s" for s in sorted(to_del)]
        ):
            with self._universe_lock:
                self._listening_marks -= to_del

    def _update_kline_subscriptions(self) -> None:
        if not self._market_ws or not self._market_ws.connected:
            return
        with self._universe_lock:
            wanted = set(self.symbols)
            to_add = wanted - self._active_klines
            to_del = self._active_klines - wanted
            if to_add and self._send_stream_command(
                "SUBSCRIBE", [f"{s.lower()}@kline_{self.cfg.market.interval}" for s in sorted(to_add)]
            ):
                self._active_klines |= to_add
                self.events.add("INFO", "ws", f"langganan kline tambah: {', '.join(sorted(to_add))}")
            if to_del and self._send_stream_command(
                "UNSUBSCRIBE", [f"{s.lower()}@kline_{self.cfg.market.interval}" for s in sorted(to_del)]
            ):
                self._active_klines -= to_del
                self.events.add("INFO", "ws", f"langganan kline lepas: {', '.join(sorted(to_del))}")

    def _activate_symbol(self, symbol: str) -> bool:
        with self._universe_lock:
            if symbol in self.symbols:
                return False
        flt = self.filters.get(symbol)
        if flt is None:
            flt = self.client.filters(symbol)
        rt = SymbolRuntime(
            symbol=symbol,
            strategy=ScalpPullbackStrategy(symbol, self.cfg.strategy, self.cfg.risk.atr_len),
            filters=flt,
        )
        self._warmup_symbol(rt)
        if rt.bars == 0:
            self.events.add("WARNING", "discovery", f"{symbol} warmup gagal, tidak diaktifkan")
            return False
        if self.broker is not None and not isinstance(self.broker, PaperBroker):
            try:
                self.broker.prepare_symbol(symbol)
            except BinanceError as exc:
                self.events.add("WARNING", "setup", f"{symbol} set leverage/margin: {exc}")
        with self._universe_lock:
            self.filters[symbol] = flt
            self.symbols[symbol] = rt
        self.events.add("INFO", "discovery", f"aktifkan {symbol} (bar {rt.bars}, harga {rt.last_price:.6g})")
        return True

    def _deactivate_symbol(self, symbol: str) -> None:
        with self._universe_lock:
            rt = self.symbols.pop(symbol, None)
        if rt is not None:
            self.events.add("INFO", "discovery", f"lepas {symbol} dari pemantauan")

    def _refresh_universe(self) -> None:
        if self.cfg.market.discovery != "top_volume" or self.trader is None:
            return
        equity, _ = self.trader.snapshot_equity()
        selection = self._select_universe(
            equity or self._discovery_equity(), force_symbols=list(self.cfg.market.symbols)
        )
        wanted = selection.symbols
        if not wanted:
            self.events.add("WARNING", "discovery", "refresh tidak menghasilkan simbol, daftar lama dipertahankan")
            return
        held = self.trader.position_symbols()
        with self._universe_lock:
            current = set(self.symbols)
        for symbol in wanted:
            if symbol not in current:
                try:
                    self._activate_symbol(symbol)
                except BinanceError as exc:
                    self.events.add("WARNING", "discovery", f"gagal aktifkan {symbol}: {exc}")
        with self._universe_lock:
            to_drop = [s for s in self.symbols if s not in wanted and s not in held]
            retained = [s for s in self.symbols if s not in wanted and s in held]
        for symbol in retained:
            self.events.add(
                "INFO", "discovery", f"{symbol} keluar dari top volume, ditahan sampai posisi tutup"
            )
        for symbol in to_drop:
            self._deactivate_symbol(symbol)
        with self._universe_lock:
            self.cfg.market.symbols = sorted(self.symbols)
            active_count = len(self.symbols)
        self._last_discovery = time.time()
        self.discovery_note = f"top_volume/{active_count}"
        self.events.add(
            "INFO",
            "discovery",
            f"refresh top_volume: {selection.summary()} (pantau {active_count} simbol)",
        )
        self._update_kline_subscriptions()

    async def _handle_kline(self, payload: dict) -> None:
        k = payload.get("k") or {}
        if not k or self.trader is None:
            return
        symbol = str(payload.get("s", "")).upper()
        with self._universe_lock:
            rt = self.symbols.get(symbol)
        if rt is None:
            return
        price = safe_float(k.get("c"), 0.0)
        if price > 0:
            rt.last_price = price
            self.trader.set_mark(symbol, price)
        if not k.get("x"):
            return
        ts = int(safe_float(k.get("t"), 0.0))
        if ts <= rt.last_bar_ts:
            return
        candle = Candle(
            ts=ts,
            open=safe_float(k.get("o"), 0.0),
            high=safe_float(k.get("h"), 0.0),
            low=safe_float(k.get("l"), 0.0),
            close=price,
            volume=safe_float(k.get("v"), 0.0),
        )
        values = (candle.open, candle.high, candle.low, candle.close)
        if not all(math.isfinite(v) and v > 0 for v in values):
            return
        rt.last_bar_ts = ts
        rt.bars += 1
        state = rt.strategy.on_bar(candle)
        self.trader.set_mark(symbol, candle.close)
        if state.signal:
            signal_obj = rt.strategy.build_signal(state)
            if signal_obj and not rt.strategy.regime_allows(state):
                self.trader.note_regime_reject(symbol, signal_obj.side)
                signal_obj = None
            if signal_obj:
                rt.last_signal = signal_obj.side
                rt.last_signal_ts = time.time()
                self.trader.mark_signal()
                self.events.add(
                    "INFO",
                    "signal",
                    (
                        f"{signal_obj.side} {symbol} @{signal_obj.price:.6g} "
                        f"PAC={signal_obj.pac_center:.6g} ATR={signal_obj.atr:.6g} trend={signal_obj.trend:+d}"
                    ),
                )
                snap = await asyncio.to_thread(self.book.snapshot, symbol, True)
                await asyncio.to_thread(self.trader.try_open, signal_obj, rt.filters, state.idx, snap)
                self._update_mark_subscriptions()

    async def _handle_mark(self, payload: dict) -> None:
        if self.trader is None:
            return
        symbol = str(payload.get("s", "")).upper()
        with self._universe_lock:
            rt = self.symbols.get(symbol)
        if rt is None:
            return
        price = safe_float(payload.get("p"), 0.0)
        if price > 0:
            self.trader.set_mark(symbol, price)
            rt.last_price = price

    async def _handle_user(self, payload: dict) -> None:
        if self.trader is None:
            return
        event = payload.get("e")
        if event == "ORDER_TRADE_UPDATE":
            o = payload.get("o") or {}
            status = o.get("X")
            otype = o.get("ot") or o.get("o")
            sym = str(o.get("s", "")).upper()
            if status == "FILLED" and otype in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"):
                fill_price = safe_float(o.get("ap"), 0.0)
                if fill_price > 0:
                    self.trader.record_external_fill(sym, fill_price)
                self.events.add(
                    "INFO",
                    "fill",
                    f"{sym} {otype} tereksekusi @{o.get('ap')} PnL={o.get('rp')}",
                )
                await asyncio.to_thread(self.trader.sync_with_exchange, True)
            elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                self.events.add("WARNING", "fill", f"{sym} {otype} {status}: {o.get('r')}")
        elif event == "ALGO_UPDATE":
            o = payload.get("o") if isinstance(payload.get("o"), dict) else payload
            sym = str(o.get("s") or payload.get("s") or "").upper()
            status = str(o.get("X") or "")
            otype = str(o.get("o") or o.get("orderType") or "")
            if not sym:
                return
            if status == "TRIGGERING":
                self.events.add("INFO", "algo", f"{sym} {otype} trigger, menunggu eksekusi")
            elif status == "FINISHED":
                price = safe_float(o.get("ap") or o.get("actualPrice"), 0.0)
                if price > 0:
                    self.trader.record_external_fill(sym, price)
                self.events.add(
                    "INFO",
                    "algo",
                    f"{sym} {otype} tereksekusi @{price or 'n/a'} algoId={o.get('aid') or payload.get('aid')}",
                )
                await asyncio.to_thread(self.trader.sync_with_exchange, True)
            elif status == "CANCELED":
                self.events.add("INFO", "algo", f"{sym} {otype} dibatalkan")
        elif event == "listenKeyExpired":
            self.events.add("WARNING", "ws", "listenKey expired, membuat ulang")
            await asyncio.to_thread(self._recreate_listen_key)

    async def event_consumer(self) -> None:
        while not self._stopping:
            queue = self._queue
            if queue is None:
                await asyncio.sleep(0.2)
                continue
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=1.0)
            except (asyncio.TimeoutError, TimeoutError):
                continue
            try:
                if kind == "market":
                    ev = payload.get("e")
                    if ev == "kline":
                        await self._handle_kline(payload)
                    elif ev == "markPriceUpdate":
                        await self._handle_mark(payload)
                    elif "result" in payload:
                        result = payload.get("result")
                        level = "WARNING" if result is not None else "DEBUG"
                        self.events.add(
                            level, "ws", f"ack stream id={payload.get('id')} result={result}"
                        )
                elif kind == "user":
                    await self._handle_user(payload)
                elif kind == "wsopen":
                    self.events.add("INFO", "ws", f"websocket {payload} tersambung")
                    if payload == "market":
                        with self._universe_lock:
                            self._active_klines = set(self._url_base_symbols)
                        self._update_kline_subscriptions()
                        self._update_mark_subscriptions()
                elif kind == "wserr":
                    now = time.time()
                    if now - self._last_ws_warn > 30:
                        self._last_ws_warn = now
                        self.events.add("WARNING", "ws", str(payload))
            except Exception as exc:
                if self.trader:
                    self.trader.bump_error()
                self.events.add("ERROR", "loop", f"event {kind}: {type(exc).__name__}: {exc}")

    async def manager_loop(self) -> None:
        last_sync = 0.0
        last_orphan = 0.0
        last_key = time.time()
        while not self._stopping:
            now = time.time()
            try:
                await asyncio.to_thread(self.trader.manage_all)
                await asyncio.to_thread(self.trader.refresh_equity)
                if now - last_sync >= self.cfg.execution.reconcile_s:
                    last_sync = now
                    await asyncio.to_thread(self.trader.sync_with_exchange, True)
                if now - last_orphan >= max(20, self.cfg.execution.reconcile_s):
                    last_orphan = now
                    await asyncio.to_thread(self.trader.check_orphan_protection)
                if self._user_ws and now - last_key > 1800:
                    last_key = now
                    await asyncio.to_thread(self._refresh_listen_key)
                self._update_kline_subscriptions()
                self._update_mark_subscriptions()
                if (
                    self.cfg.market.discovery == "top_volume"
                    and now - self._last_discovery >= self.cfg.market.discovery_refresh_s
                ):
                    await asyncio.to_thread(self._refresh_universe)
                if self._market_ws and self._market_ws.age() > 90:
                    await self._rest_fallback()
            except Exception as exc:
                if self.trader:
                    self.trader.bump_error()
                self.events.add("ERROR", "manager", f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(1.0)

    async def _rest_fallback(self) -> None:
        with self._universe_lock:
            items = list(self.symbols.items())
        open_ts = open_bar_start_ms(self.client.server_time_ms(), self.interval_seconds)
        for sym, rt in items:
            try:
                candles = await asyncio.to_thread(
                    self.client.klines, sym, self.cfg.market.interval, 3
                )
            except BinanceError:
                continue
            for c in candles:
                if c.ts >= open_ts or c.ts <= rt.last_bar_ts:
                    continue
                state = rt.strategy.on_bar(c)
                rt.last_bar_ts = c.ts
                rt.bars += 1
                self.trader.set_mark(sym, c.close)
                if state.signal:
                    signal_obj = rt.strategy.build_signal(state)
                    if signal_obj and not rt.strategy.regime_allows(state):
                        self.trader.note_regime_reject(sym, signal_obj.side)
                        signal_obj = None
                    if signal_obj:
                        rt.last_signal = signal_obj.side
                        rt.last_signal_ts = time.time()
                        self.trader.mark_signal()
                        snap = await asyncio.to_thread(self.book.snapshot, sym, True)
                        await asyncio.to_thread(
                            self.trader.try_open, signal_obj, rt.filters, state.idx, snap
                        )
                        self._update_mark_subscriptions()

    def _symbol_rows(self) -> list[dict]:
        with self._universe_lock:
            items = list(self.symbols.items())
        rows = []
        for sym, rt in items:
            bar = rt.strategy.last_bar
            snap = self.book.cached(sym) if self.book else None
            price = rt.last_price or (bar.price if bar else 0.0)
            atr = bar.atr if bar and bar.atr else 0.0
            rows.append(
                {
                    "symbol": sym,
                    "price": price,
                    "trend": bar.trend if bar else 0,
                    "pac_center": bar.pac_center if bar else None,
                    "zone": bar.bar_zone if bar else "-",
                    "armed_long": bar.pullback_armed_long if bar else False,
                    "armed_short": bar.pullback_armed_short if bar else False,
                    "imbalance": snap.imbalance if snap else None,
                    "spread": snap.spread_bps if snap else None,
                    "atr_pct": (atr / price * 100.0) if price > 0 and atr else 0.0,
                    "last_signal": rt.last_signal,
                    "age": time.time() - rt.last_signal_ts if rt.last_signal_ts else 0.0,
                }
            )
        return rows

    def dashboard_state(self) -> dict[str, Any]:
        trader = self.trader
        if trader is None:
            return {}
        stats = trader.stats()
        start_eq = stats["start_equity"] or stats["equity"] or 1.0
        day_pct = stats["realized"] / start_eq * 100.0 if start_eq else 0.0
        ip, ip_age = self.ip.snapshot()
        events_tail: list[Event] = self.events.tail(self.cfg.dashboard.log_lines)
        with self._universe_lock:
            symbol_count = len(self.symbols)
        footer = (
            "Ctrl+C = keluar | order proteksi tetap aktif di exchange"
            if self.cfg.mode != "paper"
            else "Ctrl+C = keluar | mode paper, tidak ada order nyata"
        )
        return {
            "mode": self.cfg.mode,
            "ip": ip,
            "ip_age": ip_age,
            "ws_ok": bool(self._market_ws and self._market_ws.connected),
            "ws_age": self._market_ws.age() if self._market_ws else 0.0,
            "weight": self.client.used_weight_1m,
            "latency_ms": self.client.last_latency_ms,
            "uptime_s": time.time() - self.started_at,
            "symbols": symbol_count,
            "discovery": self.discovery_note or self.cfg.market.discovery,
            "equity": stats["equity"],
            "available": stats["available"],
            "day_pnl": stats["realized"],
            "day_pnl_pct": day_pct,
            "open_count": stats["open_count"],
            "max_open": trader.max_open_effective,
            "adopted": trader.adopted_count,
            "risk_pct": self.cfg.risk.risk_pct,
            "leverage": self.cfg.risk.leverage,
            "halted_reason": stats["halted_reason"],
            "signals_today": stats["signals"],
            "trades_today": stats["trades"],
            "errors": stats["errors"],
            "positions": trader.position_rows(),
            "rows": self._symbol_rows(),
            "refresh_s": self.cfg.market.discovery_refresh_s,
            "min_vol_m": self.cfg.market.discovery_quote_volume_min / 1_000_000.0,
            "entries": trader.entry_rows(self.cfg.dashboard.history_lines),
            "events": events_tail,
            "footer": footer,
        }

    async def dashboard_loop(self) -> None:
        while not self._stopping:
            try:
                state = self.dashboard_state()
                if state:
                    self.dashboard.render(state)
            except Exception as exc:
                log.debug("dashboard render error: %s", exc)
            await asyncio.sleep(self.cfg.dashboard.refresh_ms / 1000.0)

    async def run(self) -> int:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=2000)
        self.setup_logging()
        self._file_log.info(
            "mulai mode=%s interval=%s discovery=%s symbols=%s events=%s",
            self.cfg.mode,
            self.cfg.market.interval,
            self.cfg.market.discovery,
            ",".join(self.cfg.market.symbols) or "-",
            self.events_file,
        )
        if self.cfg.mode == "live" and not confirmation_from_env():
            self.events.add("ERROR", "boot", "live butuh SCALPBOT_LIVE_CONFIRM=YES-LIVE")
            return 2
        if self.cfg.net.ip_check:
            self.ip.start()
        try:
            self.dashboard.start()
        except Exception as exc:
            self.events.add("WARNING", "boot", f"dashboard dimatikan: {type(exc).__name__}: {exc}")
        exit_code = 0
        try:
            await asyncio.to_thread(self.preflight)
            await asyncio.to_thread(self.warmup)
            # Posisi yang sudah terbuka di exchange harus diadopsi SEBELUM websocket hidup
            # dan sebelum layar menampilkan POS 0/3.
            await asyncio.to_thread(self.trader.reconcile_at_start)
            self.start_threads()
            tasks = self._tasks = [
                asyncio.create_task(self.event_consumer()),
                asyncio.create_task(self.manager_loop()),
            ]
            if self.dashboard.enabled:
                tasks.append(asyncio.create_task(self.dashboard_loop()))
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.events.add("CRITICAL", "boot", f"berhenti karena error: {type(exc).__name__}: {exc}")
            exit_code = 1
        finally:
            self._stopping = True
            for t in self._tasks:
                t.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            if self._market_ws:
                self._market_ws.stop()
            if self._user_ws:
                self._user_ws.stop()
            if self.cfg.mode != "paper" and self._listen_key:
                try:
                    self.client.listen_key_delete(self._listen_key)
                except BinanceError:
                    pass
            # Pastikan cooldown + riwayat entry terakhir tersimpan sebelum keluar.
            if trader := self.trader:
                try:
                    trader._persist_state()
                except Exception as exc:  # noqa: BLE001 - jangan gagal saat shutdown
                    log.debug("flush state gagal: %s", exc)
            self.ip.stop()
            self.dashboard.stop()
            await asyncio.sleep(0.1)
            self.client.close()
            trader = self.trader
            if trader:
                self._file_log.info(
                    "selesai mode=%s durasi=%.0fs equity=%.2f trades=%d sinyal=%d error=%d",
                    self.cfg.mode,
                    time.time() - self.started_at,
                    trader.equity,
                    trader.day.trades,
                    trader.day.signals,
                    trader.error_count,
                )
            self._print_summary()
        return exit_code

    def _print_summary(self) -> None:
        trader = self.trader
        if not trader:
            return
        lines = [
            "",
            "=" * 64,
            "RINGKASAN SESI",
            f"  mode          : {self.cfg.mode}",
            f"  log teknis    : {self.log_file}",
            f"  log event     : {self.events_file}",
            f"  state         : {self.state_file or 'dimatikan'}",
            f"  zona waktu    : {tz_label(self.cfg.logging.timezone)} "
            f"(tampilan & hari bot; hari exchange untuk tampilan Binance tetap UTC)",
            f"  durasi        : {time.time() - self.started_at:.0f}s",
            f"  equity        : {trader.equity:.2f} USDT",
            f"  realized hari : {trader.current_realized():+.2f} USDT "
            f"(hari bot {tz_label(self.cfg.logging.timezone)}; termasuk PnL sebelum bot start)",
            f"  trade         : {trader.day.trades} (W{trader.day.wins}/L{trader.day.losses})",
            f"  sinyal        : {trader.day.signals}",
            f"  posisi terbuka: {trader.position_count()}",
            f"  error         : {trader.error_count}",
        ]
        if trader.reject_stats:
            book = {k: v for k, v in trader.reject_stats.items() if k != "regime"}
            if book:
                ringkas = ", ".join(f"{k} {v}" for k, v in sorted(book.items()))
                lines.append(f"  tolak entry   : {ringkas} (orderbook)")
            if trader.reject_stats.get("regime"):
                lines.append(
                    f"  tolak rezim   : {trader.reject_stats['regime']} sinyal "
                    f"(slope < {self.cfg.strategy.regime_min_slope_bps:g} bps)"
                )
        lines += [
            "=" * 64,
        ]
        print("\n".join(lines))


async def run_app(cfg: Config, paper_balance: float = 50.0) -> int:
    app = ScalpBotApp(cfg, paper_balance=paper_balance)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    stop_requested = threading.Event()

    def _signal_handler() -> None:
        if stop_requested.is_set():
            return
        stop_requested.set()
        app.events.add("INFO", "boot", "sinyal berhenti diterima, menutup dengan rapi")
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, RuntimeError, AttributeError, ValueError):
            try:
                signal.signal(sig, lambda *_: _signal_handler())
            except (ValueError, OSError, AttributeError):
                pass

    task = asyncio.create_task(app.run())
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait({task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    if stop_task in done and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    for p in pending:
        p.cancel()
    return task.result() if task.done() and not task.cancelled() else 0
