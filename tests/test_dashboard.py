import unittest

from scalpbot.config import DashboardConfig
from scalpbot.dashboard import Dashboard, _visible, fit
from scalpbot.events import Event


def sample_state():
    return {
        "mode": "testnet",
        "ip": "203.0.113.9",
        "ip_age": 30.0,
        "ws_ok": True,
        "ws_age": 0.4,
        "weight": 12,
        "latency_ms": 45.0,
        "uptime_s": 3600.0,
        "discovery": "top_volume/5",
        "symbols": 5,
        "equity": 50.0,
        "available": 40.0,
        "day_pnl": 1.25,
        "day_pnl_pct": 2.5,
        "open_count": 1,
        "max_open": 3,
        "risk_pct": 1.5,
        "leverage": 10,
        "halted_reason": "",
        "signals_today": 4,
        "trades_today": 2,
        "errors": 0,
        "positions": [
            {
                "symbol": "BTCUSDT",
                "side": "LONG",
                "qty": 0.002,
                "entry": 64000.0,
                "mark": 64100.0,
                "r": 0.8,
                "sl": 63900.0,
                "tp": 64200.0,
                "pnl": 0.2,
                "state": "be",
            }
        ],
        "rows": [
            {
                "symbol": "BTCUSDT",
                "price": 64100.0,
                "trend": 1,
                "pac_center": 64000.0,
                "zone": "above",
                "armed_long": True,
                "armed_short": False,
                "imbalance": 0.22,
                "spread": 0.5,
                "atr_pct": 0.31,
                "last_signal": "LONG",
                "age": 120.0,
            }
        ],
        "events": [
            Event(ts=1_700_000_000, level="INFO", source="trade", message="OPEN LONG BTCUSDT"),
            Event(ts=1_700_000_100, level="WARNING", source="risk", message="spread lebar"),
        ],
        "footer": "Ctrl+C = keluar",
    }


class TestFit(unittest.TestCase):
    def test_visible_ignores_ansi(self):
        self.assertEqual(_visible("\x1b[32mabc\x1b[0m"), 3)

    def test_fit_pads(self):
        out = fit("ab", 5)
        self.assertEqual(len(out), 5)
        self.assertEqual(_visible(out), 5)

    def test_fit_truncates_without_cutting_layout(self):
        out = fit("abcdefghij", 5)
        self.assertLessEqual(_visible(out), 5)
        self.assertIn("…", out)

    def test_zero_width(self):
        self.assertEqual(fit("abc", 0), "")


class TestBuildFrame(unittest.TestCase):
    def setUp(self):
        self.dash = Dashboard(DashboardConfig(enabled=True, log_lines=4), live=False, tty=False)

    def test_frame_respects_size(self):
        for width, height in ((80, 24), (72, 22), (120, 40), (100, 12)):
            frame = self.dash.build_frame(sample_state(), width, height)
            lines = frame.split("\n")
            self.assertLessEqual(len(lines), height, f"{width}x{height}")
            for line in lines:
                self.assertLessEqual(_visible(line), width, f"{width}x{height}: {line!r}")

    def test_live_banner(self):
        state = sample_state()
        state["mode"] = "live"
        dash = Dashboard(DashboardConfig(enabled=True, log_lines=4), live=True, tty=False)
        frame = dash.build_frame(state, 100, 30)
        self.assertIn("L", frame)
        self.assertIn("V", frame)

    def test_empty_state_safe(self):
        frame = self.dash.build_frame({}, 80, 24)
        self.assertIn("SCALPTOOL", frame)

    def test_halted_banner(self):
        state = sample_state()
        state["halted_reason"] = "daily loss limit"
        frame = self.dash.build_frame(state, 100, 30)
        self.assertIn("HALT", frame)

    def test_no_positions_message(self):
        state = sample_state()
        state["positions"] = []
        frame = self.dash.build_frame(state, 90, 28)
        self.assertIn("tidak ada posisi", frame)

    def test_disabled_dashboard_does_not_write(self):
        dash = Dashboard(DashboardConfig(enabled=False), tty=False)
        self.assertFalse(dash.enabled)
        dash.render(sample_state(), force=True)

    def test_auto_tag_for_discovery(self):
        frame = self.dash.build_frame(sample_state(), 100, 30)
        self.assertIn("auto", frame)

    def test_static_tag_for_manual_symbols(self):
        state = sample_state()
        state["discovery"] = "static"
        frame = self.dash.build_frame(state, 100, 30)
        self.assertIn("static", frame)


if __name__ == "__main__":
    unittest.main()


class TestWindowsAndSmallTerminal(unittest.TestCase):
    def test_missing_sigwinch_does_not_crash(self):
        import io
        from unittest.mock import patch

        from scalpbot.config import DashboardConfig
        from scalpbot.dashboard import Dashboard

        cfg = DashboardConfig(enabled=True)
        dash = Dashboard(cfg, tty=True)
        dash._winch_sig = None
        out = io.StringIO()
        with patch("sys.stdout", out):
            dash.start()
            dash.render(sample_state(), force=True)
            dash.stop()
        self.assertIn("\x1b[?1049h", out.getvalue())
        self.assertIn("\x1b[?1049l", out.getvalue())

    def test_small_terminal_shows_warning(self):
        from scalpbot.config import DashboardConfig
        from scalpbot.dashboard import Dashboard

        cfg = DashboardConfig(enabled=True, min_width=100, min_height=24)
        dash = Dashboard(cfg, tty=True)
        frame = dash.build_frame(sample_state(), 60, 15)
        self.assertIn("terlalu kecil", frame)

    def test_fmt_dur_handles_infinity(self):
        from scalpbot.dashboard import fmt_dur

        self.assertEqual(fmt_dur(float("inf")), "-")


class TestGrouping(unittest.TestCase):
    def setUp(self):
        self.dash = Dashboard(DashboardConfig(enabled=True, log_lines=4), tty=False)

    def row(self, symbol, price, pac, trend=0, atr=0.4, long=False, short=False):
        return {
            "symbol": symbol, "price": price, "pac_center": pac, "trend": trend,
            "zone": "inside", "armed_long": long, "armed_short": short,
            "atr_pct": atr, "last_signal": "-", "age": 0.0, "imbalance": None, "spread": None,
        }

    def test_classification(self):
        self.assertEqual(self.dash._classify(self.row("AUSDT", 100, 100, long=True)), "SIAP")
        self.assertEqual(self.dash._classify(self.row("AUSDT", 100, 100, trend=0)), "FLAT")
        self.assertEqual(self.dash._classify(self.row("AUSDT", 100.05, 100, trend=1, atr=0.4)), "DEKAT")
        self.assertEqual(self.dash._classify(self.row("AUSDT", 105, 100, trend=1, atr=0.4)), "TREN")

    def test_flat_wins_over_distance(self):
        self.assertEqual(self.dash._classify(self.row("AUSDT", 100.001, 100, trend=0)), "FLAT")

    def test_armed_wins_over_trend(self):
        self.assertEqual(self.dash._classify(self.row("AUSDT", 130, 100, trend=-1, short=True)), "SIAP")

    def test_group_order_puts_actionable_first(self):
        rows = [
            self.row("FUSDT", 100, 100, trend=0),
            self.row("TUSDT", 120, 100, trend=1),
            self.row("DUSDT", 100.1, 100, trend=1),
            self.row("SUSDT", 100, 100, long=True),
        ]
        groups = self.dash._group_symbols(rows)
        names = [key for key, items in groups if items]
        self.assertEqual(names, ["SIAP", "DEKAT", "TREN", "FLAT"])

    def test_near_threshold_scales_with_atr(self):
        tight = self.dash._near_thr(self.row("AUSDT", 100, 100, atr=0.1))
        wide = self.dash._near_thr(self.row("AUSDT", 100, 100, atr=1.2))
        self.assertLess(tight, wide)
        self.assertGreaterEqual(tight, 0.05)
        self.assertLessEqual(wide, 0.60)

    def test_meter_clamped_to_width(self):
        for dd in (-99, -3, 0, 3, 99):
            meter = self.dash._meter(dd, 0.15, 10)
            self.assertEqual(_visible(meter), 10)


class TestLayoutSweep(unittest.TestCase):
    def test_all_sizes_fit(self):
        dash = Dashboard(DashboardConfig(enabled=True, log_lines=6), tty=False)
        rows = [
            {
                "symbol": f"S{i}USDT", "price": 100 + i, "pac_center": 100.0, "trend": (1, -1, 0)[i % 3],
                "zone": ("inside", "above", "below")[i % 3], "armed_long": i == 1,
                "armed_short": False, "atr_pct": 0.3 + i * 0.1, "last_signal": "LONG" if i == 2 else "-",
                "age": 120.0, "imbalance": None, "spread": None,
            }
            for i in range(25)
        ]
        base = sample_state()
        base["rows"] = rows
        base["symbols"] = 25
        for width in (72, 80, 90, 100, 108, 120, 150, 200):
            for height in (22, 28, 34, 40, 50):
                frame = dash.build_frame(base, width, height)
                lines = frame.split("\n")
                self.assertLessEqual(len(lines), height, f"{width}x{height}")
                for line in lines:
                    self.assertLessEqual(_visible(line), width, f"{width}x{height}: {line!r}")

    def test_split_mode_used_only_when_wide(self):
        import re

        dash = Dashboard(DashboardConfig(enabled=True, log_lines=6), tty=False)
        state = sample_state()

        def corners(frame):
            plain = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", frame)
            return max(line.count("\u250c") for line in plain.split("\n"))

        self.assertEqual(corners(dash.build_frame(state, 90, 40)), 1)
        self.assertEqual(corners(dash.build_frame(state, 140, 40)), 2)

    def test_many_positions_reserve_space(self):
        dash = Dashboard(DashboardConfig(enabled=True, log_lines=6), tty=False)
        state = sample_state()
        state["positions"] = [
            {"symbol": f"P{i}USDT", "side": "LONG", "qty": 1, "entry": 100, "mark": 101,
             "r": 0.5, "sl": 99, "tp": 102, "pnl": 0.1, "state": "BE"}
            for i in range(5)
        ]
        state["open_count"] = 5
        state["max_open"] = 5
        frame = dash.build_frame(state, 150, 44)
        self.assertIn("P0USDT", frame)
        self.assertIn("P4USDT", frame)

    def test_history_panel_shows_entries(self):
        dash = Dashboard(DashboardConfig(enabled=True, log_lines=4, history_lines=5), tty=False)
        state = sample_state()
        state["entries"] = [
            {"time": 1_700_000_000, "symbol": "XRPUSDT", "side": "SHORT",
             "price": 0.9515, "status": "OK", "detail": "+1.85$ +4.02R"},
            {"time": 1_700_000_060, "symbol": "BTCUSDT", "side": "LONG",
             "price": 0.0, "status": "TOLAK", "detail": "rezim x3"},
        ]
        frame = dash.build_frame(state, 150, 44)
        self.assertIn("RIWAYAT ENTRY", frame)
        self.assertIn("XRP", frame)
        self.assertIn("rezim x3", frame)
        rows = [line for line in frame.splitlines() if "RIWAYAT ENTRY" in line]
        self.assertEqual(len(rows), 1)

    def test_history_panel_respects_history_lines(self):
        dash = Dashboard(DashboardConfig(enabled=True, log_lines=4, history_lines=2), tty=False)
        state = sample_state()
        state["entries"] = [
            {"time": 1_700_000_000 + i, "symbol": f"S{i}USDT", "side": "LONG",
             "price": 100 + i, "status": "OPEN", "detail": f"R={i}bps"}
            for i in range(6)
        ]
        frame = dash.build_frame(state, 150, 44)
        self.assertIn("S5", frame)
        self.assertNotIn("S0", frame)

    def test_history_lines_zero_hides_panel(self):
        dash = Dashboard(DashboardConfig(enabled=True, log_lines=4, history_lines=0), tty=False)
        state = sample_state()
        state["entries"] = [
            {"time": 1_700_000_000, "symbol": "XRPUSDT", "side": "SHORT",
             "price": 0.9515, "status": "OPEN", "detail": "R=40bps"}
        ]
        for width, height in ((150, 44), (100, 30)):
            frame = dash.build_frame(state, width, height)
            self.assertNotIn("RIWAYAT ENTRY", frame)

    def test_history_panel_empty_state(self):
        dash = Dashboard(DashboardConfig(enabled=True, log_lines=4), tty=False)
        frame = dash.build_frame(sample_state(), 120, 40)
        self.assertIn("RIWAYAT ENTRY", frame)
        self.assertIn("belum ada entry", frame)


    def test_empty_rows_do_not_crash(self):
        dash = Dashboard(DashboardConfig(enabled=True, log_lines=4), tty=False)
        state = sample_state()
        state["rows"] = []
        frame = dash.build_frame(state, 120, 40)
        self.assertIn("SIMBOL", frame)
