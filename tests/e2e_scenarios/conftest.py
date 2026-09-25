"""
SGR E2E Lifecycle Scenarios - Fixtures
=========================================
Verdrahtet den kompletten, ECHTEN Trading-Lifecycle fuer die Szenarien
in test_lifecycle_scenarios.py. Baut bewusst NICHTS neu, was bereits
existiert - identisches Engine-Setup wie
tests/integration/test_orchestrator_pipeline.py (TradingOrchestrator +
StrategyEngine + RiskEngine + ExecutionEngine + PortfolioEngine +
FeatureStore), einziger Unterschied: statt MockExchangeAdapter wird ein
echter BinanceAdapter gegen das Binance Futures Testnet verbunden
(futures_mode=True, Root-Cause-Fix vom 2026-09-17 - siehe
sgr/api/main.py binance_pool_kwargs).

Isolation (WICHTIG, siehe Produktions-Sicherheits-Vorgaben):
    1. Eigener, bereits vorher existierender Test-Tenant
       (test_e5ab3f39@sgr.test, keine neue DB-Zeile) statt Gordon/Sumo -
       Kill-Switch-/Risk-State dieses Tenants ist unter einem eigenen
       Redis-Key-Namespace UND einer eigenen Redis-Logical-DB (siehe
       Punkt 2) vollstaendig getrennt von Gordon/Sumo.
    2. REDIS_DB=1 statt der produktiv genutzten DB 0 (siehe .env.prod) -
       FeatureStore/EventBus/KillSwitch/SafeOrderExecutor haengen alle
       an get_config().redis.url, das den DB-Index einbettet. Dieselbe
       physische Redis-Instanz, aber eine komplett separate logische
       Datenbank - kein gemeinsamer Key-Namespace mit Gordon/Sumo,
       nichts kann versehentlich deren Kill-Switch-/Feature-/Duplicate-
       Order-State ueberschreiben. Verifiziert leer vor dem ersten Lauf
       (DBSIZE == 0).
    3. Keine DB-Credentials-Injektion (load_tenant_credentials()) - der
       Test-Tenant hat bewusst KEINE api_keys-Zeile (keine manuelle
       DB-Schreibung noetig). Stattdessen dieselben Binance-Paper-
       Testnet-Keys, die bereits als Prozessumgebung vorhanden sind
       (BINANCE_PAPER_API_KEY/BINANCE_PAPER_API_SECRET), direkt via
       ExchangeFactory.create_with_credentials() - identischer
       Adapter-Code, nur ohne den DB-Lookup-Schritt.
    4. PortfolioEngine laeuft rein in-memory (kein position_repository/
       trade_repository injiziert) - identisch zum bestehenden
       Referenztest. Keine Schreibungen in die produktive
       positions/orders/trades-Tabelle durch diese Phase. Persistenz-
       Korrektheit nach Neustart ist Gegenstand von Phase 3 (baut auf
       tests/docker_crash_tests/ auf, das bereits gegen echte Postgres/
       Redis-Instanzen faehrt).
    5. Gordon/Sumo, ihr Kill-Switch, ihre Positionen: an keiner Stelle
       referenziert oder veraendert.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

E2E_TEST_TENANT_ID = (
    "61230c37-1cd2-4564-ab12-a0a1180f0cb2"  # test_e5ab3f39@sgr.test, bereits vorhanden
)

from sgr.core.config import get_config  # noqa: E402
from sgr.core.types import (  # noqa: E402
    AssetClass,
    ExchangeID,
    Position,
    PositionSide,
    Symbol,
    TradingMode,
)
from sgr.exchanges.binance import BinanceAdapter  # noqa: E402
from sgr.exchanges.factory import ExchangePool  # noqa: E402
from sgr.execution.engine import ExecutionEngine  # noqa: E402
from sgr.market_data.feature_store import FeatureStore  # noqa: E402
from sgr.market_data.types import FeatureSet, IndicatorValues  # noqa: E402
from sgr.orchestrator.engine import TradingOrchestrator  # noqa: E402
from sgr.portfolio.engine import PortfolioEngine  # noqa: E402
from sgr.risk.engine import RiskEngine  # noqa: E402
from sgr.risk.position_protection import (  # noqa: E402
    PositionProtectionManager,
    PositionProtectionWatchdog,
)
from sgr.strategy.base import ValidationStatus  # noqa: E402
from sgr.strategy.engine import StrategyEngine  # noqa: E402
from sgr.strategy.registry import StrategyRegistry  # noqa: E402
from sgr.strategy.trend_following import TrendFollowingStrategy  # noqa: E402

# Futures-only gelistetes Symbol (keine parallele Spot-Notierung auf
# Binance) - deckt bei jedem Lauf implizit den futures_mode-Root-Cause-
# Fix vom 2026-09-17 mit ab (dieses exakte Symbol schlug vor dem Fix mit
# SymbolNotFoundError fehl).
E2E_SYMBOL = Symbol(
    base="HFT", quote="USDT", exchange=ExchangeID.BINANCE, asset_class=AssetClass.FUTURES
)
E2E_TIMEFRAME = "1h"
E2E_SYMBOL_KEY = f"{ExchangeID.BINANCE.value}:{E2E_SYMBOL.ccxt_symbol}"


@pytest.fixture(autouse=True)
def e2e_config_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    """
    Setzt alle Env-Vars ausschliesslich ueber pytest's monkeypatch-Fixture
    (statt direkt os.environ auf Modulebene) - monkeypatch reverted jede
    Aenderung automatisch am Testende, UNABHAENGIG davon ob der Test
    lief oder nur kollektiert wurde.

    Root-Cause-Fund (dieser Fix, waehrend der Erstverifikation dieses
    Runners): eine vorherige Fassung setzte os.environ[...] auf
    Modulebene in conftest.py. pytest IMPORTIERT jede conftest.py im
    Collection-Baum immer, auch wenn `-m "not e2e_scenario"` die
    einzelnen Testfunktionen anschliessend deselektiert - die
    Modulebene-Zuweisungen liefen dadurch bei JEDEM pytest-Aufruf, der
    tests/ (auch nur teilweise) einsammelte, und ueberschrieben
    TENANT_ID/REDIS_DB/RISK_*-Werte fuer die GESAMTE restliche
    Test-Session. Live verifiziert: 13-34 sonst gruene Tests
    (test_config.py, test_startup_checks.py, test_risk_engine.py, ...)
    schlugen dadurch fehl, sobald tests/e2e_scenarios/ im selben
    `pytest tests/`-Lauf mit eingesammelt wurde. monkeypatch loest das
    strukturell: Aenderungen existieren ausschliesslich waehrend der
    Laufzeit EINES Testkoerpers dieses Moduls.
    """
    monkeypatch.setenv("TENANT_ID", E2E_TEST_TENANT_ID)
    monkeypatch.setenv("PRIMARY_EXCHANGE", "binance")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("REDIS_DB", "1")  # siehe Modul-Docstring Isolation Punkt 2

    # TEST_1X-Profil (siehe sgr/core/config.py RiskLimitsConfig) - kleine,
    # kontrollierte Positionsgroesse statt adaptivem Sizing, kein Hebel,
    # enge SL/TP-Abstaende fuer schnell auswertbare Testlaeufe.
    monkeypatch.setenv("RISK_RISK_PROFILE_NAME", "E2E_PHASE1")
    monkeypatch.setenv("RISK_POSITION_SIZE_USD", "20")
    monkeypatch.setenv("RISK_DEFAULT_LEVERAGE", "1")
    monkeypatch.setenv("RISK_STOP_LOSS_PCT", "0.01")
    monkeypatch.setenv("RISK_TAKE_PROFIT_PCT", "0.02")
    monkeypatch.setenv("RISK_MAX_HOLDING_MINUTES", "30")
    # Klein gehalten (Default 10), damit test_position_limit_rejection
    # nicht 10 synthetische Positionen seeden muss, um das Hard Limit zu
    # erreichen - reine Testlaufzeit-Optimierung, betrifft Phase-1-Tests
    # nicht (die oeffnen jeweils nur 1 Position).
    monkeypatch.setenv("RISK_MAX_OPEN_POSITIONS", "3")
    # Weit in der Vergangenheit -> jede in diesem Testlauf neu eroeffnete
    # Position liegt danach, bekommt also SL/TP/Max-Holding angehaengt
    # (siehe PositionProtectionManager.on_position_opened cutover-Check).
    monkeypatch.setenv("RISK_PROTECTION_CUTOVER_AT", "2020-01-01T00:00:00+00:00")

    get_config.cache_clear()
    cfg = get_config()
    assert cfg.tenant_id == E2E_TEST_TENANT_ID, (
        f"Safety guard: erwarteter E2E-Test-Tenant, bekam {cfg.tenant_id!r} - "
        "Abbruch, um Gordon/Sumo nicht zu beruehren."
    )
    assert cfg.redis.db == 1, f"Safety guard: erwartete Redis-DB 1, bekam {cfg.redis.db!r}."
    yield
    get_config.cache_clear()


@pytest.fixture(autouse=True)
async def connected_event_bus(e2e_config_guard: None) -> Any:
    """
    Verbindet den globalen EventBus-Singleton gegen die (isolierte,
    REDIS_DB=1-) Redis-Instanz - identisches Muster wie
    tests/integration/test_orchestrator_pipeline.py::connected_event_bus
    (inkl. derselben Begruendung fuer den Singleton-Reset pro Test:
    pytest-asyncio vergibt pro Testfunktion einen frischen Event Loop).
    Ohne diese Fixture liefe die Orchestrierung weiterhin durch (Bus-
    Fehler sind laut orchestrator/engine.py Modul-Docstring fail-safe),
    aber "EventBus not connected"-Fehler in jedem Zyklus wuerden echte
    Audit-Events (RiskApprovedEvent, TradingCycleCompletedEvent, ...)
    verschlucken.
    """
    import sgr.core.event_bus as event_bus_module

    event_bus_module._bus = None
    bus = event_bus_module.get_event_bus()
    await bus.connect()
    yield bus
    await bus.close()
    event_bus_module._bus = None


@pytest.fixture
async def feature_store(e2e_config_guard: None) -> Any:
    store = FeatureStore()
    await store.connect()
    yield store
    await store.close()


@pytest.fixture(autouse=True)
async def clean_kill_switch(feature_store: FeatureStore) -> Any:
    """
    Setzt den Kill-Switch des E2E-Test-Tenants vor UND nach jedem Test
    zurueck (echter Reset-Codepfad, kein manueller Redis-Write).

    Root-Cause-Fund (waehrend Phase-2-Erstverifikation): der
    max_open_positions-Hard-Limit-Test loest denselben echten
    RiskEngine-Mechanismus aus, der auch Gordons produktiven Kill-Switch
    ausgeloest hat (siehe sgr/risk/engine.py: JEDER Hard-Limit-Breach
    triggert `asyncio.create_task(self._kill_switch.trigger(...))`) -
    ohne Reset blieb der Kill-Switch dieses Test-Tenants in Redis (DB 1)
    danach aktiv und liess NACHFOLGENDE Tests in derselben Session mit
    "Kill switch active" fehlschlagen, obwohl sie inhaltlich nichts
    damit zu tun hatten. Der `get_kill_switch()`-Singleton (siehe
    sgr/risk/kill_switch.py) ist zusaetzlich prozessweit gecacht, ueberlebt
    also auch den lokalen In-Memory-State ueber Testfunktionsgrenzen
    hinweg - Redis-Reset allein reicht nicht, das lokale Objekt muss den
    echten Zustand zuerst uebernehmen (identisches Muster wie der
    gefixte POST /risk/kill-switch/reset-Endpoint, siehe
    sgr/api/routers/risk.py).
    """
    from sgr.risk.kill_switch import get_kill_switch, read_kill_switch_state_from_redis

    async def _reset() -> None:
        ks = get_kill_switch(TradingMode.PAPER, tenant_id=E2E_TEST_TENANT_ID)
        ks.inject_redis(feature_store.redis_client)
        current = await read_kill_switch_state_from_redis(
            feature_store.redis_client, TradingMode.PAPER, tenant_id=E2E_TEST_TENANT_ID
        )
        if current is not None and current.get("is_active"):
            ks._state.trigger(current.get("reason") or "unknown", TradingMode.PAPER)  # noqa: SLF001
            await ks.reset(reset_by="e2e_test_cleanup")

    await _reset()
    yield
    await _reset()


@pytest.fixture
async def exchange_pool(e2e_config_guard: None) -> Any:
    """
    Echter BinanceAdapter gegen das Futures-Testnet (Paper Mode,
    futures_mode=True). Keine DB-Credentials-Injektion (siehe Modul-
    Docstring Punkt 3) - dieselben Prozessumgebungs-Keys, mit denen auch
    Gordon/Sumo verbinden, aber ueber einen komplett eigenen
    Adapter/ExchangePool/asyncio-Prozess (dieser pytest-Lauf), keine
    gemeinsame ccxt-Client-Instanz.
    """
    api_key = os.environ["BINANCE_PAPER_API_KEY"]
    secret = os.environ["BINANCE_PAPER_API_SECRET"]

    pool = ExchangePool()
    adapter = BinanceAdapter(
        api_key=api_key, secret=secret, trading_mode=TradingMode.PAPER, futures_mode=True
    )
    await adapter.connect()
    pool._adapters[(ExchangeID.BINANCE, TradingMode.PAPER)] = adapter
    yield pool
    await pool.close_all()


@pytest.fixture
async def portfolio_protection() -> PositionProtectionManager:
    return PositionProtectionManager()


@pytest.fixture
async def portfolio_engine(
    e2e_config_guard: None, portfolio_protection: PositionProtectionManager
) -> PortfolioEngine:
    engine = PortfolioEngine(
        trading_mode=TradingMode.PAPER,
        initial_cash=Decimal("10000"),
        tenant_id=E2E_TEST_TENANT_ID,
        on_position_opened=portfolio_protection.on_position_opened,
        on_position_closed=portfolio_protection.on_position_closed,
    )
    return engine


@pytest.fixture
async def risk_engine(e2e_config_guard: None, feature_store: FeatureStore) -> RiskEngine:
    engine = RiskEngine(TradingMode.PAPER)
    await engine.initialize()
    # Cross-Prozess-Kill-Switch-Redis-Wiring (identisches Muster wie
    # sgr/api/main.py lifespan(), siehe Kommentar dort: injiziert auch
    # in den intern gehaltenen KillSwitch) - ohne das bliebe der Kill-
    # Switch-Test (spaetere Phase) rein in-memory und koennte den
    # tatsaechlichen produktiven Redis-Pfad nicht verifizieren.
    engine.inject_redis(feature_store.redis_client)
    return engine


@pytest.fixture
async def execution_engine(exchange_pool: ExchangePool) -> ExecutionEngine:
    return ExecutionEngine(exchange_pool, TradingMode.PAPER)


@pytest.fixture
async def watchdog(
    portfolio_engine: PortfolioEngine, execution_engine: ExecutionEngine
) -> PositionProtectionWatchdog:
    """Nicht gestartet (kein Background-Loop) - Szenarien rufen
    check_positions_once() explizit und deterministisch auf (siehe
    Docstring dort: genau fuer Tests vorgesehen)."""
    return PositionProtectionWatchdog(portfolio_engine, execution_engine)


@pytest.fixture
async def strategy_engine(e2e_config_guard: None, feature_store: FeatureStore) -> StrategyEngine:
    """
    Echte trend_following_v1-Strategie (sgr/strategy/trend_following.py),
    nicht MockStrategy - erfuellt "moeglichst echte Komponenten
    verwenden". Registry-Instanz ist prozessweit (Klassen-Attribut,
    siehe StrategyRegistry-Modul-Docstring "Singleton"), aber dieser
    pytest-Lauf ist ein eigener Prozess, komplett getrennt von Gordon/
    Sumo - Aktivierung hier wirkt sich auf NICHTS ausserhalb dieses
    Prozesses aus. mark_validated()+activate() bilden exakt den bereits
    verifizierten Produktions-Ist-Zustand nach (trend_following_v1 ist
    aktuell die einzige global validierte+aktive Strategie, siehe
    strategies-Tabelle) - kein neu erfundener Zustand.
    """
    registry = StrategyRegistry.get()
    registry.clear()
    strategy = TrendFollowingStrategy()
    registry.register_instance(strategy)
    registry.mark_validated(
        strategy.name,
        ValidationStatus(backtest_passed=True, walk_forward_passed=True, paper_trading_passed=True),
    )
    await registry.activate(strategy.name)

    engine = StrategyEngine(TradingMode.PAPER, feature_store, registry)
    await engine.start()
    yield engine
    await engine.stop()
    registry.clear()


@pytest.fixture
async def orchestrator(
    strategy_engine: StrategyEngine,
    risk_engine: RiskEngine,
    execution_engine: ExecutionEngine,
    portfolio_engine: PortfolioEngine,
    feature_store: FeatureStore,
) -> TradingOrchestrator:
    return TradingOrchestrator(
        strategy_engine=strategy_engine,
        risk_engine=risk_engine,
        execution_engine=execution_engine,
        portfolio_engine=portfolio_engine,
        feature_store=feature_store,
        trading_mode=TradingMode.PAPER,
    )


def strong_trending_indicators(*, direction: str) -> IndicatorValues:
    """Indikator-Set, das trend_following_v1's Scoring (siehe
    sgr/strategy/trend_following.py _evaluate_long/_evaluate_short)
    zuverlaessig ueber min_confidence=0.55 hebt. direction: 'up' oder
    'down' (siehe TrendFollowingParams: rsi_min_long/rsi_max_short=50,
    adx_min=25, volume_ratio_min=0.8)."""
    if direction == "up":
        return IndicatorValues(
            rsi_14=68.0,
            adx_14=32.0,
            di_plus=28.0,
            di_minus=12.0,
            ema_9=Decimal("101.5"),
            ema_21=Decimal("100.2"),
            ema_50=Decimal("98.0"),
            vwap=Decimal("99.5"),
            volume_ratio=1.4,
            bb_position=0.65,
            atr_14=Decimal("2.0"),
        )
    return IndicatorValues(
        rsi_14=32.0,
        adx_14=32.0,
        di_plus=12.0,
        di_minus=28.0,
        ema_9=Decimal("98.5"),
        ema_21=Decimal("99.8"),
        ema_50=Decimal("102.0"),
        vwap=Decimal("100.5"),
        volume_ratio=1.4,
        bb_position=0.35,
        atr_14=Decimal("2.0"),
    )


def build_feature_set(*, close: Decimal, direction: str) -> FeatureSet:
    return FeatureSet(
        symbol=E2E_SYMBOL,
        timeframe=E2E_TIMEFRAME,
        timestamp=datetime.now(tz=UTC),
        close=close,
        volume=Decimal("50000"),
        indicators=strong_trending_indicators(direction=direction),
    )


def build_synthetic_position(index: int) -> Position:
    """
    Fuer test_position_limit_rejection: identisches Muster wie das
    bestehende tests/integration/test_orchestrator_pipeline.py::
    test_risk_rejection_prevents_execution - befuellt das Hard Limit
    (RISK_MAX_OPEN_POSITIONS) mit synthetischen Positionen direkt im
    In-Memory-PortfolioEngine-State, um gezielt die Risk-Engine-Limit-
    Pruefung zu testen, statt das bereits in Phase 1 bewiesene
    Order-Fill-Mechanismus RISK_MAX_OPEN_POSITIONS-mal echt zu wiederholen.
    """
    now = datetime.now(tz=UTC)
    sym = Symbol(base=f"ALT{index}", quote="USDT", exchange=ExchangeID.BINANCE)
    return Position(
        symbol=sym,
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        current_price=Decimal("100"),
        opened_at=now,
        strategy_name="trend_following_v1",
        trading_mode=TradingMode.PAPER,
    )
