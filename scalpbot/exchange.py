from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
import threading
import time
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

import requests

from .config import SYMBOL_RE, Config, assert_mode_hosts
from .indicators import Candle
from .risk import SymbolFilters
from .security import clean_message, harden_session, parse_proxy, validate_listen_key

log = logging.getLogger("scalpbot.exchange")


class BinanceError(RuntimeError):
    def __init__(self, code: int, message: str, status: int = 0, path: str = "") -> None:
        safe = clean_message(message)
        super().__init__(f"[{code}] {safe} ({path} http={status})")
        self.code = code
        self.message = safe
        self.status = status
        self.path = path


class RateLimited(BinanceError):
    pass


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def fmt_num(value: Any) -> Optional[str]:
    """Format angka tanpa eksponen dan tanpa noise float (mis. 0.058365727000000006)."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    text = format(Decimal(str(num)).quantize(Decimal("0.00000001")), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


CLIENT_ALGO_ID_RE = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")


def _valid_symbol(value: Any) -> bool:
    return isinstance(value, str) and bool(SYMBOL_RE.fullmatch(value.upper()))


class BinanceFuturesClient:
    def __init__(self, cfg: Config, session: Optional[requests.Session] = None) -> None:
        assert_mode_hosts(cfg)
        self.cfg = cfg
        self.base_url = cfg.base_url().rstrip("/")
        self.base_host = (urlparse(self.base_url).hostname or "").lower()
        self.key = cfg.api_key() if cfg.needs_credentials() else ""
        self.secret = cfg.api_secret() if cfg.needs_credentials() else ""
        self._lock = threading.Lock()
        self._time_offset_ms = 0
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "X-MBX-APIKEY": self.key,
                "User-Agent": "scalptool-bot/1.0",
                "Accept": "application/json",
            }
        )
        proxy = ""
        if cfg.net.proxy:
            try:
                proxy = parse_proxy(cfg.net.proxy)["normalized"]
            except ValueError as exc:
                raise BinanceError(-1, f"net.proxy tidak valid: {exc}", 0, "config") from exc
        harden_session(self._session, proxy)
        self.used_weight_1m = 0
        self.used_weight_limit = 2400
        self.order_count_1m = 0
        self.order_limit_1m = 1200
        self.last_latency_ms = 0.0
        self._exchange_info: Optional[dict[str, Any]] = None
        self._filters: dict[str, SymbolFilters] = {}
        self._closed = False

    def close(self) -> None:
        self._closed = True
        self._session.close()

    def sync_time(self) -> int:
        local_before = int(time.time() * 1000)
        data = self._request("GET", "/fapi/v1/time", signed=False, retries=2)
        local_after = int(time.time() * 1000)
        if not isinstance(data, dict) or "serverTime" not in data:
            raise BinanceError(-1, "serverTime tidak ada", 0, "/fapi/v1/time")
        server = safe_float(data["serverTime"], 0.0)
        if server <= 0:
            raise BinanceError(-1, "serverTime tidak valid", 0, "/fapi/v1/time")
        self._time_offset_ms = int(server) - (local_before + local_after) // 2
        if abs(self._time_offset_ms) > 60_000:
            log.warning("selisih jam lokal %d ms, sinkronisasi waktu disarankan", self._time_offset_ms)
        return self._time_offset_ms

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    @staticmethod
    def _require_symbol(symbol: Any, path: str) -> str:
        if not _valid_symbol(symbol):
            raise BinanceError(-1121, "simbol tidak valid", 0, path)
        return str(symbol).upper()

    @staticmethod
    def _listen_key_params(key: Any) -> dict[str, str]:
        try:
            return {"listenKey": validate_listen_key(key)}
        except ValueError as exc:
            raise BinanceError(-1, str(exc), 0, "/fapi/v1/listenKey") from exc

    def _sign(self, query: str) -> str:
        return hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _retry_after(headers: Any) -> Optional[float]:
        raw = headers.get("Retry-After") if headers else None
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return max(0.0, value) if math.isfinite(value) else None

    @staticmethod
    def _error_from_response(resp: requests.Response, path: str) -> BinanceError:
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {}
        if isinstance(body, dict):
            raw_code = body.get("code", resp.status_code)
            message = clean_message(str(body.get("msg", resp.text[:200])))
        else:
            raw_code = resp.status_code
            message = clean_message(resp.text[:200])
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            code = resp.status_code
        return BinanceError(code, message, resp.status_code, path)

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        signed: bool = False,
        retries: int = 1,
    ) -> Any:
        if self._closed:
            raise BinanceError(-1, "client REST sudah ditutup", 0, path)
        params = {k: v for k, v in (params or {}).items() if v is not None}
        cap = max(1, int(self.cfg.api.max_retries))
        attempts = min(max(1, int(retries)), cap) if method == "GET" else 1
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            payload = dict(params)
            if signed:
                if not self.key or not self.secret:
                    raise BinanceError(-1, "API key/secret belum diset", 0, path)
                payload["timestamp"] = self._timestamp()
                payload["recvWindow"] = self.cfg.api.recv_window_ms
                payload = {k: (fmt_num(v) if isinstance(v, float) else v) for k, v in payload.items()}
                query = urlencode(payload, doseq=True)
                url = f"{self.base_url}{path}?{query}&signature={self._sign(query)}"
            else:
                url = f"{self.base_url}{path}"
                if payload:
                    url = f"{url}?{urlencode(payload, doseq=True)}"
            started = time.time()
            try:
                with self._lock:
                    resp = self._session.request(
                        method, url, timeout=self.cfg.api.timeout_s, allow_redirects=False
                    )
                self.last_latency_ms = (time.time() - started) * 1000.0
                final_host = (urlparse(str(getattr(resp, "url", url))).hostname or "").lower()
                if final_host != self.base_host:
                    raise BinanceError(-1, "host respons tidak sesuai allowlist", 0, path)
                if 300 <= resp.status_code < 400:
                    raise BinanceError(-1, "redirect ditolak", resp.status_code, path)
                self._track_limits(resp.headers)
                if resp.status_code in (418, 429) or resp.status_code >= 500:
                    exc = self._error_from_response(resp, path)
                    if method == "GET" and attempt + 1 < attempts:
                        delay = self._retry_after(resp.headers)
                        if delay is None:
                            delay = min(5.0, 0.4 * (2**attempt))
                        time.sleep(min(delay, 10.0))
                        continue
                    if resp.status_code in (418, 429):
                        raise RateLimited(exc.code, exc.message, exc.status, path)
                    raise exc
                body = resp.json() if resp.content else {}
                if resp.status_code >= 400:
                    raise self._error_from_response(resp, path)
                return body
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    time.sleep(min(5.0, 0.4 * (attempt + 1)))
                    continue
                raise BinanceError(-1, f"network error: {type(exc).__name__}", 0, path) from exc
        raise BinanceError(-1, f"gagal: {last_exc}", 0, path)

    def _track_limits(self, headers: Any) -> None:
        try:
            self.used_weight_1m = int(headers.get("X-MBX-USED-WEIGHT-1M", self.used_weight_1m))
        except (TypeError, ValueError):
            pass
        try:
            self.order_count_1m = int(headers.get("X-MBX-ORDER-COUNT-1M", self.order_count_1m))
        except (TypeError, ValueError):
            pass

    def _read_rate_limits(self, info: dict[str, Any]) -> None:
        for item in info.get("rateLimits", []) or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("rateLimitType", "")).upper()
            interval = str(item.get("interval", "")).upper()
            limit = safe_float(item.get("limit"), 0.0)
            if limit <= 0 or interval != "1M":
                continue
            if kind == "REQUEST_WEIGHT":
                self.used_weight_limit = max(1, int(limit))
            elif kind == "ORDERS":
                self.order_limit_1m = max(1, int(limit))

    def weight_pressure(self) -> float:
        if self.used_weight_limit <= 0:
            return 0.0
        return min(1.0, self.used_weight_1m / self.used_weight_limit)

    def exchange_info(self, refresh: bool = False) -> dict[str, Any]:
        if self._exchange_info is None or refresh:
            self._exchange_info = self._request("GET", "/fapi/v1/exchangeInfo", retries=2)
            self._filters.clear()
            if isinstance(self._exchange_info, dict):
                self._read_rate_limits(self._exchange_info)
        return self._exchange_info

    def load_filters(self, symbols: list[str]) -> dict[str, SymbolFilters]:
        info = self.exchange_info()
        by_symbol = {
            s["symbol"].upper(): s
            for s in info.get("symbols", [])
            if isinstance(s, dict) and _valid_symbol(s.get("symbol"))
        }
        out: dict[str, SymbolFilters] = {}
        for sym in symbols:
            sym = self._require_symbol(sym, "/fapi/v1/exchangeInfo")
            raw = by_symbol.get(sym)
            if raw is None:
                raise BinanceError(-1121, f"simbol {sym} tidak ada di exchangeInfo", 0, "/fapi/v1/exchangeInfo")
            if raw.get("status") != "TRADING":
                raise BinanceError(-1121, f"simbol {sym} status {raw.get('status')}", 0, "/fapi/v1/exchangeInfo")
            if raw.get("contractType") != "PERPETUAL":
                raise BinanceError(-1121, f"simbol {sym} bukan PERPETUAL", 0, "/fapi/v1/exchangeInfo")
            tick = step = 0.0
            min_qty = max_qty = min_notional = 0.0
            market_max_qty = market_min_qty = 0.0
            for f in raw.get("filters", []) or []:
                if not isinstance(f, dict):
                    continue
                ftype = f.get("filterType")
                if ftype == "PRICE_FILTER":
                    tick = safe_float(f.get("tickSize"), 0.0)
                elif ftype == "LOT_SIZE":
                    step = safe_float(f.get("stepSize"), 0.0)
                    min_qty = safe_float(f.get("minQty"), 0.0)
                    max_qty = safe_float(f.get("maxQty"), 0.0)
                elif ftype == "MARKET_LOT_SIZE":
                    market_min_qty = safe_float(f.get("minQty"), 0.0)
                    market_max_qty = safe_float(f.get("maxQty"), 0.0)
                elif ftype == "MIN_NOTIONAL":
                    min_notional = safe_float(f.get("notional", f.get("minNotional")), 0.0)
            if market_min_qty > 0:
                min_qty = max(min_qty, market_min_qty)
            if market_max_qty > 0:
                max_qty = min(max_qty, market_max_qty) if max_qty > 0 else market_max_qty
            tick = tick or 10 ** -int(raw.get("pricePrecision", 2))
            step = step or 10 ** -int(raw.get("quantityPrecision", 3))
            min_qty = min_qty or step
            max_qty = max_qty or 10_000.0
            min_notional = min_notional or 5.0
            if tick <= 0 or step <= 0 or min_qty <= 0 or max_qty < min_qty or min_notional <= 0:
                raise BinanceError(-1121, f"filter {sym} tidak valid", 0, "/fapi/v1/exchangeInfo")
            flt = SymbolFilters(
                symbol=sym,
                tick_size=tick,
                step_size=step,
                min_qty=min_qty,
                max_qty=max_qty,
                min_notional=min_notional,
                qty_precision=int(raw.get("quantityPrecision", 3)),
                price_precision=int(raw.get("pricePrecision", 2)),
            )
            self._filters[sym] = flt
            out[sym] = flt
        return out

    def filters(self, symbol: str) -> SymbolFilters:
        symbol = self._require_symbol(symbol, "/fapi/v1/exchangeInfo")
        if symbol not in self._filters:
            self.load_filters([symbol])
        return self._filters[symbol]

    def ticker_24hr(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/fapi/v1/ticker/24hr", retries=2)
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, dict) and _valid_symbol(row.get("symbol"))]

    def tradable_symbols(self, quote_asset: str = "USDT") -> list[str]:
        info = self.exchange_info()
        out = []
        for s in info.get("symbols", []) or []:
            if not isinstance(s, dict) or not _valid_symbol(s.get("symbol")):
                continue
            if s.get("status") != "TRADING" or s.get("contractType") != "PERPETUAL":
                continue
            if str(s.get("quoteAsset", "")).upper() != quote_asset:
                continue
            if str(s.get("marginAsset", "")).upper() != quote_asset:
                continue
            out.append(s["symbol"])
        return out

    def load_all_filters(self) -> dict[str, SymbolFilters]:
        return self.load_filters(self.tradable_symbols())

    def klines(self, symbol: str, interval: str, limit: int = 500, end_time: Optional[int] = None) -> list[Candle]:
        symbol = self._require_symbol(symbol, "/fapi/v1/klines")
        raw = self._request(
            "GET",
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": min(limit, 1500), "endTime": end_time},
            retries=2,
        )
        if not isinstance(raw, list):
            raise BinanceError(-1, "klines bukan list", 0, "/fapi/v1/klines")
        out: list[Candle] = []
        for row in raw:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            try:
                candle = Candle(
                    ts=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            except (TypeError, ValueError, OverflowError):
                continue
            values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
            if not all(math.isfinite(v) for v in values) or candle.close <= 0:
                continue
            out.append(candle)
        return out

    def depth(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        symbol = self._require_symbol(symbol, "/fapi/v1/depth")
        data = self._request("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": limit}, retries=1)
        return data if isinstance(data, dict) else {}

    def mark_price(self, symbol: Optional[str] = None) -> Any:
        if symbol:
            symbol = self._require_symbol(symbol, "/fapi/v1/premiumIndex")
            return self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol}, retries=1)
        return self._request("GET", "/fapi/v1/premiumIndex", retries=1)

    def balances(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/fapi/v3/balance", signed=True, retries=2)
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    def usdt_equity(self) -> tuple[float, float]:
        for row in self.balances():
            if row.get("asset") == "USDT":
                balance = safe_float(row.get("balance"), 0.0)
                available = safe_float(row.get("availableBalance"), 0.0)
                return max(0.0, balance), max(0.0, available)
        return 0.0, 0.0

    def account(self) -> dict[str, Any]:
        data = self._request("GET", "/fapi/v3/account", signed=True, retries=2)
        return data if isinstance(data, dict) else {}

    def position_risk(self, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        if symbol is not None:
            symbol = self._require_symbol(symbol, "/fapi/v3/positionRisk")
        data = self._request("GET", "/fapi/v3/positionRisk", {"symbol": symbol}, signed=True, retries=2)
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, dict) and _valid_symbol(row.get("symbol"))]

    def position_side_dual(self) -> bool:
        data = self._request("GET", "/fapi/v1/positionSide/dual", signed=True, retries=2)
        if not isinstance(data, dict) or "dualSidePosition" not in data:
            raise BinanceError(-1, "dualSidePosition tidak ada", 0, "/fapi/v1/positionSide/dual")
        value = data["dualSidePosition"]
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes"}
        return bool(value)

    def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        symbol = self._require_symbol(symbol, "/fapi/v1/leverage")
        return self._request(
            "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True
        )

    def set_margin_type(self, symbol: str, margin_type: str) -> None:
        symbol = self._require_symbol(symbol, "/fapi/v1/marginType")
        try:
            self._request(
                "POST",
                "/fapi/v1/marginType",
                {"symbol": symbol, "marginType": margin_type},
                signed=True,
            )
        except BinanceError as exc:
            if exc.code != -4046:
                raise

    def new_order(self, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        clean["symbol"] = self._require_symbol(clean.get("symbol"), "/fapi/v1/order")
        close_position = str(clean.get("closePosition", "")).lower() == "true"
        if close_position and ("quantity" in clean or "reduceOnly" in clean):
            raise BinanceError(
                -1102, "closePosition tidak boleh bersama quantity/reduceOnly", 0, "/fapi/v1/order"
            )
        return self._request("POST", "/fapi/v1/order", clean, signed=True)

    def cancel_order(self, symbol: str, order_id: Optional[int] = None, client_id: Optional[str] = None) -> dict[str, Any]:
        symbol = self._require_symbol(symbol, "/fapi/v1/order")
        return self._request(
            "DELETE",
            "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_id},
            signed=True,
        )

    def income_history(
        self,
        income_type: str = "REALIZED_PNL",
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/fapi/v1/income",
            {
                "incomeType": income_type,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": max(1, min(1000, int(limit))),
            },
            signed=True,
            retries=2,
        )
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    def new_algo_order(self, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        clean["symbol"] = self._require_symbol(clean.get("symbol"), "/fapi/v1/algoOrder")
        clean.setdefault("algoType", "CONDITIONAL")
        clean.setdefault("newOrderRespType", "RESULT")
        client_id = clean.get("clientAlgoId")
        if client_id is not None and not CLIENT_ALGO_ID_RE.fullmatch(str(client_id)):
            raise BinanceError(-1102, "clientAlgoId tidak valid", 0, "/fapi/v1/algoOrder")
        data = self._request("POST", "/fapi/v1/algoOrder", clean, signed=True)
        return data if isinstance(data, dict) else {}

    def cancel_algo_order(
        self, symbol: str, algo_id: Optional[str] = None, client_id: Optional[str] = None
    ) -> dict[str, Any]:
        symbol = self._require_symbol(symbol, "/fapi/v1/algoOrder")
        if algo_id is not None:
            try:
                algo_id = int(algo_id)
            except (TypeError, ValueError):
                algo_id = None
        params: dict[str, Any] = {"algoId": algo_id, "clientAlgoId": client_id}
        if algo_id is None and not client_id:
            raise BinanceError(-1102, "algoId atau clientAlgoId wajib diisi untuk cancel", 0, "/fapi/v1/algoOrder")
        data = self._request("DELETE", "/fapi/v1/algoOrder", params, signed=True)
        return data if isinstance(data, dict) else {}

    def cancel_all_algo(self, symbol: str) -> Any:
        symbol = self._require_symbol(symbol, "/fapi/v1/algoOpenOrders")
        return self._request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}, signed=True)

    def open_algo_orders(self, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        if symbol is not None:
            symbol = self._require_symbol(symbol, "/fapi/v1/openAlgoOrders")
        data = self._request(
            "GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True, retries=2
        )
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get("orders") or data.get("rows") or []
        else:
            return []
        return [
            row for row in rows
            if isinstance(row, dict) and (row.get("symbol") is None or _valid_symbol(row.get("symbol")))
        ]

    def query_algo_order(
        self, symbol: str, algo_id: Optional[str] = None, client_id: Optional[str] = None
    ) -> dict[str, Any]:
        symbol = self._require_symbol(symbol, "/fapi/v1/algoOrder")
        data = self._request(
            "GET",
            "/fapi/v1/algoOrder",
            {"algoId": algo_id, "clientAlgoId": client_id},
            signed=True,
            retries=1,
        )
        return data if isinstance(data, dict) else {}

    def cancel_all(self, symbol: str) -> Any:
        symbol = self._require_symbol(symbol, "/fapi/v1/allOpenOrders")
        return self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)

    def open_orders(self, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        if symbol is not None:
            symbol = self._require_symbol(symbol, "/fapi/v1/openOrders")
        data = self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True, retries=2)
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    def query_order(self, symbol: str, order_id: Optional[int] = None, client_id: Optional[str] = None) -> dict[str, Any]:
        symbol = self._require_symbol(symbol, "/fapi/v1/order")
        return self._request(
            "GET",
            "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_id},
            signed=True,
            retries=1,
        )

    def listen_key_create(self) -> str:
        data = self._request("POST", "/fapi/v1/listenKey", signed=False)
        key = str(data.get("listenKey", "")) if isinstance(data, dict) else ""
        try:
            return validate_listen_key(key)
        except ValueError as exc:
            raise BinanceError(-1, str(exc), 0, "/fapi/v1/listenKey") from exc

    def listen_key_keepalive(self, key: str) -> None:
        self._request("PUT", "/fapi/v1/listenKey", self._listen_key_params(key), signed=False)

    def listen_key_delete(self, key: str) -> None:
        self._request("DELETE", "/fapi/v1/listenKey", self._listen_key_params(key), signed=False)

    def server_time_ms(self) -> int:
        return self._timestamp()
