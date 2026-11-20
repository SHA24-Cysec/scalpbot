from __future__ import annotations

import base64
import itertools
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from .config import SYMBOL_RE, Config
from .exchange import BinanceError, BinanceFuturesClient
from .risk import SymbolFilters, round_tick
from .security import clean_message

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Fill:
    order_id: str
    avg_price: float
    qty: float
    ts: float
    fee: float = 0.0


@dataclass(slots=True)
class ProtectionOrders:
    stop_id: Optional[str] = None
    tp_id: Optional[str] = None


class OrderIdFactory:
    def __init__(self, prefix: str = "scb") -> None:
        self.prefix = prefix
        self._seq = itertools.count(1)

    def make(self, tag: str) -> str:
        tag = "".join(ch for ch in tag.lower() if ch.isalnum())[:5] or "x"
        token = base64.b32encode(os.urandom(3)).decode().lower().rstrip("=")[:4]
        return f"{self.prefix}{tag}{int(time.time()) % 100000}{next(self._seq) % 1000:03d}{token}"


class TestnetBroker:
    name = "binance"

    def __init__(self, client: BinanceFuturesClient, cfg: Config) -> None:
        self.client = client
        self.cfg = cfg
        self.ids = OrderIdFactory()

    def prepare_symbol(self, symbol: str) -> None:
        self.client.set_margin_type(symbol, self.cfg.risk.margin_type)
        self.client.set_leverage(symbol, self.cfg.risk.leverage)

    def filters(self, symbol: str) -> SymbolFilters:
        return self.client.filters(symbol)

    def equity(self) -> tuple[float, float]:
        return self.client.usdt_equity()

    def place_market(self, symbol: str, side: str, qty: float, tag: str) -> Fill:
        cid = self.ids.make(tag)
        try:
            data = self.client.new_order(
                symbol=symbol,
                side="BUY" if side == "LONG" else "SELL",
                type="MARKET",
                quantity=qty,
                newClientOrderId=cid,
                newOrderRespType="RESULT",
            )
        except BinanceError:
            recovered = self._recover_fill(symbol, cid, qty)
            if recovered is not None:
                return recovered
            raise
        avg = float(data.get("avgPrice") or 0.0)
        filled = float(data.get("executedQty") or 0.0)
        if not math.isfinite(filled) or filled <= 0:
            # Jangan pakai qty yang diminta: proteksi harus memakai qty yang benar-benar terisi.
            recovered = self._recover_fill(symbol, cid, qty)
            if recovered is None:
                raise BinanceError(
                    -1, f"entry {symbol} tidak terisi (executedQty={filled:g})", 0, "/fapi/v1/order"
                )
            return recovered
        if avg <= 0 or not math.isfinite(avg):
            avg = self._avg_from_order(symbol, cid)
        return Fill(order_id=str(data.get("orderId", cid)), avg_price=avg, qty=filled, ts=time.time())

    def _recover_fill(self, symbol: str, cid: str, qty: float) -> Optional[Fill]:
        try:
            info = self.client.query_order(symbol, client_id=cid)
        except BinanceError:
            return None
        status = str(info.get("status", ""))
        executed = float(info.get("executedQty") or 0.0)
        avg = float(info.get("avgPrice") or 0.0)
        if status in ("FILLED", "PARTIALLY_FILLED") and executed > 0 and avg > 0:
            return Fill(
                order_id=str(info.get("orderId", cid)),
                avg_price=avg,
                qty=executed,
                ts=time.time(),
            )
        return None

    def _avg_from_order(self, symbol: str, cid: str) -> float:
        try:
            info = self.client.query_order(symbol, client_id=cid)
            avg = float(info.get("avgPrice") or 0.0)
            return avg if math.isfinite(avg) else 0.0
        except (BinanceError, TypeError, ValueError):
            return 0.0

    @staticmethod
    def _algo_id(data: dict, symbol: str) -> str:
        algo_id = str(data.get("algoId") or "")
        if not algo_id:
            raise BinanceError(-1, f"respons algo {symbol} tanpa algoId: {data}", 0, "/fapi/v1/algoOrder")
        return algo_id

    def _verify_algo(self, symbol: str, algo_id: str, client_id: str) -> None:
        try:
            rows = self.client.open_algo_orders(symbol)
        except (BinanceError, AttributeError, TypeError) as exc:
            raise BinanceError(
                -1, f"verifikasi algo {algo_id} gagal: {exc}", 0, "/fapi/v1/openAlgoOrders"
            ) from exc
        for row in rows:
            if not isinstance(row, dict):
                continue
            same = str(row.get("algoId", "")) == algo_id or str(row.get("clientAlgoId", "")) == client_id
            if not same:
                continue
            status = clean_message(str(row.get("algoStatus", "NEW"))).upper()
            if status not in {"NEW", "TRIGGERING", "ACTIVE", "WORKING"}:
                raise BinanceError(-1, f"algo {algo_id} status {status}", 0, "/fapi/v1/openAlgoOrders")
            return
        raise BinanceError(-1, f"algo {algo_id} tidak ditemukan", 0, "/fapi/v1/openAlgoOrders")

    def _algo_stop(self, symbol: str, exit_side: str, qty: float, stop: float, tag: str) -> str:
        client_id = self.ids.make(tag + "sl")
        data = self.client.new_algo_order(
            symbol=symbol,
            side=exit_side,
            type="STOP_MARKET",
            quantity=qty,
            triggerPrice=stop,
            reduceOnly="true",
            workingType=self.cfg.execution.working_type,
            clientAlgoId=client_id,
            newOrderRespType="RESULT",
        )
        algo_id = self._algo_id(data, symbol)
        self._verify_algo(symbol, algo_id, client_id)
        return algo_id

    def _algo_tp(self, symbol: str, exit_side: str, qty: float, target: float, tag: str) -> str:
        client_id = self.ids.make(tag + "tp")
        data = self.client.new_algo_order(
            symbol=symbol,
            side=exit_side,
            type="TAKE_PROFIT_MARKET",
            quantity=qty,
            triggerPrice=target,
            reduceOnly="true",
            workingType=self.cfg.execution.working_type,
            clientAlgoId=client_id,
            newOrderRespType="RESULT",
        )
        algo_id = self._algo_id(data, symbol)
        self._verify_algo(symbol, algo_id, client_id)
        return algo_id

    def _snap(self, symbol: str, price: float, toward: str) -> float:
        """Bulatkan trigger ke kelipatan tick; arah pembulatan memperlebar jarak dari harga pasar."""
        if not math.isfinite(price) or price <= 0:
            return price
        try:
            tick = float(self.client.filters(symbol).tick_size)
        except (BinanceError, AttributeError, TypeError, ValueError):
            return price
        if not math.isfinite(tick) or tick <= 0:
            return price
        snapped = round_tick(price, tick, toward)
        return snapped if snapped > 0 else price

    def place_protection(
        self, symbol: str, side: str, qty: float, stop: float, target: Optional[float], tag: str
    ) -> ProtectionOrders:
        exit_side = "SELL" if side == "LONG" else "BUY"
        stop = self._snap(symbol, stop, "down" if side == "LONG" else "up")
        if target and target > 0:
            target = self._snap(symbol, target, "up" if side == "LONG" else "down")
        out = ProtectionOrders()
        try:
            if self.cfg.execution.place_sl and stop > 0:
                out.stop_id = self._algo_stop(symbol, exit_side, qty, stop, tag)
            if self.cfg.execution.place_tp and target and target > 0:
                out.tp_id = self._algo_tp(symbol, exit_side, qty, target, tag)
        except BinanceError:
            self._cancel_ids(symbol, out.stop_id, out.tp_id)
            raise
        return out

    def place_tp(self, symbol: str, side: str, qty: float, target: float, tag: str) -> ProtectionOrders:
        """Pasang TP saja (dipakai saat adopsi posisi lama yang kehilangan TP)."""
        exit_side = "SELL" if side == "LONG" else "BUY"
        target = self._snap(symbol, target, "up" if side == "LONG" else "down")
        return ProtectionOrders(tp_id=self._algo_tp(symbol, exit_side, qty, target, tag))

    def replace_stop(
        self, symbol: str, order_id: Optional[str], side: str, qty: float, stop: float, tag: str
    ) -> ProtectionOrders:
        exit_side = "SELL" if side == "LONG" else "BUY"
        stop = self._snap(symbol, stop, "down" if side == "LONG" else "up")
        previous = self._previous_stop(symbol, side)
        self.cancel(symbol, order_id)
        try:
            return ProtectionOrders(stop_id=self._algo_stop(symbol, exit_side, qty, stop, tag))
        except BinanceError:
            if previous is not None and previous != stop:
                return ProtectionOrders(stop_id=self._algo_stop(symbol, exit_side, qty, previous, tag))
            raise

    def _cancel_ids(self, symbol: str, *ids: Optional[str]) -> None:
        for order_id in ids:
            self.cancel(symbol, order_id)

    def _previous_stop(self, symbol: str, side: str) -> Optional[float]:
        wanted = "SELL" if side == "LONG" else "BUY"
        try:
            orders = self.client.open_algo_orders(symbol)
        except (BinanceError, AttributeError):
            return None
        for order in orders:
            if not isinstance(order, dict):
                continue
            if order.get("orderType") in ("STOP_MARKET", "STOP") and order.get("side") == wanted:
                try:
                    value = float(order.get("triggerPrice", 0) or 0)
                except (TypeError, ValueError):
                    return None
                if value > 0:
                    return value
        return None

    def cancel(self, symbol: str, order_id: Optional[str]) -> None:
        if not order_id:
            return
        try:
            self.client.cancel_algo_order(symbol, algo_id=order_id)
        except BinanceError as exc:
            if exc.code not in (-2011, -2013, -4045):
                raise

    def close_market(self, symbol: str, side: str, qty: float, tag: str, position_key: Optional[str] = None) -> Fill:
        cid = self.ids.make(tag + "x")
        try:
            data = self.client.new_order(
                symbol=symbol,
                side="SELL" if side == "LONG" else "BUY",
                type="MARKET",
                quantity=qty,
                reduceOnly="true",
                newClientOrderId=cid,
                newOrderRespType="RESULT",
            )
        except BinanceError:
            recovered = self._recover_fill(symbol, cid, qty)
            if recovered is not None:
                return recovered
            raise
        avg = float(data.get("avgPrice") or 0.0)
        if avg <= 0 or not math.isfinite(avg):
            avg = self._avg_from_order(symbol, cid)
        try:
            filled = float(data.get("executedQty") or 0.0)
        except (TypeError, ValueError):
            filled = 0.0
        if not math.isfinite(filled) or filled <= 0:
            recovered = self._recover_fill(symbol, cid, qty)
            if recovered is None:
                raise BinanceError(
                    -1,
                    f"close {symbol} tidak terisi (executedQty={data.get('executedQty')!r})",
                    0,
                    "/fapi/v1/order",
                )
            return recovered
        return Fill(order_id=str(data.get("orderId", cid)), avg_price=avg, qty=filled, ts=time.time())

    def emergency_close(
        self, symbol: str, side: str, qty: float, tag: str, position_key: str | None = None
    ) -> Fill:
        last_exc: Exception | None = None
        remaining = qty
        for attempt in range(3):
            for use_close_position in (False, True):
                try:
                    if use_close_position:
                        fill = self._close_position_flag(symbol, side, tag, attempt)
                    else:
                        fill = self.close_market(symbol, side, remaining, f"{tag}em{attempt}", position_key)
                    if not math.isfinite(fill.qty) or fill.qty <= 0:
                        raise BinanceError(-1, "fill close darurat kosong", 0, "/fapi/v1/order")
                    remaining = self._remaining_qty(symbol)
                    if remaining <= 1e-9:
                        return fill
                    last_exc = BinanceError(
                        -1, f"sisa posisi {remaining:g} setelah close", 0, "/fapi/v3/positionRisk"
                    )
                except BinanceError as exc:
                    last_exc = exc
            time.sleep(0.25 * (attempt + 1))
        raise BinanceError(
            -1,
            f"close darurat {symbol} gagal setelah 3 percobaan: {last_exc}",
            0,
            "/fapi/v1/order",
        )

    def _close_position_flag(self, symbol: str, side: str, tag: str, attempt: int) -> Fill:
        cid = self.ids.make(f"{tag}cp{attempt}")
        data = self.client.new_order(
            symbol=symbol,
            side="SELL" if side == "LONG" else "BUY",
            type="MARKET",
            closePosition="true",
            newClientOrderId=cid,
            newOrderRespType="RESULT",
        )
        try:
            filled = float(data.get("executedQty") or 0.0)
        except (TypeError, ValueError):
            filled = 0.0
        if not math.isfinite(filled) or filled <= 0:
            recovered = self._recover_fill(symbol, cid, 0.0)
            if recovered is None:
                raise BinanceError(-1, "closePosition tidak terisi", 0, "/fapi/v1/order")
            return recovered
        avg = float(data.get("avgPrice") or 0.0)
        return Fill(order_id=str(data.get("orderId", cid)), avg_price=avg, qty=filled, ts=time.time())

    def _remaining_qty(self, symbol: str) -> float:
        try:
            rows = self.client.position_risk(symbol)
        except (BinanceError, AttributeError, TypeError) as exc:
            raise BinanceError(
                -1, f"verifikasi posisi gagal: {exc}", 0, "/fapi/v3/positionRisk"
            ) from exc
        total = 0.0
        for row in rows:
            if not isinstance(row, dict) or row.get("symbol") != symbol:
                continue
            try:
                total += abs(float(row.get("positionAmt", 0) or 0))
            except (TypeError, ValueError):
                continue
        return total

    def cancel_all(self, symbol: str) -> None:
        for call in (self.client.cancel_all, self.client.cancel_all_algo):
            try:
                call(symbol)
            except BinanceError:
                pass

    def income_since(self, start_ms: int, label: Optional[str] = None) -> Optional[dict]:
        """Rincian PnL dari `start_ms` sampai sekarang, untuk dasar batas rugi harian.

        `start_ms` dikirim pemanggil supaya batas hari bisa mengikuti zona waktu bot
        (WIB), bukan dipaksa 00:00 UTC. Bila `start_ms` jatuh di tengah hari UTC,
        `/fapi/v1/income` tetap melayani karena endpoint itu menerima rentang bebas.
        """
        now = datetime.now(timezone.utc)
        end_ms = int(now.timestamp() * 1000)
        kinds: dict[str, float] = {}
        by_symbol: dict[str, float] = {}
        counts: dict[str, int] = {}
        for kind in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
            try:
                rows = self.client.income_history(kind, start_ms=start_ms, end_ms=end_ms, limit=1000)
            except BinanceError as exc:
                log.debug("income %s gagal: %s", kind, exc)
                return None
            counts[kind] = len(rows)
            subtotal = 0.0
            for row in rows:
                try:
                    value = float(row.get("income") or 0.0)
                except (TypeError, ValueError):
                    continue
                subtotal += value
                raw_symbol = row.get("symbol")
                symbol = clean_message(str(raw_symbol)).upper() if raw_symbol else "OTHER"
                if not SYMBOL_RE.fullmatch(symbol):
                    symbol = "OTHER"
                by_symbol[symbol] = by_symbol.get(symbol, 0.0) + value
            kinds[kind] = subtotal
        return {
            "day": label or now.strftime("%Y-%m-%d"),
            "total": sum(kinds.values()),
            "kinds": kinds,
            "by_symbol": by_symbol,
            "counts": counts,
            "start_ms": start_ms,
            "end_ms": end_ms,
        }

    def income_today(self) -> Optional[dict]:
        """Rincian PnL versi Binance untuk hari exchange UTC (00:00 UTC).

        Ini **bukan** dasar batas rugi harian, batas itu memakai hari bot (WIB) lewat
        `realized_since`. Angka di sini dipakai `pnl` supaya cocok dengan aplikasi
        Binance, yang memang memotong hari pada 00:00 UTC.
        """
        now = datetime.now(timezone.utc)
        start_ms = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        return self.income_since(start_ms, label=now.strftime("%Y-%m-%d"))

    def realized_since(self, start_ms: int) -> Optional[float]:
        """Total PnL bersih sejak `start_ms` menurut Binance, atau None bila gagal.

        REALIZED_PNL + COMMISSION + FUNDING_FEE dijumlahkan supaya angkanya setara
        pnl_net bot (fee masuk; funding membuat angkanya lebih konservatif).
        Dipakai sebagai dasar batas rugi harian yang tahan restart, dengan `start_ms`
        = awal hari bot (00:00 WIB), bukan 00:00 UTC.
        """
        info = self.income_since(start_ms)
        if info is None:
            return None
        for kind, count in info["counts"].items():
            if count >= 1000:
                log.warning("income %s mencapai limit 1000 baris, batas rugi harian bisa kurang tepat", kind)
        return float(info["total"])

    def realized_today(self) -> Optional[float]:
        """Pnl bersih hari exchange UTC (00:00 UTC) menurut Binance.

        Dipertahankan untuk kompatibilitas; **jangan** dipakai sebagai dasar batas rugi
        harian karena hari bot mengikuti `logging.timezone`. Pakai `realized_since`.
        """
        info = self.income_today()
        if info is None:
            return None
        for kind, count in info["counts"].items():
            if count >= 1000:
                log.warning("income %s mencapai limit 1000 baris, batas rugi harian bisa kurang tepat", kind)
        return float(info["total"])

    def open_protection(self, symbol: str) -> list[dict]:
        rows: list[dict] = []
        for order in self.client.open_algo_orders(symbol):
            if not isinstance(order, dict):
                continue
            kind = clean_message(str(order.get("orderType", "")))
            if kind not in ("STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
                continue
            try:
                trigger = float(order.get("triggerPrice", 0) or 0)
            except (TypeError, ValueError):
                trigger = 0.0
            rows.append(
                {
                    "id": clean_message(str(order.get("algoId") or "")),
                    "type": kind,
                    "side": clean_message(str(order.get("side", ""))),
                    "trigger": trigger,
                    "qty": float(order.get("quantity", 0) or 0) or 0.0,
                    "status": clean_message(str(order.get("algoStatus", ""))),
                }
            )
        return rows

    def open_stop_ids(self, symbol: str) -> set[str]:
        return {row["id"] for row in self.open_protection(symbol) if row["type"] in ("STOP_MARKET", "STOP")}

    def open_positions(self) -> dict[str, dict]:
        rows = self.client.position_risk()
        out: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            if not SYMBOL_RE.fullmatch(symbol):
                continue
            amt = float(row.get("positionAmt", 0) or 0)
            if amt != 0:
                out[symbol] = {
                    "qty": abs(amt),
                    "entry": float(row.get("entryPrice", 0) or 0),
                    "side": "LONG" if amt > 0 else "SHORT",
                    "mark": float(row.get("markPrice", 0) or 0),
                    "unrealized": float(row.get("unRealizedProfit", 0) or 0),
                }
        return out


class PaperBroker:
    name = "paper"

    def __init__(
        self,
        cfg: Config,
        filters_provider: Callable[[str], SymbolFilters],
        price_provider: Callable[[str], Optional[float]],
        start_balance: float = 1000.0,
    ) -> None:
        self.cfg = cfg
        self._filters = filters_provider
        self._price = price_provider
        self.ids = OrderIdFactory("pap")
        self.start_balance = start_balance
        self.cash = start_balance
        self.realized = 0.0
        self.fees = 0.0
        self._seq = 0
        self._lock = threading.RLock()
        self._positions: dict[str, dict] = {}

    def prepare_symbol(self, symbol: str) -> None:
        return None

    def filters(self, symbol: str) -> SymbolFilters:
        return self._filters(symbol)

    def _next_id(self) -> str:
        self._seq += 1
        return f"paper-{self._seq}"

    def equity(self) -> tuple[float, float]:
        with self._lock:
            unreal = sum(p["unrealized"] for p in self._positions.values())
            return self.cash + unreal, self.cash

    def place_market(self, symbol: str, side: str, qty: float, tag: str) -> Fill:
        price = self._price(symbol)
        if not price or not math.isfinite(price) or price <= 0 or not math.isfinite(qty) or qty <= 0:
            raise BinanceError(-1, f"harga/qty paper tidak valid untuk {symbol}")
        slip = price * self.cfg.risk.slippage_bps / 10_000.0
        fill = price + slip if side == "LONG" else price - slip
        fee = fill * qty * self.cfg.risk.fee_rate
        key = self._next_id()
        with self._lock:
            self.cash -= fee
            self.fees += fee
            self._positions[key] = {
                "symbol": symbol,
                "qty": qty,
                "entry": fill,
                "side": side,
                "mark": fill,
                "unrealized": 0.0,
            }
        return Fill(order_id=key, avg_price=fill, qty=qty, ts=time.time(), fee=fee)

    def place_protection(
        self, symbol: str, side: str, qty: float, stop: float, target: Optional[float], tag: str
    ) -> ProtectionOrders:
        return ProtectionOrders(stop_id=self._next_id(), tp_id=self._next_id() if target else None)

    def replace_stop(self, symbol: str, order_id: Optional[str], side: str, qty: float, stop: float, tag: str) -> ProtectionOrders:
        return ProtectionOrders(stop_id=self._next_id())

    def place_tp(self, symbol: str, side: str, qty: float, target: float, tag: str) -> ProtectionOrders:
        return ProtectionOrders(tp_id=self._next_id())

    def cancel(self, symbol: str, order_id: Optional[str]) -> None:
        return None

    def cancel_all(self, symbol: str) -> None:
        return None

    def realized_today(self) -> Optional[float]:
        return None

    def open_protection(self, symbol: str) -> list[dict]:
        # Paper broker tidak menyimpan order algo; proteksi disimulasikan di trader.
        return []

    def open_stop_ids(self, symbol: str) -> set[str]:
        return set()

    def close_market(self, symbol: str, side: str, qty: float, tag: str, position_key: Optional[str] = None) -> Fill:
        price = self._price(symbol)
        if not price or not math.isfinite(price) or price <= 0:
            raise BinanceError(-1, f"harga paper tidak tersedia untuk {symbol}")
        with self._lock:
            if position_key and position_key in self._positions:
                pos = self._positions.pop(position_key)
            else:
                matches = [(k, v) for k, v in self._positions.items() if v["symbol"] == symbol]
                if not matches:
                    raise BinanceError(-1, f"tidak bisa menutup posisi paper {symbol}")
                key, pos = matches[0]
                self._positions.pop(key)
        slip = price * self.cfg.risk.slippage_bps / 10_000.0
        fill = price - slip if side == "LONG" else price + slip
        gross = (fill - pos["entry"]) * qty if side == "LONG" else (pos["entry"] - fill) * qty
        fee = fill * qty * self.cfg.risk.fee_rate
        with self._lock:
            self.cash += gross - fee
            self.fees += fee
            self.realized += gross - fee
        return Fill(order_id=self._next_id(), avg_price=fill, qty=qty, ts=time.time(), fee=fee)

    def emergency_close(
        self, symbol: str, side: str, qty: float, tag: str, position_key: str | None = None
    ) -> Fill:
        fill = self.close_market(symbol, side, qty, tag, position_key=position_key)
        with self._lock:
            still_open = any(pos["symbol"] == symbol for pos in self._positions.values())
        if still_open:
            raise BinanceError(-1, f"posisi paper {symbol} masih terbuka", 0, "paper")
        return fill

    def mark(self, symbol: str, price: float) -> None:
        if not math.isfinite(price) or price <= 0:
            return
        with self._lock:
            for pos in self._positions.values():
                if pos["symbol"] != symbol:
                    continue
                pos["mark"] = price
                if pos["side"] == "LONG":
                    pos["unrealized"] = (price - pos["entry"]) * pos["qty"]
                else:
                    pos["unrealized"] = (pos["entry"] - price) * pos["qty"]

    def open_positions(self) -> dict[str, dict]:
        with self._lock:
            items = [dict(v) for v in self._positions.values() if v["qty"] > 0]
        out: dict[str, dict] = {}
        for pos in items:
            symbol = pos["symbol"]
            if symbol not in out:
                out[symbol] = {
                    "qty": pos["qty"],
                    "entry": pos["entry"],
                    "side": pos["side"],
                    "mark": pos["mark"],
                    "unrealized": pos["unrealized"],
                }
                continue
            current = out[symbol]
            total = current["qty"] + pos["qty"]
            current["entry"] = (current["entry"] * current["qty"] + pos["entry"] * pos["qty"]) / total
            current["qty"] = total
            current["unrealized"] += pos["unrealized"]
        return out
