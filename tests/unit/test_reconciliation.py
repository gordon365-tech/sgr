"""
Tests für den ReconciliationEngine (Phase 7B).

Dieser Engine schließt die zuvor dokumentierte, aber nie implementierte
Lücke: PositionRepository (Persistenz) und ExchangeAdapter.get_positions()
(Exchange-Abfrage) existierten beide, aber nichts verglich sie. Ein
Split-Brain-Fall (Order auf der Exchange ausgeführt, lokaler State nie
aktualisiert) wäre unentdeckt geblieben.

Teststrategie:
    1. Nicht LIVE -> SKIPPED_NOT_LIVE, kein Exchange-Call
    2. Alle Positionen stimmen überein -> CLEAN, keine Discrepancies
    3. Position existiert nur auf der Exchange -> MISSING_LOCALLY
       (Split-Brain-Fall) + SPLIT_BRAIN_RISK-Log
    4. Position existiert nur lokal -> MISSING_ON_EXCHANGE
    5. Menge weicht ab -> QUANTITY_MISMATCH
    6. Side weicht ab (gleiche Menge) -> QUANTITY_MISMATCH
    7. Rundungsdifferenz innerhalb Toleranz -> keine Abweichung
    8. Exchange-Fehler -> FAILED, keine unbehandelte Exception
    9. Event Bus: ReconciliationCompletedEvent wird immer publiziert,
       auch bei SKIPPED_NOT_LIVE und FAILED
   10. Event Bus nicht verbunden -> Reconciliation liefert trotzdem Ergebnis
   11. Mehrere Symbole gemischt -> jedes wird korrekt klassifiziert
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from sgr.core.types import (
    DiscrepancyType,
    ExchangeID,
    Position,
    PositionSide,
    ReconciliationStatus,
    Symbol,
    TradingMode,
)
from sgr.reconciliation.engine import ReconciliationEngine

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------


def _symbol(base: str = "BTC") -> Symbol:
    return Symbol(base=base, quote="USDT", exchange=ExchangeID.PIONEX)


def _position(
    symbol: Symbol,
    quantity: Decimal,
    side: PositionSide = PositionSide.LONG,
) -> Position:
    return Position(
        symbol=symbol,
        side=side,
        quantity=quantity,
        entry_price=Decimal("50000"),
        current_price=Decimal("50000"),
        opened_at=datetime.now(tz=UTC),
        strategy_name="trend_v1",
        trading_mode=TradingMode.LIVE,
    )


class _Setup:
    """Bündelt Mock-Pool/Portfolio-Engine für den ReconciliationEngine."""

    def __init__(self, trading_mode: TradingMode = TradingMode.LIVE) -> None:
        self.adapter = AsyncMock()
        self.pool = MagicMock()
        self.pool.get.return_value = self.adapter
        self.portfolio_engine = MagicMock()
        self.portfolio_engine.positions = []
        self.trading_mode = trading_mode

    def engine(self) -> ReconciliationEngine:
        return ReconciliationEngine(
            exchange_pool=self.pool,
            portfolio_engine=self.portfolio_engine,
            trading_mode=self.trading_mode,
            exchange_id=ExchangeID.PIONEX,
        )


# ---------------------------------------------------------------------------
# 1. Nicht LIVE -> SKIPPED_NOT_LIVE
# ---------------------------------------------------------------------------


async def test_skipped_when_not_live() -> None:
    s = _Setup(trading_mode=TradingMode.PAPER)
    engine = s.engine()

    result = await engine.reconcile()

    assert result.status == ReconciliationStatus.SKIPPED_NOT_LIVE
    s.pool.get.assert_not_called()
    s.adapter.get_positions.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Alles stimmt überein -> CLEAN
# ---------------------------------------------------------------------------


async def test_clean_when_positions_match() -> None:
    s = _Setup()
    btc = _symbol("BTC")
    s.portfolio_engine.positions = [_position(btc, Decimal("0.5"))]
    s.adapter.get_positions.return_value = [_position(btc, Decimal("0.5"))]
    engine = s.engine()

    result = await engine.reconcile()

    assert result.status == ReconciliationStatus.CLEAN
    assert result.discrepancies == []
    assert result.checked_symbols == 1


async def test_clean_when_no_positions_anywhere() -> None:
    s = _Setup()
    s.adapter.get_positions.return_value = []
    engine = s.engine()

    result = await engine.reconcile()

    assert result.status == ReconciliationStatus.CLEAN
    assert result.checked_symbols == 0


# ---------------------------------------------------------------------------
# 3. Split-Brain: nur auf der Exchange bekannt
# ---------------------------------------------------------------------------


async def test_missing_locally_is_split_brain_risk(mocker) -> None:
    s = _Setup()
    btc = _symbol("BTC")
    s.portfolio_engine.positions = []  # lokal nichts bekannt
    s.adapter.get_positions.return_value = [_position(btc, Decimal("0.3"))]
    engine = s.engine()

    mock_log = mocker.patch("sgr.reconciliation.engine.log")
    result = await engine.reconcile()

    assert result.status == ReconciliationStatus.DISCREPANCIES_FOUND
    assert len(result.discrepancies) == 1
    d = result.discrepancies[0]
    assert d.discrepancy_type == DiscrepancyType.MISSING_LOCALLY
    assert d.exchange_quantity == Decimal("0.3")
    assert result.has_split_brain_risk is True

    # SPLIT_BRAIN_RISK muss als error-level strukturiertes Log erscheinen
    error_calls = [c for c in mock_log.error.call_args_list]
    assert any(c.args[0] == "execution_engine.SPLIT_BRAIN_RISK" for c in error_calls)


# ---------------------------------------------------------------------------
# 4. Nur lokal bekannt, nicht mehr auf der Exchange
# ---------------------------------------------------------------------------


async def test_missing_on_exchange() -> None:
    s = _Setup()
    btc = _symbol("BTC")
    s.portfolio_engine.positions = [_position(btc, Decimal("0.2"))]
    s.adapter.get_positions.return_value = []
    engine = s.engine()

    result = await engine.reconcile()

    assert result.status == ReconciliationStatus.DISCREPANCIES_FOUND
    d = result.discrepancies[0]
    assert d.discrepancy_type == DiscrepancyType.MISSING_ON_EXCHANGE
    assert d.local_quantity == Decimal("0.2")
    assert result.has_split_brain_risk is False


# ---------------------------------------------------------------------------
# 5. Mengen-Abweichung
# ---------------------------------------------------------------------------


async def test_quantity_mismatch() -> None:
    s = _Setup()
    btc = _symbol("BTC")
    s.portfolio_engine.positions = [_position(btc, Decimal("0.5"))]
    s.adapter.get_positions.return_value = [_position(btc, Decimal("0.7"))]
    engine = s.engine()

    result = await engine.reconcile()

    assert result.status == ReconciliationStatus.DISCREPANCIES_FOUND
    d = result.discrepancies[0]
    assert d.discrepancy_type == DiscrepancyType.QUANTITY_MISMATCH
    assert d.local_quantity == Decimal("0.5")
    assert d.exchange_quantity == Decimal("0.7")


# ---------------------------------------------------------------------------
# 6. Side-Abweichung bei gleicher Menge
# ---------------------------------------------------------------------------


async def test_side_mismatch_is_quantity_mismatch_type() -> None:
    s = _Setup()
    btc = _symbol("BTC")
    s.portfolio_engine.positions = [_position(btc, Decimal("0.5"), PositionSide.LONG)]
    s.adapter.get_positions.return_value = [_position(btc, Decimal("0.5"), PositionSide.SHORT)]
    engine = s.engine()

    result = await engine.reconcile()

    assert result.status == ReconciliationStatus.DISCREPANCIES_FOUND
    d = result.discrepancies[0]
    assert d.discrepancy_type == DiscrepancyType.QUANTITY_MISMATCH
    assert d.local_side == PositionSide.LONG
    assert d.exchange_side == PositionSide.SHORT


# ---------------------------------------------------------------------------
# 7. Rundungsdifferenz innerhalb Toleranz -> kein False Positive
# ---------------------------------------------------------------------------


async def test_tiny_rounding_difference_is_not_a_discrepancy() -> None:
    s = _Setup()
    btc = _symbol("BTC")
    s.portfolio_engine.positions = [_position(btc, Decimal("0.50000000"))]
    s.adapter.get_positions.return_value = [_position(btc, Decimal("0.50000001"))]
    engine = s.engine()

    result = await engine.reconcile()

    # Differenz von 1e-8 liegt exakt an der Toleranzgrenze -> nicht > Toleranz
    assert result.status == ReconciliationStatus.CLEAN
    assert result.discrepancies == []


# ---------------------------------------------------------------------------
# 8. Exchange-Fehler -> FAILED, kein Crash
# ---------------------------------------------------------------------------


async def test_exchange_error_yields_failed_not_raised() -> None:
    s = _Setup()
    s.adapter.get_positions.side_effect = RuntimeError("exchange unreachable")
    engine = s.engine()

    result = await engine.reconcile()

    assert result.status == ReconciliationStatus.FAILED
    assert result.error is not None
    assert "exchange unreachable" in result.error


async def test_pool_key_error_yields_failed_not_raised() -> None:
    """Adapter nicht im Pool (z.B. Exchange nicht initialisiert) -> FAILED."""
    s = _Setup()
    s.pool.get.side_effect = KeyError("pionex not in pool")
    engine = s.engine()

    result = await engine.reconcile()

    assert result.status == ReconciliationStatus.FAILED


# ---------------------------------------------------------------------------
# 9. Event wird immer publiziert
# ---------------------------------------------------------------------------


async def test_event_published_on_clean(mocker) -> None:
    s = _Setup()
    s.adapter.get_positions.return_value = []
    mock_bus = AsyncMock()
    mocker.patch("sgr.reconciliation.engine.get_event_bus", return_value=mock_bus)
    engine = s.engine()

    await engine.reconcile()

    published_types = [type(c.args[0]).__name__ for c in mock_bus.publish.call_args_list]
    assert "ReconciliationCompletedEvent" in published_types


async def test_event_published_on_skipped_not_live(mocker) -> None:
    s = _Setup(trading_mode=TradingMode.PAPER)
    mock_bus = AsyncMock()
    mocker.patch("sgr.reconciliation.engine.get_event_bus", return_value=mock_bus)
    engine = s.engine()

    await engine.reconcile()

    mock_bus.publish.assert_called_once()
    event = mock_bus.publish.call_args.args[0]
    assert event.result.status == ReconciliationStatus.SKIPPED_NOT_LIVE


async def test_event_published_on_failed(mocker) -> None:
    s = _Setup()
    s.adapter.get_positions.side_effect = RuntimeError("boom")
    mock_bus = AsyncMock()
    mocker.patch("sgr.reconciliation.engine.get_event_bus", return_value=mock_bus)
    engine = s.engine()

    await engine.reconcile()

    published_types = [type(c.args[0]).__name__ for c in mock_bus.publish.call_args_list]
    assert "ReconciliationCompletedEvent" in published_types


# ---------------------------------------------------------------------------
# 10. Event Bus nicht verbunden -> Reconciliation liefert trotzdem Ergebnis
# ---------------------------------------------------------------------------


async def test_reconcile_succeeds_even_when_event_bus_unconnected() -> None:
    """
    Verwendet den echten (nicht gemockten) get_event_bus()-Singleton ohne
    .connect() - publish() wirft RuntimeError. Fail-Safe-Grundsatz: das
    eigentliche Ergebnis darf davon nicht beeinflusst werden.
    """
    s = _Setup()
    btc = _symbol("BTC")
    s.portfolio_engine.positions = [_position(btc, Decimal("0.5"))]
    s.adapter.get_positions.return_value = [_position(btc, Decimal("0.5"))]
    engine = s.engine()

    result = await engine.reconcile()

    assert result.status == ReconciliationStatus.CLEAN


# ---------------------------------------------------------------------------
# 11. Mehrere Symbole gemischt
# ---------------------------------------------------------------------------


async def test_multiple_symbols_each_classified_independently() -> None:
    s = _Setup()
    btc, eth, sol = _symbol("BTC"), _symbol("ETH"), _symbol("SOL")

    s.portfolio_engine.positions = [
        _position(btc, Decimal("0.5")),  # matched
        _position(eth, Decimal("2.0")),  # missing on exchange
    ]
    s.adapter.get_positions.return_value = [
        _position(btc, Decimal("0.5")),  # matched
        _position(sol, Decimal("10")),  # missing locally (split-brain)
    ]
    engine = s.engine()

    result = await engine.reconcile()

    assert result.status == ReconciliationStatus.DISCREPANCIES_FOUND
    assert result.checked_symbols == 3
    types_by_symbol = {d.symbol_key: d.discrepancy_type for d in result.discrepancies}
    assert types_by_symbol[str(eth)] == DiscrepancyType.MISSING_ON_EXCHANGE
    assert types_by_symbol[str(sol)] == DiscrepancyType.MISSING_LOCALLY
    assert str(btc) not in types_by_symbol  # matched -> keine Discrepancy
