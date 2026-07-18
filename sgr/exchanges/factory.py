"""
SGR Exchange Factory
====================
Zentraler Einstiegspunkt für alle Exchange-Adapter.

Warum eine Factory?
- Kein direkter Import von Adapter-Klassen in Business Logic
- Austausch von Adaptern ohne Code-Änderung (z.B. für Testing)
- Konfiguration zentralisiert (nicht verstreut)
- Lifecycle-Management: connect/close automatisch über Context Manager

Usage:
    # Standard (lädt Credentials aus Config)
    async with ExchangeFactory.create(ExchangeID.BINANCE, TradingMode.PAPER) as adapter:
        ticker = await adapter.get_ticker("BTC/USDT")

    # Mit expliziten Credentials (z.B. SaaS: User-spezifische Keys)
    adapter = ExchangeFactory.create_with_credentials(
        exchange_id=ExchangeID.BINANCE,
        trading_mode=TradingMode.LIVE,
        api_key="user_key",
        secret="user_secret",
    )
    await adapter.connect()
    ...
    await adapter.close()

Registry:
    Neue Exchanges registrieren mit @ExchangeFactory.register(ExchangeID.BYBIT)
    Kein Code im Factory selbst ändern nötig.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from sgr.core.logging import get_logger
from sgr.core.types import ExchangeID, TradingMode
from sgr.exchanges.ccxt_base import CCXTBaseAdapter

log = get_logger(__name__)

# Type alias for adapter constructor
AdapterConstructor = Callable[..., CCXTBaseAdapter]

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[ExchangeID, AdapterConstructor] = {}


def _register_defaults() -> None:
    """Register all built-in adapters. Called lazily on first use."""
    from sgr.exchanges.binance import BinanceAdapter
    from sgr.exchanges.pionex import PionexAdapter

    _REGISTRY[ExchangeID.BINANCE] = BinanceAdapter
    _REGISTRY[ExchangeID.PIONEX] = PionexAdapter


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class ExchangeFactory:
    """
    Factory for creating and managing exchange adapter instances.

    All adapters implement ExchangeAdapter protocol.
    Factory handles registration, construction, and lifecycle.
    """

    @staticmethod
    def register(exchange_id: ExchangeID) -> Callable[[AdapterConstructor], AdapterConstructor]:
        """
        Decorator to register a new exchange adapter.

        Usage:
            @ExchangeFactory.register(ExchangeID.BYBIT)
            class BybitAdapter(CCXTBaseAdapter):
                ...
        """

        def decorator(cls: AdapterConstructor) -> AdapterConstructor:
            _REGISTRY[exchange_id] = cls
            log.info("exchange_factory.registered", exchange=exchange_id.value)
            return cls

        return decorator

    @staticmethod
    def create(
        exchange_id: ExchangeID,
        trading_mode: TradingMode,
        **kwargs: Any,
    ) -> CCXTBaseAdapter:
        """
        Create adapter with credentials from SGR config.
        Does NOT connect – call adapter.connect() or use as context manager.

        Args:
            exchange_id: Which exchange
            trading_mode: PAPER or LIVE
            **kwargs: Exchange-specific options (e.g. futures_mode=True for Binance)
        """
        if not _REGISTRY:
            _register_defaults()

        if exchange_id not in _REGISTRY:
            raise ValueError(
                f"Exchange {exchange_id.value} not registered. "
                f"Available: {[e.value for e in _REGISTRY]}"
            )

        adapter_class = _REGISTRY[exchange_id]

        # Use from_config factory method if available
        if hasattr(adapter_class, "from_config"):
            return adapter_class.from_config(trading_mode=trading_mode, **kwargs)

        raise ValueError(f"Adapter {adapter_class.__name__} has no from_config method")

    @staticmethod
    def create_with_credentials(
        exchange_id: ExchangeID,
        trading_mode: TradingMode,
        api_key: str,
        secret: str,
        **kwargs: Any,
    ) -> CCXTBaseAdapter:
        """
        Create adapter with explicit credentials.
        Used for SaaS: per-user API keys from encrypted storage.

        SECURITY: api_key and secret must be decrypted before passing here.
        This method never stores credentials – they exist only in the adapter instance.
        """
        if not _REGISTRY:
            _register_defaults()

        if exchange_id not in _REGISTRY:
            raise ValueError(f"Exchange {exchange_id.value} not registered.")

        adapter_class = _REGISTRY[exchange_id]
        return adapter_class(
            api_key=api_key,
            secret=secret,
            trading_mode=trading_mode,
            **kwargs,
        )

    @staticmethod
    @asynccontextmanager
    async def session(
        exchange_id: ExchangeID,
        trading_mode: TradingMode,
        **kwargs: Any,
    ) -> AsyncIterator[CCXTBaseAdapter]:
        """
        Context manager: creates, connects, and auto-closes adapter.

        Usage:
            async with ExchangeFactory.session(ExchangeID.BINANCE, TradingMode.PAPER) as ex:
                candles = await ex.get_ohlcv("BTC/USDT", "1h")
        """
        adapter = ExchangeFactory.create(exchange_id, trading_mode, **kwargs)
        try:
            await adapter.connect()
            yield adapter
        finally:
            await adapter.close()

    @staticmethod
    def available_exchanges() -> list[ExchangeID]:
        """List all registered exchange IDs."""
        if not _REGISTRY:
            _register_defaults()
        return list(_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Exchange Pool (reuse adapters across requests)
# ---------------------------------------------------------------------------


class ExchangePool:
    """
    Pool of connected exchange adapters.
    Prevents re-connecting on every request (expensive: loads markets).

    Usage:
        pool = ExchangePool()
        await pool.initialize([ExchangeID.BINANCE], TradingMode.PAPER)

        adapter = pool.get(ExchangeID.BINANCE)
        ticker = await adapter.get_ticker("BTC/USDT")

        await pool.close_all()
    """

    def __init__(self) -> None:
        self._adapters: dict[tuple[ExchangeID, TradingMode], CCXTBaseAdapter] = {}
        self._lock = asyncio.Lock()

    async def initialize(
        self,
        exchanges: list[ExchangeID],
        trading_mode: TradingMode,
        **kwargs: Any,
    ) -> None:
        """Connect all specified exchanges concurrently."""
        async with self._lock:
            tasks = []
            for exchange_id in exchanges:
                if (exchange_id, trading_mode) not in self._adapters:
                    adapter = ExchangeFactory.create(exchange_id, trading_mode, **kwargs)
                    self._adapters[(exchange_id, trading_mode)] = adapter
                    tasks.append(adapter.connect())

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for exchange_id, result in zip(exchanges, results, strict=False):
                    if isinstance(result, Exception):
                        log.error(
                            "exchange_pool.connect_failed",
                            exchange=exchange_id.value,
                            error=str(result),
                        )

        log.info(
            "exchange_pool.initialized",
            exchanges=[e.value for e in exchanges],
            trading_mode=trading_mode.value,
            connected=len(self._adapters),
        )

    def get(
        self,
        exchange_id: ExchangeID,
        trading_mode: TradingMode | None = None,
    ) -> CCXTBaseAdapter:
        """
        Get connected adapter.
        Raises KeyError if exchange not in pool.
        """
        from sgr.core.config import get_config

        mode = trading_mode or get_config().trading_mode
        key = (exchange_id, mode)

        if key not in self._adapters:
            raise KeyError(
                f"Exchange {exchange_id.value} ({mode.value}) not in pool. Call initialize() first."
            )
        return self._adapters[key]

    async def close_all(self) -> None:
        """Close all adapters in pool concurrently."""
        tasks = [adapter.close() for adapter in self._adapters.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._adapters.clear()
        log.info("exchange_pool.closed")

    def __len__(self) -> int:
        return len(self._adapters)


# ---------------------------------------------------------------------------
# Global pool singleton
# ---------------------------------------------------------------------------

_pool: ExchangePool | None = None


def get_exchange_pool() -> ExchangePool:
    global _pool
    if _pool is None:
        _pool = ExchangePool()
    return _pool
