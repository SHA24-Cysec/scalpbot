import os
import unittest
from unittest.mock import patch

from scalpbot.config import Config
from scalpbot.exchange import BinanceError, BinanceFuturesClient, RateLimited


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text="", url=""):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.content = b"{}" if body is not None else b""
        self.text = text
        self.url = url

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.proxies = {}
        self.trust_env = False
        self.verify = True

    def request(self, method, url, data=None, timeout=None, **kwargs):
        self.calls.append(
            {"method": method, "url": url, "data": data, "timeout": timeout, "kwargs": kwargs}
        )
        if not self.responses:
            raise AssertionError("tidak ada respons lagi")
        resp = self.responses.pop(0)
        resp.url = url
        return resp

    def close(self):
        return None


def signed_config():
    cfg = Config()
    cfg.mode = "testnet"
    return cfg


class TestSignedRequests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {"BINANCE_API_KEY": "testkey", "BINANCE_API_SECRET": "testsecret"},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_post_order_uses_query_string(self):
        session = FakeSession([FakeResponse(body={"orderId": 1})])
        client = BinanceFuturesClient(signed_config(), session=session)
        client.new_order(symbol="BTCUSDT", side="BUY", type="MARKET", quantity=0.01)
        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertIn("symbol=BTCUSDT", call["url"])
        self.assertIn("signature=", call["url"])
        self.assertIn("timestamp=", call["url"])
        self.assertIsNone(call["data"])

    def test_listen_key_is_unsigned(self):
        session = FakeSession([FakeResponse(body={"listenKey": "abc"})])
        client = BinanceFuturesClient(signed_config(), session=session)
        self.assertEqual(client.listen_key_create(), "abc")
        url = session.calls[0]["url"]
        self.assertNotIn("signature=", url)
        self.assertNotIn("timestamp=", url)

    def test_keepalive_puts_key_in_query(self):
        session = FakeSession([FakeResponse(body={})])
        client = BinanceFuturesClient(signed_config(), session=session)
        client.listen_key_keepalive("abc")
        call = session.calls[0]
        self.assertEqual(call["method"], "PUT")
        self.assertIn("listenKey=abc", call["url"])

    def test_get_retries_on_429(self):
        session = FakeSession(
            [
                FakeResponse(status=429, body={"code": -1003, "msg": "busy"}, headers={"Retry-After": "0"}),
                FakeResponse(body=[[1, "1", "1", "1", "1", "1"]]),
            ]
        )
        client = BinanceFuturesClient(signed_config(), session=session)
        candles = client.klines("BTCUSDT", "5m", limit=2)
        self.assertEqual(len(candles), 1)
        self.assertEqual(len(session.calls), 2)

    def test_get_raises_rate_limited_after_attempts(self):
        session = FakeSession(
            [
                FakeResponse(status=429, body={"code": -1003, "msg": "busy"}, headers={"Retry-After": "0"}),
            ]
        )
        client = BinanceFuturesClient(signed_config(), session=session)
        with self.assertRaises(RateLimited):
            client._request("GET", "/fapi/v1/time", retries=1)

    def test_market_lot_size_min_qty(self):
        info = {
            "rateLimits": [
                {"rateLimitType": "REQUEST_WEIGHT", "interval": "1M", "limit": 6000},
                {"rateLimitType": "ORDERS", "interval": "1M", "limit": 1200},
            ],
            "symbols": [
                {
                    "symbol": "TESTUSDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "pricePrecision": 2,
                    "quantityPrecision": 3,
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
                        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.01", "maxQty": "50"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ],
        }
        session = FakeSession([FakeResponse(body=info)])
        client = BinanceFuturesClient(signed_config(), session=session)
        flt = client.load_filters(["TESTUSDT"])["TESTUSDT"]
        self.assertAlmostEqual(flt.min_qty, 0.01)
        self.assertAlmostEqual(flt.max_qty, 50.0)
        self.assertEqual(client.used_weight_limit, 6000)
        self.assertEqual(client.order_limit_1m, 1200)

    def test_close_guards_further_requests(self):
        session = FakeSession([FakeResponse(body={"serverTime": 1})])
        client = BinanceFuturesClient(signed_config(), session=session)
        client.close()
        with self.assertRaises(BinanceError):
            client._request("GET", "/fapi/v1/time")


if __name__ == "__main__":
    unittest.main()
