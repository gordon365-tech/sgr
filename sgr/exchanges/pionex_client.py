from __future__ import annotations

from typing import Any

import requests


class PionexAPIError(RuntimeError):
    """Raised when the Pionex API returns an error response."""


class PionexClient:
    """Small, independent public Pionex HTTP client."""

    BASE_URL = "https://api.pionex.com"
    TIMEOUT = 10

    KLINE_INTERVALS = {
        "1m": "1M",
        "5m": "5M",
        "15m": "15M",
        "30m": "30M",
        "1h": "60M",
        "4h": "4H",
        "8h": "8H",
        "12h": "12H",
        "1d": "1D",
    }

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int = TIMEOUT,
    ) -> None:
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "SGR-PionexClient/1.0",
            }
        )

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}{path}",
            params=params,
            timeout=self.timeout,
        )

        response.raise_for_status()

        payload = response.json()

        if payload.get("result") is not True:
            code = payload.get("code", "UNKNOWN")
            message = payload.get("message", "Unknown Pionex API error")
            raise PionexAPIError(f"{code}: {message}")

        return payload

    def get_ticker(self, symbol: str = "BTC_USDT") -> dict[str, Any]:
        """Return the 24h ticker for a symbol."""
        payload = self._get(
            "/api/v1/market/tickers",
            {"symbol": symbol},
        )

        tickers = payload.get("data", {}).get("tickers", [])

        if not tickers:
            raise PionexAPIError(
                f"No ticker returned for symbol {symbol}"
            )

        return tickers[0]

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
            {
                "symbol": symbol,
                "limit": limit,
            },
        )

        return payload.get("data", {})

    def get_ohlcv(
        self,
        symbol: str = "BTC_USDT",
        interval: str = "1m",
        limit: int = 100,
        end_time: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return OHLCV klines using SGR-friendly interval names."""

        pionex_interval = self.KLINE_INTERVALS.get(interval.lower())

        if pionex_interval is None:
            valid = ", ".join(self.KLINE_INTERVALS)
            raise ValueError(
                f"Unsupported interval '{interval}'. "
                f"Supported intervals: {valid}"
            )

        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")

        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": pionex_interval,
            "limit": limit,
        }

        if end_time is not None:
            params["endTime"] = end_time

        payload = self._get(
            "/api/v1/market/klines",
            params,
        )

        return payload.get("data", {}).get("klines", [])
