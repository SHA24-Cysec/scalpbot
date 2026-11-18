import time
import unittest

from scalpbot.broker import TestnetBroker as LiveBroker
from scalpbot.config import Config
from scalpbot.events import EventLog
from scalpbot.exchange import BinanceError
from scalpbot.orderbook import OrderbookFilter
from scalpbot.risk import SymbolFilters
from scalpbot.trader import ManagedPosition, Trader


class IncomeClient:
    """Klien palsu yang meniru /fapi/v1/income."""

    def __init__(self, rows=None, fail=False):
        self.rows = rows or {}
        self.fail = fail
        self.calls = []

    def income_history(self, income_type="REALIZED_PNL", start_ms=None, end_ms=None, limit=1000):
        self.calls.append((income_type, start_ms, end_ms))
        if self.fail:
            raise BinanceError(-1001, "internal error", 500, "/fapi/v1/income")
        return [{"incomeType": income_type, "income": value} for value in self.rows.get(income_type, [])]


def filters():
    return SymbolFilters(
        symbol="TESTUSDT",
        tick_size=0.01,
        step_size=0.001,
        min_qty=0.001,
        max_qty=1000.0,
        min_notional=5.0,
        qty_precision=3,
        price_precision=2,
    )


class StubBroker:
    name = "stub"

    def __init__(self, realized=0.0, fail=False):
        self.realized = realized
        self.fail = fail

    def equity(self):
        return 100.0, 100.0

    def open_positions(self):
        return {}

    def realized_since(self, start_ms=None):
        self.start_ms = start_ms
        if self.fail:
            raise BinanceError(-1001, "internal error", 500, "/fapi/v1/income")
        return self.realized


def make_trader(broker, cfg=None):
    cfg = cfg or Config()
    cfg.mode = "testnet"
    cfg.orderbook.enabled = False
    events = EventLog(capacity=100)
    trader = Trader(cfg, broker, {"TESTUSDT": filters()}, events, OrderbookFilter(cfg.orderbook, lambda s, lim: None), 300)
    return trader, events


def open_position(trader, stop=99.0):
    pos = ManagedPosition(
        symbol="TESTUSDT",
        side="LONG",
        qty=1.0,
        entry=100.0,
        stop=stop,
        target=101.0,
        initial_stop=stop,
        risk_dist=1.0,
        atr=1.0,
        opened_ts=time.time(),
        signal_ts=0,
        key="k1",
    )
    trader.positions["TESTUSDT"] = [pos]
    return pos


class TestIncomeBroker(unittest.TestCase):
    def test_realized_today_sums_pnl_and_commission(self):
        client = IncomeClient({"REALIZED_PNL": ["-3.50", "1.25"], "COMMISSION": ["-0.40", "-0.10"]})
        broker = LiveBroker(client, Config())
        self.assertAlmostEqual(broker.realized_today(), -2.75, places=6)
        kinds = [call[0] for call in client.calls]
        self.assertEqual(kinds, ["REALIZED_PNL", "COMMISSION", "FUNDING_FEE"])

    def test_income_today_includes_funding_and_breakdown(self):
        client = IncomeClient(
            {
                "REALIZED_PNL": ["-5.00"],
                "COMMISSION": ["-0.30"],
                "FUNDING_FEE": ["-0.84"],
            }
        )
        broker = LiveBroker(client, Config())
        info = broker.income_today()
        self.assertAlmostEqual(info["kinds"]["FUNDING_FEE"], -0.84, places=6)
        self.assertAlmostEqual(info["total"], -6.14, places=6)
        self.assertEqual(info["counts"]["REALIZED_PNL"], 1)

    def test_realized_today_window_starts_at_utc_midnight(self):
        client = IncomeClient({"REALIZED_PNL": [], "COMMISSION": [], "FUNDING_FEE": []})
        broker = LiveBroker(client, Config())
        broker.realized_today()
        start_ms = client.calls[0][1]
        start = time.gmtime(start_ms / 1000.0)
        self.assertEqual((start.tm_hour, start.tm_min, start.tm_sec), (0, 0, 0))

    def test_realized_today_returns_none_on_error(self):
        broker = LiveBroker(IncomeClient(fail=True), Config())
        self.assertIsNone(broker.realized_today())

    def test_realized_today_ignores_bad_rows(self):
        client = IncomeClient({"REALIZED_PNL": ["-1.0", "bukan angka", None], "COMMISSION": []})
        broker = LiveBroker(client, Config())
        self.assertAlmostEqual(broker.realized_today(), -1.0, places=6)


class TestDailyGuardBaseline(unittest.TestCase):
    def test_baseline_adds_previous_losses(self):
        trader, events = make_trader(StubBroker(realized=-4.0))
        trader.equity = 100.0
        trader.day.start_equity = 100.0
        trader.day.realized = -0.5
        self.assertTrue(trader.sync_day_baseline())
        self.assertAlmostEqual(trader.current_realized(), -4.0, places=6)
        self.assertEqual(trader.halted_reason, "")
        messages = [ev.message for ev in events.tail(5)]
        self.assertTrue(any("PnL di luar bot" in m for m in messages))

    def test_baseline_halts_when_limit_already_breached(self):
        trader, events = make_trader(StubBroker(realized=-6.0))
        trader.equity = 100.0
        trader.day.start_equity = 100.0
        trader.sync_day_baseline()
        self.assertEqual(trader.halted_reason, "daily loss limit")
        ok, reason = trader.guard_entry("TESTUSDT", 10)
        self.assertFalse(ok)
        self.assertIn("halted", reason)
        self.assertTrue(any(ev.level == "ERROR" for ev in events.tail(5)))

    def test_baseline_survives_restart_simulation(self):
        first, _ = make_trader(StubBroker(realized=-4.0))
        first.equity = 100.0
        first.day.start_equity = 100.0
        first.sync_day_baseline()
        # restart: Trader baru, exchange sudah mencatat rugi 4.0
        second, _ = make_trader(StubBroker(realized=-4.0))
        second.equity = 100.0
        second.day.start_equity = 100.0
        second.sync_day_baseline()
        # trade baru sesi ini -1.2, exchange akan melaporkan -5.2 total: harus sama
        second.day.realized = -1.2
        self.assertAlmostEqual(second.current_realized(), -5.2, places=6)

    def test_baseline_fetched_once_per_day(self):
        broker = StubBroker(realized=-1.0)
        calls = []
        original = broker.realized_since

        def counting(start_ms=None):
            calls.append(1)
            return original(start_ms)

        broker.realized_since = counting
        trader, _ = make_trader(broker)
        trader.equity = 100.0
        trader.day.start_equity = 100.0
        self.assertTrue(trader.sync_day_baseline())
        self.assertFalse(trader.sync_day_baseline())
        self.assertEqual(len(calls), 1)

    def test_failed_fetch_keeps_session_only_guard(self):
        trader, events = make_trader(StubBroker(fail=True))
        trader.equity = 100.0
        trader.day.start_equity = 100.0
        self.assertFalse(trader.sync_day_baseline())
        self.assertEqual(trader.day_baseline, 0.0)
        self.assertTrue(any("gagal dibaca" in ev.message for ev in events.tail(5)))

    def test_paper_broker_has_no_baseline(self):
        from scalpbot.broker import PaperBroker

        broker = PaperBroker(Config(), lambda s: filters(), lambda s: 100.0, start_balance=50.0)
        trader, _ = make_trader(broker)
        trader.equity = 50.0
        trader.day.start_equity = 50.0
        self.assertFalse(trader.sync_day_baseline())
        self.assertEqual(trader.current_realized(), 0.0)

    def test_rollover_refetches_baseline_for_new_day(self):
        """Hari baru: baseline harus diambil ulang dari exchange, bukan diwarisi hari sebelumnya."""
        broker = StubBroker(realized=-2.0)
        trader, _ = make_trader(broker)
        trader.equity = 100.0
        trader.day.start_equity = 100.0
        trader.day.realized = -2.0
        trader.sync_day_baseline()
        # baseline = total exchange ( -2 ) - PnL sesi ( -2 ) = 0
        self.assertAlmostEqual(trader.day_baseline, 0.0, places=6)
        self.assertAlmostEqual(trader.current_realized(), -2.0, places=6)
        broker.realized = -7.0  # hari baru, exchange sudah mencatat rugi 7
        trader.day.day = "2000-01-01"
        trader._roll_day()
        self.assertAlmostEqual(trader.day_baseline, -7.0, places=6)
        self.assertAlmostEqual(trader.current_realized(), -7.0, places=6)
        self.assertEqual(trader.day.day, trader._today())

    def test_failed_baseline_is_throttled(self):
        """Endpoint mati tidak boleh dipanggil tiap poll (weight 100 per request)."""
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
        for _ in range(20):
            trader._last_equity_poll = 0.0
            trader.refresh_equity()
        self.assertEqual(len(calls), 1)
        # setelah satu menit, percobaan boleh diulang
        trader._baseline_try_ts -= 61.0
        trader._last_equity_poll = 0.0
        trader.refresh_equity()
        self.assertEqual(len(calls), 2)

    def test_guard_uses_total_not_session_only(self):
        trader, _ = make_trader(StubBroker(realized=-4.5))
        trader.equity = 100.0
        trader.day.start_equity = 100.0
        trader.sync_day_baseline()
        self.assertTrue(trader.guard_entry("TESTUSDT", 100)[0])
        trader.day.realized = -0.6
        ok, reason = trader.guard_entry("TESTUSDT", 101)
        self.assertFalse(ok)
        self.assertEqual(reason, "daily loss limit")


class TestDailyWindowFollowsDisplayZone(unittest.TestCase):
    """Bug yang diperbaiki: jendela PnL harian dulu 00:00 UTC walau hari bot = WIB.

    Akibat lamanya: antara 00:00-07:00 WIB, bot masih menghitung rugi 7 jam terakhir
    hari kemarin, sehingga bisa berhenti tepat setelah hari WIB berganti.
    """

    def test_window_starts_at_midnight_wib_not_utc(self):
        trader, _ = make_trader(StubBroker(realized=-1.0))
        trader.equity = 100.0
        trader.day.start_equity = 100.0
        trader.cfg.logging.timezone = "WIB"
        trader.sync_day_baseline()
        start_ms = trader.broker.start_ms
        self.assertIsNotNone(start_ms)
        # 00:00 WIB == 17:00 UTC hari sebelumnya
        utc = time.gmtime(start_ms / 1000.0)
        self.assertEqual((utc.tm_hour, utc.tm_min, utc.tm_sec), (17, 0, 0))

    def test_window_starts_at_midnight_utc_when_zone_utc(self):
        trader, _ = make_trader(StubBroker(realized=-1.0))
        trader.equity = 100.0
        trader.day.start_equity = 100.0
        trader.cfg.logging.timezone = "UTC"
        trader.sync_day_baseline()
        utc = time.gmtime(trader.broker.start_ms / 1000.0)
        self.assertEqual((utc.tm_hour, utc.tm_min, utc.tm_sec), (0, 0, 0))

    def test_window_is_not_seven_hours_of_yesterday(self):
        """Yang dulu bikin bot berhenti pagi-pagi: jendela 24 jam ke belakang, bukan 7."""
        trader, _ = make_trader(StubBroker(realized=-1.0))
        trader.equity = 100.0
        trader.day.start_equity = 100.0
        trader.cfg.logging.timezone = "WIB"
        before = time.time()
        trader.sync_day_baseline()
        span_h = (before - trader.broker.start_ms / 1000.0) / 3600.0
        self.assertGreater(span_h, 0.0)
        self.assertLess(span_h, 24.0)


class TestRolloverKeepsTodayTrades(unittest.TestCase):
    def test_rollover_keeps_trades_closed_today(self):
        """Rollover tengah malam tidak boleh membuang trade hari berjalan."""
        from scalpbot.trader import TradeRecord

        trader, _ = make_trader(StubBroker(realized=0.0))
        trader.equity = 100.0
        now = time.time()
        trader.trades = [
            TradeRecord("TESTUSDT", "LONG", 1.0, 100.0, 100.5, 0.5, 0.4, 0.1, now - 90_000, now - 60, "tp"),
            TradeRecord("TESTUSDT", "LONG", 1.0, 100.0, 99.5, -0.5, -0.6, -0.2, now - 200_000, now - 90_000, "sl"),
        ]
        trader.day.realized = -0.2
        trader.day.day = "2000-01-01"  # paksa dianggap hari lama
        trader._roll_day()
        self.assertEqual(len(trader.trades), 1)
        self.assertAlmostEqual(trader.trades[0].pnl_net, 0.4, places=6)
        # trade yang dipertahankan tetap terhitung di PnL harian
        self.assertAlmostEqual(trader.day.realized, 0.4, places=6)
        self.assertEqual(trader.day.trades, 1)
        self.assertEqual(trader.day.wins, 1)

    def test_rollover_does_not_double_count(self):
        """Setelah rollover, current_realized = baseline + trade hari ini, tanpa sisa hari lama."""
        from scalpbot.trader import TradeRecord

        trader, _ = make_trader(StubBroker(realized=0.25))
        trader.equity = 100.0
        trader.day.start_equity = 100.0
        now = time.time()
        trader.trades = [
            TradeRecord("TESTUSDT", "LONG", 1.0, 100.0, 100.5, 0.25, 0.25, 0.1, now - 60, now - 30, "tp"),
        ]
        trader.day.realized = 0.25
        trader.day.day = "2000-01-01"
        trader._roll_day()
        # exchange melaporkan 0.25 untuk hari baru; bot sudah punya 0.25 dari trade ini
        self.assertAlmostEqual(trader.day_baseline, 0.0, places=6)
        self.assertAlmostEqual(trader.current_realized(), 0.25, places=6)


class TestIncomeBreakdown(unittest.TestCase):
    def test_breakdown_by_symbol(self):
        client = IncomeClient(
            {
                "REALIZED_PNL": ["-4.20", "-1.10"],
                "COMMISSION": ["-0.18", "-0.06"],
                "FUNDING_FEE": ["-0.60"],
            }
        )
        client.rows["REALIZED_PNL"] = ["-4.20", "-1.10"]
        broker = LiveBroker(client, Config())
        info = broker.income_today()
        self.assertAlmostEqual(info["kinds"]["REALIZED_PNL"], -5.30, places=6)
        self.assertEqual(set(info["counts"]), {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"})
        self.assertEqual(info["day"], time.strftime("%Y-%m-%d", time.gmtime()))

    def test_funding_makes_guard_stricter(self):
        client = IncomeClient({"REALIZED_PNL": ["-4.0"], "COMMISSION": ["-0.2"], "FUNDING_FEE": ["-1.0"]})
        broker = LiveBroker(client, Config())
        self.assertAlmostEqual(broker.realized_today(), -5.2, places=6)


if __name__ == "__main__":
    unittest.main()
