
import os
import time
import unittest
from unittest.mock import patch

from scalpbot.broker import TestnetBroker as LiveBroker
from scalpbot.broker import ProtectionOrders
from scalpbot.config import Config
from scalpbot.events import EventLog
from scalpbot.exchange import BinanceError, BinanceFuturesClient, fmt_num
from scalpbot.orderbook import OrderbookFilter
from scalpbot.risk import SymbolFilters, breakeven_price, round_tick
from scalpbot.trader import ManagedPosition, Trader


def filters(tick=0.01, step=0.001):
    return SymbolFilters(
        symbol="TESTUSDT",
        tick_size=tick,
        step_size=step,
        min_qty=step,
        max_qty=1000.0,
        min_notional=5.0,
        qty_precision=3,
        price_precision=2,
    )


class OrderClient:
    """Klien order palsu dengan respons yang bisa dikonfigurasi."""

    def __init__(self, resp=None, query=None, tick=0.0):
        self.resp = resp or {"orderId": 1, "avgPrice": "100.5", "executedQty": "0.005"}
        self.query = query or {"avgPrice": "100.5", "executedQty": "0.005", "status": "FILLED"}
        self.tick = tick
        self.calls = []

    def new_order(self, **params):
        self.calls.append(params)
        return dict(self.resp)

    def query_order(self, symbol, order_id=None, client_id=None):
        return dict(self.query)

    def filters(self, symbol):
        flt = filters(tick=self.tick or 0.01)
        return flt


class TestFillQuantity(unittest.TestCase):
    def test_market_order_never_invents_quantity(self):
        client = OrderClient(resp={"orderId": 1, "avgPrice": "100.5", "executedQty": "0"})
        client.query = {"avgPrice": "0", "executedQty": "0", "status": "NEW"}
        broker = LiveBroker(client, Config())
        with self.assertRaises(BinanceError):
            broker.place_market("TESTUSDT", "LONG", 0.005, "en")

    def test_market_order_reports_executed_qty(self):
        client = OrderClient(resp={"orderId": 1, "avgPrice": "100.5", "executedQty": "0.003"})
        fill = LiveBroker(client, Config()).place_market("TESTUSDT", "LONG", 0.005, "en")
        self.assertAlmostEqual(fill.qty, 0.003)

    def test_partial_fill_recovered_from_query(self):
        client = OrderClient(resp={"orderId": 1, "avgPrice": "0", "executedQty": "0"})
        client.query = {"avgPrice": "99.5", "executedQty": "0.004", "status": "PARTIALLY_FILLED"}
        fill = LiveBroker(client, Config()).place_market("TESTUSDT", "LONG", 0.005, "en")
        self.assertAlmostEqual(fill.qty, 0.004)
        self.assertAlmostEqual(fill.avg_price, 99.5)

    def test_missing_algo_id_raises(self):
        class C:
            def new_algo_order(self, **params):
                return {"algoStatus": "NEW"}

            def filters(self, symbol):
                raise BinanceError(-1, "x")

        broker = LiveBroker(C(), Config())
        with self.assertRaises(BinanceError):
            broker._algo_stop("TESTUSDT", "SELL", 1.0, 99.0, "en")


class TestTickSnapping(unittest.TestCase):
    def test_trigger_price_snapped_to_tick(self):
        class C:
            def __init__(self):
                self.params = None

            def new_algo_order(self, **params):
                self.params = params
                return {"algoId": 7}

            def open_algo_orders(self, symbol=None):
                return [{
                    "algoId": 7,
                    "clientAlgoId": (self.params or {}).get("clientAlgoId"),
                    "algoStatus": "NEW",
                    "symbol": (self.params or {}).get("symbol"),
                }]

            def filters(self, symbol):
                return filters(tick=0.01)

        client = C()
        broker = LiveBroker(client, Config())
        broker.place_protection("TESTUSDT", "LONG", 1.0, 99.98765, None, "en")
        self.assertAlmostEqual(client.params["triggerPrice"], 99.98)
        self.assertAlmostEqual(client.params["triggerPrice"] % 0.01, 0.0, places=8)

    def test_target_snapped_up_for_long(self):
        class C:
            def __init__(self):
                self.params = None

            def new_algo_order(self, **params):
                self.params = params
                return {"algoId": 8}

            def open_algo_orders(self, symbol=None):
                return [{
                    "algoId": 8,
                    "clientAlgoId": (self.params or {}).get("clientAlgoId"),
                    "algoStatus": "NEW",
                    "symbol": (self.params or {}).get("symbol"),
                }]

            def filters(self, symbol):
                return filters(tick=0.01)

        client = C()
        broker = LiveBroker(client, Config())
        broker.place_protection("TESTUSDT", "LONG", 1.0, 98.0, 101.234, "en")
        self.assertGreaterEqual(client.params["triggerPrice"], 101.234)

    def test_float_noise_removed_from_signed_query(self):
        self.assertEqual(fmt_num(100.11000000000001), "100.11")
        self.assertEqual(fmt_num(0.1 + 0.2), "0.3")
        self.assertEqual(fmt_num(1000.0), "1000")
        self.assertIsNone(fmt_num(float("inf")))

    def test_signed_request_uses_clean_numbers(self):
        env = patch.dict(os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"})
        env.start()
        self.addCleanup(env.stop)
        from tests.test_exchange import FakeResponse, FakeSession

        session = FakeSession([FakeResponse(body={"algoId": 1})])
        cfg = Config()
        cfg.mode = "testnet"
        client = BinanceFuturesClient(cfg, session=session)
        client.new_algo_order(symbol="TESTUSDT", side="SELL", type="STOP_MARKET", triggerPrice=100.11000000000001)
        self.assertIn("triggerPrice=100.11&", session.calls[0]["url"])


class StubBroker:
    name = "stub"

    def __init__(self, realized=-2.0, fail=False):
        self.realized = realized
        self.fail = fail
        self.replaced = []

    def equity(self):
        return 100.0, 100.0

    def open_positions(self):
        return {}

    def realized_since(self, start_ms=None):
        self.start_ms = start_ms
        if self.fail:
            raise BinanceError(-1001, "down", 500, "/fapi/v1/income")
        return self.realized

    def replace_stop(self, symbol, order_id, side, qty, stop, tag):
        self.replaced.append(stop)
        return ProtectionOrders(stop_id="s2")

    def cancel(self, symbol, order_id):
        return None

    def cancel_all(self, symbol):
        return None

    def emergency_close(self, *args, **kwargs):
        from scalpbot.broker import Fill
        return Fill(order_id="em", avg_price=100.0, qty=1.0, ts=time.time())


def make_trader(broker, cfg=None):
    cfg = cfg or Config()
    cfg.mode = "testnet"
    cfg.orderbook.enabled = False
    events = EventLog(capacity=100)
    return (
        Trader(cfg, broker, {"TESTUSDT": filters()}, events, OrderbookFilter(cfg.orderbook, lambda s, lim: None), 300),
        events,
    )


class TestDailyBaselineFixes(unittest.TestCase):
    def test_new_day_refetches_baseline(self):
        broker = StubBroker(realized=-3.0)
        trader, _ = make_trader(broker)
        trader.equity = 100.0
        trader.day.start_equity = 100.0
        trader.sync_day_baseline()
        broker.realized = -8.0
        trader.day.day = "2000-01-01"
        trader._roll_day()
        self.assertAlmostEqual(trader.day_baseline, -8.0, places=6)

    def test_income_failure_is_throttled(self):
        broker = StubBroker(fail=True)
        calls = []
        original = broker.realized_since

        def counting(start_ms=None):
            calls.append(1)
            return original(start_ms)

        broker.realized_since = counting
        trader, _ = make_trader(broker)
        trader.equity = 100.0
        trader.day.start_equity = 100.0
        for _ in range(30):
            trader._last_equity_poll = 0.0
            trader.refresh_equity()
        self.assertEqual(len(calls), 1)

    def test_zero_qty_fill_does_not_call_emergency_close(self):
        trader, _ = make_trader(StubBroker())
        called = []
        trader._emergency_close = lambda *a, **k: called.append(a)
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
            opened_ts=time.time(),
            signal_ts=0,
            key="k",
        )
        trader.positions["TESTUSDT"] = [pos]
        self.assertEqual(called, [])


class TestBreakEvenSnapping(unittest.TestCase):
    def _run(self, price, stop=99.0):
        broker = StubBroker()
        trader, _ = make_trader(broker)
        trader.equity = 100.0
        pos = ManagedPosition(
            symbol="TESTUSDT",
            side="LONG",
            qty=1.0,
            entry=100.0,
            stop=stop,
            target=101.5,
            initial_stop=stop,
            risk_dist=1.0,
            atr=1.0,
            opened_ts=time.time(),
            signal_ts=0,
            peak_price=100.0,
            mark=100.0,
            key="k",
        )
        trader.positions["TESTUSDT"] = [pos]
        trader.set_mark("TESTUSDT", price)
        trader.manage_position(pos)
        return pos, broker

    def test_be_is_below_market(self):
        pos, broker = self._run(101.0)
        self.assertTrue(broker.replaced)
        self.assertLess(broker.replaced[0], 101.0)

    def test_broker_snaps_trigger_before_sending(self):
        class C:
            def __init__(self):
                self.params = None

            def new_algo_order(self, **params):
                self.params = params
                return {"algoId": 5}

            def open_algo_orders(self, symbol=None):
                return [{
                    "algoId": 5,
                    "clientAlgoId": (self.params or {}).get("clientAlgoId"),
                    "algoStatus": "NEW",
                    "symbol": (self.params or {}).get("symbol"),
                }]

            def filters(self, symbol):
                return filters(tick=0.01)

        client = C()
        broker = LiveBroker(client, Config())
        broker.replace_stop("TESTUSDT", None, "LONG", 1.0, 100.11000000000001, "be")
        self.assertEqual(client.params["triggerPrice"], 100.11)

    def test_be_never_closer_than_two_ticks(self):
        pos, broker = self._run(100.12)
        be = breakeven_price("LONG", 100.0, Config().risk)
        for stop in broker.replaced:
            self.assertLessEqual(stop, round_tick(be, 0.01, "down"))


class TestBookRejectAdvice(unittest.TestCase):
    def _trader(self, min_imbalance=0.15):
        broker = StubBroker()
        trader, events = make_trader(broker)
        trader.cfg.orderbook.min_imbalance = min_imbalance
        trader.equity = 100.0
        trader.available = 100.0
        return trader, events

    def test_rejects_are_counted_and_advice_emitted_once(self):
        trader, events = self._trader()
        for _ in range(6):
            trader._note_book_reject("TESTUSDT", "imbalance +0.02 < +0.15")
        self.assertEqual(trader.reject_stats["imbalance"], 6)
        warnings = [e for e in events.tail(20) if "imbalance" in e.message and e.level == "WARNING"]
        self.assertEqual(len(warnings), 1)
        # sampel tambahan tidak menambah saran baru
        trader._note_book_reject("TESTUSDT", "imbalance +0.02 < +0.15")
        warnings = [e for e in events.tail(20) if e.level == "WARNING" and "imbalance" in e.message]
        self.assertEqual(len(warnings), 1)

    def test_stale_reason_gets_its_own_advice(self):
        trader, events = self._trader()
        for _ in range(6):
            trader._note_book_reject("TESTUSDT", "snapshot orderbook terlalu lama (>15s)")
        self.assertEqual(trader.reject_stats["stale"], 6)
        self.assertTrue(any("max_age_s" in e.message for e in events.tail(20)))

    def test_spread_blocks_do_not_trigger_imbalance_advice(self):
        trader, events = self._trader()
        for _ in range(6):
            trader._note_book_reject("TESTUSDT", "spread 9.0bps > 5.0bps")
        self.assertEqual(trader.reject_stats["spread"], 6)
        self.assertFalse(any("min_imbalance" in e.message for e in events.tail(20)))


class TestOrphanProtectionFixes(unittest.TestCase):
    def _trader_with_position(self, broker):
        trader, events = make_trader(broker)
        trader.equity = 100.0
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
            opened_ts=time.time(),
            signal_ts=0,
            key="k",
            stop_order_id="sl-1",
        )
        trader.positions["TESTUSDT"] = [pos]
        return trader, events, pos

    def test_unreadable_algo_list_does_not_duplicate_stop(self):
        class Broken(StubBroker):
            def open_positions(self):
                return {
                    "TESTUSDT": {
                        "qty": 1.0,
                        "entry": 100.0,
                        "side": "LONG",
                        "mark": 100.0,
                        "unrealized": 0.0,
                    }
                }

            def open_stop_ids(self, symbol):
                raise BinanceError(-1001, "internal", 500, "/fapi/v1/openAlgoOrders")

        broker = Broken()
        trader, events, pos = self._trader_with_position(broker)
        trader.check_orphan_protection()
        self.assertEqual(broker.replaced, [])
        self.assertEqual(pos.stop_order_id, "sl-1")

    def test_missing_stop_is_reinstalled(self):
        class Empty(StubBroker):
            def open_positions(self):
                return {
                    "TESTUSDT": {
                        "qty": 1.0,
                        "entry": 100.0,
                        "side": "LONG",
                        "mark": 100.0,
                        "unrealized": 0.0,
                    }
                }

            def open_stop_ids(self, symbol):
                return set()

        broker = Empty()
        trader, events, pos = self._trader_with_position(broker)
        trader.check_orphan_protection()
        self.assertEqual(len(broker.replaced), 1)
        self.assertEqual(pos.stop_order_id, "s2")


if __name__ == "__main__":
    unittest.main()
