import os
import unittest
import unittest.mock
from unittest.mock import patch

from scalpbot.broker import OrderIdFactory, ProtectionOrders
from scalpbot.broker import TestnetBroker as _LiveBroker
from scalpbot.config import Config
from scalpbot.exchange import BinanceError, BinanceFuturesClient

from .test_exchange import FakeResponse, FakeSession


class RecordingClient:
    """Klien algo palsu: meniru respons /fapi/v1/algoOrder."""

    def __init__(self, fail_types=(), open_rows=None, fail_counts=None):
        self.fail_types = set(fail_types)
        self.fail_counts = dict(fail_counts or {})
        self.open_rows = list(open_rows or [])
        self.algo_orders = []
        self.cancelled = []
        self.cancel_all_called = 0
        self._seq = 0

    def new_algo_order(self, **params):
        kind = params.get("type")
        if self.fail_counts.get(kind, 0) > 0:
            self.fail_counts[kind] -= 1
            raise BinanceError(-4131, "would immediately trigger", 400, "/fapi/v1/algoOrder")
        if kind in self.fail_types:
            raise BinanceError(-4131, "would immediately trigger", 400, "/fapi/v1/algoOrder")
        self._seq += 1
        algo_id = 800000 + self._seq
        self.algo_orders.append(dict(params, algoId=algo_id, algoStatus="NEW"))
        return {"algoId": algo_id, "algoStatus": "NEW", "clientAlgoId": params.get("clientAlgoId")}

    def open_algo_orders(self, symbol=None):
        return [row for row in self.open_rows if symbol is None or row.get("symbol") in (None, symbol)]

    def cancel_algo_order(self, symbol, algo_id=None, client_id=None):
        self.cancelled.append((symbol, str(algo_id) if algo_id is not None else client_id))
        return {"algoId": algo_id, "clientAlgoId": client_id, "code": "200", "msg": "success"}

    def cancel_all_algo(self, symbol):
        self.cancel_all_called += 1
        return {"code": "200", "msg": "success"}


def broker(cfg=None):
    cfg = cfg or Config()
    cfg.execution.working_type = "MARK_PRICE"
    return _LiveBroker(RecordingClient(), cfg)


class TestAlgoEndpoint(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"})
        env.start()
        self.addCleanup(env.stop)

    def _client(self, responses):
        cfg = Config()
        cfg.mode = "testnet"
        session = FakeSession(responses)
        return BinanceFuturesClient(cfg, session=session), session

    def test_new_algo_order_always_sends_algo_type(self):
        client, session = self._client([FakeResponse(body={"algoId": 11, "algoStatus": "NEW"})])
        client.new_algo_order(symbol="BTCUSDT", side="SELL", type="STOP_MARKET", triggerPrice=99.0)
        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertIn("/fapi/v1/algoOrder", call["url"])
        self.assertIn("algoType=CONDITIONAL", call["url"])
        self.assertIn("triggerPrice=99", call["url"])
        self.assertNotIn("stopPrice", call["url"])
        self.assertIn("signature=", call["url"])

    def test_new_algo_order_drops_none_params(self):
        client, session = self._client([FakeResponse(body={"algoId": 12})])
        client.new_algo_order(symbol="BTCUSDT", side="SELL", type="STOP_MARKET", triggerPrice=99.0, price=None)
        url = session.calls[0]["url"]
        self.assertNotIn("price=", url)
        self.assertNotIn("None", url)

    def test_cancel_algo_order_by_id(self):
        client, session = self._client([FakeResponse(body={"algoId": 12, "code": "200"})])
        client.cancel_algo_order("BTCUSDT", algo_id="12")
        call = session.calls[0]
        self.assertEqual(call["method"], "DELETE")
        self.assertIn("/fapi/v1/algoOrder", call["url"])
        self.assertIn("algoId=12", call["url"])

    def test_cancel_algo_order_requires_identifier(self):
        client, _ = self._client([FakeResponse(body={})])
        with self.assertRaises(BinanceError):
            client.cancel_algo_order("BTCUSDT")

    def test_cancel_all_algo_hits_algo_open_orders(self):
        client, session = self._client([FakeResponse(body={"code": "200"})])
        client.cancel_all_algo("BTCUSDT")
        call = session.calls[0]
        self.assertEqual(call["method"], "DELETE")
        self.assertIn("/fapi/v1/algoOpenOrders", call["url"])
        self.assertIn("symbol=BTCUSDT", call["url"])

    def test_open_algo_orders_always_passes_symbol(self):
        rows = [{"algoId": 1, "orderType": "STOP_MARKET"}]
        client, session = self._client([FakeResponse(body=rows)])
        self.assertEqual(client.open_algo_orders("BTCUSDT")[0]["algoId"], 1)
        self.assertIn("symbol=BTCUSDT", session.calls[0]["url"])

    def test_open_algo_orders_accepts_wrapped_payload(self):
        client, _ = self._client([FakeResponse(body={"orders": [{"algoId": 2}]})])
        self.assertEqual(client.open_algo_orders("BTCUSDT")[0]["algoId"], 2)

    def test_query_algo_order_uses_client_id(self):
        client, session = self._client([FakeResponse(body={"algoId": 3, "actualOrderId": ""})])
        client.query_algo_order("BTCUSDT", client_id="scbsl1")
        self.assertIn("clientAlgoId=scbsl1", session.calls[0]["url"])


class TestAlgoBroker(unittest.TestCase):
    def test_place_protection_uses_trigger_price_and_algo_ids(self):
        br = broker()
        out = br.place_protection("BTCUSDT", "LONG", 0.01, 99.0, 101.0, "en")
        self.assertIsInstance(out, ProtectionOrders)
        self.assertEqual(out.stop_id, "800001")
        self.assertEqual(out.tp_id, "800002")
        sl, tp = br.client.algo_orders
        self.assertEqual(sl["type"], "STOP_MARKET")
        self.assertEqual(sl["triggerPrice"], 99.0)
        self.assertEqual(sl["side"], "SELL")
        self.assertEqual(sl["reduceOnly"], "true")
        self.assertEqual(sl["workingType"], "MARK_PRICE")
        self.assertLessEqual(len(sl["clientAlgoId"]), 36)
        self.assertEqual(tp["type"], "TAKE_PROFIT_MARKET")
        self.assertEqual(tp["triggerPrice"], 101.0)
        self.assertNotIn("stopPrice", sl)

    def test_place_protection_short_uses_buy(self):
        br = broker()
        br.place_protection("BTCUSDT", "SHORT", 0.01, 101.0, 99.0, "en")
        self.assertEqual({row["side"] for row in br.client.algo_orders}, {"BUY"})

    def test_stop_only_when_tp_disabled(self):
        cfg = Config()
        cfg.execution.place_tp = False
        br = broker(cfg)
        out = br.place_protection("BTCUSDT", "LONG", 1.0, 99.0, 101.0, "en")
        self.assertTrue(out.stop_id)
        self.assertIsNone(out.tp_id)
        self.assertEqual(len(br.client.algo_orders), 1)

    def test_place_protection_failure_rolls_back(self):
        cfg = Config()
        br = _LiveBroker(RecordingClient(fail_types=("TAKE_PROFIT_MARKET",)), cfg)
        with self.assertRaises(BinanceError):
            br.place_protection("BTCUSDT", "LONG", 1.0, 99.0, 101.0, "en")
        self.assertEqual(br.client.cancelled, [("BTCUSDT", "800001")])

    def test_replace_stop_cancels_old_algo_then_places_new(self):
        br = broker()
        out = br.replace_stop("BTCUSDT", "800000", "LONG", 1.0, 100.5, "be")
        self.assertEqual(br.client.cancelled, [("BTCUSDT", "800000")])
        self.assertEqual(out.stop_id, "800001")
        self.assertEqual(br.client.algo_orders[0]["triggerPrice"], 100.5)
        self.assertTrue(br.client.algo_orders[0]["clientAlgoId"].startswith("scbbe"))

    def test_replace_stop_restores_previous_trigger(self):
        rows = [
            {
                "symbol": "BTCUSDT",
                "algoId": 77,
                "orderType": "STOP_MARKET",
                "side": "SELL",
                "triggerPrice": "99.5",
            }
        ]
        br = _LiveBroker(RecordingClient(fail_counts={"STOP_MARKET": 1}, open_rows=rows), Config())
        out = br.replace_stop("BTCUSDT", None, "LONG", 1.0, 100.5, "tr")
        self.assertEqual(out.stop_id, "800001")
        self.assertEqual(br.client.algo_orders[0]["triggerPrice"], 99.5)

    def test_replace_stop_raises_when_restore_also_fails(self):
        rows = [
            {
                "symbol": "BTCUSDT",
                "algoId": 77,
                "orderType": "STOP_MARKET",
                "side": "SELL",
                "triggerPrice": "99.5",
            }
        ]
        br = _LiveBroker(RecordingClient(fail_types=("STOP_MARKET",), open_rows=rows), Config())
        with self.assertRaises(BinanceError):
            br.replace_stop("BTCUSDT", None, "LONG", 1.0, 100.5, "tr")

    def test_cancel_ignores_unknown_algo(self):
        br = broker()
        br.client.cancel_algo_order = lambda *a, **k: (_ for _ in ()).throw(BinanceError(-2011, "unknown order", 400, "x"))
        br.cancel("BTCUSDT", "12345")
        br.cancel("BTCUSDT", None)

    def test_cancel_reraises_real_errors(self):
        br = broker()
        br.client.cancel_algo_order = lambda *a, **k: (_ for _ in ()).throw(BinanceError(-1001, "internal", 500, "x"))
        with self.assertRaises(BinanceError):
            br.cancel("BTCUSDT", "12345")

    def test_open_stop_ids_filters_only_stops(self):
        rows = [
            {"algoId": 1, "orderType": "STOP_MARKET", "side": "SELL", "triggerPrice": "99"},
            {"algoId": 2, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL", "triggerPrice": "101"},
        ]
        br = _LiveBroker(RecordingClient(open_rows=rows), Config())
        self.assertEqual(br.open_stop_ids("BTCUSDT"), {"1"})
        self.assertEqual(len(br.open_protection("BTCUSDT")), 2)

    def test_cancel_all_clears_regular_and_algo(self):
        br = broker()
        called = []
        br.client.cancel_all = lambda symbol: called.append(symbol)
        br.cancel_all("BTCUSDT")
        self.assertEqual(called, ["BTCUSDT"])
        self.assertEqual(br.client.cancel_all_called, 1)

    def test_cancel_all_survives_errors(self):
        br = broker()

        def boom(symbol):
            raise BinanceError(-1001, "internal", 500, "x")

        br.client.cancel_all = boom
        br.client.cancel_all_algo = boom
        br.cancel_all("BTCUSDT")

    def test_order_id_fits_algo_client_id(self):
        ids = OrderIdFactory()
        for tag in ("ensl", "entp", "besl", "trsl", "rcsl"):
            cid = ids.make(tag)
            self.assertLessEqual(len(cid), 36)
            self.assertRegex(cid, r"^[\.A-Z\:/a-z0-9_-]{1,36}$")


class AlgoStubBroker:
    """Broker stub dengan antarmuka algo (open_protection/open_stop_ids)."""

    name = "stub"

    def __init__(self, positions=None, algo_rows=None):
        self.positions = positions or {}
        self.algo_rows = algo_rows or {}
        self.replaced = []
        self.cancels = []

    def equity(self):
        return 1000.0, 1000.0

    def open_positions(self):
        return dict(self.positions)

    def open_protection(self, symbol):
        return list(self.algo_rows.get(symbol, []))

    def open_stop_ids(self, symbol):
        return {row["id"] for row in self.algo_rows.get(symbol, []) if row["type"] == "STOP_MARKET"}

    def replace_stop(self, symbol, order_id, side, qty, stop, tag):
        self.replaced.append((symbol, order_id, stop, tag))
        return ProtectionOrders(stop_id="new-sl")

    def cancel(self, symbol, order_id):
        self.cancels.append((symbol, order_id))


class TestTraderAlgoIntegration(unittest.TestCase):
    def _trader(self, broker):
        from scalpbot.config import Config as Cfg
        from scalpbot.events import EventLog
        from scalpbot.orderbook import OrderbookFilter
        from scalpbot.risk import SymbolFilters
        from scalpbot.trader import ManagedPosition, Trader
        import time as _time

        cfg = Cfg()
        cfg.mode = "testnet"
        cfg.orderbook.enabled = False
        flt = SymbolFilters(
            symbol="TESTUSDT",
            tick_size=0.01,
            step_size=0.001,
            min_qty=0.001,
            max_qty=1000.0,
            min_notional=5.0,
            qty_precision=3,
            price_precision=2,
        )
        trader = Trader(cfg, broker, {"TESTUSDT": flt}, EventLog(capacity=50), OrderbookFilter(cfg.orderbook, lambda s, lim: None), 300)
        pos = ManagedPosition(
            symbol="TESTUSDT",
            side="LONG",
            qty=1.0,
            entry=100.0,
            stop=99.0,
            target=101.0,
            initial_stop=99.0,
            risk_dist=1.0,
            atr=1.0,
            opened_ts=_time.time(),
            signal_ts=0,
            stop_order_id="sl-algo-1",
            tp_order_id="tp-algo-1",
            key="k1",
        )
        trader.positions["TESTUSDT"] = [pos]
        return trader, pos

    def test_orphan_detection_reads_algo_orders(self):
        broker = AlgoStubBroker({"TESTUSDT": {"qty": 1.0, "entry": 100.0, "side": "LONG", "mark": 100.0, "unrealized": 0.0}})
        trader, pos = self._trader(broker)
        trader.check_orphan_protection()
        self.assertEqual(broker.replaced[0][:3], ("TESTUSDT", None, 99.0))
        self.assertEqual(broker.replaced[0][3], "rs")
        self.assertEqual(pos.stop_order_id, "new-sl")

    def test_orphan_detection_quiet_when_algo_stop_present(self):
        broker = AlgoStubBroker(
            {"TESTUSDT": {"qty": 1.0, "entry": 100.0, "side": "LONG", "mark": 100.0, "unrealized": 0.0}},
            {"TESTUSDT": [{"id": "sl-algo-1", "type": "STOP_MARKET", "side": "SELL", "trigger": 99.0, "qty": 1.0, "status": "NEW"}]},
        )
        trader, _ = self._trader(broker)
        trader.check_orphan_protection()
        self.assertEqual(broker.replaced, [])

    def test_unknown_position_logs_but_keeps_manual_algos(self):
        broker = AlgoStubBroker(
            {
                "TESTUSDT": {"qty": 1.0, "entry": 100.0, "side": "LONG", "mark": 100.0, "unrealized": 0.0},
                "MANUALUSDT": {"qty": 2.0, "entry": 50.0, "side": "SHORT", "mark": 50.0, "unrealized": 0.0},
            },
            {"MANUALUSDT": [{"id": "x1", "type": "STOP_MARKET", "side": "BUY", "trigger": 52.0, "qty": 2.0, "status": "NEW"}]},
        )
        trader, _ = self._trader(broker)
        trader.sync_with_exchange(True)
        self.assertEqual(broker.cancels, [])
        messages = [ev.message for ev in trader.events.tail(10)]
        self.assertTrue(any("di luar bot" in m for m in messages))


class TestAlgoUpdateEvent(unittest.IsolatedAsyncioTestCase):
    """ALGO_UPDATE wajib memicu rekonsiliasi seperti ORDER_TRADE_UPDATE."""

    def _app(self):
        from scalpbot.app import ScalpBotApp

        app = ScalpBotApp.__new__(ScalpBotApp)
        app.trader = unittest.mock.MagicMock()
        app.trader.sync_with_exchange = unittest.mock.MagicMock(return_value=None)
        from scalpbot.events import EventLog

        app.events = EventLog(capacity=20)
        app._recreate_listen_key = unittest.mock.MagicMock(return_value=None)
        return app

    async def test_finished_algo_records_fill_and_syncs(self):
        app = self._app()
        payload = {
            "e": "ALGO_UPDATE",
            "o": {"s": "BTCUSDT", "X": "FINISHED", "o": "STOP_MARKET", "ap": "98.5", "aid": 42, "caid": "scbsl1"},
        }
        await app._handle_user(payload)
        app.trader.record_external_fill.assert_called_once_with("BTCUSDT", 98.5)
        app.trader.sync_with_exchange.assert_called_once_with(True)
        messages = [ev.message for ev in app.events.tail(5)]
        self.assertTrue(any("STOP_MARKET" in m for m in messages))

    async def test_triggering_and_canceled_are_logged(self):
        app = self._app()
        await app._handle_user({"e": "ALGO_UPDATE", "o": {"s": "ETHUSDT", "X": "TRIGGERING", "o": "TAKE_PROFIT_MARKET"}})
        await app._handle_user({"e": "ALGO_UPDATE", "o": {"s": "ETHUSDT", "X": "CANCELED", "o": "STOP_MARKET"}})
        messages = [ev.message for ev in app.events.tail(5)]
        self.assertEqual(len(messages), 2)


if __name__ == "__main__":
    unittest.main()
