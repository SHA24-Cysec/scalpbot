"""Uji kebebasan risk_pct (0.01–100%) dan perilaku modal kecil."""

import unittest

from scalpbot.config import Config, ConfigError, validate_config
from scalpbot.risk import SymbolFilters, size_position


def filters(price_tag="DOGEUSDT", min_notional=5.0):
    return SymbolFilters(
        symbol=price_tag,
        tick_size=0.00001,
        step_size=1.0,
        min_qty=1.0,
        max_qty=1e7,
        min_notional=min_notional,
        qty_precision=0,
        price_precision=5,
    )


def risk_cfg(risk_pct, leverage=10, frac=1.0):
    cfg = Config().risk
    cfg.risk_pct = risk_pct
    cfg.leverage = leverage
    cfg.max_notional_frac_equity = frac
    return cfg


class TestRiskRange(unittest.TestCase):
    def test_risk_pct_bebas_sampai_100(self):
        for value in (1, 2.5, 10, 25, 50, 100):
            cfg = Config()
            cfg.risk.risk_pct = value
            validate_config(cfg)

    def test_risk_pct_di_atas_100_ditolak(self):
        cfg = Config()
        cfg.risk.risk_pct = 101
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_risk_pct_terlalu_kecil_ditolak(self):
        cfg = Config()
        cfg.risk.risk_pct = 0.0
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_peringatan_makin_tegas_di_atas_25(self):
        cfg = Config()
        cfg.risk.risk_pct = 50
        warns = validate_config(cfg)
        self.assertTrue(any("menghabiskan akun" in w for w in warns), warns)

    def test_default_tidak_berubah(self):
        self.assertEqual(Config().risk.risk_pct, 1.5)


class TestSmallCapital(unittest.TestCase):
    """Modal $10-$50 dengan minimum notional Binance 5 USDT."""

    price = 0.06          # DOGE
    sl_bps = 94           # SL median hasil backtest

    def stop(self):
        return self.price * (1 - self.sl_bps / 10_000)

    def test_entry_tetap_mungkin_pada_modal_10_dolar(self):
        s = size_position(10.0, self.price, self.stop(), risk_cfg(1.5), filters())
        self.assertTrue(s.ok, s.reason)
        self.assertGreaterEqual(s.notional, 5.0)

    def test_risk_5_persen_bisa_dieksekusi(self):
        s = size_position(50.0, self.price, self.stop(), risk_cfg(5), filters())
        self.assertTrue(s.ok, s.reason)
        self.assertFalse(s.capped)
        self.assertAlmostEqual(s.risk_usd, 2.5, places=2)

    def test_risk_besar_terpotong_dan_dilaporkan(self):
        # equity 100, leverage 10, frac 1.0 -> notional maks 1000 -> risiko maks ~9.4% pada SL 94bps
        s = size_position(100.0, self.price, self.stop(), risk_cfg(100), filters())
        self.assertTrue(s.ok, s.reason)
        self.assertTrue(s.capped)
        self.assertIn("dipotong", s.reason)
        self.assertLess(s.risk_usd, 100.0)
        self.assertAlmostEqual(s.notional, 1000.0, delta=1.0)

    def test_pemotongan_bisa_dihindari_dengan_leverage(self):
        # leverage 110 memberi ruang notional lebih dari 106x equity yang dibutuhkan
        s = size_position(100.0, self.price, self.stop(), risk_cfg(100, leverage=110), filters())
        self.assertTrue(s.ok, s.reason)
        self.assertFalse(s.capped)
        self.assertAlmostEqual(s.risk_usd, 100.0, delta=1.0)

    def test_min_notional_5_usdt_tidak_memblokir_modal_10(self):
        s = size_position(10.0, self.price, self.stop(), risk_cfg(1), filters(min_notional=5.0))
        self.assertTrue(s.ok, s.reason)


if __name__ == "__main__":
    unittest.main()
