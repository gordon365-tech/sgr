"""
Shared test fixtures.
All tests share these via conftest auto-discovery.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sgr.core.config import get_config
from sgr.core.types import (
    Candle,
    ExchangeID,
    MarketRegime,
    Signal,
    SignalDirection,
    Symbol,
)


# Reset config cache before each test module
@pytest.fixture(autouse=True)
def reset_config_cache() -> None:
    get_config.cache_clear()
    yield
    get_config.cache_clear()


@pytest.fixture
def btc_symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


@pytest.fixture
def eth_symbol() -> Symbol:
    return Symbol(base="ETH", quote="USDT", exchange=ExchangeID.BINANCE)


@pytest.fixture
def now() -> datetime:
    return datetime.now(tz=UTC)


@pytest.fixture
def sample_candle(btc_symbol: Symbol, now: datetime) -> Candle:
    return Candle(
        symbol=btc_symbol,
        timestamp=now,
        timeframe="1h",
        open=Decimal("50000"),
        high=Decimal("51000"),
        low=Decimal("49500"),
        close=Decimal("50800"),
        volume=Decimal("1234.56"),
    )


@pytest.fixture
def sample_signal(btc_symbol: Symbol, now: datetime) -> Signal:
    return Signal(
        timestamp=now,
        strategy_name="test_strategy",
        symbol=btc_symbol,
        direction=SignalDirection.LONG,
        confidence=0.75,
        regime=MarketRegime.TRENDING_UP,
    )
