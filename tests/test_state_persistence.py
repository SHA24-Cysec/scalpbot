"""Uji state yang bertahan melewati restart: cooldown entry + riwayat entry."""

import json
import tempfile
import unittest
from pathlib import Path

from scalpbot.config import Config
from scalpbot.events import EventLog
from scalpbot.orderbook import OrderbookFilter
from scalpbot.risk import SymbolFilters
from scalpbot.state import StateStore, resolve_path
from scalpbot.trader import EntryRecord, Trader

BAR_MS = 300_000


def filters(symbol="DOGEUSDT"):
    return SymbolFilters(
        symbol=symbol,
        tick_size=0.00001,
        step_size=1.0,
        min_qty=1.0,
        max_qty=1e7,
        min_notional=5.0,
        qty_precision=0,
        price_precision=5,
    )


class StubBroker:
    name = "testnet"

    def open_positions(self):
        return {}

    def equity(self):
        return (50.0, 50.0)


def make_trader(tmpdir, cooldown_bars=2):
    cfg = Config()
    cfg.mode = "testnet"
    cfg.orderbook.enabled = False
    cfg.risk.cooldown_bars = cooldown_bars
    events = EventLog(capacity=100)
    trader = Trader(
        cfg, StubBroker(), {"DOGEUSDT": filters()}, events,
        OrderbookFilter(cfg.orderbook, lambda s, l: None), 300,
    )
    trader.equity = trader.available = 50.0
    trader.day.start_equity = 50.0
    store = StateStore(Path(tmpdir) / "state.json")
    trader.attach_state(store)
    return trader, store


class TestStateStore(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = StateStore(path)
            self.assertEqual(store.load(), {})
            self.assertTrue(store.save({"cooldown": {"DOGEUSDT": 123}, "entries": []}))
            data = store.load()
            self.assertEqual(data["cooldown"], {"DOGEUSDT": 123})
            self.assertEqual(data["version"], 1)

    def test_corrupt_file_is_quarantined_not_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{rusak", encoding="utf-8")
            store = StateStore(path)
            self.assertEqual(store.load(), {})
            self.assertTrue((Path(tmp) / "state.json.corrupt").exists())
            self.assertIn("rusak", store.last_error)

    def test_write_is_atomic_no_tmp_left(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = StateStore(path)
            store.save({"cooldown": {}, "entries": []})
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_disabled_store_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = StateStore(path, enabled=False)
            self.assertFalse(store.save({"cooldown": {}}))
            self.assertFalse(path.exists())

    def test_sanitises_bad_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(
                json.dumps({
                    "version": 1,
                    "cooldown": {"DOGEUSDT": "bukan angka", "ETHUSDT": 0, "XRPUSDT": 999},
                    "entries": [{"status": ""}, {"status": "OPEN", "symbol": "XRPUSDT", "time": "x"}],
                }),
                encoding="utf-8",
            )
            data = StateStore(path).load()
            self.assertEqual(StateStore.cooldown_from_state(data), {"XRPUSDT": 999})
            rows = StateStore.entries_from_state(data, 10)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["time"], 0.0)


class TestCooldownSurvivesRestart(unittest.TestCase):
    def test_cooldown_uses_bar_time_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader, store = make_trader(tmp)
            trader._last_entry_bar_ts["DOGEUSDT"] = 1_700_000_000_000
            trader._persist_state()

            # Simulasi proses baru: objek Trader baru, index bar mulai dari 0 lagi.
            reborn, _ = make_trader(tmp)
            self.assertEqual(reborn.attach_state(store), 0)
            self.assertEqual(reborn._last_entry_bar_ts["DOGEUSDT"], 1_700_000_000_000)

            last = 1_700_000_000_000
            self.assertFalse(reborn.guard_entry("DOGEUSDT", 0, last)[0])
            self.assertFalse(reborn.guard_entry("DOGEUSDT", 1, last + BAR_MS)[0])
            self.assertTrue(reborn.guard_entry("DOGEUSDT", 2, last + 2 * BAR_MS)[0])

    def test_index_based_cooldown_still_works_without_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader, _ = make_trader(tmp)
            trader._last_trade_bar["DOGEUSDT"] = 100
            self.assertFalse(trader.guard_entry("DOGEUSDT", 101, 0)[0])
            self.assertTrue(trader.guard_entry("DOGEUSDT", 102, 0)[0])

    def test_cooldown_zero_never_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader, _ = make_trader(tmp, cooldown_bars=0)
            trader._last_entry_bar_ts["DOGEUSDT"] = 1_700_000_000_000
            self.assertTrue(trader.guard_entry("DOGEUSDT", 5, 1_700_000_000_000)[0])

    def test_old_symbol_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader, _ = make_trader(tmp)
            trader._last_entry_bar_ts["DOGEUSDT"] = 1_700_000_000_000
            self.assertTrue(trader.guard_entry("ETHUSDT", 0, 1_700_000_000_000)[0])


class TestHistorySurvivesRestart(unittest.TestCase):
    def test_entries_are_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader, store = make_trader(tmp)
            trader._note_entry(
                EntryRecord(ts=1_700_000_000, symbol="DOGEUSDT", side="LONG",
                            price=0.06, qty=100, status="OPEN", detail="R=94bps")
            )
            trader._note_entry(
                EntryRecord(ts=1_700_000_060, symbol="XRPUSDT", side="SHORT",
                            price=0.95, qty=10, status="TOLAK", detail="rezim", count=4)
            )

            reborn, _ = make_trader(tmp)
            self.assertEqual(reborn.attach_state(store), 2)
            rows = reborn.entry_rows(10)
            self.assertEqual([r["symbol"] for r in rows], ["DOGEUSDT", "XRPUSDT"])
            self.assertEqual(rows[1]["status"], "TOLAK")
            self.assertEqual(rows[1]["detail"], "rezim x4")

    def test_history_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader, store = make_trader(tmp)
            for i in range(Trader.ENTRY_CAP + 25):
                trader._note_entry(
                    EntryRecord(ts=1_700_000_000 + i, symbol="DOGEUSDT", side="LONG",
                                price=0.06, qty=1, status="OPEN", detail=f"R={i}bps")
                )
            data = store.load()
            self.assertEqual(len(data["entries"]), Trader.ENTRY_CAP)

    def test_persist_happens_on_new_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader, store = make_trader(tmp)
            self.assertEqual(store.load(), {})
            trader._note_entry(
                EntryRecord(ts=1, symbol="DOGEUSDT", side="LONG", price=0.06, qty=1, status="OPEN")
            )
            self.assertEqual(len(store.load()["entries"]), 1)


class TestConfigWiring(unittest.TestCase):
    def test_default_path_per_mode(self):
        cfg = Config()
        self.assertTrue(cfg.logging.persist_state)
        self.assertTrue(cfg.logging.state_for("paper").endswith("paper/state.json"))
        self.assertIn("testnet", cfg.logging.state_for("testnet"))

    def test_resolve_path_respects_switch(self):
        cfg = Config()
        self.assertTrue(resolve_path(cfg, "paper"))
        cfg.logging.persist_state = False
        self.assertIsNone(resolve_path(cfg, "paper"))

    def test_validation_rejects_bad_switch(self):
        from scalpbot.config import ConfigError, validate_config

        cfg = Config()
        cfg.logging.persist_state = "ya"
        with self.assertRaises(ConfigError):
            validate_config(cfg)


if __name__ == "__main__":
    unittest.main()
