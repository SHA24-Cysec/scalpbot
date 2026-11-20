from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scalpbot.__main__ import _load
from scalpbot.app import ScalpBotApp, ws_proxy_kwargs
from scalpbot.broker import TestnetBroker as LiveBroker
from scalpbot.config import Config, ConfigError, load_config, validate_config
from scalpbot.dashboard import _clean_state
from scalpbot.discovery import select_symbols
from scalpbot.events import Event, EventLog, EventLogHandler, redact
from scalpbot.exchange import BinanceError, BinanceFuturesClient
from scalpbot.netinfo import IpDetector
from scalpbot.orderbook import OrderbookFilter
from scalpbot.risk import SymbolFilters
from scalpbot.security import (
    parse_proxy,
    resolve_under,
    safe_print,
    validate_listen_key,
)
from scalpbot.state import StateStore
from scalpbot.trader import Trader

from .test_exchange import FakeResponse, FakeSession, signed_config

ROOT = Path(__file__).resolve().parents[1]


def filters(symbol="TESTUSDT"):
    return SymbolFilters(
        symbol=symbol,
        tick_size=0.01,
        step_size=0.001,
        min_qty=0.001,
        max_qty=1000.0,
        min_notional=5.0,
        qty_precision=3,
        price_precision=2,
    )


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return self.path

    def ns(self, **overrides):
        values = {
            "config": str(self.path),
            "live": False,
            "paper": False,
            "log_level": None,
            "no_dashboard": False,
            "command": "pnl",
        }
        values.update(overrides)
        return argparse.Namespace(**values)


class TestHostAllowlist(ConfigCase):
    def test_rejects_arbitrary_rest_and_ws_hosts(self):
        bad = [
            ("api", "base_url_testnet", "https://attacker.example"),
            ("api", "base_url_testnet", "https://fapi.binance.com"),
            ("api", "base_url_live", "https://demo-fapi.binance.com"),
            ("api", "ws_testnet", "wss://fstream.binance.com"),
            ("api", "ws_live", "wss://demo-fstream.binance.com"),
            ("net", "ip_url", "https://169.254.169.254/latest/meta-data"),
        ]
        for section, key, value in bad:
            with self.subTest(key=key, value=value):
                cfg = Config()
                getattr(cfg, section).__dict__[key] = value
                with self.assertRaises(ConfigError):
                    validate_config(cfg)

    def test_accepts_old_and_official_demo_testnet_pair(self):
        cfg = Config()
        validate_config(cfg)
        cfg.api.base_url_testnet = "https://demo-fapi.binance.com"
        cfg.api.ws_testnet = "wss://demo-fstream.binance.com"
        self.assertIsInstance(validate_config(cfg), list)
        self.assertEqual(cfg.base_url(), "https://demo-fapi.binance.com")

    def test_live_mode_requires_live_hosts(self):
        cfg = Config()
        cfg.mode = "live"
        self.assertIsInstance(validate_config(cfg, live_requested=True), list)
        cfg.api.base_url_live = "https://testnet.binancefuture.com"
        with self.assertRaises(ConfigError):
            validate_config(cfg, live_requested=True)

    def test_client_revalidates_urls_even_without_cli(self):
        cfg = Config()
        cfg.api.base_url_testnet = "https://attacker.example"
        with self.assertRaises(ConfigError):
            BinanceFuturesClient(cfg, session=FakeSession([]))

    def test_example_config_is_valid(self):
        cfg = load_config(ROOT / "config.example.json")
        self.assertIsInstance(validate_config(cfg), list)


class TestLiveGate(ConfigCase):
    def setUp(self):
        super().setUp()
        self.write({"mode": "live"})

    def test_every_subcommand_demands_live_flag(self):
        for command in ("run", "check", "pnl", "bookscan", "signals"):
            with self.subTest(command=command), self.assertRaises(ConfigError):
                _load(self.ns(command=command, live=False))

    def test_live_flag_without_confirmation_is_rejected(self):
        with (
            patch.dict(os.environ, {"SCALPBOT_LIVE_CONFIRM": ""}),
            patch("scalpbot.__main__.sys.stdin", io.StringIO()),
            self.assertRaises(ConfigError),
        ):
            _load(self.ns(command="pnl", live=True))

    def test_env_confirmation_authorizes_all_commands(self):
        with patch.dict(os.environ, {"SCALPBOT_LIVE_CONFIRM": "YES-LIVE"}):
            for command in ("run", "check", "pnl", "bookscan", "signals"):
                with self.subTest(command=command):
                    cfg, warnings = _load(self.ns(command=command, live=True))
                    self.assertEqual(cfg.mode, "live")
                    self.assertTrue(cfg.live_authorized)
                    self.assertIsInstance(warnings, list)

    def test_paper_and_live_are_mutually_exclusive(self):
        with self.assertRaises(ConfigError):
            _load(self.ns(command="run", live=True, paper=True))

    def test_testnet_cannot_point_to_live_host(self):
        self.write({
            "mode": "testnet",
            "api": {"base_url_testnet": "https://fapi.binance.com"},
        })
        with self.assertRaises(ConfigError):
            _load(self.ns(command="pnl"))


class TestRedirectAndTrustEnv(unittest.TestCase):
    def setUp(self):
        env = patch.dict(
            os.environ,
            {"BINANCE_API_KEY": "testkey", "BINANCE_API_SECRET": "testsecret"},
        )
        env.start()
        self.addCleanup(env.stop)

    def test_requests_disable_redirects_and_environment_proxy(self):
        session = FakeSession([FakeResponse(body={"serverTime": 1_700_000_000_000})])
        client = BinanceFuturesClient(signed_config(), session=session)
        self.assertIsInstance(client.sync_time(), int)
        self.assertFalse(session.calls[0]["kwargs"]["allow_redirects"])
        self.assertFalse(session.trust_env)
        self.assertEqual(session.headers["X-MBX-APIKEY"], "testkey")

    def test_302_is_rejected_without_following_location(self):
        resp = FakeResponse(status=302, body={}, headers={"Location": "https://attacker.example"})
        session = FakeSession([resp])
        client = BinanceFuturesClient(signed_config(), session=session)
        with self.assertRaises(BinanceError):
            client.sync_time()
        self.assertEqual(len(session.calls), 1)

    def test_cross_host_response_is_rejected(self):
        class CrossHostSession:
            def __init__(self):
                self.headers = {}
                self.proxies = {}
                self.trust_env = False
                self.verify = True
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                return FakeResponse(
                    body={"serverTime": 1}, url="https://attacker.example/fapi/v1/time"
                )

            def close(self):
                return None

        session = CrossHostSession()
        client = BinanceFuturesClient(signed_config(), session=session)
        with self.assertRaises(BinanceError):
            client.sync_time()
        self.assertEqual(len(session.calls), 1)

    def test_error_body_is_redacted(self):
        resp = FakeResponse(
            status=400,
            body={"code": -1121, "msg": "BINANCE_API_SECRET=short-secret-123"},
            text="BINANCE_API_SECRET=short-secret-123",
        )
        session = FakeSession([resp])
        client = BinanceFuturesClient(signed_config(), session=session)
        with self.assertRaises(BinanceError) as ctx:
            client.sync_time()
        self.assertNotIn("short-secret-123", str(ctx.exception))


class TestRedactionPermissionsAndPaths(unittest.TestCase):
    def test_all_secret_shapes_are_redacted(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signaturepart"
        cases = [
            'api_key="REALVALUE"',
            "api_key='REALVALUE'",
            "BINANCE_API_SECRET=short-secret-123",
            "signature=" + "a" * 64,
            "Bearer " + "Z" * 70,
            "Authorization: Bearer " + jwt,
            "proxy=http://user:p@ss@10.0.0.1:8080",
            "listenKey=" + "k" * 64,
            "x-mbx-apikey: " + "A" * 64,
            "ALERT \x1b[2J\x1b[H\r\x07",
        ]
        forbidden = [
            "REALVALUE",
            "short-secret-123",
            "a" * 64,
            "Z" * 70,
            jwt,
            "p@ss",
            "k" * 64,
            "A" * 64,
            "\x1b",
            "\r",
            "\x07",
        ]
        for raw in cases:
            out = redact(raw)
            for token in forbidden:
                self.assertNotIn(token, out, msg=f"{raw!r} masih memuat {token!r}: {out!r}")

    def test_setup_logging_redacts_file_and_event_sinks(self):
        root = logging.getLogger()
        session_log = logging.getLogger("scalpbot.session")
        old_root = list(root.handlers)
        old_root_level = root.level
        old_propagate = session_log.propagate
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = Config()
            cfg.mode = "paper"
            cfg.dashboard.enabled = False
            cfg.logging.file = str(base / "logs" / "bot.log")
            cfg.logging.events_file = str(base / "logs" / "ev.jsonl")
            cfg.logging.state_file = str(base / "logs" / "state.json")
            app = ScalpBotApp(cfg)
            try:
                app.setup_logging()
                secret = "BINANCE_API_SECRET=short-secret-123"  # noqa: RUF100, S105
                long_key = "K" * 64
                proxy = "proxy=http://u:p@ss@10.0.0.1:8080"
                session_log.info("%s %s %s", secret, long_key, proxy)
                logging.getLogger("scalpbot.trader").warning(secret)
                app.events.add("ERROR", "test", secret + " " + proxy)
                text = Path(cfg.logging.file_for("paper")).read_text(encoding="utf-8")
                events = Path(cfg.logging.events_for("paper")).read_text(encoding="utf-8")
                self.assertNotIn("short-secret-123", text)
                self.assertNotIn(long_key, text)
                self.assertNotIn("p@ss", text)
                self.assertNotIn("short-secret-123", events)
                self.assertNotIn("p@ss", events)
                for path in (
                    Path(cfg.logging.file_for("paper")),
                    Path(cfg.logging.events_for("paper")),
                ):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            finally:
                app.client.close()
                for handler in list(root.handlers):
                    root.removeHandler(handler)
                    handler.close()
                for handler in old_root:
                    if not isinstance(handler, EventLogHandler):
                        root.addHandler(handler)
                if not root.handlers:
                    root.addHandler(logging.NullHandler())
                root.setLevel(old_root_level)
                for handler in list(session_log.handlers):
                    session_log.removeHandler(handler)
                    handler.close()
                session_log.propagate = old_propagate

    def test_state_rejects_non_finite_json_and_quarantines_nan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = StateStore(path)
            self.assertTrue(store.save({
                "cooldown": {"BTCUSDT": float("nan")},
                "entries": [{"time": float("inf"), "status": "OPEN"}],
            }))
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("NaN", raw)
            self.assertNotIn("Infinity", raw)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = store.load()
            self.assertEqual(loaded["cooldown"]["BTCUSDT"], 0.0)
            self.assertEqual(loaded["entries"][0]["time"], 0.0)

            path.write_text('{"version":1,"cooldown":{"BTCUSDT":NaN}}', encoding="utf-8")
            self.assertEqual(StateStore(path).load(), {})
            self.assertTrue((Path(tmp) / "state.json.corrupt").exists())

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW tidak tersedia")
    def test_event_log_does_not_follow_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / "target.txt"
            target.write_text("aman", encoding="utf-8")
            link = base / "events.jsonl"
            os.symlink(target, link)
            log = EventLog(capacity=5, file_path=str(link), console=False)
            log.add("INFO", "test", "hello")
            self.assertTrue(log.last_error)
            self.assertEqual(target.read_text(encoding="utf-8"), "aman")

    def test_paths_are_confined_to_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                resolve_under("../outside.log", base=root)
            with self.assertRaises(ValueError):
                resolve_under("/etc/passwd", base=root)
            with self.assertRaises(ValueError):
                resolve_under("logs/\x1b[2J.log", base=root)
            self.assertEqual(resolve_under("logs/x.log", base=root), root / "logs" / "x.log")
        cfg = Config()
        cfg.logging.file = "../evil.log"
        with self.assertRaises(ConfigError):
            validate_config(cfg)


class TestSSRFProxyAndTerminal(unittest.TestCase):
    def test_ip_detector_allowlist_no_redirect_no_forced_fallback(self):
        class Resp:
            def __init__(self, status=200, text="1.2.3.4", url="https://api.ipify.org", headers=None):
                self.status_code = status
                self.text = text
                self.url = url
                self.headers = headers or {}

        class Session:
            def __init__(self, responses):
                self.responses = list(responses)
                self.calls = []
                self.headers = {}
                self.proxies = {}
                self.trust_env = True
                self.verify = True
                self.closed = False

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return self.responses.pop(0)

            def close(self):
                self.closed = True

        redirect = Resp(
            status=302,
            text="",
            headers={"Location": "http://169.254.169.254/latest/meta-data"},
        )
        fake = Session([redirect])
        with patch("scalpbot.netinfo.requests.Session", return_value=fake):
            det = IpDetector("https://api.ipify.org", fallback=False)
            self.assertIsNone(det.fetch_once())
        self.assertEqual(len(fake.calls), 1)
        self.assertFalse(fake.calls[0][1]["allow_redirects"])
        self.assertFalse(fake.trust_env)
        self.assertTrue(fake.closed)

        empty = Session([Resp(text="")])
        with patch("scalpbot.netinfo.requests.Session", return_value=empty):
            self.assertIsNone(IpDetector("https://api.ipify.org", fallback=False).fetch_once())

        internal = Session([])
        with patch("scalpbot.netinfo.requests.Session", return_value=internal):
            det = IpDetector("https://169.254.169.254/latest/meta-data", fallback=False)
            self.assertIsNone(det.fetch_once())
        self.assertEqual(internal.calls, [])
        self.assertEqual(
            IpDetector("https://api.ipify.org", fallback=False)._urls(),
            ["https://api.ipify.org"],
        )
        self.assertIn("https://ifconfig.me/ip", IpDetector(fallback=True)._urls())

    def test_proxy_normalization_and_fail_closed(self):
        info = parse_proxy("http://user:p%40ss@127.0.0.1:8080")
        self.assertEqual(info["password"], "p@ss")
        self.assertEqual(info["port"], 8080)
        with self.assertRaises(ValueError):
            parse_proxy("http://host:99999")
        with self.assertRaises(ValueError):
            parse_proxy("ftp://host")
        with self.assertRaises(ValueError):
            validate_listen_key("bad\x1b[2J")
        self.assertEqual(validate_listen_key("abcDEF_123-"), "abcDEF_123-")

        cfg = Config()
        cfg.net.proxy = "http://user:p%40ss@127.0.0.1:8080"
        warnings = validate_config(cfg)
        self.assertEqual(cfg.net.proxy, "http://user:p%40ss@127.0.0.1:8080")
        self.assertFalse(any("PySocks" in w for w in warnings))
        cfg.net.proxy = "http://host:99999"
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        with patch.dict(sys.modules, {"socks": None}), self.assertRaises(ConfigError):
            ws_proxy_kwargs("socks5://127.0.0.1:1080")

    def test_terminal_and_dashboard_sanitize_control_characters(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            safe_print("ALERT \x1b[2J\r\x07 BINANCE_API_SECRET=short-secret-123")
        out = buf.getvalue()
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\r", out)
        self.assertNotIn("\x07", out)
        self.assertNotIn("short-secret-123", out)

        multi_buf = io.StringIO()
        with contextlib.redirect_stdout(multi_buf):
            safe_print(
                "baris satu\r\nbaris dua \x1b]0;judul-palsu\x07\x9b2J\n"
                "BINANCE_API_SECRET=rahasia-456",
                sep="\r\x07\n",
            )
        multi_out = multi_buf.getvalue()
        self.assertEqual(
            multi_out.splitlines(),
            ["baris satu", "baris dua ", "BINANCE_API_SECRET=***"],
        )
        self.assertNotIn("judul-palsu", multi_out)
        self.assertNotIn("rahasia-456", multi_out)

        event = Event(ts=0.0, level="INFO", source="ws\x1b[2J", message="secret=abc \x1b[H")
        state = _clean_state({"events": [event], "halted": "ESC\x1b[2J"})
        self.assertNotIn("\x1b", state["events"][0].source)
        self.assertNotIn("\x1b", state["events"][0].message)
        self.assertNotIn("abc", state["events"][0].message)
        self.assertNotIn("\x1b", state["halted"])

    def test_user_payload_dict_and_universe_membership(self):
        app = ScalpBotApp.__new__(ScalpBotApp)
        app.trader = MagicMock()
        app.events = EventLog(capacity=10)
        app._universe_lock = threading.RLock()
        app.symbols = {"BTCUSDT": object()}
        asyncio.run(app._handle_user({"e": "ORDER_TRADE_UPDATE", "o": "bukan-dict"}))
        asyncio.run(app._handle_user({"e": "ALGO_UPDATE", "o": {"s": "UNKNOWNUSDT", "X": "FINISHED"}}))
        self.assertEqual(app.events.count(), 0)

    def test_symbols_from_exchange_are_filtered(self):
        tickers = [
            {"symbol": "BTCUSDT\x1b[2J", "quoteVolume": "100"},
            {"symbol": "ETHUSDT", "quoteVolume": "200"},
        ]
        flt = {"BTCUSDT": filters("BTCUSDT"), "ETHUSDT": filters("ETHUSDT")}
        sel = select_symbols(
            tickers,
            tradable=["BTCUSDT", "ETHUSDT", "BAD\x1b[2J"],
            filters=flt,
            top_n=5,
        )
        self.assertEqual(sel.symbols, ["ETHUSDT"])

    def test_position_risk_filters_malicious_symbol(self):
        rows = [
            {"symbol": "BTCUSDT", "positionAmt": "1"},
            {"symbol": "BAD\x1b[2JUSDT", "positionAmt": "2"},
        ]
        session = FakeSession([FakeResponse(body=rows)])
        with patch.dict(
            os.environ,
            {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
        ):
            client = BinanceFuturesClient(signed_config(), session=session)
            clean = client.position_risk()
        self.assertEqual([row["symbol"] for row in clean], ["BTCUSDT"])


class TestHedgeAlgoAndEmergency(unittest.TestCase):
    def test_preflight_rejects_hedge_mode(self):
        class StubClient:
            def sync_time(self):
                return 0

            def position_side_dual(self):
                return True

        app = ScalpBotApp.__new__(ScalpBotApp)
        app.cfg = Config()
        app.cfg.mode = "testnet"
        app.client = StubClient()
        with self.assertRaises(BinanceError):
            app.preflight()

    def test_position_side_dual_parses_string(self):
        session = FakeSession([FakeResponse(body={"dualSidePosition": "true"})])
        with patch.dict(
            os.environ,
            {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
        ):
            client = BinanceFuturesClient(signed_config(), session=session)
            self.assertTrue(client.position_side_dual())

    def test_close_market_never_invents_executed_qty(self):
        class ZeroClient:
            def new_order(self, **params):
                return {"orderId": 1, "avgPrice": "100", "executedQty": "0"}

            def query_order(self, symbol, order_id=None, client_id=None):
                return {"avgPrice": "0", "executedQty": "0", "status": "NEW"}

            def filters(self, symbol):
                return filters()

        broker = LiveBroker(ZeroClient(), Config())
        with self.assertRaises(BinanceError):
            broker.close_market("TESTUSDT", "LONG", 1.0, "cl")

    def test_emergency_close_verifies_position_is_flat(self):
        class Client:
            def __init__(self):
                self.calls = []

            def new_order(self, **params):
                self.calls.append(params)
                return {"orderId": 1, "avgPrice": "100", "executedQty": "1"}

            def query_order(self, *args, **kwargs):
                return {"avgPrice": "0", "executedQty": "0", "status": "NEW"}

            def position_risk(self, symbol=None):
                return []

            def filters(self, symbol):
                return filters()

        client = Client()
        broker = LiveBroker(client, Config())
        fill = broker.emergency_close("TESTUSDT", "LONG", 1.0, "em")
        self.assertEqual(fill.qty, 1.0)
        self.assertEqual(client.calls[0]["reduceOnly"], "true")

    def test_emergency_close_falls_back_to_close_position_flag(self):
        class Client:
            def __init__(self):
                self.calls = []
                self.responses = [
                    {"orderId": 1, "avgPrice": "100", "executedQty": "0"},
                    {"orderId": 2, "avgPrice": "100", "executedQty": "1"},
                ]

            def new_order(self, **params):
                self.calls.append(params)
                return self.responses.pop(0)

            def query_order(self, *args, **kwargs):
                return {"avgPrice": "0", "executedQty": "0", "status": "NEW"}

            def position_risk(self, symbol=None):
                return []

            def filters(self, symbol):
                return filters()

        client = Client()
        broker = LiveBroker(client, Config())
        fill = broker.emergency_close("TESTUSDT", "LONG", 1.0, "em")
        self.assertEqual(fill.qty, 1.0)
        self.assertEqual(client.calls[1]["closePosition"], "true")
        self.assertNotIn("reduceOnly", client.calls[1])

    def test_emergency_close_fails_after_retries(self):
        class Client:
            def __init__(self):
                self.calls = []

            def new_order(self, **params):
                self.calls.append(params)
                return {"orderId": 1, "avgPrice": "100", "executedQty": "0"}

            def query_order(self, *args, **kwargs):
                return {"avgPrice": "0", "executedQty": "0", "status": "NEW"}

            def position_risk(self, symbol=None):
                return [{"symbol": "TESTUSDT", "positionAmt": "1"}]

            def filters(self, symbol):
                return filters()

        client = Client()
        broker = LiveBroker(client, Config())
        with patch("scalpbot.broker.time.sleep", return_value=None), self.assertRaises(BinanceError):
            broker.emergency_close("TESTUSDT", "LONG", 1.0, "em")
        self.assertEqual(len(client.calls), 6)

    def test_failed_emergency_close_halts_entries(self):
        class FailBroker:
            def __init__(self):
                self.cancelled = []

            def emergency_close(self, *args, **kwargs):
                raise BinanceError(-1, "close gagal", 0, "/fapi/v1/order")

            def cancel_all(self, symbol):
                self.cancelled.append(symbol)

        cfg = Config()
        cfg.mode = "testnet"
        cfg.orderbook.enabled = False
        broker = FailBroker()
        events = EventLog(capacity=20)
        trader = Trader(
            cfg,
            broker,
            {"TESTUSDT": filters()},
            events,
            OrderbookFilter(cfg.orderbook, lambda s, lim: None),
            300,
        )
        trader._emergency_close("TESTUSDT", "LONG", 1.0, "k")
        self.assertTrue(trader.halted_reason.startswith("emergency close failed"))
        allowed, reason = trader.guard_entry("TESTUSDT", 0, 0)
        self.assertFalse(allowed)
        self.assertIn("halted", reason)
        self.assertEqual(broker.cancelled, ["TESTUSDT"])
        self.assertTrue(any("CLOSE DARURAT GAGAL" in ev.message for ev in events.tail(10)))

    def test_algo_orders_request_result_and_are_verified(self):
        class AlgoClient:
            def __init__(self, rows=None):
                self.calls = []
                self.rows = rows
                self.verify_calls = 0

            def new_algo_order(self, **params):
                self.calls.append(params)
                return {"algoId": 7, "algoStatus": "NEW", "clientAlgoId": params.get("clientAlgoId")}

            def open_algo_orders(self, symbol=None):
                self.verify_calls += 1
                if self.rows is not None:
                    return self.rows
                return [
                    {
                        "algoId": 7,
                        "clientAlgoId": call.get("clientAlgoId"),
                        "algoStatus": "NEW",
                        "symbol": call.get("symbol"),
                    }
                    for call in self.calls
                ]

            def cancel_algo_order(self, *args, **kwargs):
                return {}

            def filters(self, symbol):
                return filters()

        client = AlgoClient()
        broker = LiveBroker(client, Config())
        out = broker.place_protection("TESTUSDT", "LONG", 1.0, 99.0, 101.0, "en")
        self.assertEqual(out.stop_id, "7")
        self.assertEqual(out.tp_id, "7")
        self.assertTrue(all(call["newOrderRespType"] == "RESULT" for call in client.calls))
        self.assertGreaterEqual(client.verify_calls, 2)

        missing = AlgoClient(rows=[])
        broker = LiveBroker(missing, Config())
        with self.assertRaises(BinanceError):
            broker.place_protection("TESTUSDT", "LONG", 1.0, 99.0, 101.0, "en")
        self.assertGreaterEqual(missing.verify_calls, 1)

    def test_exchange_validates_algo_client_id_and_defaults_result(self):
        session = FakeSession([FakeResponse(body={"algoId": 1})])
        with patch.dict(
            os.environ,
            {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"},
        ):
            client = BinanceFuturesClient(signed_config(), session=session)
            with self.assertRaises(BinanceError):
                client.new_algo_order(symbol="BTCUSDT", clientAlgoId="bad id")
            self.assertEqual(session.calls, [])
            client.new_algo_order(symbol="BTCUSDT", clientAlgoId="good-id")
        self.assertIn("newOrderRespType=RESULT", session.calls[0]["url"])


class TestManifestAndDocs(unittest.TestCase):
    def test_pysocks_is_declared(self):
        req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("requests[socks]==2.34.2", req)
        self.assertIn("PySocks==1.7.1", req)

    def test_example_config_has_optional_ip_fallback(self):
        example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertIs(example["net"]["ip_fallback"], False)

    def test_readme_records_security_fixes(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for phrase in (
            "One-way Mode",
            "PySocks",
            "allowlist",
            "executedQty=0",
            "konfirmasi terminal",
            "demo-fapi.binance.com",
        ):
            self.assertIn(phrase, readme, msg=f"README belum memuat: {phrase}")


if __name__ == "__main__":
    unittest.main()
