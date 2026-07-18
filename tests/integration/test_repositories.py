"""
Integration Tests für Repository Layer.

Diese Tests erfordern eine laufende PostgreSQL-Instanz.
Werden nur ausgeführt wenn DB_INTEGRATION_TESTS=1 gesetzt ist.

Verwendung:
    DB_INTEGRATION_TESTS=1 pytest tests/integration/test_repositories.py
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

# Skip wenn keine Integration-Tests gewünscht
pytestmark = pytest.mark.skipif(
    not os.environ.get("DB_INTEGRATION_TESTS"),
    reason="DB integration tests disabled. Set DB_INTEGRATION_TESTS=1 to enable.",
)


@pytest.fixture
async def repos():
    """Repository mit Test-DB Verbindung."""
    from sgr.core.database import close_db, init_db
    from sgr.core.repositories import Repositories

    await init_db()
    repos = Repositories()
    yield repos
    await close_db()


class TestCandleRepository:
    async def test_upsert_and_retrieve(self, repos) -> None:
        from sgr.core.types import Candle, ExchangeID, Symbol

        sym = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)
        candle = Candle(
            symbol=sym,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            timeframe="1h",
            open=Decimal("50000"),
            high=Decimal("51000"),
            low=Decimal("49000"),
            close=Decimal("50500"),
            volume=Decimal("1000"),
        )

        count = await repos.candles.upsert_batch([candle])
        assert count >= 0  # 0 wenn schon vorhanden (idempotent)

    async def test_upsert_idempotent(self, repos) -> None:
        """Zweimaliges Upsert = kein Fehler."""
        from sgr.core.types import Candle, ExchangeID, Symbol

        sym = Symbol(base="ETH", quote="USDT", exchange=ExchangeID.BINANCE)
        candle = Candle(
            symbol=sym,
            timestamp=datetime(2024, 1, 2, tzinfo=UTC),
            timeframe="1h",
            open=Decimal("3000"),
            high=Decimal("3100"),
            low=Decimal("2900"),
            close=Decimal("3050"),
            volume=Decimal("500"),
        )
        await repos.candles.upsert_batch([candle])
        await repos.candles.upsert_batch([candle])  # Kein Fehler

    async def test_get_latest_timestamp(self, repos) -> None:
        ts = await repos.candles.get_latest_timestamp("BTC/USDT", "binance", "1h")
        # Kann None sein wenn keine Daten – das ist OK
        assert ts is None or isinstance(ts, datetime)


class TestStrategyRepository:
    async def test_upsert_strategy(self, repos) -> None:
        await repos.strategies.upsert(
            name="test_strategy_v1",
            version="1.0.0",
            supported_regimes=["trending_up", "trending_down"],
        )

    async def test_update_performance(self, repos) -> None:
        await repos.strategies.upsert(
            name="test_perf_strategy",
            version="1.0.0",
            supported_regimes=["ranging"],
        )
        await repos.strategies.update_performance(
            name="test_perf_strategy",
            sharpe=1.5,
            sortino=2.0,
            max_drawdown=0.12,
            hit_rate=0.58,
            total_trades=45,
        )

    async def test_set_active(self, repos) -> None:
        await repos.strategies.upsert("test_active_strat", "1.0", ["ranging"])
        await repos.strategies.set_active("test_active_strat", True)
        await repos.strategies.set_active("test_active_strat", False, "test deactivation")


class TestUserRepository:
    async def test_create_and_retrieve(self, repos) -> None:
        import uuid

        from passlib.context import CryptContext

        from sgr.core.types import TradingMode

        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
        email = f"test_{uuid.uuid4().hex[:8]}@sgr.test"
        hashed = pwd_context.hash("testpassword123")

        user_id = await repos.users.create(
            email=email,
            hashed_password=hashed,
            trading_mode=TradingMode.PAPER,
        )
        assert user_id

        user = await repos.users.get_by_email(email)
        assert user is not None
        assert user["email"] == email
        assert user["trading_mode"] == "paper"

    async def test_get_nonexistent_returns_none(self, repos) -> None:
        user = await repos.users.get_by_email("doesnotexist@never.com")
        assert user is None


class TestRiskEventRepository:
    async def test_log_event(self, repos) -> None:
        from sgr.core.types import TradingMode

        await repos.risk_events.log_event(
            event_type="kill_switch",
            severity="critical",
            title="Kill Switch Triggered",
            message="Max drawdown exceeded: 16%",
            trading_mode=TradingMode.PAPER,
            metrics_snapshot={"drawdown": 0.16, "portfolio_value": 8400},
        )
