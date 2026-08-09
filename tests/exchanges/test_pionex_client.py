import pytest

from sgr.exchanges.pionex_client import PionexAPIError, PionexClient


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.headers = {}
        self.last_url = None
        self.last_params = None

    def get(self, url, params=None, timeout=None):
        self.last_url = url
        self.last_params = params
        return self.response


def test_get_ticker():
    response = FakeResponse(
        {
            "result": True,
            "data": {
                "tickers": [
                    {
                        "symbol": "BTC_USDT",
                        "time": 1786237722477,
                        "open": "64910.01",
                        "close": "64986.11",
                        "high": "65192.30",
                        "low": "64894.49",
                        "volume": "10339.779318",
                    }
                ]
            },
        }
    )

    client = PionexClient()
    session = FakeSession(response)
    client.session = session

    ticker = client.get_ticker("BTC_USDT")

    assert ticker["symbol"] == "BTC_USDT"
    assert ticker["close"] == "64986.11"
    assert session.last_url.endswith("/api/v1/market/tickers")
    assert session.last_params == {"symbol": "BTC_USDT"}


def test_get_orderbook():
    response = FakeResponse(
        {
            "result": True,
            "data": {
                "bids": [["64986.11", "9.457114"]],
                "asks": [["64986.12", "23.756785"]],
            },
        }
    )

    client = PionexClient()
    session = FakeSession(response)
    client.session = session

    orderbook = client.get_orderbook("BTC_USDT", 5)

    assert orderbook["bids"][0] == ["64986.11", "9.457114"]
    assert orderbook["asks"][0] == ["64986.12", "23.756785"]
    assert session.last_url.endswith("/api/v1/market/depth")
    assert session.last_params == {
        "symbol": "BTC_USDT",
        "limit": 5,
    }


def test_get_ohlcv_translates_interval():
    response = FakeResponse(
        {
            "result": True,
            "data": {
                "klines": [
                    {
                        "time": 1786237680000,
                        "open": "64986.11",
                        "close": "64986.11",
                        "high": "64986.12",
                        "low": "64986.11",
                        "volume": "0.042172",
                    }
                ]
            },
        }
    )

    client = PionexClient()
    session = FakeSession(response)
    client.session = session

    candles = client.get_ohlcv(
        "BTC_USDT",
        interval="1m",
        limit=5,
    )

    assert len(candles) == 1
    assert candles[0]["open"] == "64986.11"
    assert session.last_url.endswith("/api/v1/market/klines")
    assert session.last_params == {
        "symbol": "BTC_USDT",
        "interval": "1M",
        "limit": 5,
    }


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        ("1m", "1M"),
        ("5m", "5M"),
        ("15m", "15M"),
        ("30m", "30M"),
        ("1h", "60M"),
        ("4h", "4H"),
        ("8h", "8H"),
        ("12h", "12H"),
        ("1d", "1D"),
    ],
)
def test_kline_interval_mapping(interval, expected):
    assert PionexClient.KLINE_INTERVALS[interval] == expected


def test_invalid_interval():
    client = PionexClient()

    with pytest.raises(ValueError, match="Unsupported interval"):
        client.get_ohlcv(
            "BTC_USDT",
            interval="2m",
        )


def test_invalid_orderbook_limit():
    client = PionexClient()

    with pytest.raises(ValueError, match="limit must be between"):
        client.get_orderbook(
            "BTC_USDT",
            limit=0,
        )


def test_invalid_ohlcv_limit():
    client = PionexClient()

    with pytest.raises(ValueError, match="limit must be between"):
        client.get_ohlcv(
            "BTC_USDT",
            limit=0,
        )


def test_pionex_api_error():
    response = FakeResponse(
        {
            "result": False,
            "code": 10001,
            "message": "Invalid symbol",
        }
    )

    client = PionexClient()
    client.session = FakeSession(response)

    with pytest.raises(PionexAPIError, match="10001"):
        client.get_ticker("INVALID")
