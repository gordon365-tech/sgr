"""
Tests für den TradingOrchestrator.

Dieser Orchestrator schließt die zentrale Integrationslücke des Systems:
vor seiner Einführung gab es keinen Codepfad, auf dem ein generiertes
Signal jemals RiskEngine.evaluate(), ExecutionEngine.execute() oder
PortfolioEngine.on_order_filled() erreichte.

Teststrategie:
    1. Kein Signal → Zyklus endet NO_SIGNAL, Risk/Execution werden nie
       aufgerufen (Beweis, dass nichts übersprungen aber auch nichts
       unnötig ausgeführt wird)
    2. Risk lehnt ab → Execution wird nie aufgerufen (kein Signal darf
       Risk Management umgehen)
    3. Happy Path → Signal -> Risk APPROVED -> Order FILLED -> Portfolio
       wird aktualisiert, in exakt dieser Reihenfolge
    4. Order wird submitted aber nicht gefüllt → Portfolio wird NICHT
       aktualisiert (kein Fill = keine Positionsänderung)
    5. Fail-Safe: Exception an beliebiger Stelle → FAILED, niemals eine
       unbehandelte Exception
    6. Event Bus nicht verbunden → Zyklus schlägt trotzdem nicht fehl
       (Bus-Fehler dürfen Trading-Ergebnisse nie beeinflussen)
    7. Event Bus verbunden → RiskApprovedEvent/RiskRejectedEvent/
       TradingCycleCompletedEvent werden mit korrekten Daten publiziert
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from sgr.core.types import (
    ExchangeID,
    MarketRegime,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    RiskAssessment,
    RiskDecision,
    RiskMetrics,
    Side,
    Signal,
    SignalDirection,
    Symbol,
    TradingCycleStatus,
    TradingMode,
)
from sgr.market_data.types import FeatureSet, IndicatorValues
from sgr.orchestrator.engine import TradingOrchestrator

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def btc_symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX)


@pytest.fixture
def sample_signal(btc_symbol: Symbol) -> Signal:
    return Signal(
        timestamp=datetime.now(tz=UTC),
        strategy_name="trend_v1",
        symbol=btc_symbol,
        direction=SignalDirection.LONG,
        confidence=0.80,
        regime=MarketRegime.TRENDING_UP,
    )


@pytest.fixture
def sample_features(btc_symbol: Symbol) -> FeatureSet:
    return FeatureSet(
        symbol=btc_symbol,
        timestamp=datetime.now(tz=UTC),
        timeframe="1h",
        close=Decimal("50000"),
        volume=Decimal("100"),
        indicators=IndicatorValues(atr_14=Decimal("500")),
    )


def _risk_metrics() -> RiskMetrics:
    return RiskMetrics(
        timestamp=datetime.now(tz=UTC),
        portfolio_value=Decimal("10000"),
        daily_pnl=Decimal("0"),
        daily_pnl_pct=0.0,
        drawdown_from_peak=0.0,
        var_95=0.01,
        expected_shortfall=0.0,
        portfolio_heat=0.1,
        active_positions=0,
        correlation_exposure=0.0,
    )


def _approved_assessment(signal: Signal, qty: Decimal = Decimal("0.1")) -> RiskAssessment:
    return RiskAssessment(
        signal_id=signal.id,
        decision=RiskDecision.APPROVED,
        approved_quantity=qty,
        risk_metrics_snapshot=_risk_metrics(),
    )


def _rejected_assessment(signal: Signal) -> RiskAssessment:
    return RiskAssessment(
        signal_id=signal.id,
        decision=RiskDecision.REJECTED,
        approved_quantity=Decimal("0"),
        rejection_reason="Kill switch active",
        risk_metrics_snapshot=_risk_metrics(),
    )


def _order_request(signal: Signal, symbol: Symbol) -> OrderRequest:
    return OrderRequest(
        signal_id=signal.id,
        symbol=symbol,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.1"),
        trading_mode=TradingMode.PAPER,
    )


def _filled_result(signal: Signal, symbol: Symbol) -> OrderResult:
    return OrderResult(
        request_id=signal.id,
        exchange_order_id="EX-1",
        symbol=symbol,
        status=OrderStatus.FILLED,
        filled_quantity=Decimal("0.1"),
        average_fill_price=Decimal("50000"),
        submitted_at=datetime.now(tz=UTC),
        trading_mode=TradingMode.PAPER,
    )


class _Engines:
    """Bündelt Mock-Engines für den Orchestrator, mit sinnvollen Defaults."""

    def __init__(self) -> None:
        self.strategy_engine = AsyncMock()
        self.risk_engine = AsyncMock()
        # build_order_request ist synchron im echten RiskEngine (kein I/O) -
        # AsyncMock würde hier einen ungeawaiteten Coroutine-Wert liefern.
        self.risk_engine.build_order_request = MagicMock()
        self.execution_engine = AsyncMock()
        self.portfolio_engine = AsyncMock()
        self.portfolio_engine.positions = []
        self.portfolio_engine.portfolio_value = Decimal("10000")
        self.portfolio_engine.cash = Decimal("10000")
        self.feature_store = AsyncMock()

    def orchestrator(self) -> TradingOrchestrator:
        return TradingOrchestrator(
            strategy_engine=self.strategy_engine,
            risk_engine=self.risk_engine,
            execution_engine=self.execution_engine,
            portfolio_engine=self.portfolio_engine,
            feature_store=self.feature_store,
            trading_mode=TradingMode.PAPER,
        )


# ---------------------------------------------------------------------------
# 1. Kein Signal
# ---------------------------------------------------------------------------


async def test_no_signal_short_circuits_before_risk() -> None:
    e = _Engines()
    e.strategy_engine.process.return_value = None
    orchestrator = e.orchestrator()

    result = await orchestrator.run_cycle("pionex:BTC/USDT", "1h")

    assert result.status == TradingCycleStatus.NO_SIGNAL
    e.risk_engine.evaluate.assert_not_called()
    e.execution_engine.execute.assert_not_called()
    e.portfolio_engine.on_order_filled.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Risk lehnt ab
# ---------------------------------------------------------------------------


async def test_risk_rejection_never_reaches_execution(
    sample_signal: Signal, sample_features: FeatureSet
) -> None:
    e = _Engines()
    e.strategy_engine.process.return_value = sample_signal
    e.feature_store.get_latest.return_value = sample_features
    e.risk_engine.evaluate.return_value = _rejected_assessment(sample_signal)
    orchestrator = e.orchestrator()

    result = await orchestrator.run_cycle("pionex:BTC/USDT", "1h")

    assert result.status == TradingCycleStatus.RISK_REJECTED
    assert result.assessment is not None
    assert result.assessment.decision == RiskDecision.REJECTED
    e.execution_engine.execute.assert_not_called()
    e.portfolio_engine.on_order_filled.assert_not_called()
    # build_order_request darf bei Ablehnung nie aufgerufen werden
    e.risk_engine.build_order_request.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Happy Path: Signal -> Risk -> Order -> Portfolio
# ---------------------------------------------------------------------------


async def test_happy_path_updates_portfolio_on_fill(
    sample_signal: Signal, sample_features: FeatureSet, btc_symbol: Symbol
) -> None:
    e = _Engines()
    e.strategy_engine.process.return_value = sample_signal
    e.feature_store.get_latest.return_value = sample_features
    assessment = _approved_assessment(sample_signal)
    e.risk_engine.evaluate.return_value = assessment
    order_request = _order_request(sample_signal, btc_symbol)
    e.risk_engine.build_order_request.return_value = order_request
    order_result = _filled_result(sample_signal, btc_symbol)
    e.execution_engine.execute.return_value = order_result
    orchestrator = e.orchestrator()

    result = await orchestrator.run_cycle("pionex:BTC/USDT", "1h")

    assert result.status == TradingCycleStatus.ORDER_FILLED
    assert result.order_result is not None
    assert result.order_result.status == OrderStatus.FILLED

    # Reihenfolge: current_price wird an build_order_request weitergereicht
    e.risk_engine.build_order_request.assert_called_once_with(
        sample_signal, assessment, current_price=sample_features.close
    )
    e.execution_engine.execute.assert_called_once_with(order_request)
    e.portfolio_engine.on_order_filled.assert_called_once_with(order_result)


# ---------------------------------------------------------------------------
# 4. Submitted, aber nicht gefüllt -> kein Portfolio-Update
# ---------------------------------------------------------------------------


async def test_unfilled_order_does_not_update_portfolio(
    sample_signal: Signal, sample_features: FeatureSet, btc_symbol: Symbol
) -> None:
    e = _Engines()
    e.strategy_engine.process.return_value = sample_signal
    e.feature_store.get_latest.return_value = sample_features
    e.risk_engine.evaluate.return_value = _approved_assessment(sample_signal)
    e.risk_engine.build_order_request.return_value = _order_request(sample_signal, btc_symbol)

    pending_result = OrderResult(
        request_id=sample_signal.id,
        exchange_order_id="EX-2",
        symbol=btc_symbol,
        status=OrderStatus.CANCELLED,
        filled_quantity=Decimal("0"),
        submitted_at=datetime.now(tz=UTC),
        trading_mode=TradingMode.PAPER,
    )
    e.execution_engine.execute.return_value = pending_result
    orchestrator = e.orchestrator()

    result = await orchestrator.run_cycle("pionex:BTC/USDT", "1h")

    assert result.status == TradingCycleStatus.ORDER_NOT_FILLED
    e.portfolio_engine.on_order_filled.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Fail-Safe bei unerwarteten Exceptions
# ---------------------------------------------------------------------------


async def test_unexpected_exception_yields_failed_status_not_raised(
    sample_signal: Signal,
) -> None:
    e = _Engines()
    e.strategy_engine.process.side_effect = RuntimeError("boom")
    orchestrator = e.orchestrator()

    result = await orchestrator.run_cycle("pionex:BTC/USDT", "1h")

    assert result.status == TradingCycleStatus.FAILED
    assert result.error is not None
    assert "boom" in result.error


async def test_missing_features_after_signal_is_failed_not_market_order(
    sample_signal: Signal,
) -> None:
    """
    Race Condition: Signal wurde generiert, aber Features sind beim
    Preis-Lookup schon weg. Darf NIE einen Fallback-Preis annehmen -
    lieber der Zyklus scheitert, als mit falschem Preis zu handeln.
    """
    e = _Engines()
    e.strategy_engine.process.return_value = sample_signal
    e.feature_store.get_latest.return_value = None
    orchestrator = e.orchestrator()

    result = await orchestrator.run_cycle("pionex:BTC/USDT", "1h")

    assert result.status == TradingCycleStatus.FAILED
    e.risk_engine.evaluate.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Event Bus nicht verbunden -> darf Trading-Ergebnis nicht beeinflussen
# ---------------------------------------------------------------------------


async def test_cycle_succeeds_even_when_event_bus_unconnected(
    sample_signal: Signal, sample_features: FeatureSet, btc_symbol: Symbol
) -> None:
    """
    Verwendet den echten (nicht gemockten) get_event_bus() Singleton ohne
    .connect() - publish() wirft RuntimeError. Der Zyklus muss trotzdem
    sein reguläres Ergebnis liefern (Fail-Safe-Grundsatz).
    """
    e = _Engines()
    e.strategy_engine.process.return_value = sample_signal
    e.feature_store.get_latest.return_value = sample_features
    assessment = _approved_assessment(sample_signal)
    e.risk_engine.evaluate.return_value = assessment
    e.risk_engine.build_order_request.return_value = _order_request(sample_signal, btc_symbol)
    e.execution_engine.execute.return_value = _filled_result(sample_signal, btc_symbol)
    orchestrator = e.orchestrator()

    result = await orchestrator.run_cycle("pionex:BTC/USDT", "1h")

    assert result.status == TradingCycleStatus.ORDER_FILLED
    e.portfolio_engine.on_order_filled.assert_called_once()


# ---------------------------------------------------------------------------
# 7. Events werden korrekt publiziert, wenn Bus verfügbar ist
# ---------------------------------------------------------------------------


async def test_risk_approved_event_published_with_correct_payload(
    sample_signal: Signal, sample_features: FeatureSet, btc_symbol: Symbol, mocker
) -> None:
    e = _Engines()
    e.strategy_engine.process.return_value = sample_signal
    e.feature_store.get_latest.return_value = sample_features
    assessment = _approved_assessment(sample_signal)
    e.risk_engine.evaluate.return_value = assessment
    order_request = _order_request(sample_signal, btc_symbol)
    e.risk_engine.build_order_request.return_value = order_request
    e.execution_engine.execute.return_value = _filled_result(sample_signal, btc_symbol)

    mock_bus = AsyncMock()
    mocker.patch("sgr.orchestrator.engine.get_event_bus", return_value=mock_bus)
    orchestrator = e.orchestrator()

    await orchestrator.run_cycle("pionex:BTC/USDT", "1h")

    published_types = [type(call.args[0]).__name__ for call in mock_bus.publish.call_args_list]
    assert "RiskApprovedEvent" in published_types
    assert "TradingCycleCompletedEvent" in published_types

    risk_event = next(
        call.args[0]
        for call in mock_bus.publish.call_args_list
        if type(call.args[0]).__name__ == "RiskApprovedEvent"
    )
    assert risk_event.assessment.signal_id == sample_signal.id
    assert risk_event.order_request == order_request


async def test_risk_rejected_event_published(
    sample_signal: Signal, sample_features: FeatureSet, mocker
) -> None:
    e = _Engines()
    e.strategy_engine.process.return_value = sample_signal
    e.feature_store.get_latest.return_value = sample_features
    e.risk_engine.evaluate.return_value = _rejected_assessment(sample_signal)

    mock_bus = AsyncMock()
    mocker.patch("sgr.orchestrator.engine.get_event_bus", return_value=mock_bus)
    orchestrator = e.orchestrator()

    await orchestrator.run_cycle("pionex:BTC/USDT", "1h")

    published_types = [type(call.args[0]).__name__ for call in mock_bus.publish.call_args_list]
    assert "RiskRejectedEvent" in published_types
    assert "TradingCycleCompletedEvent" in published_types


# ---------------------------------------------------------------------------
# on_candle_event: additiver Event-Trigger
# ---------------------------------------------------------------------------


async def test_on_candle_event_triggers_cycle_for_matching_symbol(
    btc_symbol: Symbol, sample_candle_event_factory
) -> None:
    e = _Engines()
    e.strategy_engine.process.return_value = None  # NO_SIGNAL reicht als Beweis
    orchestrator = e.orchestrator()

    event = sample_candle_event_factory(btc_symbol, "1h")
    await orchestrator.on_candle_event(event)

    e.strategy_engine.process.assert_called_once_with(
        "pionex:BTC/USDT", "1h", MarketRegime.UNKNOWN
    )


async def test_on_candle_event_never_raises_on_malformed_event() -> None:
    e = _Engines()
    orchestrator = e.orchestrator()

    class _Broken:
        pass

    # Darf keine Exception nach außen werfen - würde sonst den
    # Market-Data-Loop im Aufrufer zum Absturz bringen.
    await orchestrator.on_candle_event(_Broken())
    e.strategy_engine.process.assert_not_called()


@pytest.fixture
def sample_candle_event_factory():
    from sgr.core.types import Candle, CandleEvent

    def _make(symbol: Symbol, timeframe: str):
        candle = Candle(
            symbol=symbol,
            timestamp=datetime.now(tz=UTC),
            timeframe=timeframe,
            open=Decimal("50000"),
            high=Decimal("50100"),
            low=Decimal("49900"),
            close=Decimal("50050"),
            volume=Decimal("10"),
        )
        return CandleEvent(timestamp=datetime.now(tz=UTC), candle=candle)

    return _make


# ---------------------------------------------------------------------------
# Symbol Kill Switch: allererster Check, vor Signal-Generierung
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_symbol_kill_switch():
    """Isoliert den modul-globalen SymbolKillSwitch-Singleton zwischen Tests."""
    from sgr.risk.symbol_kill_switch import SymbolKillSwitch

    SymbolKillSwitch._instance = None
    yield
    SymbolKillSwitch._instance = None


async def test_disabled_symbol_skips_signal_generation_entirely() -> None:
    from sgr.risk.symbol_kill_switch import get_symbol_kill_switch

    await get_symbol_kill_switch().deactivate("pionex:BTC/USDT", "anomalous behavior")

    e = _Engines()
    orchestrator = e.orchestrator()

    result = orchestrator.run_cycle("pionex:BTC/USDT", "1h")
    result = await result

    assert result.status == TradingCycleStatus.SYMBOL_DISABLED
    e.strategy_engine.process.assert_not_called()
    e.risk_engine.evaluate.assert_not_called()
    e.execution_engine.execute.assert_not_called()


async def test_disabled_symbol_does_not_affect_other_symbols() -> None:
    from sgr.risk.symbol_kill_switch import get_symbol_kill_switch

    await get_symbol_kill_switch().deactivate("pionex:BTC/USDT", "reason")

    e = _Engines()
    e.strategy_engine.process.return_value = None  # NO_SIGNAL is sufficient proof.
    orchestrator = e.orchestrator()

    result = await orchestrator.run_cycle("pionex:ETH/USDT", "1h")

    assert result.status == TradingCycleStatus.NO_SIGNAL
    e.strategy_engine.process.assert_called_once()


async def test_reactivated_symbol_runs_cycle_normally() -> None:
    from sgr.risk.symbol_kill_switch import get_symbol_kill_switch

    sks = get_symbol_kill_switch()
    await sks.deactivate("pionex:BTC/USDT", "reason")
    await sks.activate("pionex:BTC/USDT")

    e = _Engines()
    e.strategy_engine.process.return_value = None
    orchestrator = e.orchestrator()

    result = await orchestrator.run_cycle("pionex:BTC/USDT", "1h")

    assert result.status == TradingCycleStatus.NO_SIGNAL
    e.strategy_engine.process.assert_called_once()
