"""Small synchronous client for Pionex's public REST market API.

The client deliberately stays independent from the CCXT adapter.  It is useful
for exchange specific market endpoints where the SGR exchange abstraction needs
raw Pionex data or where CCXT does not expose an endpoint directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import requests


class PionexAPIError(RuntimeError):
    """Raised when Pionex returns an application level API error."""

    def __init__(self, message: str, code: str | int | None = None) -> None:
        super().__init__(message)
        self.code = code


class PionexHTTPError(RuntimeError):
    """Raised when Pionex returns an unsuccessful HTTP status code."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class PionexClient:
    """Synchronous client for Pionex public market endpoints.

    No authentication is required for the endpoints exposed here.
    ``requests.Session`` is reused so callers can keep connection pooling and
    configure a proxy or transport for tests without changing the client API.
    """

    BASE_URL = "https://api.pionex.com"
    TIMEOUT = 10

    KLINE_INTERVALS: dict[str, str] = {
        "1m": "1M",
        "5m": "5M",
        "15m": "15M",
        "30m": "30M",
        "1h": "60M",
        "4h": "4H",
        "8h": "8H",
        "12h": "12H",
        "1d": "1D",
        "1w": "1W",
        "1mo": "1m",
    }

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")

        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "SGR-PionexClient/1.0",
            }
        )

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self.session.close()

    def __enter__(self) -> PionexClient:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self.session.get(
                f"{self.base_url}{path}",
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            raise PionexHTTPError(
                status_code,
                f"Pionex HTTP {status_code}: {exc}",
            ) from exc
        except requests.RequestException as exc:
            raise PionexHTTPError(0, f"Pionex request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise PionexAPIError("Pionex returned invalid JSON") from exc

        if not isinstance(payload, Mapping):
            raise PionexAPIError("Pionex returned an invalid response object")

        if payload.get("result") is not True:
            code = payload.get("code", "UNKNOWN")
            message = payload.get("message", "Unknown Pionex API error")
            raise PionexAPIError(f"{code}: {message}", code=code)

        return dict(payload)

    @staticmethod
    def _data(payload: Mapping[str, Any]) -> dict[str, Any]:
        data = payload.get("data", {})
        if not isinstance(data, Mapping):
            raise PionexAPIError("Pionex returned an invalid data object")
        return dict(data)

    def get_symbols(self) -> list[dict[str, Any]]:
        """Return Pionex spot symbol metadata."""
        payload = self._get("/api/v1/common/symbols")
        symbols = self._data(payload).get("symbols", [])
        if not isinstance(symbols, list):
            raise PionexAPIError("Pionex returned invalid symbols data")
        return symbols

    def get_tickers(
        self,
        symbol: str | None = None,
        market_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return 24 hour tickers, optionally filtered by symbol and type."""
        params: dict[str, Any] = {}
        if symbol is not None:
            params["symbol"] = symbol
        if market_type is not None:
            normalized_type = market_type.upper()
            if normalized_type not in {"SPOT", "PERP"}:
                raise ValueError("market_type must be SPOT or PERP")
            params["type"] = normalized_type

        payload = self._get("/api/v1/market/tickers", params or None)
        tickers = self._data(payload).get("tickers", [])
        if not isinstance(tickers, list):
            raise PionexAPIError("Pionex returned invalid tickers data")
        return tickers

    def get_ticker(self, symbol: str = "BTC_USDT") -> dict[str, Any]:
        """Return the 24 hour ticker for exactly one symbol."""
        tickers = self.get_tickers(symbol=symbol)
        if not tickers:
            raise PionexAPIError(f"No ticker returned for symbol {symbol}")
        return tickers[0]

    def get_book_tickers(
        self,
        symbol: str | None = None,
        market_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return best bid and ask prices."""
        params: dict[str, Any] = {}
        if symbol is not None:
            params["symbol"] = symbol
        if market_type is not None:
            normalized_type = market_type.upper()
            if normalized_type not in {"SPOT", "PERP"}:
                raise ValueError("market_type must be SPOT or PERP")
            params["type"] = normalized_type

        payload = self._get("/api/v1/market/bookTickers", params or None)
        tickers = self._data(payload).get("tickers", [])
        if not isinstance(tickers, list):
            raise PionexAPIError("Pionex returned invalid book tickers data")
        return tickers

    def get_orderbook(
        self,
        symbol: str = "BTC_USDT",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return the current order book snapshot."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")

        payload = self._get(
            "/api/v1/market/depth",
            {"symbol": symbol, "limit": limit},
        )
        return self._data(payload)

    def get_trades(
        self,
        symbol: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return recent spot or futures trades for a symbol."""
        if not 10 <= limit <= 500:
            raise ValueError("limit must be between 10 and 500")

        payload = self._get(
            "/api/v1/market/trades",
            {"symbol": symbol, "limit": limit},
        )
        trades = self._data(payload).get("trades", [])
        if not isinstance(trades, list):
            raise PionexAPIError("Pionex returned invalid trades data")
        return trades

    def get_ohlcv(
        self,
        symbol: str = "BTC_USDT",
        interval: str = "1m",
        limit: int = 100,
        end_time: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return OHLCV klines using SGR friendly interval names."""
        pionex_interval = self.KLINE_INTERVALS.get(interval.lower())
        if pionex_interval is None:
            valid = ", ".join(self.KLINE_INTERVALS)
            raise ValueError(
                f"Unsupported interval '{interval}'. Supported intervals: {valid}"
            )

        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")

        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": pionex_interval,
            "limit": limit,
        }
        if end_time is not None:
            if end_time <= 0:
                raise ValueError("end_time must be greater than 0")
            params["endTime"] = end_time

        payload = self._get("/api/v1/market/klines", params)
        klines = self._data(payload).get("klines", [])
        if not isinstance(klines, list):
            raise PionexAPIError("Pionex returned invalid klines data")
        return klines
