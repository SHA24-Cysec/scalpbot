import json
import os
import tempfile
import unittest
from pathlib import Path

from scalpbot.config import (
    Config,
    ConfigError,
    confirmation_from_env,
    load_config,
    validate_config,
)


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, data):
        path = Path(self.tmp.name) / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_defaults_when_empty_object(self):
        cfg = load_config(self.write({}))
        self.assertEqual(cfg.mode, "testnet")
        self.assertEqual(cfg.market.interval, "5m")
        self.assertEqual(len(cfg.market.symbols), 5)
        self.assertEqual(cfg.risk.risk_pct, 1.5)

    def test_symbol_aliases_and_dedupe(self):
        cfg = load_config(self.write({"market": {"symbols": ["btc", "BTCUSDT", " eth "]}}))
        self.assertEqual(cfg.market.symbols, ["BTCUSDT", "ETHUSDT"])

    def test_unknown_key_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write({"risk": {"risk_pct": 1.0, "bogus": 1}}))
        with self.assertRaises(ConfigError):
            load_config(self.write({"bogus_section": {}}))

    def test_invalid_json(self):
        path = Path(self.tmp.name) / "bad.json"
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config(Path(self.tmp.name) / "nope.json")

    def test_invalid_symbol(self):
        with self.assertRaises(ConfigError):
            load_config(self.write({"market": {"symbols": ["B T C"]}}))

    def test_validate_accepts_risk_up_to_100(self):
        # risk_pct dibebaskan sampai 100% atas permintaan pengguna.
        for value in (1, 5, 25, 50, 100):
            cfg = load_config(self.write({"risk": {"risk_pct": value}}))
            validate_config(cfg)
            self.assertEqual(cfg.risk.risk_pct, value)

    def test_validate_rejects_risk_above_100(self):
        cfg = load_config(self.write({"risk": {"risk_pct": 100.5}}))
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_validate_rejects_risk_near_zero(self):
        cfg = load_config(self.write({"risk": {"risk_pct": 0.001}}))
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_validate_rejects_bad_interval(self):
        cfg = load_config(self.write({"market": {"interval": "7m"}}))
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_validate_rejects_bad_sl_bounds(self):
        cfg = load_config(self.write({"risk": {"sl_min_bps": 100, "sl_max_bps": 50}}))
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_live_requires_flag(self):
        cfg = load_config(self.write({"mode": "live"}))
        with self.assertRaises(ConfigError):
            validate_config(cfg, live_requested=False)
        warnings = validate_config(cfg, live_requested=True)
        self.assertTrue(any("LIVE" in w for w in warnings))

    def test_live_flag_without_live_mode(self):
        cfg = load_config(self.write({"mode": "testnet"}))
        with self.assertRaises(ConfigError):
            validate_config(cfg, live_requested=True)

    def test_paper_mode_ok(self):
        cfg = load_config(self.write({"mode": "paper"}))
        warnings = validate_config(cfg)
        self.assertIsInstance(warnings, list)

    def test_base_urls(self):
        cfg = load_config(self.write({"mode": "testnet"}))
        self.assertIn("testnet", cfg.base_url())
        cfg.mode = "live"
        self.assertEqual(cfg.base_url(), "https://fapi.binance.com")
        self.assertEqual(cfg.ws_url(), "wss://fstream.binance.com")

    def test_env_credentials(self):
        cfg = Config()
        os.environ[cfg.api.key_env] = "abc"
        os.environ[cfg.api.secret_env] = "def"
        try:
            self.assertEqual(cfg.api_key(), "abc")
            self.assertEqual(cfg.api_secret(), "def")
        finally:
            del os.environ[cfg.api.key_env]
            del os.environ[cfg.api.secret_env]

    def test_confirmation_env(self):
        os.environ["SCALPBOT_LIVE_CONFIRM"] = "YES-LIVE"
        try:
            self.assertTrue(confirmation_from_env())
        finally:
            del os.environ["SCALPBOT_LIVE_CONFIRM"]
        self.assertFalse(confirmation_from_env())

    def test_warns_when_ema_order_wrong(self):
        cfg = load_config(self.write({"strategy": {"fast_ema": 300, "medium_ema": 200}}))
        warnings = validate_config(cfg)
        self.assertTrue(any("EMA" in w for w in warnings))

    def test_discovery_defaults(self):
        cfg = load_config(self.write({}))
        self.assertEqual(cfg.market.discovery, "top_volume")
        self.assertEqual(cfg.market.discovery_top_n, 5)
        self.assertEqual(cfg.market.discovery_refresh_s, 1800)
        self.assertEqual(cfg.market.discovery_exclude, [])

    def test_discovery_top_volume_allows_empty_symbols(self):
        cfg = load_config(self.write({
            "market": {"symbols": [], "discovery": "top_volume"},
        }))
        warnings = validate_config(cfg)
        self.assertEqual(cfg.market.symbols, [])
        self.assertIsInstance(warnings, list)

    def test_static_requires_symbols(self):
        cfg = load_config(self.write({"market": {"symbols": [], "discovery": "static"}}))
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_default_discovery_allows_empty_symbols(self):
        cfg = load_config(self.write({"market": {"symbols": []}}))
        self.assertEqual(cfg.market.discovery, "top_volume")
        self.assertIsInstance(validate_config(cfg), list)

    def test_log_paths_split_per_mode(self):
        cfg = load_config(self.write({"mode": "paper"}))
        validate_config(cfg)
        self.assertEqual(cfg.logging.file_for("paper"), "logs/paper/scalpbot.log")
        self.assertEqual(cfg.logging.events_for("paper"), "logs/paper/events.jsonl")
        self.assertEqual(cfg.logging.file_for("live"), "logs/live/scalpbot.log")
        self.assertEqual(cfg.logging.events_for("testnet"), "logs/testnet/events.jsonl")

    def test_log_path_without_mode_subdir(self):
        cfg = load_config(self.write({
            "logging": {"mode_subdir": False, "file": "logs/x.log", "events_file": "logs/y.jsonl"},
        }))
        validate_config(cfg)
        self.assertEqual(cfg.logging.file_for("live"), "logs/x.log")
        self.assertEqual(cfg.logging.events_for("live"), "logs/y.jsonl")

    def test_log_path_rejects_invalid_mode(self):
        cfg = load_config(self.write({"mode": "paper"}))
        with self.assertRaises(ConfigError):
            cfg.logging.file_for("shadow")

    def test_log_path_respects_custom_dir(self):
        cfg = load_config(self.write({"logging": {"file": "var/log/bot.log",
                                                  "events_file": "var/log/ev.jsonl"}}))
        validate_config(cfg)
        self.assertEqual(cfg.logging.file_for("testnet"), "var/log/testnet/bot.log")

    def test_log_file_cannot_equal_events_file(self):
        cfg = load_config(self.write({"logging": {"file": "logs/same.log",
                                                  "events_file": "logs/./same.log"}}))
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_example_config_uses_top_volume(self):
        example = Path(__file__).resolve().parents[1] / "config.example.json"
        cfg = load_config(example)
        self.assertEqual(cfg.market.discovery, "top_volume")
        self.assertTrue(cfg.market.symbols)

    def test_invalid_discovery_mode(self):
        cfg = load_config(self.write({"market": {"discovery": "top_gainers"}}))
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_discovery_limits(self):
        cfg = load_config(self.write({"market": {"discovery": "top_volume", "discovery_top_n": 0}}))
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        cfg = load_config(self.write({"market": {"discovery": "top_volume", "discovery_top_n": 26}}))
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        cfg = load_config(self.write({"market": {"discovery": "top_volume", "discovery_refresh_s": 60}}))
        with self.assertRaises(ConfigError):
            validate_config(cfg)
        cfg = load_config(self.write({
            "market": {"discovery": "top_volume", "discovery_quote_volume_min": -1},
        }))
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_discovery_exclude_normalized(self):
        cfg = load_config(self.write({
            "market": {"discovery": "top_volume", "discovery_exclude": ["btc", "BTCUSDT", "eth"]},
        }))
        self.assertEqual(cfg.market.discovery_exclude, ["BTCUSDT", "ETHUSDT"])

    def test_many_symbols_warns_in_discovery_mode(self):
        cfg = load_config(self.write({
            "market": {"discovery": "top_volume", "discovery_top_n": 12},
        }))
        warnings = validate_config(cfg)
        self.assertTrue(any("discovery_top_n" in w for w in warnings))

    def test_example_config_is_valid(self):
        example = Path(__file__).resolve().parents[1] / "config.example.json"
        cfg = load_config(example)
        validate_config(cfg)


if __name__ == "__main__":
    unittest.main()
