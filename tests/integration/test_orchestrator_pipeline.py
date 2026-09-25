"""
End-to-end Trading Pipeline Test
==================================
Testet den kompletten Pfad: CandleEvent → Signal → Risk → Execution → OrderFilled → Portfolio

Scenario: BTC/USDT Paper Trading
    1. Create initial portfolio state with $10,000 USDT
    2. Generate market features (Price, ATR, Indicators)
    3. StrategyEngine processes candle → generates Signal
    4. RiskEngine evaluates Signal → RiskAssessment (APPROVED/REJECTED)
    5. ExecutionEngine executes OrderRequest → OrderResult (FILLED/REJECTED)
    6. PortfolioEngine updates positions → Portfolio state change
    7. Verify entire state: positions, PnL, portfolio value

Test Coverage:
    ✅ Happy path: Signal → Risk Approved → Order Filled → Position Opened
    ✅ Risk rejection path: High risk → Risk Rejected → No order sent
    ✅ Kill switch path: Kill switch active → Order cancelled → No position
    ✅ Paper trading enforcement: PAPER mode is default, no live orders
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sgr.core.types import (
    Environment,
    ExchangeID,
    MarketRegime,
    OrderStatus,
    PositionSide,
    RiskDecision,
    Signal,
    SignalDirection,
    Symbol,
    TradingCycleStatus,
    TradingMode,
)
from sgr.exchanges.factory import ExchangePool
from sgr.execution.engine import ExecutionEngine
from sgr.market_data.feature_store import FeatureStore
from sgr.market_data.types import FeatureSet
from sgr.orchestrator.engine import TradingOrchestrator
from sgr.portfolio.engine import PortfolioEngine
from sgr.risk.engine import RiskEngine
from sgr.strategy.base import BaseStrategy, ValidationStatus
from sgr.strategy.engine import StrategyEngine
from sgr.strategy.registry import StrategyRegistry

# Benoetigt eine echte Redis-Instanz (echter EventBus, kein Mock) -
# in Sandbox/CI ohne laufende Redis-Instanz nicht ausfuehrbar.
# Siehe pyproject.toml: standardmaessig via -m ausgeschlossen.
pytestmark = pytest.mark.requires_redis

# ============================================================================
# Test Fixtures
# ============================================================================


class MockStrategy(BaseStrategy):
    """
    Einfache Test-Strategie die konsistent LONGs generiert - oder gar
    kein Signal, wenn should_signal=False (siehe test_no_signal_path:
    frueher testete dieser Fall "leere Features im Feature Store", was
    aber gar nicht der tatsaechliche Grund fuer NO_SIGNAL im echten
    Orchestrator-Pfad ist - orchestrator.run_cycle() baut den
    MarketContext auch ohne gespeichertes FeatureSet und ruft die
    Strategie trotzdem auf; ob ein Signal entsteht, entscheidet
    ausschliesslich generate_signal() selbst, wie im echten Protocol.
    Das explizite should_signal-Flag testet diesen tatsaechlichen
    Entscheidungspunkt direkt, statt sich auf einen Nebeneffekt zu
    verlassen).
    """

    name = "test_strategy"
    version = "1.0.0"
    supported_regimes = [MarketRegime.TRENDING_UP, MarketRegime.UNKNOWN]

    def __init__(self, should_signal: bool = True) -> None:
        self.should_signal = should_signal

    def generate_signal(self, context: Any) -> Signal | None:
        if not self.should_signal:
            return None
        symbol = context.symbol
        return Signal(
            symbol=symbol,
            strategy_name=self.name,
            direction=SignalDirection.LONG,
            confidence=Decimal("0.85"),
            timestamp=datetime.now(tz=UTC),
            regime=context.regime,
            metadata={},
        )


@pytest.fixture
def paper_mode_config(tmp_path: Any) -> Any:
    """Konfiguration für Paper Trading."""
    from sgr.core.config import SGRConfig

    return SGRConfig(
        trading_mode=TradingMode.PAPER,
        environment=Environment.DEVELOPMENT,
        version="0.1.0-test",
    )


@pytest.fixture
async def exchange_pool(paper_mode_config: Any) -> Any:
    """
    Exchange Pool für PAPER mode - mit MockExchangeAdapter statt einer
    echten ccxt-Verbindung.

    Vorher: pool.initialize([ExchangeID.PIONEX], TradingMode.PAPER) - das
    baute einen echten PionexAdapter und rief dessen connect() auf. Das
    ist strukturell kaputt (installierte ccxt-Version 4.5.78 kennt keine
    Exchange-ID "pionex" mehr, getattr(ccxt, "pionex") wirft
    AttributeError), UND selbst wenn Pionex funktionieren wuerde, waere
    ein Test, der echte Netzwerk-Calls gegen eine externe Exchange macht,
    langsam, flaky und rate-limit-gefaehrdet (siehe Go-Live-Report:
    selbstverursachter Binance-Testnet-IP-Ban waehrend der Multi-Asset-
    Verifikation) - fuer einen Orchestrierungs-Test (Signal -> Risk ->
    Order -> Portfolio), der die Exchange-Anbindung selbst nicht prueft,
    voellig unnoetig. MockExchangeAdapter (tests/mocks/mock_exchange.py)
    ist die im Rest der Suite etablierte Loesung dafuer (siehe
    tests/unit/test_exchange_layer.py, tests/exchanges/
    test_factory_tenant_credentials.py: pool._adapters[...] = mock).
    """
    from tests.mocks.mock_exchange import MockExchangeAdapter

    pool = ExchangePool()
    mock_adapter = MockExchangeAdapter(trading_mode=TradingMode.PAPER)
    # Fill-Preis fuer place_order() - an btc_features.close angeglichen,
    # damit der Happy-Path-Test einen deterministischen, nachvollziehbaren
    # Fuellpreis pruefen kann statt eines zufaelligen Mock-Defaults.
    mock_adapter.ticker_price = Decimal("50500")
    await mock_adapter.connect()
    pool._adapters[(ExchangeID.BINANCE, TradingMode.PAPER)] = mock_adapter
    return pool


@pytest.fixture(autouse=True)
async def connected_event_bus() -> Any:
    """
    Verbindet den globalen EventBus-Singleton (sgr.core.event_bus.
    get_event_bus()) gegen die echte Redis-Instanz, bevor jeder Test
    laeuft - Orchestrator/KillSwitch publizieren Events darueber
    (publish_cycle_completed, kill_switch Events) und werfen sonst
    "EventBus not connected. Call connect() first." Ohne diese Fixture
    lief die Orchestrierung trotzdem strukturell durch (Fehler werden
    fail-safe nur geloggt, siehe orchestrator.publish_cycle_completed_failed),
    aber das verschleiert genau die Observability-Luecke, die dieser
    Test eigentlich absichern soll.
    """
    import sgr.core.event_bus as event_bus_module

    # get_event_bus() ist ein Modul-weites Singleton (sgr.core.event_bus._bus),
    # das NICHT pro Test zurueckgesetzt wird. pytest-asyncio vergibt pro
    # Testfunktion einen frischen Event Loop - ein Singleton, das in Test N
    # per bus.subscribe() einen Consumer-Task auf Test-N's Loop erzeugt hat,
    # lässt diesen Task in Test N+1 als Altlast zurueck; dessen
    # bus.close()-Aufruf versucht dann per asyncio.gather() auf einen Task
    # zu warten, dessen Loop laengst geschlossen ist ("RuntimeError: Event
    # loop is closed"). Fix: Singleton-Referenz vor jedem Test explizit
    # zuruecksetzen, damit jeder Test eine eigene EventBus-Instanz (und
    # damit eigene Consumer-Tasks, gebunden an den eigenen Loop) bekommt.
    event_bus_module._bus = None
    bus = event_bus_module.get_event_bus()
    await bus.connect()
    yield bus
    await bus.close()
    event_bus_module._bus = None


@pytest.fixture
async def feature_store() -> Any:
    """In-memory Feature Store für Tests."""
    store = FeatureStore()
    await store.connect()
    return store


@pytest.fixture
def btc_symbol() -> Symbol:
    """BTC/USDT Symbol."""
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


@pytest.fixture
def btc_features(btc_symbol: Symbol) -> FeatureSet:
    """
    Generiert realistische BTC/USDT Features.

    FeatureSet hat kein verschachteltes ohlcv-Feld (mehr) - close/volume
    liegen direkt auf FeatureSet, "indicators" ist ein IndicatorValues-
    Model, kein rohes dict (siehe sgr/market_data/types.py). Die vorherige
    Version dieser Fixture (OHLCV(timestamp=..., open=..., ...) und ein
    dict fuer indicators) passte zu einer frueheren Version dieser Typen -
    OHLCV (sgr/market_data/feature_engineering.py) ist inzwischen ein
    numpy-Array-Batch-Typ fuer die Indikator-Berechnung selbst, kein
    Einzel-Bar-Werttraeger mehr.
    """
    from sgr.market_data.types import IndicatorValues

    now = datetime.now(tz=UTC)

    return FeatureSet(
        symbol=btc_symbol,
        timeframe="1h",
        timestamp=now,
        close=Decimal("50500"),
        volume=Decimal("100"),
        indicators=IndicatorValues(
            sma_20=Decimal("49900"),
            rsi_14=65.0,
            atr_14=Decimal("500"),
            bb_upper=Decimal("52000"),
            bb_lower=Decimal("48000"),
        ),
    )


@pytest.fixture
async def portfolio_engine(paper_mode_config: Any) -> PortfolioEngine:
    """Initialisiert Portfolio Engine."""
    engine = PortfolioEngine(
        trading_mode=TradingMode.PAPER,
        initial_cash=Decimal("10000"),  # $10,000 USDT
    )
    return engine


@pytest.fixture
async def risk_engine(paper_mode_config: Any) -> RiskEngine:
    """Initialisiert Risk Engine."""
    engine = RiskEngine(TradingMode.PAPER)
    await engine.initialize()
    return engine


@pytest.fixture
async def execution_engine(exchange_pool: ExchangePool) -> ExecutionEngine:
    """Initialisiert Execution Engine."""
    return ExecutionEngine(exchange_pool, TradingMode.PAPER)


@pytest.fixture
async def strategy_engine(paper_mode_config: Any, feature_store: FeatureStore) -> StrategyEngine:
    """Initialisiert Strategy Engine mit Test-Strategie."""
    registry = StrategyRegistry.get()
    registry.clear()

    # Registriere und validiere Test-Strategie
    test_strat = MockStrategy()
    registry.register_instance(test_strat)
    registry.mark_validated(
        test_strat.name,
        ValidationStatus(backtest_passed=True, walk_forward_passed=True, paper_trading_passed=True),
    )
    # mark_validated() setzt nur is_validated (in-memory) - StrategyEngine
    # filtert auf is_active (siehe registry.get_active()), das erst durch
    # einen expliziten activate()-Aufruf gesetzt wird. Production macht das
    # in sgr/api/main.py lifespan() nach der Validierung
    # (`for entry in registry.get_all().values(): if entry.is_validated:
    # await registry.activate(...)`); hier direkt nachgebildet, sonst
    # bleibt active_strategies=0 und run_cycle() liefert ausnahmslos
    # NO_SIGNAL, unabhaengig von den Features.
    await registry.activate(test_strat.name)

    engine = StrategyEngine(TradingMode.PAPER, feature_store, registry)
    await engine.start()
    return engine


@pytest.fixture
async def orchestrator(
    strategy_engine: StrategyEngine,
    risk_engine: RiskEngine,
    execution_engine: ExecutionEngine,
    portfolio_engine: PortfolioEngine,
    feature_store: FeatureStore,
) -> TradingOrchestrator:
    """Initialisiert Complete Trading Orchestrator."""
    return TradingOrchestrator(
        strategy_engine=strategy_engine,
        risk_engine=risk_engine,
        execution_engine=execution_engine,
        portfolio_engine=portfolio_engine,
        feature_store=feature_store,
        trading_mode=TradingMode.PAPER,
    )


# ============================================================================
# Happy Path: Signal → Risk Approved → Order Filled → Position Opened
# ============================================================================


@pytest.mark.asyncio
async def test_happy_path_complete_trading_cycle(
    orchestrator: TradingOrchestrator,
    feature_store: FeatureStore,
    btc_symbol: Symbol,
    btc_features: FeatureSet,
    portfolio_engine: PortfolioEngine,
) -> None:
    """Testet den kompletten Happy-Path Zyklus."""
    # Setup: Store Features in Feature Store
    await feature_store.save(btc_features)

    # Initial State
    assert portfolio_engine.portfolio_value == Decimal("10000")
    assert len(portfolio_engine.positions) == 0
    assert portfolio_engine.cash == Decimal("10000")

    # Run Trading Cycle: CandleEvent → ... → Portfolio Update
    result = await orchestrator.run_cycle(
        symbol_key=f"{ExchangeID.BINANCE.value}:{btc_symbol.ccxt_symbol}",
        timeframe="1h",
        regime=MarketRegime.UNKNOWN,
    )

    # Verify Cycle Result
    assert result is not None
    assert result.status == TradingCycleStatus.ORDER_FILLED
    assert result.signal is not None
    assert result.signal.direction == SignalDirection.LONG
    assert result.assessment is not None
    # APPROVED (voll) oder REDUCED (kleinere Position, siehe
    # position_sizer.computed "Portfolio heat limit" - mit den
    # Default-Risk-Limits UND einer frischen $10k-Position reduziert der
    # PositionSizer bereits das allererste Signal leicht) sind beides
    # gueltige "Order wurde ausgefuehrt"-Ergebnisse - nur REJECTED waere
    # ein Fehler in diesem Happy-Path-Test.
    assert result.assessment.decision in (RiskDecision.APPROVED, RiskDecision.REDUCED)
    assert result.order_result is not None
    assert result.order_result.status == OrderStatus.FILLED

    # Verify Position was Opened
    assert len(portfolio_engine.positions) == 1
    position = portfolio_engine.positions[0]
    assert position.symbol == btc_symbol
    assert position.side == PositionSide.LONG
    assert position.quantity > 0
    assert position.entry_price == btc_features.close
    assert position.strategy_name == "test_strategy"

    # Verify Portfolio State Changed
    # Cash reduced by: (quantity * entry_price + fees)
    # PnL should be 0 (position just opened at entry price)
    assert portfolio_engine.cash < Decimal("10000")  # Money tied up in position
    assert portfolio_engine.portfolio_value <= Decimal("10000")  # Account for fees


@pytest.mark.asyncio
async def test_no_signal_path(
    orchestrator: TradingOrchestrator,
    portfolio_engine: PortfolioEngine,
    feature_store: FeatureStore,
    btc_symbol: Symbol,
) -> None:
    """Testet Szenario wo StrategyEngine kein Signal generiert (weil die
    Strategie selbst keins liefert - siehe MockStrategy.should_signal
    Docstring: das ist der tatsaechliche Entscheidungspunkt fuer
    NO_SIGNAL im echten Orchestrator, nicht "keine Features")."""
    registry = StrategyRegistry.get()
    registry.get_entry("test_strategy").strategy.should_signal = False  # type: ignore[union-attr]

    result = await orchestrator.run_cycle(
        symbol_key=f"{ExchangeID.BINANCE.value}:{btc_symbol.ccxt_symbol}",
        timeframe="1h",
    )

    # Verify: No cycle
    assert result.status == TradingCycleStatus.NO_SIGNAL
    assert result.signal is None
    assert len(portfolio_engine.positions) == 0


# ============================================================================
# Risk Rejection Path
# ============================================================================


@pytest.mark.asyncio
async def test_risk_rejection_prevents_execution(
    orchestrator: TradingOrchestrator,
    feature_store: FeatureStore,
    risk_engine: RiskEngine,
    btc_symbol: Symbol,
    btc_features: FeatureSet,
    portfolio_engine: PortfolioEngine,
) -> None:
    """
    Testet Risk-Rejection ueber das "max_open_positions"-Hard-Limit.

    Vorher versuchte dieser Test, ueber risk_engine._peak_portfolio_value/
    _daily_pnl_start einen Drawdown-Hard-Reject zu simulieren - das
    triggert im aktuellen RiskEngine aber den KILL SWITCH (siehe
    RiskEngine-Modul-Docstring: "max_portfolio_drawdown > 15% -> KILL"),
    nicht bloss eine einzelne REJECTED-Assessment, UND mit den gewaehlten
    Werten (peak 10500 vs. tatsaechlichem portfolio_value ~10000) wird gar
    keine der beiden Schwellen ueberschritten - die Assertion akzeptierte
    am Ende (RISK_REJECTED, NO_SIGNAL) *und* liess durch einen Bug
    ORDER_FILLED durchrutschen, ohne dass ueberhaupt etwas wirklich
    getestet wurde. max_open_positions ist ein Hard Limit, das
    unabhaengig vom Kill Switch direkt zu RiskDecision.REJECTED fuehrt
    (siehe sgr/risk/engine.py _check_threshold "max_positions") - klarer
    und deterministisch reproduzierbar.
    """
    from sgr.core.types import Position, PositionSide, TradingMode as TM

    await feature_store.save(btc_features)

    # Portfolio künstlich über das Hard Limit (Default 10) befüllen -
    # unterschiedliche Symbole, damit PortfolioEngine sie als 11
    # getrennte offene Positionen zählt.
    now = datetime.now(tz=UTC)
    for i in range(11):
        sym = Symbol(base=f"ALT{i}", quote="USDT", exchange=ExchangeID.BINANCE)
        pos = Position(
            symbol=sym,
            side=PositionSide.LONG,
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            current_price=Decimal("100"),
            opened_at=now,
            strategy_name="test_strategy",
            trading_mode=TM.PAPER,
        )
        portfolio_engine._state._positions[str(sym)] = pos

    result = await orchestrator.run_cycle(
        symbol_key=f"{ExchangeID.BINANCE.value}:{btc_symbol.ccxt_symbol}",
        timeframe="1h",
    )

    assert result.status == TradingCycleStatus.RISK_REJECTED
    assert result.signal is not None  # Strategie hat ein Signal erzeugt
    assert result.assessment is not None
    assert result.assessment.decision == RiskDecision.REJECTED
    assert "position" in (result.assessment.rejection_reason or "").lower()
    # Keine zusaetzliche (12.) Position darf entstanden sein
    assert len(portfolio_engine.positions) == 11


# ============================================================================
# Kill Switch Path
# ============================================================================


@pytest.mark.asyncio
async def test_kill_switch_prevents_execution(
    orchestrator: TradingOrchestrator,
    feature_store: FeatureStore,
    risk_engine: RiskEngine,
    btc_symbol: Symbol,
    btc_features: FeatureSet,
    portfolio_engine: PortfolioEngine,
) -> None:
    """Testet Kill Switch: verhindert Order-Submitierung."""
    # Setup: Store Features
    await feature_store.save(btc_features)

    # Activate Kill Switch
    from sgr.risk.kill_switch import get_kill_switch

    kill_switch = get_kill_switch(TradingMode.PAPER)
    await kill_switch.trigger("test_reason", triggered_by="test")

    try:
        # Run Trading Cycle
        result = await orchestrator.run_cycle(
            symbol_key=f"{ExchangeID.BINANCE.value}:{btc_symbol.ccxt_symbol}",
            timeframe="1h",
        )

        # Verify: Kill Switch prevented execution
        assert result.status == TradingCycleStatus.RISK_REJECTED
        assert result.assessment is not None
        assert result.assessment.decision == RiskDecision.REJECTED
        assert "Kill switch" in (result.assessment.rejection_reason or "")
    finally:
        # get_kill_switch() ist ein Prozess-weites Singleton (siehe
        # sgr/risk/kill_switch.py _kill_switches) - ohne try/finally
        # wuerde ein fehlgeschlagener assert oben den Reset ueberspringen
        # und den aktiven Kill Switch in JEDEN nachfolgenden Test in
        # diesem Prozess durchsickern lassen (beobachtet: fuehrte zu
        # ERROR statt FAILED in zwei nachgelagerten Tests).
        await kill_switch.reset(reset_by="test_cleanup")


# ============================================================================
# Paper Trading Enforcement
# ============================================================================


@pytest.mark.asyncio
async def test_paper_trading_is_default_mode(paper_mode_config: Any) -> None:
    """Testet dass PAPER Trading Default ist."""
    from sgr.core.config import get_config

    config = get_config()
    # Note: get_config() returns actual config based on env.
    # PAPER must be the config default (fail-safe against accidental live trading).
    assert TradingMode.PAPER in list(TradingMode)
    assert config.trading_mode == TradingMode.PAPER


@pytest.mark.asyncio
async def test_live_trading_impossible_in_default_config() -> None:
    """Testet dass Live-Credentials im Default Config leer sind."""
    from sgr.core.config import get_config

    config = get_config()
    # Default: no Pionex live API key
    live_key = config.credentials.pionex_live_api_key
    assert not live_key or live_key == ""


# ============================================================================
# Risk Event Publishing
# ============================================================================


@pytest.mark.asyncio
async def test_risk_approved_event_published(
    orchestrator: TradingOrchestrator,
    feature_store: FeatureStore,
    btc_symbol: Symbol,
    btc_features: FeatureSet,
) -> None:
    """Testet dass RiskApprovedEvent auf Event Bus publiziert wird."""
    from sgr.core.event_bus import get_event_bus
    from sgr.core.types import RiskApprovedEvent

    events_received: list[RiskApprovedEvent] = []

    def capture_event(event: Any) -> None:
        if isinstance(event, RiskApprovedEvent):
            events_received.append(event)

    # Subscribe to RiskApprovedEvent
    bus = get_event_bus()
    bus.subscribe(
        RiskApprovedEvent,
        capture_event,
        consumer_group="test",
        consumer_name="test-capture",
    )

    # Setup and Run Cycle
    await feature_store.save(btc_features)

    result = await orchestrator.run_cycle(
        symbol_key=f"{ExchangeID.BINANCE.value}:{btc_symbol.ccxt_symbol}",
        timeframe="1h",
    )

    # Verify: RiskApprovedEvent was published (if order was filled)
    if result.status == TradingCycleStatus.ORDER_FILLED:
        assert len(events_received) > 0


@pytest.mark.asyncio
async def test_portfolio_update_on_order_filled(
    orchestrator: TradingOrchestrator,
    feature_store: FeatureStore,
    btc_symbol: Symbol,
    btc_features: FeatureSet,
    portfolio_engine: PortfolioEngine,
) -> None:
    """Testet dass PortfolioEngine.on_order_filled aufgerufen wird."""
    # Setup
    await feature_store.save(btc_features)

    initial_cash = portfolio_engine.cash

    # Run Cycle
    result = await orchestrator.run_cycle(
        symbol_key=f"{ExchangeID.BINANCE.value}:{btc_symbol.ccxt_symbol}",
        timeframe="1h",
    )

    # If order filled, portfolio should be updated
    if result.status == TradingCycleStatus.ORDER_FILLED:
        assert portfolio_engine.cash < initial_cash
        assert len(portfolio_engine.positions) > 0


# ============================================================================
# Position Sizing
# ============================================================================


@pytest.mark.asyncio
async def test_risk_engine_reduces_position_on_soft_limits(
    orchestrator: TradingOrchestrator,
    feature_store: FeatureStore,
    btc_symbol: Symbol,
    btc_features: FeatureSet,
    portfolio_engine: PortfolioEngine,
) -> None:
    """Testet dass Risk Engine Positionsgröße bei Soft Limits reduziert."""
    # Setup: Initial features
    await feature_store.save(btc_features)

    # Run Cycle
    result = await orchestrator.run_cycle(
        symbol_key=f"{ExchangeID.BINANCE.value}:{btc_symbol.ccxt_symbol}",
        timeframe="1h",
    )

    # If assessment has reduction reason, verify it
    if result.assessment is not None and result.assessment.decision == RiskDecision.REDUCED:
        assert result.assessment.approved_quantity > Decimal("0")
        assert len(result.assessment.warnings) > 0
