from __future__ import annotations

import pytest

from sgr.exchanges.pionex_client import PionexAPIError, PionexClient, PionexHTTPError


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(f"HTTP {self.status_code}", response=response)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.headers = {}
        self.last_url = None
        self.last_params = None
        self.closed = False

    def get(self, url, params=None, timeout=None):
        self.last_url = url
        self.last_params = params
        self.last_timeout = timeout
        return self.response

    def close(self):
        self.closed = True


def make_client(payload, status_code=200):
    session = FakeSession(FakeResponse(payload, status_code=status_code))
    return PionexClient(session=session), session


def test_get_ticker():
    client, session = make_client(
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

    ticker = client.get_ticker("BTC_USDT")

    assert ticker["symbol"] == "BTC_USDT"
    assert ticker["close"] == "64986.11"
    assert session.last_url.endswith("/api/v1/market/tickers")
    assert session.last_params == {"symbol": "BTC_USDT"}


def test_get_tickers_supports_market_type():
    client, session = make_client({"result": True, "data": {"tickers": []}})

    assert client.get_tickers(market_type="perp") == []
    assert session.last_params == {"type": "PERP"}


def test_get_orderbook():
    client, session = make_client(
        {
            "result": True,
            "data": {
                "bids": [["64986.11", "9.457114"]],
                "asks": [["64986.12", "23.756785"]],
            },
        }
    )

    orderbook = client.get_orderbook("BTC_USDT", 5)

    assert orderbook["bids"][0] == ["64986.11", "9.457114"]
    assert orderbook["asks"][0] == ["64986.12", "23.756785"]
    assert session.last_url.endswith("/api/v1/market/depth")
    assert session.last_params == {"symbol": "BTC_USDT", "limit": 5}


def test_get_book_tickers():
    client, session = make_client(
        {"result": True, "data": {"tickers": [{"symbol": "BTC_USDT", "bidPrice": "1"}]}}
    )

    result = client.get_book_tickers("BTC_USDT")

    assert result[0]["symbol"] == "BTC_USDT"
    assert session.last_url.endswith("/api/v1/market/bookTickers")
    assert session.last_params == {"symbol": "BTC_USDT"}


def test_get_symbols():
    client, session = make_client(
        {"result": True, "data": {"symbols": [{"symbol": "BTC_USDT"}]}}
    )

    assert client.get_symbols() == [{"symbol": "BTC_USDT"}]
    assert session.last_url.endswith("/api/v1/common/symbols")
    assert session.last_params is None


def test_get_trades_validates_limit():
    client, _ = make_client({"result": True, "data": {"trades": []}})

    with pytest.raises(ValueError, match="10 and 500"):
        client.get_trades("BTC_USDT", limit=5)


def test_get_ohlcv_translates_interval_and_end_time():
    client, session = make_client(
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

    candles = client.get_ohlcv("BTC_USDT", interval="1m", limit=5, end_time=1234567890000)

    assert len(candles) == 1
    assert candles[0]["open"] == "64986.11"
    assert session.last_url.endswith("/api/v1/market/klines")
    assert session.last_params == {
        "symbol": "BTC_USDT",
        "interval": "1M",
        "limit": 5,
        "endTime": 1234567890000,
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
        ("1w", "1W"),
        ("1mo", "1m"),
    ],
)
def test_kline_interval_mapping(interval, expected):
    assert PionexClient.KLINE_INTERVALS[interval] == expected


def test_invalid_interval():
    client, _ = make_client({"result": True, "data": {"klines": []}})

    with pytest.raises(ValueError, match="Unsupported interval"):
        client.get_ohlcv("BTC_USDT", interval="2m")


def test_invalid_market_type():
    client, _ = make_client({"result": True, "data": {"tickers": []}})

    with pytest.raises(ValueError, match="SPOT or PERP"):
        client.get_tickers(market_type="futures")


def test_invalid_orderbook_limit():
    client, _ = make_client({"result": True, "data": {}})

    with pytest.raises(ValueError, match="limit must be between"):
        client.get_orderbook("BTC_USDT", limit=0)


def test_invalid_ohlcv_limit():
    client, _ = make_client({"result": True, "data": {}})

    with pytest.raises(ValueError, match="limit must be between"):
        client.get_ohlcv("BTC_USDT", limit=0)


def test_invalid_end_time():
    client, _ = make_client({"result": True, "data": {}})

    with pytest.raises(ValueError, match="end_time"):
        client.get_ohlcv("BTC_USDT", end_time=0)


def test_empty_ticker_is_api_error():
    client, _ = make_client({"result": True, "data": {"tickers": []}})

    with pytest.raises(PionexAPIError, match="No ticker returned"):
        client.get_ticker("INVALID")


def test_pionex_api_error():
    client, _ = make_client(
        {"result": False, "code": 10001, "message": "Invalid symbol"}
    )

    with pytest.raises(PionexAPIError, match="10001") as exc_info:
        client.get_ticker("INVALID")

    assert exc_info.value.code == 10001


def test_http_error_is_mapped():
    client, _ = make_client({}, status_code=500)

    with pytest.raises(PionexHTTPError) as exc_info:
        client.get_ticker("BTC_USDT")

    assert exc_info.value.status_code == 500


def test_context_manager_closes_session():
    session = FakeSession(FakeResponse({"result": True, "data": {"tickers": []}}))
    with PionexClient(session=session):
        pass
    assert session.closed is True
