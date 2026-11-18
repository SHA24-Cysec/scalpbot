"""Uji zona waktu tampilan (WIB) tanpa menggeser jendela hari exchange (UTC)."""

import unittest
from datetime import datetime, timedelta, timezone

from scalpbot.config import Config, ConfigError, load_config
from scalpbot.timeutil import (
    TimezoneError,
    day_key,
    describe_boundary,
    format_ts,
    parse_timezone,
    tz_abbrev,
    tz_label,
    utc_day_label,
    day_start_ms,
    day_start_ms_from_label,
    utc_day_start_ms,
)

WIB = timezone(timedelta(hours=7), "WIB")


class TestParse(unittest.TestCase):
    def test_wib_is_utc_plus_7(self):
        self.assertEqual(parse_timezone("WIB").utcoffset(None), timedelta(hours=7))

    def test_case_insensitive_and_spaces(self):
        self.assertEqual(parse_timezone("  wib ").utcoffset(None), timedelta(hours=7))

    def test_utc_still_available(self):
        self.assertEqual(parse_timezone("UTC").utcoffset(None), timedelta(0))

    def test_iana_name(self):
        # ZoneInfo butuh datetime nyata untuk menghitung offset (bukan None).
        zone = parse_timezone("Asia/Jakarta")
        moment = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(moment.astimezone(zone).utcoffset(), timedelta(hours=7))

    def test_other_indonesian_zones(self):
        self.assertEqual(parse_timezone("WITA").utcoffset(None), timedelta(hours=8))
        self.assertEqual(parse_timezone("WIT").utcoffset(None), timedelta(hours=9))

    def test_bad_timezone_rejected(self):
        for bad in ("", "  ", "Jupiter", "WIB+7"):
            with self.assertRaises(TimezoneError):
                parse_timezone(bad)

    def test_label(self):
        self.assertEqual(tz_label("wib"), "WIB")
        self.assertEqual(tz_label("Asia/Jakarta"), "Asia/Jakarta")


class TestFormatting(unittest.TestCase):
    def test_epoch_formatted_in_wib(self):
        # 2026-11-17 18:00:00 UTC -> 2026-11-18 01:00 WIB
        ts = datetime(2026, 11, 17, 18, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(format_ts(ts, "WIB", "%Y-%m-%d %H:%M"), "2026-11-18 01:00")
        self.assertEqual(format_ts(ts, "UTC", "%Y-%m-%d %H:%M"), "2026-11-17 18:00")

    def test_zero_returns_dash(self):
        self.assertEqual(format_ts(0, "WIB"), "-")

    def test_abbrev(self):
        ts = datetime(2026, 11, 17, 18, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(tz_abbrev("WIB", ts), "WIB")

    def test_day_key_uses_requested_zone(self):
        # 17:30 UTC = 00:30 WIB hari berikutnya
        ts = datetime(2026, 11, 17, 17, 30, tzinfo=timezone.utc)
        local = ts.astimezone(WIB)
        self.assertEqual(local.strftime("%Y-%m-%d"), "2026-11-18")


class TestBotDayBoundary(unittest.TestCase):
    """Hari bot (dasar batas rugi harian) mengikuti zona tampilan, bukan UTC."""

    def test_wib_day_start_is_1700_utc(self):
        moment = datetime(2026, 11, 18, 0, 30, tzinfo=timezone(timedelta(hours=7)))
        start = day_start_ms("WIB", moment)
        utc = datetime.fromtimestamp(start / 1000.0, tz=timezone.utc)
        self.assertEqual((utc.day, utc.hour, utc.minute), (17, 17, 0))

    def test_utc_zone_day_start_is_utc_midnight(self):
        moment = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
        start = day_start_ms("UTC", moment)
        utc = datetime.fromtimestamp(start / 1000.0, tz=timezone.utc)
        self.assertEqual((utc.hour, utc.minute), (0, 0))

    def test_wib_and_utc_windows_differ_by_seven_hours(self):
        moment = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
        diff_h = (day_start_ms("UTC", moment) - day_start_ms("WIB", moment)) / 3_600_000.0
        self.assertAlmostEqual(diff_h, 7.0, places=6)

    def test_day_start_from_label_matches_day_start(self):
        moment = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(day_start_ms_from_label("2026-11-18", "WIB"), day_start_ms("WIB", moment))


class TestExchangeBoundary(unittest.TestCase):
    """Bagian paling penting: hari exchange tetap UTC walau tampilan WIB."""

    def test_utc_day_start_is_utc_midnight(self):
        moment = datetime(2026, 11, 17, 18, 30, tzinfo=timezone.utc)
        start = utc_day_start_ms(moment)
        expected = datetime(2026, 11, 17, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
        self.assertEqual(start, int(expected))

    def test_boundary_shown_as_07_00_wib(self):
        self.assertEqual(describe_boundary("WIB"), "07:00")
        self.assertEqual(describe_boundary("UTC"), "00:00")
        self.assertEqual(describe_boundary("WITA"), "08:00")

    def test_utc_label_unaffected_by_display_zone(self):
        moment = datetime(2026, 11, 17, 18, 30, tzinfo=timezone.utc)
        self.assertEqual(utc_day_label(moment), "2026-11-17")


class TestConfig(unittest.TestCase):
    def test_default_is_wib(self):
        self.assertEqual(Config().logging.timezone, "WIB")

    def test_load_rejects_unknown_zone(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"logging": {"timezone": "Mars/Olympus"}}))
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_load_accepts_wib_and_utc(self):
        import json
        import tempfile
        from pathlib import Path

        for value in ("WIB", "UTC", "Asia/Jakarta"):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "c.json"
                path.write_text(json.dumps({"logging": {"timezone": value}}))
                cfg = load_config(path)
                self.assertEqual(cfg.logging.timezone, value)


class TestDashboardClock(unittest.TestCase):
    def test_event_and_history_use_configured_zone(self):
        from scalpbot.dashboard import Dashboard

        # 2026-11-17 18:30 UTC = 2026-11-18 01:30 WIB
        ts = datetime(2026, 11, 17, 18, 30, tzinfo=timezone.utc).timestamp()
        state = {
            "mode": "paper", "ip": "-", "ip_age": 0, "ws_ok": True, "ws_age": 0,
            "weight": 0, "latency_ms": 0, "uptime_s": 0, "symbols": 0, "discovery": "static",
            "equity": 10, "available": 10, "day_pnl": 0, "day_pnl_pct": 0, "open_count": 0,
            "max_open": 1, "risk_pct": 1.5, "leverage": 1, "halted_reason": "",
            "signals_today": 0, "trades_today": 0, "errors": 0, "refresh_s": 0, "min_vol_m": 0,
            "rows": [], "positions": [],
            "entries": [{"time": ts, "symbol": "DOGEUSDT", "side": "LONG", "price": 0.06,
                         "status": "OPEN", "detail": "R=94bps"}],
            "events": [],
        }
        from scalpbot.events import Event

        state["events"] = [Event(ts, "INFO", "trade", "contoh")]
        import re

        ansi = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
        dash = Dashboard(Config().dashboard, tty=False, live=False, timezone_name="WIB")
        frame = ansi.sub("", dash.build_frame(state, 120, 30))
        self.assertIn("01:30 DOGE", frame, "jam riwayat harus WIB")
        self.assertIn("01:30:00", frame, "jam event harus WIB")

        dash_utc = Dashboard(Config().dashboard, tty=False, live=False, timezone_name="UTC")
        frame_utc = ansi.sub("", dash_utc.build_frame(state, 120, 30))
        self.assertIn("18:30 DOGE", frame_utc, "zona UTC harus tetap tersedia")


class TestTraderDay(unittest.TestCase):
    def test_trader_today_follows_config_zone(self):
        from scalpbot.trader import Trader

        cfg = Config()
        cfg.logging.timezone = "WIB"
        trader = Trader.__new__(Trader)
        trader.cfg = cfg
        self.assertEqual(trader._today(), day_key("WIB"))

        cfg.logging.timezone = "UTC"
        self.assertEqual(trader._today(), day_key("UTC"))

    def test_halt_reset_happens_at_local_midnight(self):
        """Rollover memakai tanggal zona tampilan: halt harian reset 00:00 WIB."""
        from scalpbot.trader import Trader

        cfg = Config()
        cfg.logging.timezone = "WIB"
        trader = Trader.__new__(Trader)
        trader.cfg = cfg
        trader.entries = []
        self.assertEqual(trader._today(), day_key("WIB"))


if __name__ == "__main__":
    unittest.main()
