import math
import unittest

from scalpbot.discovery import (
    SELECTED,
    SKIP_DUPLICATE,
    SKIP_EXCLUDED,
    SKIP_NOTIONAL,
    SKIP_LIMIT,
    SKIP_NOT_TRADABLE,
    SKIP_VOLUME,
    select_symbols,
)
from scalpbot.risk import SymbolFilters


def flt(symbol, min_notional=5.0):
    return SymbolFilters(
        symbol=symbol,
        tick_size=0.01,
        step_size=0.001,
        min_qty=0.001,
        max_qty=1000.0,
        min_notional=min_notional,
        qty_precision=3,
        price_precision=2,
    )


def ticker(symbol, quote_volume):
    return {"symbol": symbol, "quoteVolume": str(quote_volume)}


SYMBOLS = ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"]
FILTERS = {s: flt(s) for s in SYMBOLS}
FILTERS["BBBUSDT"] = flt("BBBUSDT", min_notional=500.0)


class TestSelection(unittest.TestCase):
    def test_rank_by_quote_volume(self):
        tickers = [ticker("AAAUSDT", 100), ticker("CCCUSDT", 300), ticker("DDDUSDT", 200)]
        sel = select_symbols(tickers, SYMBOLS, FILTERS, top_n=3)
        self.assertEqual(sel.symbols, ["CCCUSDT", "DDDUSDT", "AAAUSDT"])

    def test_top_n_limits_result(self):
        tickers = [ticker("AAAUSDT", 100), ticker("CCCUSDT", 300), ticker("DDDUSDT", 200)]
        sel = select_symbols(tickers, SYMBOLS, FILTERS, top_n=2)
        self.assertEqual(sel.symbols, ["CCCUSDT", "DDDUSDT"])

    def test_min_volume_filter(self):
        tickers = [ticker("AAAUSDT", 100), ticker("CCCUSDT", 50_000_000)]
        sel = select_symbols(tickers, SYMBOLS, FILTERS, min_quote_volume=1_000_000, top_n=5)
        self.assertEqual(sel.symbols, ["CCCUSDT"])
        statuses = {c.symbol: c.status for c in sel.candidates}
        self.assertEqual(statuses["AAAUSDT"], SKIP_VOLUME)

    def test_exclude_list(self):
        tickers = [ticker("AAAUSDT", 900), ticker("CCCUSDT", 300)]
        sel = select_symbols(tickers, SYMBOLS, FILTERS, exclude=["AAAUSDT"], top_n=5)
        self.assertEqual(sel.symbols, ["CCCUSDT"])
        statuses = {c.symbol: c.status for c in sel.candidates}
        self.assertEqual(statuses["AAAUSDT"], SKIP_EXCLUDED)

    def test_untradable_symbol_skipped(self):
        tickers = [ticker("ZZZUSDT", 1000), ticker("AAAUSDT", 10)]
        sel = select_symbols(tickers, SYMBOLS, FILTERS, top_n=5)
        self.assertEqual(sel.symbols, ["AAAUSDT"])
        statuses = {c.symbol: c.status for c in sel.candidates}
        self.assertEqual(statuses["ZZZUSDT"], SKIP_NOT_TRADABLE)

    def test_affordability_filter(self):
        tickers = [ticker("BBBUSDT", 900), ticker("AAAUSDT", 100)]
        sel = select_symbols(
            tickers,
            SYMBOLS,
            FILTERS,
            top_n=5,
            equity=10.0,
            leverage=5,
            max_notional_frac=1.0,
            require_affordable=True,
        )
        self.assertEqual(sel.symbols, ["AAAUSDT"])
        statuses = {c.symbol: c.status for c in sel.candidates}
        self.assertEqual(statuses["BBBUSDT"], SKIP_NOTIONAL)

    def test_affordability_can_be_disabled(self):
        tickers = [ticker("BBBUSDT", 900)]
        sel = select_symbols(
            tickers,
            SYMBOLS,
            FILTERS,
            top_n=5,
            equity=10.0,
            leverage=5,
            require_affordable=False,
        )
        self.assertEqual(sel.symbols, ["BBBUSDT"])

    def test_invalid_volume_skipped(self):
        tickers = [{"symbol": "AAAUSDT", "quoteVolume": "abc"}, {"symbol": "CCCUSDT", "quoteVolume": None}]
        sel = select_symbols(tickers, SYMBOLS, FILTERS, top_n=5)
        self.assertEqual(sel.symbols, [])

    def test_duplicate_symbols(self):
        tickers = [ticker("AAAUSDT", 100), ticker("AAAUSDT", 500)]
        sel = select_symbols(tickers, SYMBOLS, FILTERS, top_n=5)
        self.assertEqual(sel.symbols, ["AAAUSDT"])
        self.assertTrue(any(c.status == SKIP_DUPLICATE for c in sel.candidates))

    def test_top_n_bounds(self):
        tickers = [ticker("AAAUSDT", 100)]
        self.assertEqual(select_symbols(tickers, SYMBOLS, FILTERS, top_n=0).symbols, ["AAAUSDT"])
        tickers_many = [ticker(s, 1000 - i) for i, s in enumerate(SYMBOLS)]
        self.assertEqual(len(select_symbols(tickers_many, SYMBOLS, FILTERS, top_n=99).symbols), 4)

    def test_empty_inputs(self):
        sel = select_symbols([], SYMBOLS, FILTERS)
        self.assertEqual(sel.symbols, [])
        self.assertEqual(sel.summary(), "-")
        sel2 = select_symbols([ticker("AAAUSDT", 10)], [], FILTERS)
        self.assertEqual(sel2.symbols, [])

    def test_selected_flag_and_sorting(self):
        tickers = [ticker("AAAUSDT", 100), ticker("CCCUSDT", 300)]
        sel = select_symbols(tickers, SYMBOLS, FILTERS, top_n=1)
        statuses = [c.status for c in sel.candidates]
        self.assertEqual(statuses.count(SELECTED), 1)
        self.assertEqual(sel.candidates[0].symbol, "CCCUSDT")
        self.assertTrue(any(c.status == SKIP_LIMIT for c in sel.candidates))

    def test_non_finite_volume_rejected(self):
        tickers = [
            {"symbol": "AAAUSDT", "quoteVolume": "nan"},
            {"symbol": "CCCUSDT", "quoteVolume": "inf"},
            {"symbol": "DDDUSDT", "quoteVolume": "1e999"},
            ticker("BBBUSDT", 100),
        ]
        sel = select_symbols(tickers, SYMBOLS, FILTERS, top_n=5)
        self.assertEqual(sel.symbols, ["BBBUSDT"])
        self.assertTrue(all(math.isfinite(c.quote_volume) for c in sel.candidates))

    def test_summary_format(self):
        tickers = [ticker("CCCUSDT", 120_000_000)]
        sel = select_symbols(tickers, SYMBOLS, FILTERS, top_n=1)
        self.assertIn("CCCUSDT(120M)", sel.summary())


if __name__ == "__main__":
    unittest.main()
