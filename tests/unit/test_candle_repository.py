"""
Unit-Tests für CandleRepository.upsert_batch() Chunking.

Im Gegensatz zu tests/integration/test_repositories.py (echte PostgreSQL
noetig, DB_INTEGRATION_TESTS=1) mockt dieser Test get_session(), um ohne
laufende DB zu funktionieren.

Hintergrund: PostgreSQL/asyncpg erlaubt maximal 32767 Query-Parameter pro
Statement. Bei 9 Spalten pro Candle-Row liegt das theoretische Limit bei
32767 // 9 = 3640 Rows in einem einzigen INSERT. Auf dem Produktionsserver
durch echten Log-Fehler bestätigt (asyncpg.exceptions._base.InterfaceError:
"the number of query arguments cannot exceed 32767"), ausgelöst durch
BacktestDataLoader._persist_to_db() bei einem langen Walk-Forward-
Validierungszeitraum. upsert_batch() chunked seitdem intern in Gruppen von
_CHUNK_SIZE (3000), transparent für den Aufrufer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from sgr.core.repositories import CandleRepository
from sgr.core.types import Candle, ExchangeID, Symbol

if TYPE_CHECKING:
    import pytest_mock

SYMBOL = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def make_candle(ts: datetime, price: float = 50000.0) -> Candle:
    p = Decimal(str(price))
    return Candle(
        symbol=SYMBOL,
        timestamp=ts,
        timeframe="1h",
        open=p,
        high=p + 1,
        low=p - 1,
        close=p,
        volume=Decimal("10"),
    )


def make_candles(n: int) -> list[Candle]:
    from datetime import timedelta

    base = datetime(2026, 1, 1, tzinfo=UTC)
    return [make_candle(base + timedelta(hours=i)) for i in range(n)]


class _FakeAsyncSession:
    """Stand-in für AsyncSession: execute() liefert MagicMock mit
    konfigurierbarem rowcount, jeder Call wird für Assertions erfasst."""

    def __init__(self) -> None:
        self.executed_statements: list = []

    async def execute(self, stmt):
        self.executed_statements.append(stmt)
        result = MagicMock()
        # rowcount = Anzahl der Rows im jeweiligen VALUES-Klausel dieses
        # Statements - simuliert "alle Rows neu eingefügt" (kein Konflikt).
        result.rowcount = len(stmt.compile().params) // 9 if hasattr(stmt, "compile") else 0
        return result


def _patch_get_session_multi(mocker: pytest_mock.MockerFixture, session: _FakeAsyncSession):
    """Patcht get_session() in sgr.core.repositories als Async-Context-
    Manager, der bei jedem Aufruf dieselbe Fake-Session zurückgibt (um
    executed_statements über mehrere Chunks hinweg zu sammeln)."""

    def _factory():
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    mocker.patch("sgr.core.repositories.get_session", side_effect=_factory)


class TestCandleRepositoryUpsertBatchChunking:
    async def test_small_batch_uses_single_statement(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession()
        _patch_get_session_multi(mocker, session)

        repo = CandleRepository()
        candles = make_candles(10)
        await repo.upsert_batch(candles)

        assert len(session.executed_statements) == 1

    async def test_batch_larger_than_chunk_size_splits_into_multiple_statements(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession()
        _patch_get_session_multi(mocker, session)

        repo = CandleRepository()
        # 2.5x _CHUNK_SIZE -> erwartet 3 Chunks (3000, 3000, 1000)
        n = int(repo._CHUNK_SIZE * 2.5)
        candles = make_candles(n)
        await repo.upsert_batch(candles)

        assert len(session.executed_statements) == 3

    async def test_batch_exactly_chunk_size_uses_single_statement(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession()
        _patch_get_session_multi(mocker, session)

        repo = CandleRepository()
        candles = make_candles(repo._CHUNK_SIZE)
        await repo.upsert_batch(candles)

        assert len(session.executed_statements) == 1

    async def test_batch_one_over_chunk_size_splits_into_two_statements(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession()
        _patch_get_session_multi(mocker, session)

        repo = CandleRepository()
        candles = make_candles(repo._CHUNK_SIZE + 1)
        await repo.upsert_batch(candles)

        assert len(session.executed_statements) == 2

    async def test_chunk_size_stays_under_asyncpg_parameter_limit(self) -> None:
        """9 Spalten pro Row - _CHUNK_SIZE * 9 muss klar unter der
        asyncpg-Grenze von 32767 Parametern pro Statement bleiben."""
        repo = CandleRepository()
        assert repo._CHUNK_SIZE * 9 < 32767

    async def test_empty_list_makes_no_db_call(self, mocker: pytest_mock.MockerFixture) -> None:
        session = _FakeAsyncSession()
        _patch_get_session_multi(mocker, session)

        repo = CandleRepository()
        result = await repo.upsert_batch([])

        assert result == 0
        assert session.executed_statements == []

    async def test_returns_sum_of_rowcounts_across_chunks(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        session = _FakeAsyncSession()
        _patch_get_session_multi(mocker, session)

        repo = CandleRepository()
        n = repo._CHUNK_SIZE + 50
        candles = make_candles(n)
        result = await repo.upsert_batch(candles)

        # Beide Fake-Chunks werden als vollstaendig eingefuegt simuliert
        # (kein ON CONFLICT-Skip in diesem Fake) -> Summe == n.
        assert result == n
