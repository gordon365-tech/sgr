"""
Tests für ExchangePool.initialize(credentials=...) (Commit 5, Option A).

Fokussiert nur auf den neuen credentials-Parameter-Pfad - nicht auf die
gesamte ExchangeFactory/ExchangePool-Oberfläche (dafür existieren noch
keine dedizierten Tests, siehe fehlende tests/exchanges/test_factory.py;
das wird hier bewusst nicht nachgeholt, um im Scope von Commit 5 zu
bleiben).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from sgr.core.types import ExchangeID, TradingMode
from sgr.exchanges.factory import ExchangePool


class TestExchangePoolInitializeWithCredentials:
    async def test_credentials_none_uses_create_from_config(self) -> None:
        """Unveraendertes Verhalten: ohne credentials-Argument wird
        ExchangeFactory.create() (config-basiert) verwendet, nicht
        create_with_credentials()."""
        pool = ExchangePool()
        mock_adapter = AsyncMock()
        mock_adapter.connect = AsyncMock()

        with patch(
            "sgr.exchanges.factory.ExchangeFactory.create", return_value=mock_adapter
        ) as mock_create:
            await pool.initialize([ExchangeID.PIONEX], TradingMode.PAPER)

        mock_create.assert_called_once_with(ExchangeID.PIONEX, TradingMode.PAPER)
        mock_adapter.connect.assert_awaited_once()
        assert pool.get(ExchangeID.PIONEX, TradingMode.PAPER) is mock_adapter

    async def test_credentials_provided_uses_create_with_credentials(self) -> None:
        """Multi-Tenant-Pfad: credentials-dict fuehrt zu
        create_with_credentials() statt create() - kein .env-Zugriff."""
        pool = ExchangePool()
        mock_adapter = AsyncMock()
        mock_adapter.connect = AsyncMock()

        creds = {"apiKey": "tenant-key", "secret": "tenant-secret"}

        with (
            patch(
                "sgr.exchanges.factory.ExchangeFactory.create_with_credentials",
                return_value=mock_adapter,
            ) as mock_create_with_creds,
            patch("sgr.exchanges.factory.ExchangeFactory.create") as mock_create_plain,
        ):
            await pool.initialize(
                [ExchangeID.PIONEX], TradingMode.PAPER, credentials=creds
            )

        mock_create_with_creds.assert_called_once_with(
            exchange_id=ExchangeID.PIONEX,
            trading_mode=TradingMode.PAPER,
            api_key="tenant-key",
            secret="tenant-secret",
        )
        mock_create_plain.assert_not_called()
        mock_adapter.connect.assert_awaited_once()

    async def test_credentials_passed_through_kwargs(self) -> None:
        """Zusaetzliche kwargs (z.B. futures_mode) muessen bei beiden
        Pfaden durchgereicht werden."""
        pool = ExchangePool()
        mock_adapter = AsyncMock()
        mock_adapter.connect = AsyncMock()

        with patch(
            "sgr.exchanges.factory.ExchangeFactory.create_with_credentials",
            return_value=mock_adapter,
        ) as mock_create_with_creds:
            await pool.initialize(
                [ExchangeID.BINANCE],
                TradingMode.LIVE,
                credentials={"apiKey": "k", "secret": "s"},
                futures_mode=True,
            )

        mock_create_with_creds.assert_called_once_with(
            exchange_id=ExchangeID.BINANCE,
            trading_mode=TradingMode.LIVE,
            api_key="k",
            secret="s",
            futures_mode=True,
        )

    async def test_already_pooled_adapter_not_recreated(self) -> None:
        """Bestehendes Cache-Verhalten bleibt unveraendert: ein bereits
        im Pool vorhandener (exchange, mode)-Key wird nicht erneut ueber
        create_with_credentials() angelegt."""
        pool = ExchangePool()
        existing_adapter = MagicMock()
        pool._adapters[(ExchangeID.PIONEX, TradingMode.PAPER)] = existing_adapter

        with patch(
            "sgr.exchanges.factory.ExchangeFactory.create_with_credentials"
        ) as mock_create_with_creds:
            await pool.initialize(
                [ExchangeID.PIONEX],
                TradingMode.PAPER,
                credentials={"apiKey": "k", "secret": "s"},
            )

        mock_create_with_creds.assert_not_called()
        assert pool.get(ExchangeID.PIONEX, TradingMode.PAPER) is existing_adapter
