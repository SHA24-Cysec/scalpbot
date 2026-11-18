"""Uji adopsi posisi yang sudah terbuka di exchange saat bot dinyalakan."""

import unittest

from scalpbot.config import Config
from scalpbot.events import EventLog
from scalpbot.exchange import BinanceError
from scalpbot.orderbook import OrderbookFilter
from scalpbot.risk import SymbolFilters
from scalpbot.trader import Trader


def filters(symbol="BTCUSDT"):
    return SymbolFilters(
        symbol=symbol,
        tick_size=0.1,
        step_size=0.001,
        min_qty=0.001,
        max_qty=1000.0,
        min_notional=5.0,
        qty_precision=3,
        price_precision=1,
    )


class StubBroker:
    """Broker tiruan: cukup untuk jalur adopsi, tanpa jaringan."""

    name = "testnet"

    def __init__(self, positions=None, protection=None, fail_positions=False):
        self.positions = positions or {}
        self.protection = protection or {}
        self.fail_positions = fail_positions
        self.canceled = []
        self.placed = []

    def open_positions(self):
        if self.fail_positions:
            raise BinanceError(-1001, "koneksi putus", 0, "/fapi/v3/positionRisk")
        return self.positions

    def open_protection(self, symbol):
        return self.protection.get(symbol, [])

    def open_stop_ids(self, symbol):
        return {row["id"] for row in self.protection.get(symbol, [])}

    def equity(self):
        return (100.0, 80.0)

    def filters(self, symbol):
        return filters(symbol)

    def cancel(self, symbol, order_id):
        self.canceled.append((symbol, order_id))

    def replace_stop(self, symbol, order_id, side, qty, stop, tag):
        self.placed.append(("SL", symbol, stop))
        return type("P", (), {"stop_id": "sl-new", "tp_id": None})()

    def place_tp(self, symbol, side, qty, target, tag):
        self.placed.append(("TP", symbol, target))
        return type("P", (), {"stop_id": None, "tp_id": "tp-new"})()


def stopped(symbol, side, stop, target=None):
    exit_side = "SELL" if side == "LONG" else "BUY"
    rows = [{"id": f"sl-{symbol}", "type": "STOP_MARKET", "side": exit_side, "trigger": stop}]
    if target:
        rows.append({"id": f"tp-{symbol}", "type": "TAKE_PROFIT_MARKET", "side": exit_side, "trigger": target})
    return rows


def make_trader(broker, symbols=("BTCUSDT",)):
    cfg = Config()
    cfg.mode = "testnet"
    cfg.orderbook.enabled = False
    events = EventLog(capacity=200)
    trader = Trader(
        cfg,
        broker,
        {s: filters(s) for s in symbols},
        events,
        OrderbookFilter(cfg.orderbook, lambda s, lim: None),
        300,
    )
    trader.equity = 100.0
    trader.available = 80.0
    trader.day.start_equity = 100.0
    return trader, events


class TestAdoption(unittest.TestCase):
    def test_long_with_sl_and_tp_uses_exchange_levels(self):
        broker = StubBroker(
            {"BTCUSDT": {"qty": 0.002, "entry": 64000.0, "side": "LONG", "mark": 64100.0, "unrealized": 0.2}},
            {"BTCUSDT": stopped("BTCUSDT", "LONG", 63000.0, 68000.0)},
        )
        trader, events = make_trader(broker)
        trader.set_mark("BTCUSDT", 64100.0)
        trader.reconcile_at_start()

        pos = trader._all_positions()[0]
        self.assertEqual(pos.symbol, "BTCUSDT")
        self.assertEqual(pos.side, "LONG")
        self.assertAlmostEqual(pos.qty, 0.002)
        self.assertAlmostEqual(pos.stop, 63000.0)
        self.assertAlmostEqual(pos.target, 68000.0)
        self.assertEqual(pos.stop_order_id, "sl-BTCUSDT")
        self.assertEqual(pos.tp_order_id, "tp-BTCUSDT")
        self.assertEqual(trader.adopted_count, 1)
        # level dari exchange tidak boleh diganti
        self.assertEqual(broker.placed, [])
        self.assertEqual(broker.canceled, [])
        self.assertTrue(any(e.source == "adopt" for e in events.tail(10)))

    def test_short_missing_tp_gets_tp_only(self):
        broker = StubBroker(
            {"BTCUSDT": {"qty": 0.5, "entry": 1800.0, "side": "SHORT", "mark": 1790.0, "unrealized": 5.0}},
            {"BTCUSDT": stopped("BTCUSDT", "SHORT", 1830.0)},
        )
        trader, _ = make_trader(broker)
        trader.set_mark("BTCUSDT", 1790.0)
        trader.reconcile_at_start()

        pos = trader._all_positions()[0]
        self.assertAlmostEqual(pos.stop, 1830.0)
        self.assertGreater(pos.target, 0)
        self.assertLess(pos.target, 1790.0)
        self.assertEqual([p[0] for p in broker.placed], ["TP"])
        self.assertAlmostEqual(broker.placed[0][2], pos.target)

    def test_position_without_any_protection_is_left_alone(self):
        broker = StubBroker(
            {"BTCUSDT": {"qty": 0.001, "entry": 64000.0, "side": "LONG", "mark": 64000.0, "unrealized": 0.0}}
        )
        trader, events = make_trader(broker)
        trader.reconcile_at_start()
        self.assertEqual(trader._all_positions(), [])
        self.assertEqual(trader.adopted_count, 0)
        self.assertEqual(broker.placed, [])
        self.assertTrue(any("tidak punya SL maupun TP" in e.message for e in events.tail(10)))

    def test_missing_sl_is_placed_on_safe_side_of_price(self):
        # Posisi LONG sudah rugi jauh melewati SL dari entry; SL lama tidak ada.
        broker = StubBroker(
            {"BTCUSDT": {"qty": 0.002, "entry": 64000.0, "side": "LONG", "mark": 62000.0, "unrealized": -4.0}},
            {"BTCUSDT": stopped("BTCUSDT", "LONG", 63000.0, 68000.0)},
        )
        broker.protection["BTCUSDT"] = [
            r for r in broker.protection["BTCUSDT"] if r["type"] != "STOP_MARKET"
        ]
        trader, _ = make_trader(broker)
        trader.set_mark("BTCUSDT", 62000.0)
        trader.reconcile_at_start()

        pos = trader._all_positions()[0]
        self.assertLess(pos.stop, 62000.0, "SL LONG wajib di bawah harga, kalau tidak langsung trigger")
        self.assertEqual([p[0] for p in broker.placed], ["SL"])
        self.assertLess(broker.placed[0][2], 62000.0)

    def test_missing_sl_short_safe_side(self):
        broker = StubBroker(
            {"BTCUSDT": {"qty": 0.002, "entry": 64000.0, "side": "SHORT", "mark": 66000.0, "unrealized": -4.0}},
            {"BTCUSDT": stopped("BTCUSDT", "SHORT", 65000.0, 60000.0)},
        )
        broker.protection["BTCUSDT"] = [
            r for r in broker.protection["BTCUSDT"] if r["type"] != "STOP_MARKET"
        ]
        trader, _ = make_trader(broker)
        trader.set_mark("BTCUSDT", 66000.0)
        trader.reconcile_at_start()

        pos = trader._all_positions()[0]
        self.assertGreater(pos.stop, 66000.0)

    def test_adopted_position_counts_toward_max_open(self):
        broker = StubBroker(
            {
                "BTCUSDT": {"qty": 0.002, "entry": 64000.0, "side": "LONG", "mark": 64100.0, "unrealized": 0.2},
            },
            {"BTCUSDT": stopped("BTCUSDT", "LONG", 63000.0, 68000.0)},
        )
        trader, _ = make_trader(broker)
        trader.cfg.risk.max_open_positions = 1
        trader.set_mark("BTCUSDT", 64100.0)
        trader.reconcile_at_start()

        self.assertEqual(trader.max_open_effective, 1)
        ok, reason = trader.guard_entry("BTCUSDT", bar_idx=1)
        self.assertFalse(ok)
        self.assertIn("max posisi", reason)

    def test_adoption_above_limit_blocks_new_entries(self):
        broker = StubBroker(
            {
                "BTCUSDT": {"qty": 0.002, "entry": 64000.0, "side": "LONG", "mark": 64100.0, "unrealized": 0.2},
                "ETHUSDT": {"qty": 0.5, "entry": 1800.0, "side": "SHORT", "mark": 1795.0, "unrealized": 2.5},
            },
            {
                "BTCUSDT": stopped("BTCUSDT", "LONG", 63000.0, 68000.0),
                "ETHUSDT": stopped("ETHUSDT", "SHORT", 1830.0, 1760.0),
            },
        )
        trader, events = make_trader(broker, symbols=("BTCUSDT", "ETHUSDT"))
        trader.cfg.risk.max_open_positions = 1
        trader.set_mark("BTCUSDT", 64100.0)
        trader.set_mark("ETHUSDT", 1795.0)
        trader.reconcile_at_start()

        self.assertEqual(trader.adopted_count, 2)
        self.assertEqual(trader.max_open_effective, 2)
        self.assertFalse(trader.guard_entry("BTCUSDT", bar_idx=1)[0])
        self.assertTrue(any("tidak ada entry baru" in e.message for e in events.tail(10)))

    def test_position_already_in_profit_does_not_move_stop_backwards(self):
        # +2R: state BE/trail harus dianggap sudah lewat, SL tidak boleh dimundurkan.
        broker = StubBroker(
            {"BTCUSDT": {"qty": 0.002, "entry": 64000.0, "side": "LONG", "mark": 66000.0, "unrealized": 4.0}},
            {"BTCUSDT": stopped("BTCUSDT", "LONG", 63500.0, 68000.0)},
        )
        trader, _ = make_trader(broker)
        trader.set_mark("BTCUSDT", 66000.0)
        trader.reconcile_at_start()
        pos = trader._all_positions()[0]
        self.assertTrue(pos.be_done)
        trader.manage_all()
        self.assertAlmostEqual(pos.stop, 63500.0)
        self.assertEqual(broker.placed, [])

    def test_adoption_can_be_disabled(self):
        broker = StubBroker(
            {"BTCUSDT": {"qty": 0.002, "entry": 64000.0, "side": "LONG", "mark": 64100.0, "unrealized": 0.2}},
            {"BTCUSDT": stopped("BTCUSDT", "LONG", 63000.0, 68000.0)},
        )
        trader, events = make_trader(broker)
        trader.cfg.risk.adopt_existing = False
        trader.reconcile_at_start()
        self.assertEqual(trader._all_positions(), [])
        self.assertTrue(any("dimatikan" in e.message for e in events.tail(10)))

    def test_exchange_failure_is_reported_loudly(self):
        broker = StubBroker(fail_positions=True)
        trader, events = make_trader(broker)
        trader.reconcile_at_start()
        self.assertEqual(trader.adopted_count, 0)
        self.assertTrue(trader.adopt_error)
        self.assertTrue(any(e.level == "ERROR" and "gagal membaca posisi" in e.message for e in events.tail(10)))

    def test_paper_mode_is_noop(self):
        from scalpbot.broker import PaperBroker

        cfg = Config()
        cfg.mode = "paper"
        cfg.orderbook.enabled = False
        events = EventLog(capacity=10)
        broker = PaperBroker(cfg, lambda s: filters(s), lambda s: 100.0, start_balance=100.0)
        trader = Trader(cfg, broker, {"BTCUSDT": filters()}, events, OrderbookFilter(cfg.orderbook, lambda s, l: None), 300)
        trader.reconcile_at_start()
        self.assertEqual(trader.adopted_count, 0)


if __name__ == "__main__":
    unittest.main()
