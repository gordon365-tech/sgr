"""
SGR E2E Lifecycle Scenarios - Phase 2
========================================
Fuenf weitere Szenarien ueber denselben echten Produktionspfad wie
Phase 1 (siehe conftest.py): Stop-Loss-Exit, Max-Holding-Time-Exit,
Position-Limit, Duplicate-Order-Protection, Unknown-Order-State.

Jeder Test folgt SETUP -> ACTION -> EXPECTED -> ACTUAL -> PASS/FAIL
(siehe Docstring/Kommentare je Test).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest

from sgr.core.types import (
    AssetClass,
    ExchangeID,
    MarketRegime,
    OrderRequest,
    OrderStatus,
    OrderType,
    RiskDecision,
    Side,
    Symbol,
    TradingCycleStatus,
    TradingMode,
)
from sgr.execution.engine import ExecutionEngine
from sgr.market_data.feature_store import FeatureStore
from sgr.orchestrator.engine import TradingOrchestrator
from sgr.portfolio.engine import PortfolioEngine
from sgr.risk.position_protection import PositionProtectionWatchdog

from .conftest import (
    E2E_SYMBOL,
    E2E_SYMBOL_KEY,
    E2E_TIMEFRAME,
    build_feature_set,
    build_synthetic_position,
)

pytestmark = pytest.mark.e2e_scenario


# ============================================================================
# Stop-Loss Exit
# ============================================================================


@pytest.mark.asyncio
async def test_stop_loss_exit(
    orchestrator: TradingOrchestrator,
    feature_store: FeatureStore,
    portfolio_engine: PortfolioEngine,
    watchdog: PositionProtectionWatchdog,
) -> None:
    """
    SETUP: echte LONG-Position wie test_long_entry, mit angehaengtem
        Stop-Loss (RISK_STOP_LOSS_PCT=0.01 unter Entry).
    ACTION: current_price synthetisch UNTER die SL-Schwelle senken
        (identischer Preis-Tick-Mechanismus wie bei Take-Profit, siehe
        dortigen Docstring), dann watchdog.check_positions_once() -
        loest einen ECHTEN reduce-only Exit gegen das Testnet aus.
    """
    await feature_store.save(build_feature_set(close=Decimal("0.0060"), direction="up"))
    entry_result = await orchestrator.run_cycle(
        symbol_key=E2E_SYMBOL_KEY, timeframe=E2E_TIMEFRAME, regime=MarketRegime.TRENDING_UP
    )
    assert entry_result.status == TradingCycleStatus.ORDER_FILLED, entry_result
    assert len(portfolio_engine.positions) == 1
    position = portfolio_engine.positions[0]
    stop_loss_price = position.stop_loss_price
    assert stop_loss_price is not None

    triggering_price = stop_loss_price * Decimal("0.99")
    await portfolio_engine.update_prices({E2E_SYMBOL.ccxt_symbol: triggering_price})
    await watchdog.check_positions_once()

    # EXPECTED vs ACTUAL vs PASS/FAIL
    assert len(portfolio_engine.positions) == 0, portfolio_engine.positions
    trades = portfolio_engine.trade_history
    assert len(trades) == 1, trades
    assert trades[0]["close_reason"] == "stop_loss", trades[0]
    assert trades[0]["symbol"] == str(E2E_SYMBOL), trades[0]


# ============================================================================
# Max-Holding-Time Exit
# ============================================================================


@pytest.mark.asyncio
async def test_max_holding_time_exit(
    orchestrator: TradingOrchestrator,
    feature_store: FeatureStore,
    portfolio_engine: PortfolioEngine,
    watchdog: PositionProtectionWatchdog,
) -> None:
    """
    SETUP: echte LONG-Position, danach opened_at/max_holding_until der
        Position direkt in die Vergangenheit versetzt - reine In-Memory-
        Manipulation des Test-eigenen Python-Objekts (keine DB, kein
        Warten auf 30 echte Minuten), analog zu freezegun-artigem
        Zeitreisen. Preis bleibt bewusst UNVERAENDERT (zwischen SL und
        TP), damit ausschliesslich die Max-Holding-Bedingung greift,
        nicht Preis-Schwellen.
    ACTION: watchdog.check_positions_once() - derselbe Code-Pfad wie
        beim echten periodischen Tick, greift hier auf die
        zurueckdatierte Deadline.
    """
    from datetime import UTC, datetime, timedelta

    await feature_store.save(build_feature_set(close=Decimal("0.0060"), direction="up"))
    entry_result = await orchestrator.run_cycle(
        symbol_key=E2E_SYMBOL_KEY, timeframe=E2E_TIMEFRAME, regime=MarketRegime.TRENDING_UP
    )
    assert entry_result.status == TradingCycleStatus.ORDER_FILLED, entry_result
    assert len(portfolio_engine.positions) == 1
    position = portfolio_engine.positions[0]
    assert position.max_holding_until is not None

    past = datetime.now(tz=UTC) - timedelta(hours=1)
    backdated = position.model_copy(update={"opened_at": past, "max_holding_until": past})
    portfolio_engine._state._positions[str(position.symbol)] = backdated

    await watchdog.check_positions_once()

    # EXPECTED vs ACTUAL vs PASS/FAIL
    assert len(portfolio_engine.positions) == 0, portfolio_engine.positions
    trades = portfolio_engine.trade_history
    assert len(trades) == 1, trades
    assert trades[0]["close_reason"] == "max_holding_time", trades[0]


# ============================================================================
# Position-Limit (Risk Rejection)
# ============================================================================


@pytest.mark.asyncio
async def test_position_limit_rejection(
    orchestrator: TradingOrchestrator,
    feature_store: FeatureStore,
    portfolio_engine: PortfolioEngine,
) -> None:
    """
    SETUP: Portfolio mit RISK_MAX_OPEN_POSITIONS (=3, siehe conftest.py)
        synthetischen Positionen ueber das Hard Limit befuellt -
        identisches, bereits etabliertes Muster wie
        tests/integration/test_orchestrator_pipeline.py::
        test_risk_rejection_prevents_execution.
    ACTION: orchestrator.run_cycle() fuer ein VIERTES, echtes Signal.
    """
    for i in range(3):
        pos = build_synthetic_position(i)
        portfolio_engine._state._positions[str(pos.symbol)] = pos
    assert len(portfolio_engine.positions) == 3

    await feature_store.save(build_feature_set(close=Decimal("0.0060"), direction="up"))
    result = await orchestrator.run_cycle(
        symbol_key=E2E_SYMBOL_KEY, timeframe=E2E_TIMEFRAME, regime=MarketRegime.TRENDING_UP
    )

    # EXPECTED vs ACTUAL vs PASS/FAIL
    assert result.status == TradingCycleStatus.RISK_REJECTED, result
    assert result.signal is not None, result  # Strategie hat ein Signal erzeugt
    assert result.assessment is not None
    assert result.assessment.decision == RiskDecision.REJECTED, result.assessment
    assert "position" in (result.assessment.rejection_reason or "").lower(), result.assessment
    # Keine vierte Position darf entstanden sein - Order darf nicht
    # einmal versucht worden sein.
    assert len(portfolio_engine.positions) == 3


# ============================================================================
# Duplicate-Order-Protection
# ============================================================================


@pytest.mark.asyncio
async def test_duplicate_order_protection(execution_engine: ExecutionEngine) -> None:
    """
    SETUP: eine einzelne, echte OrderRequest (gleiche order.id fuer
        beide Aufrufe).
    ACTION: dieselbe OrderRequest-Instanz ZWEIMAL GLEICHZEITIG
        (asyncio.gather) an execution_engine.execute() - simuliert
        einen Orchestrator-/Netzwerk-Retry derselben Order waehrend der
        erste Versuch noch auf die (echte) Testnet-Antwort wartet. Siehe
        sgr/execution/order_safety.py Modul-Docstring Punkt 1+2: der
        Placeholder wird VOR dem Exchange-Call gesetzt, genau um dieses
        Race zu faengen.
    """
    order = OrderRequest(
        signal_id=uuid4(),
        symbol=E2E_SYMBOL,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("3500"),  # ~$19-20 Notional bei HFT/USDT-Testnet-Preis, ueber der Exchange-Mindestnotional von $5
        trading_mode=TradingMode.PAPER,
    )

    result_a, result_b = await asyncio.gather(
        execution_engine.execute(order), execution_engine.execute(order)
    )

    # EXPECTED vs ACTUAL vs PASS/FAIL
    results = [result_a, result_b]
    filled = [r for r in results if r.status == OrderStatus.FILLED]
    duplicates = [r for r in results if r.raw_response.get("duplicate") is True]

    assert len(filled) == 1, results
    assert len(duplicates) == 1, results
    assert duplicates[0].status == OrderStatus.REJECTED, duplicates[0]
    assert duplicates[0].raw_response["rejection_reason"] == "Duplicate order submission blocked"


# ============================================================================
# Unknown-Order-State
# ============================================================================


@pytest.mark.asyncio
async def test_unknown_order_state_on_submit_failure(
    execution_engine: ExecutionEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    SETUP: echte, gueltige OrderRequest fuer E2E_SYMBOL. Ein echter
        Netzwerk-Timeout/-Abbruch laesst sich gegen ein stabiles
        Testnet nicht zuverlaessig erzwingen (siehe bereits bestehendes,
        identisches Vorgehen in
        tests/docker_crash_tests/test_crash_scenarios.py -
        "Injected exchange-timeout fault via monkeypatched place_order",
        dort dokumentiert warum ein echter Netzwerk-Timeout in PAPER
        Mode architektonisch nicht erzwingbar ist). Einzige, gezielt
        injizierte Abweichung vom echten Pfad in diesem gesamten
        Runner: der Exchange-Adapter selbst bleibt real (Verbindung,
        Preflight, Leverage-Setzung liefen bereits echt), nur der
        eigentliche place_order()-Call schlaegt fuer DIESEN einen aufruf
        gezielt fehl.
    ACTION: execution_engine.execute(order) gegen den derart
        praeparierten Adapter.
    """
    from sgr.core.types import ExchangeID as _ExchangeID

    adapter = execution_engine._pool.get(_ExchangeID.BINANCE, TradingMode.PAPER)

    async def _raise_connection_error(order: OrderRequest) -> None:
        raise ConnectionError("Simulated exchange connectivity failure (E2E fault injection)")

    monkeypatch.setattr(adapter, "place_order", _raise_connection_error)

    order = OrderRequest(
        signal_id=uuid4(),
        symbol=E2E_SYMBOL,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("3500"),  # ~$19-20 Notional bei HFT/USDT-Testnet-Preis, ueber der Exchange-Mindestnotional von $5
        trading_mode=TradingMode.PAPER,
    )

    result = await execution_engine.execute(order)

    # EXPECTED vs ACTUAL vs PASS/FAIL
    assert result.status == OrderStatus.REJECTED, result
    assert result.raw_response.get("unknown") is True, result.raw_response
    assert "action_required" in result.raw_response, result.raw_response
    assert "Simulated exchange connectivity failure" in result.raw_response.get("error", "")
