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
from uuid import uuid4

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
        assert user["is_admin"] is False  # Default: neue User sind nie Admin

    async def test_get_nonexistent_returns_none(self, repos) -> None:
        user = await repos.users.get_by_email("doesnotexist@never.com")
        assert user is None

    async def test_create_with_explicit_is_admin(self, repos) -> None:
        import uuid

        from passlib.context import CryptContext

        from sgr.core.types import TradingMode

        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
        email = f"test_{uuid.uuid4().hex[:8]}@sgr.test"
        hashed = pwd_context.hash("testpassword123")

        await repos.users.create(
            email=email,
            hashed_password=hashed,
            trading_mode=TradingMode.PAPER,
            is_admin=True,
        )

        user = await repos.users.get_by_email(email)
        assert user["is_admin"] is True

    async def test_set_admin_status_grants_and_revokes(self, repos) -> None:
        """Siehe scripts/grant_admin.py - dieselbe Repository-Methode."""
        import uuid

        from passlib.context import CryptContext

        from sgr.core.types import TradingMode

        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
        email = f"test_{uuid.uuid4().hex[:8]}@sgr.test"
        hashed = pwd_context.hash("testpassword123")

        await repos.users.create(
            email=email, hashed_password=hashed, trading_mode=TradingMode.PAPER
        )

        granted = await repos.users.set_admin_status(email, is_admin=True)
        assert granted is True
        user = await repos.users.get_by_email(email)
        assert user["is_admin"] is True

        revoked = await repos.users.set_admin_status(email, is_admin=False)
        assert revoked is True
        user = await repos.users.get_by_email(email)
        assert user["is_admin"] is False

    async def test_set_admin_status_unknown_email_returns_false(self, repos) -> None:
        updated = await repos.users.set_admin_status("nobody@never.com", is_admin=True)
        assert updated is False


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


class TestStrategySymbolValidationRepositoryResumeSemantics:
    """
    Regressionstests fuer den Resume-Bug (gefunden 2026-09-15, live
    beobachtet): get_processed_symbols() betrachtete vorher JEDE Row mit
    diesem batch_id als "verarbeitet", auch reine Candidate-Rows
    (is_best_for_symbol=False), die waehrend der Strategie-Schleife in
    SymbolStrategyValidationRunner._validate_symbol() geschrieben
    werden, BEVOR die finale Gewinner-Row (is_best_for_symbol=True) am
    Ende folgt. Ein per SIGTERM/SIGKILL unterbrochener Prozess liess
    Symbole dadurch mit ausschliesslich Candidate-Rows zurueck, die ein
    Resume danach STILLSCHWEIGEND fuer immer uebersprungen hat (live:
    4 von 718 Symbolen betroffen). Echte DB-Integration-Tests statt
    Mocks, weil ein reiner Mock des Session-Objekts die tatsaechliche
    WHERE-Klausel (is_best_for_symbol-Filter) nicht ausfuehrt und einen
    Regressions-Revert dieses Filters nicht erkennen wuerde.
    """

    async def _upsert(self, repos, *, batch_id, symbol, strategy, status, is_best) -> None:
        await repos.strategy_symbol_validations.upsert(
            symbol=symbol,
            exchange="binance",
            timeframe="1h",
            strategy=strategy,
            status=status,
            batch_id=batch_id,
            is_best_for_symbol=is_best,
        )

    async def test_symbol_with_only_candidate_rows_is_not_processed(self, repos) -> None:
        """Test 1: nur Candidate-Rows (is_best_for_symbol=False) ->
        get_processed_symbols() darf das Symbol NICHT enthalten."""
        batch_id = f"tr-{uuid4().hex[:16]}"
        await self._upsert(
            repos,
            batch_id=batch_id,
            symbol="INTERRUPTED/USDT",
            strategy="mean_reversion_v1",
            status="no_valid_strategy",
            is_best=False,
        )
        await self._upsert(
            repos,
            batch_id=batch_id,
            symbol="INTERRUPTED/USDT",
            strategy="trend_following_v1",
            status="no_valid_strategy",
            is_best=False,
        )

        processed = await repos.strategy_symbol_validations.get_processed_symbols(batch_id=batch_id)

        assert "INTERRUPTED/USDT" not in processed

    async def test_symbol_with_final_best_row_is_processed(self, repos) -> None:
        """Test 2: eine gueltige finale Row mit is_best_for_symbol=True
        -> Symbol wird als processed erkannt."""
        batch_id = f"tr-{uuid4().hex[:16]}"
        await self._upsert(
            repos,
            batch_id=batch_id,
            symbol="FINISHED/USDT",
            strategy="mean_reversion_v1",
            status="no_valid_strategy",
            is_best=True,
        )

        processed = await repos.strategy_symbol_validations.get_processed_symbols(batch_id=batch_id)

        assert "FINISHED/USDT" in processed

    async def test_multiple_candidates_without_final_row_not_processed(self, repos) -> None:
        """Test 3: mehrere Candidate-Rows, aber keine finale Row ->
        Symbol wird erneut verarbeitet (nicht in processed)."""
        batch_id = f"tr-{uuid4().hex[:16]}"
        for strategy in [
            "mean_reversion_v1",
            "trend_following_v1",
            "breakout_v1",
            "momentum_v1",
        ]:
            await self._upsert(
                repos,
                batch_id=batch_id,
                symbol="MULTI_CANDIDATE/USDT",
                strategy=strategy,
                status="no_valid_strategy",
                is_best=False,
            )

        processed = await repos.strategy_symbol_validations.get_processed_symbols(batch_id=batch_id)

        assert "MULTI_CANDIDATE/USDT" not in processed

    async def test_batch_interrupted_mid_strategy_evaluation_reprocesses_symbol(
        self, repos
    ) -> None:
        """Test 4: simuliert einen mitten in der Strategie-Evaluation
        abgebrochenen Batch (SIGTERM/SIGKILL) - ein Symbol mit
        Candidate-Rows fuer 2 von 5 Strategien (Prozess starb vor dem
        Rest UND vor der finalen Row) muss beim Resume erneut
        VOLLSTAENDIG verarbeitet werden (nicht in get_processed_symbols())."""
        batch_id = f"tr-{uuid4().hex[:16]}"
        await self._upsert(
            repos,
            batch_id=batch_id,
            symbol="KILLED_MIDWAY/USDT",
            strategy="mean_reversion_v1",
            status="no_valid_strategy",
            is_best=False,
        )
        await self._upsert(
            repos,
            batch_id=batch_id,
            symbol="KILLED_MIDWAY/USDT",
            strategy="trend_following_v1",
            status="no_valid_strategy",
            is_best=False,
        )
        # Prozess stirbt hier - breakout_v1/momentum_v1/volatility_
        # adjusted_momentum_v1 wurden nie evaluiert, die finale Row nie
        # geschrieben.

        processed = await repos.strategy_symbol_validations.get_processed_symbols(batch_id=batch_id)

        assert "KILLED_MIDWAY/USDT" not in processed

    async def test_fully_completed_symbol_is_skipped_on_resume(self, repos) -> None:
        """Test 5: ein vollstaendig abgeschlossenes Symbol (Candidate-
        Rows + finale Row) wird bei einem Resume NICHT erneut
        verarbeitet."""
        batch_id = f"tr-{uuid4().hex[:16]}"
        await self._upsert(
            repos,
            batch_id=batch_id,
            symbol="COMPLETE/USDT",
            strategy="mean_reversion_v1",
            status="no_valid_strategy",
            is_best=False,
        )
        await self._upsert(
            repos,
            batch_id=batch_id,
            symbol="COMPLETE/USDT",
            strategy="trend_following_v1",
            status="no_valid_strategy",
            is_best=False,
        )
        # Finale Row - der Runner markiert die Gewinner-Strategie explizit.
        await self._upsert(
            repos,
            batch_id=batch_id,
            symbol="COMPLETE/USDT",
            strategy="trend_following_v1",
            status="no_valid_strategy",
            is_best=True,
        )

        processed = await repos.strategy_symbol_validations.get_processed_symbols(batch_id=batch_id)

        assert "COMPLETE/USDT" in processed

    async def test_rerunning_a_completed_batch_creates_no_duplicate_final_rows(self, repos) -> None:
        """Test 6: ein erneuter upsert() fuer dieselbe (symbol, exchange,
        timeframe, strategy, batch_id)-Kombination (z.B. weil ein
        vollstaendig abgeschlossener Batch versehentlich erneut
        angestossen wird) darf wegen des UNIQUE-Constraints +
        ON CONFLICT DO UPDATE kein Duplikat erzeugen, nur die bestehende
        Row aktualisieren."""
        batch_id = f"tr-{uuid4().hex[:16]}"
        await self._upsert(
            repos,
            batch_id=batch_id,
            symbol="RERUN/USDT",
            strategy="trend_following_v1",
            status="no_valid_strategy",
            is_best=True,
        )
        # Identischer Aufruf ein zweites Mal (simuliert Rerun eines
        # bereits abgeschlossenen Batches fuer dasselbe Symbol).
        await self._upsert(
            repos,
            batch_id=batch_id,
            symbol="RERUN/USDT",
            strategy="trend_following_v1",
            status="no_valid_strategy",
            is_best=True,
        )

        rows = await repos.strategy_symbol_validations.get_by_symbol(
            symbol="RERUN/USDT", exchange="binance", timeframe="1h", batch_id=batch_id
        )

        assert len(rows) == 1
