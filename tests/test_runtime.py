import tempfile
import unittest
from pathlib import Path

from scalpbot.app import WsThread, open_bar_start_ms, ws_proxy_kwargs
from scalpbot.config import Config


class FakeWs:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, data):
        self.sent.append(data)
        return len(data)

    def close(self):
        self.closed = True


class TestWsCommands(unittest.TestCase):
    def _thread(self):
        return WsThread("test", lambda: "wss://example", lambda payload: None)

    def test_send_while_connected_is_immediate(self):
        thread = self._thread()
        ws = FakeWs()
        thread._ws = ws
        thread._connected = True
        self.assertTrue(thread.send({"method": "SUBSCRIBE", "params": ["a"], "id": 1}))
        self.assertEqual(len(ws.sent), 1)

    def test_queued_command_flushed_on_open(self):
        thread = self._thread()
        self.assertFalse(thread.send({"method": "SUBSCRIBE", "params": ["a"], "id": 2}))
        ws = FakeWs()
        thread._handle_open(ws)
        self.assertTrue(thread.connected)
        self.assertEqual(len(ws.sent), 1)

    def test_reconnect_closes_socket(self):
        thread = self._thread()
        ws = FakeWs()
        thread._ws = ws
        thread.reconnect()
        self.assertTrue(ws.closed)

    def test_clear_commands_on_new_connection(self):
        thread = self._thread()
        thread.send({"method": "UNSUBSCRIBE", "params": ["stale"], "id": 3})
        thread._clear_commands()
        ws = FakeWs()
        thread._handle_open(ws)
        self.assertEqual(ws.sent, [])


class TestHelpers(unittest.TestCase):
    def test_open_bar_start(self):
        now = 1_700_000_000_000
        width = 300_000
        self.assertEqual(open_bar_start_ms(now, 300), (now // width) * width)
        self.assertEqual(open_bar_start_ms(width * 7, 300), width * 7)

    def test_proxy_kwargs(self):
        self.assertEqual(ws_proxy_kwargs(""), {})
        kwargs = ws_proxy_kwargs("http://user:pass@127.0.0.1:8080")
        self.assertEqual(kwargs["http_proxy_host"], "127.0.0.1")
        self.assertEqual(kwargs["http_proxy_port"], 8080)
        self.assertEqual(kwargs["http_proxy_auth"], ("user", "pass"))
        self.assertEqual(kwargs["proxy_type"], "http")
        self.assertEqual(ws_proxy_kwargs("ftp://host"), {})

    def test_market_ticks_are_throttled(self):
        from scalpbot.app import ScalpBotApp

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config()
            cfg.mode = "paper"
            cfg.dashboard.enabled = False
            cfg.logging.events_file = str(Path(tmp) / "events.jsonl")
            app = ScalpBotApp(cfg)
            pushed = []
            app._push = lambda kind, payload: pushed.append((kind, payload))
            open_bar = {"e": "kline", "s": "TESTUSDT", "k": {"x": False, "c": "100"}}
            app._on_market_message(dict(open_bar))
            app._on_market_message(dict(open_bar))
            self.assertEqual(len(pushed), 1)
            app._on_market_message({"e": "kline", "s": "TESTUSDT", "k": {"x": True, "c": "100"}})
            self.assertEqual(len(pushed), 2)
            app.client.close()


if __name__ == "__main__":
    unittest.main()


class TestRestFallback(unittest.TestCase):
    def test_open_candle_is_not_fed_as_closed(self):
        import asyncio
        import threading

        from scalpbot.app import ScalpBotApp, SymbolRuntime
        from scalpbot.events import EventLog
        from scalpbot.indicators import Candle

        open_ts = 300_000 * 100
        now_ms = open_ts + 12_345
        candles = [
            Candle(open_ts - 600_000, 1, 1, 1, 1),
            Candle(open_ts - 300_000, 1, 1, 1, 1),
            Candle(open_ts, 1, 1, 1, 1),
        ]

        class StubClient:
            def server_time_ms(self):
                return now_ms

            def klines(self, symbol, interval, limit):
                return candles

        class StubStrategy:
            def __init__(self):
                self.fed = []

            def on_bar(self, candle):
                self.fed.append(candle.ts)
                return type("State", (), {"signal": None, "idx": 10, "atr": 1.0})()

            def build_signal(self, state):
                return None

        class StubTrader:
            def __init__(self):
                self.marks = []

            def set_mark(self, symbol, price):
                self.marks.append(price)

            def mark_signal(self):
                return None

            def try_open(self, *args):
                return None

        class StubBook:
            def snapshot(self, symbol, force=False):
                return None

        app = object.__new__(ScalpBotApp)
        app._universe_lock = threading.RLock()
        app.cfg = Config()
        app.interval_seconds = 300
        app.client = StubClient()
        app.book = StubBook()
        app.trader = StubTrader()
        app.events = EventLog(capacity=20)
        app._market_ws = None
        strategy = StubStrategy()
        app.symbols = {
            "TESTUSDT": SymbolRuntime(
                symbol="TESTUSDT",
                strategy=strategy,
                filters=None,
            )
        }
        asyncio.run(app._rest_fallback())
        self.assertEqual(strategy.fed, [open_ts - 600_000, open_ts - 300_000])

    def test_console_logging_when_dashboard_unavailable(self):
        from scalpbot.app import ScalpBotApp

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config()
            cfg.mode = "paper"
            cfg.dashboard.enabled = True
            cfg.logging.events_file = str(Path(tmp) / "events.jsonl")
            app = ScalpBotApp(cfg)
            self.assertFalse(app.dashboard.enabled)
            self.assertTrue(app.events.console)
            app.client.close()


class TestDiscoveryFallback(unittest.TestCase):
    def test_empty_selection_uses_config_symbols(self):
        from scalpbot.app import ScalpBotApp

        class StubClient:
            def ticker_24hr(self):
                return []

            def tradable_symbols(self):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config()
            cfg.mode = "paper"
            cfg.dashboard.enabled = False
            cfg.logging.events_file = str(Path(tmp) / "events.jsonl")
            app = ScalpBotApp(cfg)
            app.client = StubClient()
            app.events.console = False
            app.filters = {}
            selection = app._select_universe(50.0, force_symbols=["AAAUSDT", "BBBUSDT"])
            self.assertEqual(selection.symbols, ["AAAUSDT", "BBBUSDT"])
            self.assertTrue(any("memakai" in ev.message for ev in app.events.tail(5)))
            app.client.close = lambda: None
            app.client.close()


class TestPerModeLogFiles(unittest.TestCase):
    def test_app_writes_only_to_mode_folder(self):
        import json

        from scalpbot.app import ScalpBotApp

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config()
            cfg.mode = "paper"
            cfg.dashboard.enabled = False
            base = Path(tmp)
            cfg.logging.file = str(base / "logs" / "bot.log")
            cfg.logging.events_file = str(base / "logs" / "ev.jsonl")
            app = ScalpBotApp(cfg)
            app.events.console = False
            app.setup_logging()
            app.events.add("INFO", "test", "halo paper")
            logging_paths = sorted(
                str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()
            )
            app.client.close()
            self.assertEqual(logging_paths, ["logs/paper/bot.log", "logs/paper/ev.jsonl"])
            rows = [
                json.loads(line)
                for line in (base / "logs" / "paper" / "ev.jsonl").read_text().splitlines()
            ]
            self.assertTrue(any(r["message"] == "halo paper" for r in rows))

    def test_rotated_event_file_stays_in_mode_folder(self):
        from scalpbot.events import EventLog

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs" / "paper" / "events.jsonl"
            log = EventLog(capacity=10, file_path=str(path), max_bytes=1024, mode="paper")
            for i in range(80):
                log.add("INFO", "test", f"baris {i} " + "x" * 40)
            files = sorted(p.name for p in path.parent.iterdir())
            self.assertEqual(files, ["events.jsonl", "events.jsonl.1"])
            self.assertFalse((path.parent.parent / "events.jsonl").exists())

    def test_mode_is_visible_in_console_line(self):
        import io
        import sys as _sys

        from scalpbot.events import EventLog

        with tempfile.TemporaryDirectory() as tmp:
            log = EventLog(capacity=5, file_path=str(Path(tmp) / "ev.jsonl"),
                           console=True, mode="testnet")
            buf = io.StringIO()
            old = _sys.stderr
            _sys.stderr = buf
            try:
                log.add("INFO", "boot", "start")
            finally:
                _sys.stderr = old
            self.assertIn("TESTNET", buf.getvalue())
            self.assertIn("boot", buf.getvalue())


class TestSessionTechLog(unittest.TestCase):
    def test_session_lines_only_in_tech_log(self):
        import json
        import logging

        from scalpbot.app import ScalpBotApp

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config()
            cfg.mode = "live"
            cfg.dashboard.enabled = False
            base = Path(tmp)
            cfg.logging.file = str(base / "logs" / "bot.log")
            cfg.logging.events_file = str(base / "logs" / "ev.jsonl")
            app = ScalpBotApp(cfg)
            app.events.console = False
            app.setup_logging()
            app._file_log.info("mulai mode=%s", cfg.mode)
            logging.getLogger("scalpbot.trader").warning("warning teknis")
            app.client.close()

            tech = (base / "logs" / "live" / "bot.log").read_text().splitlines()
            self.assertEqual(len(tech), 2)
            self.assertTrue(all("[LIVE]" in line for line in tech))
            self.assertIn("mulai mode=live", tech[0])
            self.assertIn("warning teknis", tech[1])

            events = [
                json.loads(line)
                for line in (base / "logs" / "live" / "ev.jsonl").read_text().splitlines()
            ]
            messages = [e["message"] for e in events]
            self.assertFalse(any("mulai mode=live" in m for m in messages))
            self.assertTrue(any("warning teknis" in m for m in messages))
