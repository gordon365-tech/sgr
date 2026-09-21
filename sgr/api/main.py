"""
SGR FastAPI Application
=======================
Haupteinstiegspunkt der REST + WebSocket API.

Startup-Sequenz (via lifespan):
    1. Config validieren
    1a. Startup Safety Checks (fail-fast, siehe core/startup_checks.py)
    2. Logging initialisieren
    3. DB + Redis verbinden
    4. Exchange Pool initialisieren
    5. Market Data Engine starten
    6. Strategy Engine starten
    7. Risk Engine initialisieren
    8. API bereit (Traffic annehmen)

Middleware-Stack (außen → innen):
    CORS → RequestID → RateLimit → Auth → Handler

Routers:
    /health          → Health Check (kein Auth)
    /api/v1/market   → Market Data
    /api/v1/portfolio→ Positionen + PnL
    /api/v1/risk     → Risk Metriken + Limits
    /api/v1/strategy → Strategy Engine Status
    /api/v1/orders   → Order History
    /api/v1/system   → Kill Switch, Status
    /api/v1/trading  → Manueller Trading-Cycle-Trigger (Orchestrator)
    /api/v1/reconciliation → Exchange- vs. lokaler State-Abgleich (Phase 7B)
    /ws              → WebSocket Streams
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from sgr.core.config import get_config
from sgr.core.logging import get_logger, setup_logging
from sgr.core.types import Environment, TradingMode

log = get_logger(__name__)

# Live-Market-Data-Universum (Multi-Asset-Erweiterung, explizite operative
# Anweisung): Binance USDT-M Perpetual-Futures-Symbole im ccxt-Unified-Format
# ("BASE/USDT:USDT" - der ":USDT"-Suffix ist der Settle-Currency-Marker fuer
# einen Swap/Future, siehe Symbol._parse_symbol() in
# sgr/exchanges/ccxt_base.py, die diesen Fall bereits VOR dieser Aenderung
# korrekt in AssetClass.FUTURES uebersetzte - keine Anpassung an
# get_ohlcv()/get_ticker()/_parse_symbol() noetig, beide reichen den
# rohen ccxt-Symbol-String unveraendert durch).
#
# Jedes einzelne Symbol wurde vor Aufnahme LIVE gegen Binance (ccxt,
# oeffentliche load_markets()-Daten, Mainnet + Testnet) verifiziert -
# keine erfundenen Symbole. Wichtige Befunde dabei:
#   - PEPE/SHIB/FLOKI/BONK existieren NICHT unter ihrem einfachen Namen
#     als Future, sondern unter der "1000X"-Konvention, die Binance fuer
#     Coins mit sehr niedrigem Preis verwendet (vermeidet uebermaessige
#     Nachkommastellen im Kontraktpreis) - 1000PEPE/USDT:USDT etc.
#   - XAU/USDT:USDT und XAG/USDT:USDT existieren wirklich als aktive,
#     linear USDT-marginierte Perpetual-Swaps (verifiziert: type=swap,
#     contract=True, linear=True, settle=USDT, active=True) - Binance
#     bietet damit tatsaechlich Gold/Silber-Perpetuals an. Reine
#     Spot-Rohstoffe (z.B. PAXG, ein Spot-Gold-Token) werden bewusst
#     NICHT aufgenommen (Punkt 2 der Anweisung: "Keine Spot Rohstoffe").
#   - Binance bietet zusaetzlich eine deutlich groessere Palette an
#     tokenisierten Aktien-/Rohstoff-Perpetuals (u.a. CL/USDT:USDT =
#     Rohoel, MSTR/USDT:USDT, SOXL/USDT:USDT, weitere), die beim
#     Liquiditaets-Check entdeckt wurden - NICHT aufgenommen, da explizit
#     nur Gold/Silber angefordert wurden und diese Produktkategorie
#     (tokenisierte Einzelaktien) eine eigene, hier nicht beauftragte
#     Einordnungsentscheidung waere. Siehe Go-Live-Report fuer die volle
#     Liste als Kandidat fuer eine spaetere, bewusste Erweiterung.
#   - "Automatisch um weitere liquide USDT Futures erweitern" wurde
#     bewusst NICHT zusaetzlich zu den 22 angefragten Symbolen umgesetzt:
#     eine Liquiditaets-Bulk-Abfrage (fetch_tickers) waehrend dieser
#     Verifikation loeste einen kurzzeitigen Binance-Testnet-IP-Ban aus
#     (418 DDoSProtection, ~1.5h) - genau das Risiko, vor dem Punkt 6 der
#     Anweisung ausdruecklich warnt ("keine unkontrollierte parallele
#     CCXT-Abfrage"). Eine weitere automatische Ausweitung ueber die 22
#     explizit angefragten Symbole hinaus haette dieses Risiko erhoeht,
#     ohne dass sie explizit gefordert war.
#
# Bewusst getrennt von sgr.strategy.validation_runner.DEFAULT_SYMBOLS
# (BTC/USDT, ETH/USDT, SPOT) - die dortige Eignungsanalyse (Schritt
# 16/18, docs/ANALYSIS-mean-reversion-v1-schritt18-*.md) ist explizit
# auf BTC/ETH-SPOT gescoped; eine Ausweitung des Validierungs-Backtests
# auf alle 24 Symbole/Futures hier wäre ein separater, deutlich
# teurerer Analyseauftrag (24x Backtest+Walk-Forward pro Strategie,
# jeweils gegen andere Instrumentmechanik) und keine reine
# Konfigurationsänderung - nicht Teil dieser Änderung.
LIVE_MARKET_DATA_SYMBOLS: list[str] = [
    # Large-Cap
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "XRP/USDT:USDT",
    "BNB/USDT:USDT",
    # Mid-Cap / Volatile
    "ADA/USDT:USDT",
    "AVAX/USDT:USDT",
    "DOT/USDT:USDT",
    "NEAR/USDT:USDT",
    "LINK/USDT:USDT",
    "FET/USDT:USDT",
    "RENDER/USDT:USDT",
    "INJ/USDT:USDT",
    "SUI/USDT:USDT",
    "APT/USDT:USDT",
    "TIA/USDT:USDT",
    # Small-Cap / High-Beta Altcoins (1000X-Kontraktkonvention siehe oben)
    "1000PEPE/USDT:USDT",
    "DOGE/USDT:USDT",
    "1000SHIB/USDT:USDT",
    "1000FLOKI/USDT:USDT",
    "WIF/USDT:USDT",
    "1000BONK/USDT:USDT",
    # Commodities (Binance USDT-M Perpetuals, kein Spot)
    "XAU/USDT:USDT",  # Gold
    "XAG/USDT:USDT",  # Silber
]


async def apply_strategy_force_activate_override(
    registry: Any, strategy_repo: Any, names: list[str]
) -> list[str]:
    """
    Aktiviert Strategien trotz NO-GO/negativem Ergebnis aus der
    automatischen Backtest+Walk-Forward-Validierung (siehe
    sgr/strategy/validation_runner.py) - AUSSCHLIESSLICH auf explizite
    operative Anweisung fuer einen Paper-Trading-Pipeline-Testlauf, NICHT
    weil die Strategie den Go-Live-Gate tatsaechlich bestanden haette
    (siehe docs/ANALYSIS-mean-reversion-v1-schritt18-*.md: Klassifikation
    C - grundsaetzlich ungeeignet fuer mean_reversion_v1; trend_following_v1
    NO-GO mit OOS-Sharpe -7.2). Aufgerufen aus lifespan() NACH
    StrategyValidationRunner.validate_pending_strategies() - das echte
    Ergebnis bleibt in dessen Log-Event und in
    entry.last_validation_result unveraendert sichtbar, dieser Override
    ersetzt nur ValidationStatus/is_validated fuer die explizit genannten
    Namen.

    names: aus STRATEGY_FORCE_ACTIVATE env var (kommagetrennt). Leer =
    kein Override, unveraendertes Verhalten.

    Returns: Liste der tatsaechlich uebersteuerten Namen (fuer Logging/Tests).
    """
    from sgr.strategy.base import ValidationStatus

    overridden: list[str] = []
    for name in names:
        entry = registry.get_entry(name)
        if entry is None:
            log.warning("sgr.api.strategy_force_activate_unknown_name", name=name)
            continue
        real_notes = entry.validation_status.notes
        override_status = ValidationStatus(
            backtest_passed=True,
            walk_forward_passed=True,
            paper_trading_passed=True,
            live_approved=False,
            is_operator_override=True,
            notes=(
                "MANUAL OVERRIDE (STRATEGY_FORCE_ACTIVATE env var): echte "
                f"Validierung bestand NICHT ({real_notes!r}). Forciert aktiv "
                "fuer einen Paper-Trading-Pipeline-Testlauf auf explizite "
                "operative Anweisung - KEIN oekonomisch validiertes "
                "Go-Live-Signal, kein Live-Trading-Freigabe."
            ),
        )
        registry.mark_validated(name, override_status, backtest_result=entry.last_validation_result)
        await strategy_repo.set_validated(name, True)
        log.warning(
            "sgr.api.strategy_force_activate_override",
            name=name,
            real_validation_notes=real_notes,
        )
        overridden.append(name)
    return overridden


# Rolle, mit der lifespan() aufgerufen wird. Steuert, ob der volle Trading
# Lifecycle (Exchange Pool, Strategy Engine, Execution Engine, Orchestrator,
# Market Data Engine) gestartet wird, oder nur die Read-Pfad-Infrastruktur
# (DB, Redis, Event Bus, Feature Store), die die Read-Only-API-Router
# brauchen (siehe sgr/api/dependencies.py Modul-Docstring).
#
# "worker" ist bewusst der Default: er entspricht dem Verhalten VOR dieser
# Aufteilung und ist das, was die bestehende Test-Suite (tests/api/
# test_main.py) bereits durchgehend prueft (voller Lifecycle inkl.
# strategy_engine.start(), md_engine.start() etc.). sgr-worker/main.py
# ruft explizit role="worker" auf; create_app() (fuer sgr-api) uebergibt
# explizit role="api".
LifespanRole = Literal["api", "worker"]


# ---------------------------------------------------------------------------
# Application State (shared across requests)
# ---------------------------------------------------------------------------


class AppState:
    """
    Singleton App-State: alle Engines und Pools.
    Wird im lifespan initialisiert und via request.app.state zugegriffen.
    """

    exchange_pool: Any = None
    market_data_engine: Any = None
    strategy_engine: Any = None
    risk_engine: Any = None
    portfolio_engine: Any = None
    execution_engine: Any = None
    orchestrator: Any = None
    reconciliation_engine: Any = None
    feature_store: Any = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI, role: LifespanRole = "worker") -> AsyncIterator[None]:
    """
    Startup + Shutdown aller Systemkomponenten.
    Fehler beim Startup → Server startet nicht (fail fast).

    role="worker" (Default): startet den vollstaendigen Trading Lifecycle
        (Exchange Pool, Risk/Portfolio/Strategy/Execution Engines,
        Orchestrator, Reconciliation Engine, Crash Recovery, Market Data
        Engine mit Candle-Event-Subscription). sgr-worker ist alleiniger
        Owner dieser Komponenten (siehe sgr/worker/main.py).
    role="api": startet ausschliesslich die Infrastruktur, die die
        Read-Only-API-Router brauchen (DB, Redis, Event Bus, Feature
        Store als Redis-Fassade). Kein Exchange Pool, keine Trading
        Engines, kein Orchestrator, kein Market Data Polling - die API
        besitzt seit der sgr-api/sgr-worker-Trennung keinen eigenen
        Trading Lifecycle mehr (siehe sgr/api/dependencies.py
        Modul-Docstring). app.state.* Engine-Attribute bleiben dabei
        auf ihrem AppState-Klassendefault None stehen.
    """
    config = get_config()

    # 1. Logging
    setup_logging(
        log_level=config.monitoring.log_level,
        json_output=config.environment == Environment.PRODUCTION,
        trading_mode=config.trading_mode,
    )

    log.info(
        "sgr.api.starting",
        version=config.version,
        environment=config.environment.value,
        trading_mode=config.trading_mode.value,
    )

    # 1a. Startup Safety Checks
    # Laufen bewusst VOR jeder Verbindung (DB, Redis, Exchange) und VOR
    # Observability-Setup. Fail-Fast: eine unsichere LIVE-Konfiguration
    # (fehlende Credentials, deaktivierte Fat-Finger-Caps, zu lasche Hard
    # Limits, bereits aktiver Kill Switch) darf den Server nicht mit
    # scheinbar funktionierenden, aber ungeschützten Trading-Pfaden
    # hochfahren lassen. Anders als Crash Recovery (Schritt 8d, fail-safe)
    # ist dieser Schritt absichtlich fail-fast - siehe
    # sgr/core/startup_checks.py Modul-Docstring.
    from sgr.core.startup_checks import StartupSafetyChecker

    StartupSafetyChecker(config).run_or_raise()

    # 1b. Observability (OpenTelemetry Metrics + Auto-Instrumentation)
    # Wurde zuvor nirgends aufgerufen (deferred finding: monitoring/
    # observability.py war reiner Dead Code, 0% Coverage). Jetzt verdrahtet:
    # setup_metrics() registriert den Prometheus-MeterProvider, ohne den
    # sgr.monitoring.metrics.SGRMetrics-Meter (bereits an anderer Stelle
    # verwendet) stillschweigend gegen den OpenTelemetry-No-Op-Meter fällt.
    # Tracing bleibt bewusst No-Op (siehe setup_tracing()-Docstring), bis
    # die OTLP-Migration explizit angegangen wird.
    from sgr.monitoring.observability import setup_observability

    try:
        setup_observability(app)
    except Exception as e:
        # Observability-Fehler sollen den Start nicht verhindern
        # (fail-safe, nicht fail-fast - analog zu Crash Recovery unten).
        log.warning("sgr.api.observability_setup_failed", error=str(e))

    # 2. Database
    from sgr.core.database import close_db, init_db

    await init_db()

    # 2b. Repositories (Persistenz-Schicht)
    # Vorher NIRGENDS im Lifespan instanziiert - PositionRepository,
    # OrderRepository, StrategyRepository etc. existierten isoliert von
    # der laufenden App, obwohl vollstaendig implementiert und getestet.
    # Ohne diesen Schritt ist echte Crash-Recovery unmoeglich (keine
    # Injektion in PortfolioEngine/ExecutionEngine/StrategyRegistry).
    from sgr.core.repositories import get_repositories

    repos = get_repositories()
    app.state.repositories = repos

    # 3. Event Bus
    from sgr.core.event_bus import get_event_bus

    bus = get_event_bus()
    await bus.connect()

    # 4. Feature Store
    from sgr.market_data.feature_store import FeatureStore

    feature_store = FeatureStore()
    await feature_store.connect()
    app.state.feature_store = feature_store

    # ------------------------------------------------------------------
    # Ab hier: nur role="worker" startet den vollstaendigen Trading
    # Lifecycle (Exchange Pool, Trading Engines, Orchestrator, Market
    # Data Engine). role="api" ueberspringt diesen gesamten Block - die
    # entsprechenden app.state.*-Attribute bleiben auf ihrem
    # AppState-Klassendefault None (siehe Klassendefinition oben).
    # ------------------------------------------------------------------
    pool: Any = None
    strategy_engine: Any = None
    execution_engine: Any = None
    md_engine: Any = None

    if role == "worker":
        # 5. Exchange Pool (nur bei konfigurierten Keys)
        from sgr.core.types import ExchangeID
        from sgr.exchanges.factory import ExchangePool

        pool = ExchangePool()
        primary_exchange = config.primary_exchange

        # UM-Futures statt Spot fuer Binance (BinanceAdapter.futures_mode,
        # Default False): ccxt laedt zwar immer ALLE Maerkte unabhaengig
        # von diesem Flag, loest aber die suffixfreie Symbol-Form
        # (Symbol.ccxt_symbol, siehe core/types.py - "HFT/USDT" statt
        # "HFT/USDT:USDT") nur dann korrekt gegen den Futures-Markt auf,
        # wenn defaultType tatsaechlich futures ist. Root-Cause-Fix (live
        # am Server reproduziert, 2026-09-17): dieses Kwarg wurde beim
        # Pool-Setup hier nie durchgereicht, obwohl jede Order/Position im
        # System durchgaengig asset_class=FUTURES traegt und
        # ExecutionEngine bereits adapter.set_leverage() (ein reiner
        # Futures-Call) nutzt - Futures war immer die beabsichtigte
        # Marktart. Folge: fetch_ticker() scheiterte mit
        # SymbolNotFoundError fuer jedes futures-only gelistete Symbol
        # ohne parallele Spot-Notierung (die meisten des dynamisch
        # entdeckten Universums) - nur die Handvoll Symbole MIT
        # zusaetzlicher Spot-Notierung (z.B. QUICK, AI) funktionierten je
        # zufaellig. Gilt NICHT fuer Pionex (kein Futures-Konzept,
        # PionexAdapter.from_config() kennt dieses Kwarg nicht - siehe
        # eigener Zweig unten).
        binance_pool_kwargs: dict[str, Any] = (
            {"futures_mode": True} if primary_exchange == ExchangeID.BINANCE else {}
        )

        try:
            if config.tenant_id is not None:
                # Multi-Tenant-Worker (Commit 5, Option A): Credentials
                # kommen aus der DB (APIKeyModel) statt aus config.credentials
                # (.env). Kein Pionex/Testnet-Sonderfall hier - Paper-Mode-
                # Credentials muessen fuer Tenants genauso explizit in der DB
                # hinterlegt sein wie Live-Credentials (siehe
                # sgr.core.tenant_credentials.load_tenant_credentials()
                # Docstring: Tenant ohne konfigurierte Keys faehrt den
                # Worker trotzdem hoch, nur ohne Exchange Pool - kein
                # fail-fast, analog zum bestehenden .env-ValueError-Pfad
                # unten).
                from sgr.core.tenant_credentials import load_tenant_credentials

                try:
                    tenant_credentials = await load_tenant_credentials(
                        config.tenant_id, primary_exchange, config.trading_mode
                    )
                    await pool.initialize(
                        [primary_exchange],
                        config.trading_mode,
                        credentials=tenant_credentials,
                        **binance_pool_kwargs,
                    )
                except ValueError:
                    log.warning(
                        "sgr.api.tenant_credentials_missing",
                        tenant_id=config.tenant_id,
                        exchange=primary_exchange.value,
                        trading_mode=config.trading_mode.value,
                    )
            elif primary_exchange == ExchangeID.PIONEX:
                # Pionex hat kein Testnet: Paper Mode braucht keine echten Keys
                # (PionexAdapter.connect() simuliert lokal, siehe pionex.py)
                if config.trading_mode == TradingMode.PAPER or (
                    config.credentials.pionex_live_api_key and config.credentials.pionex_live_secret
                ):
                    await pool.initialize([primary_exchange], config.trading_mode)
            else:
                # Andere Exchanges (z.B. Binance) haben ein echtes Testnet und
                # brauchen dafuer konfigurierte Paper-Testnet-Keys, bzw. echte
                # Live-Keys im LIVE-Modus. get_credentials() wirft ValueError,
                # wenn die entsprechenden Env-Vars nicht gesetzt sind.
                try:
                    config.credentials.get_credentials(primary_exchange.value, config.trading_mode)
                    await pool.initialize(
                        [primary_exchange], config.trading_mode, **binance_pool_kwargs
                    )
                except ValueError:
                    log.warning(
                        "sgr.api.exchange_credentials_missing",
                        exchange=primary_exchange.value,
                        trading_mode=config.trading_mode.value,
                    )
        except Exception as e:
            log.warning(
                "sgr.api.exchange_init_failed", exchange=primary_exchange.value, error=str(e)
            )

        app.state.exchange_pool = pool

        # 6. Risk Engine
        from sgr.risk.engine import RiskEngine

        risk_engine = RiskEngine(config.trading_mode)
        await risk_engine.initialize()
        # BUG-FIX (Produktions-Audit 2026-09-16): inject_redis() existierte
        # bereits (RiskEngine + intern KillSwitch), wurde aber nie von hier
        # aus aufgerufen - /health/trading und die Risk-API-Router lasen
        # daher dauerhaft "unknown" statt des echten, im Worker-Prozess
        # korrekten Kill-Switch-/Risk-Zustands (siehe RiskEngine.inject_redis
        # Docstring fuer den vollen Befund). feature_store.redis_client ist
        # an dieser Stelle bereits initialisiert (siehe oben, Schritt 3).
        if feature_store.redis_client is not None:
            risk_engine.inject_redis(feature_store.redis_client)
            # BUG-FIX (siehe RiskEngine.start_kill_switch_remote_sync()
            # Docstring): ohne diesen Call wirkt ein externer kill-switch/
            # reset oder /trigger auf diesen bereits laufenden Worker nie,
            # bis zum naechsten Neustart.
            await risk_engine.start_kill_switch_remote_sync()
        app.state.risk_engine = risk_engine

        # 7. Portfolio Engine
        from sgr.portfolio.engine import PortfolioEngine

        portfolio_engine = PortfolioEngine(
            config.trading_mode,
            initial_cash=config.paper_initial_capital,
            position_repository=repos.positions,
            tenant_id=config.tenant_id,
            trade_repository=repos.trades,
        )
        app.state.portfolio_engine = portfolio_engine

        # 8. Strategy Engine
        # Strategien registrieren (Import triggert @register Decorator)
        import sgr.strategy.breakout  # noqa: F401
        import sgr.strategy.futures_grid  # noqa: F401
        import sgr.strategy.mean_reversion  # noqa: F401
        import sgr.strategy.momentum  # noqa: F401
        import sgr.strategy.trend_following  # noqa: F401
        import sgr.strategy.volatility_adjusted_momentum  # noqa: F401
        from sgr.strategy.engine import StrategyEngine
        from sgr.strategy.registry import StrategyRegistry

        registry = StrategyRegistry.get()
        registry.inject_repository(repos.strategies)
        await registry.sync_registrations_to_db()

        # Go-Live-Gate 1 (Backtest + Walk-Forward): schliesst die Lücke,
        # die zuvor dazu führte, dass is_validated nie produktiv True
        # wurde (siehe sgr/strategy/validation_runner.py Modul-Docstring
        # für die vollständige Herleitung). Fail-safe: läuft nur, wenn
        # ein Exchange Pool verbunden ist; Fehler pro Strategie blockieren
        # nicht den Startup der übrigen Strategien oder der API selbst.
        from sgr.strategy.validation_runner import StrategyValidationRunner

        try:
            validation_runner = StrategyValidationRunner(
                exchange_pool=pool, exchange_id=primary_exchange
            )
            validation_summary = await validation_runner.validate_pending_strategies()
            log.info(
                "sgr.api.strategy_validation_completed",
                validated=validation_summary.validated,
                skipped=validation_summary.skipped,
                failed=list(validation_summary.failed.keys()),
            )
        except Exception as e:
            log.error("sgr.api.strategy_validation_runner_failed", error=str(e))

        # Grid-Strategien (Futures Grid Long/Short/Adaptive) durchlaufen
        # eine EIGENE Backtest+Walk-Forward-Pipeline (siehe
        # sgr/strategy/grid_validation_runner.py Modul-Docstring - der
        # klassische StrategyValidationRunner oben ueberspringt sie
        # bereits explizit). is_validated=True setzt hier NUR das
        # Registry-Flag (Paper-Aktivierungs-Kandidat); es startet KEIN
        # automatisches Live-Grid-Trading und weist kein echtes Kapital
        # zu - dafuer existiert (bewusst, siehe Strategiebericht "offene
        # Punkte") noch kein automatischer Scheduler, der aktive
        # GridTradingStrategy-Instanzen periodisch gegen
        # GridController.open_grid() ausfuehrt.
        from sgr.strategy.grid_validation_runner import GridValidationRunner

        try:
            grid_validation_runner = GridValidationRunner()
            grid_validation_summary = (
                await grid_validation_runner.validate_pending_grid_strategies()
            )
            log.info(
                "sgr.api.grid_strategy_validation_completed",
                validated=grid_validation_summary.validated,
                skipped=grid_validation_summary.skipped,
                failed=list(grid_validation_summary.failed.keys()),
            )
        except Exception as e:
            log.error("sgr.api.grid_validation_runner_failed", error=str(e))

        # Manueller Override (STRATEGY_FORCE_ACTIVATE env var), siehe
        # apply_strategy_force_activate_override() Docstring oben im Modul.
        import os

        await apply_strategy_force_activate_override(
            registry=registry,
            strategy_repo=repos.strategies,
            names=[
                n.strip()
                for n in os.environ.get("STRATEGY_FORCE_ACTIVATE", "").split(",")
                if n.strip()
            ],
        )

        # Aktiviere alle validierten Strategien für das Paper Trading.
        # Default: Strategien starten deaktiviert, müssen explizit aktiviert werden.
        # Hier aktivieren wir nur die, die bereits validiert sind (is_validated=True
        # nach erfolgreichem Backtest). Weitere Strategien können durch Management-APIs
        # später aktiviert werden.
        for entry in registry.get_all().values():
            if entry.is_validated:
                await registry.activate(entry.strategy.name)
                log.info(
                    "sgr.api.strategy_activated",
                    name=entry.strategy.name,
                    version=entry.strategy.version,
                )

        strategy_engine = StrategyEngine(config.trading_mode, feature_store)
        await strategy_engine.start()
        app.state.strategy_engine = strategy_engine

        # 8b. Execution Engine + Trading Orchestrator
        # Verdrahtet den zuvor nicht verbundenen Pfad Signal -> Risk -> Order ->
        # Portfolio. Siehe sgr/orchestrator/engine.py für die Architekturbegründung.
        from sgr.execution.engine import ExecutionEngine
        from sgr.orchestrator.engine import TradingOrchestrator

        execution_engine = ExecutionEngine(pool, config.trading_mode, order_repository=repos.orders)
        app.state.execution_engine = execution_engine

        orchestrator = TradingOrchestrator(
            strategy_engine=strategy_engine,
            risk_engine=risk_engine,
            execution_engine=execution_engine,
            portfolio_engine=portfolio_engine,
            feature_store=feature_store,
            trading_mode=config.trading_mode,
            tenant_id=config.tenant_id,
        )
        app.state.orchestrator = orchestrator

        # 8b-ii. Position Liquidator (schliesst die Kill-Switch
        # close_positions=True-Luecke, siehe sgr/risk/position_liquidator.py
        # Modul-Docstring: KillSwitch publizierte das Event schon immer,
        # aber niemand hoerte je zu). Konsumiert denselben KillSwitchEvent-
        # Stream wie jeder andere Tenant, filtert intern per tenant_id -
        # siehe dortigen Docstring fuer die Begruendung.
        from sgr.core.types import KillSwitchEvent
        from sgr.risk.position_liquidator import PositionLiquidator

        liquidator = PositionLiquidator(
            portfolio_engine=portfolio_engine,
            execution_engine=execution_engine,
            tenant_id=config.tenant_id,
        )
        app.state.position_liquidator = liquidator
        liquidator_tenant_suffix = config.tenant_id or "default"
        bus.subscribe(
            KillSwitchEvent,
            liquidator.on_kill_switch_event,
            consumer_group=f"position_liquidator:{liquidator_tenant_suffix}",
            consumer_name=f"position_liquidator-{liquidator_tenant_suffix}-1",
        )

        # 8b-iii. Position Protection (SL/TP/Max-Holding-Time), siehe
        # sgr/risk/position_protection.py Modul-Docstring. Hooks werden
        # per set_protection_hooks() NACH der Konstruktion injiziert
        # (siehe dortiger Docstring: PortfolioEngine existiert bereits vor
        # ExecutionEngine, gleiches Nachtraeglich-Injizieren-Muster wie
        # risk_engine.inject_redis() oben).
        from sgr.risk.position_protection import (
            PositionProtectionManager,
            PositionProtectionWatchdog,
        )

        protection_manager = PositionProtectionManager()
        portfolio_engine.set_protection_hooks(
            on_position_opened=protection_manager.on_position_opened,
            on_position_closed=protection_manager.on_position_closed,
        )
        protection_watchdog = PositionProtectionWatchdog(
            portfolio_engine=portfolio_engine,
            execution_engine=execution_engine,
        )
        await protection_watchdog.start()
        app.state.position_protection_watchdog = protection_watchdog

        # 8c. Reconciliation Engine (Phase 7B)
        # Nur in LIVE aussagekräftig (siehe sgr/reconciliation/engine.py
        # Modul-Docstring) - wird trotzdem immer instanziiert, damit
        # get_reconciliation_engine() nicht je nach Modus fehlschlägt.
        # reconcile() selbst gibt in PAPER/DRY_RUN fail-safe SKIPPED_NOT_LIVE
        # zurück, statt einen Fehler zu werfen.
        from sgr.reconciliation.engine import ReconciliationEngine

        reconciliation_engine = ReconciliationEngine(
            exchange_pool=pool,
            portfolio_engine=portfolio_engine,
            trading_mode=config.trading_mode,
        )
        app.state.reconciliation_engine = reconciliation_engine

        # 8d. Crash Recovery
        # Frueher reiner Pseudo-Code (RecoveryManager._restore_*() taten
        # nichts). Jetzt echter Delegat an die gerade injizierten Komponenten.
        # Laeuft VOR dem Market-Data-Start (Schritt 9), damit Recovery
        # abgeschlossen ist, bevor Live-Candle-Events den Orchestrator ausloesen.
        # Fehler hier stoppen den Start NICHT (fail-safe, nicht fail-fast) -
        # ein unvollstaendiges Recovery ist besser als ein Server, der gar
        # nicht hochkommt; die naechste ReconciliationEngine (Phase 7B)
        # deckt verbleibende Diskrepanzen ohnehin auf.
        from sgr.core.resilience import RecoveryManager

        recovery_manager = RecoveryManager(
            portfolio_engine=portfolio_engine,
            order_repository=repos.orders,
            strategy_registry=registry,
            trading_mode=config.trading_mode,
            redis_client=feature_store.redis_client,
            tenant_id=config.tenant_id,
        )
        await recovery_manager.recover_after_crash()

        # 9. Market Data Engine
        from sgr.market_data.engine import MarketDataEngine

        md_engine = MarketDataEngine(pool, config.trading_mode, feature_store)
        # Standard-Subscriptions (siehe LIVE_MARKET_DATA_SYMBOLS oben).
        # BTC/USDT zusaetzlich auf 4h, wie zuvor. Ein einzelnes Symbol, das
        # auf der jeweiligen Exchange (z.B. Binance Testnet) nicht gelistet
        # ist oder keine History liefert, blockiert die uebrigen Feeds
        # nicht - SymbolFeed.initialize()-Fehler werden pro Feed isoliert
        # behandelt (asyncio.gather(..., return_exceptions=True) in
        # MarketDataEngine.start()), der Poll-Loop des betroffenen Feeds
        # laeuft trotzdem weiter und versucht es bei jedem Intervall erneut.
        if pool._adapters:
            for symbol in LIVE_MARKET_DATA_SYMBOLS:
                timeframes = ["1h", "4h"] if symbol == "BTC/USDT:USDT" else ["1h"]
                md_engine.subscribe(symbol, primary_exchange, timeframes)
            await md_engine.start()

            # Orchestrator automatisch bei jedem neuen Candle auslösen
            # (additiver Event-Trigger; run_cycle() bleibt auch direkt aufrufbar,
            # z.B. für manuelle Trigger oder Tests, ohne Redis-Abhängigkeit)
            #
            # Tenant-Scoping (siehe Audit nach Commit 5): consumer_group
            # und consumer_name enthalten die tenant_id (Default "default"
            # fuer Single-Tenant-Deployments ohne TENANT_ID). Redis Streams
            # verteilen Nachrichten INNERHALB einer Consumer-Group per
            # Round-Robin auf ihre Consumer - mit dem vorherigen, fuer
            # ALLE Tenants identischen "orchestrator"/"orchestrator-1"
            # haetten sich Gordon und Sumo CandleEvents gegenseitig
            # weggenommen, statt dass beide JEDES Event erhalten. Getrennte
            # Consumer-Groups pro Tenant sind bei Redis Streams die
            # korrekte Loesung fuer "mehrere unabhaengige Konsumenten
            # desselben Streams" - jede Gruppe sieht den vollen Stream.
            from sgr.core.types import CandleEvent

            tenant_suffix = config.tenant_id or "default"
            bus.subscribe(
                CandleEvent,
                orchestrator.on_candle_event,
                consumer_group=f"orchestrator:{tenant_suffix}",
                consumer_name=f"orchestrator-{tenant_suffix}-1",
            )
        app.state.market_data_engine = md_engine

        # 10. Monitoring Engine
        # Liest periodisch (alle 10s) State aus RiskEngine/PortfolioEngine/
        # StrategyRegistry und schreibt ihn ueber die OTel-Metrik-API
        # (sgr/monitoring/metrics.py SGRMetrics) - dieselbe REGISTRY, die
        # setup_observability() bereits als PrometheusMetricReader-Ziel
        # konfiguriert hat (siehe oben), landet also automatisch im
        # bestehenden /metrics-Endpoint ohne weitere Verdrahtung.
        # Bisher (dokumentierter Bestandsbefund) wurde diese Klasse nie
        # instanziiert - Portfolio/Risk/Strategy-Gauges blieben dauerhaft
        # auf ihrem Initialwert 0, unabhaengig vom tatsaechlichen State.
        from sgr.monitoring.engine import MonitoringEngine

        monitoring_engine = MonitoringEngine(
            risk_engine=risk_engine,
            portfolio_engine=portfolio_engine,
            strategy_registry=registry,
            trading_mode=config.trading_mode.value,
        )
        await monitoring_engine.start()
        app.state.monitoring_engine = monitoring_engine

        # 11. Worker Metrics Publisher (siehe sgr/monitoring/
        # worker_metrics_bridge.py Modul-Docstring): macht die gerade
        # aktivierten Metriken (MonitoringEngine + trading_metrics.py)
        # fuer Prometheus sichtbar, OHNE dass der Worker einen eigenen
        # HTTP-Port oeffnen muss (bewusstes Architekturprinzip, siehe
        # sgr/worker/main.py). Push statt Pull, analog zum Redis-Ticker-
        # Cache-Muster aus Schritt 6.
        from sgr.monitoring.worker_metrics_bridge import WorkerMetricsPublisher

        worker_metrics_publisher = WorkerMetricsPublisher(
            redis_client=feature_store.redis_client,
            tenant_id=config.tenant_id or "default",
            trading_mode=config.trading_mode.value,
        )
        await worker_metrics_publisher.start()
        app.state.worker_metrics_publisher = worker_metrics_publisher

        # 12. Asset Universe Engine (Market Discovery, siehe
        # sgr/market_data/asset_universe.py Modul-Docstring): periodisch
        # (Default alle 6h, sofortiger erster Lauf) welche Maerkte auf
        # Binance/Pionex tatsaechlich existieren.
        #
        # Autonomous-Paper-Trading-Rollout (Aenderung ggue. der
        # urspruenglichen, bewusst konservativen Fassung): vorher fuetterte
        # dieser Engine NUR Grafanas $symbol-Variable, LIVE_MARKET_DATA_
        # SYMBOLS blieb die alleinige, statische Quelle fuer Candle-Feeds.
        # Jetzt (explizite Anweisung: "alle 922 relevanten Symbole
        # analysieren, keine kuenstliche Reduzierung auf die bereits
        # bestehenden Subscriptions") treibt der on_discovery-Callback
        # unten zusaetzlich MarketDataEngine.reconcile_subscriptions() an -
        # jeder Markt, der die TRADABLE-Kaskade erreicht, bekommt
        # automatisch einen echten 1h-Feed. LIVE_MARKET_DATA_SYMBOLS bleibt
        # das Sicherheitsnetz (siehe AssetUniverseEngine.__init__-
        # Docstring), nicht mehr die Obergrenze. Trading-Subscription
        # (Candle-Feed) und Trading-AKTIVIERUNG (validierte, aktivierte
        # Strategie) bleiben weiterhin getrennt (siehe Schritt 8 oben,
        # StrategyRegistry.get_active()) - ein neu subscribed-tes Symbol
        # loest fuer sich allein noch keinen Trade aus.
        from sgr.market_data.asset_universe import AssetUniverseEngine

        binance_adapter = None
        try:
            binance_adapter = pool.get(ExchangeID.BINANCE, config.trading_mode)
        except KeyError:
            log.info("asset_universe.binance_adapter_unavailable")

        async def _on_asset_universe_discovery(subscribed: dict[ExchangeID, set[str]]) -> None:
            # binance_symbols kommt aus AssetUniverseEngine/MarketInfo.symbol
            # in der settle-freien kanonischen Form ("PONKE/USDT", siehe
            # Symbol.ccxt_symbol - dieselbe Form, die auch das Grafana
            # $symbol-Filter und alle Position-Metriken nutzen).
            #
            # BUG (live am Server gefunden, Autonomous-Paper-Trading-
            # Rollout): ccxt laedt fuer Binance USDT-M Perpetuals einen
            # EIGENEN Markt-Key MIT Settle-Suffix ("PONKE/USDT:USDT"). Fuer
            # Coins ohne Spot-Listing existiert die settle-freie Form
            # ueberhaupt nicht -> reconcile_subscriptions() schlaegt fehl
            # ("does not have market symbol"). Fuer Coins MIT zusaetzlichem
            # Spot-Listing (z.B. ACH, ZEC) existiert die settle-freie Form
            # SEHR WOHL, referenziert dann aber den SPOT-Markt statt des
            # beabsichtigten Perpetual-Markts - ein stiller Wechsel auf
            # falsche (Spot- statt Future-)Preisdaten fuer ein als "swap"
            # klassifiziertes Symbol. Empirisch verifiziert per
            # ccxt.binance({'options': {'defaultType': 'future'}}).markets:
            # "ACH/USDT" UND "ACH/USDT:USDT" existieren beide parallel.
            #
            # Fix: exakt dieselbe volle ccxt-Futures-Form wie im
            # urspruenglichen LIVE_MARKET_DATA_SYMBOLS (":USDT"-Suffix)
            # herstellen, BEVOR an reconcile_subscriptions() uebergeben
            # wird. Sicher, weil AssetUniverseEngine's Binance-Zweig
            # ausschliesslich USDT-quotierte "swap"-Maerkte klassifiziert
            # (siehe classify_asset_status: quote_asset != "USDT" und
            # market_type != expected_market_type scheitern beide vorher).
            # MarketInfo.symbol/das Grafana $symbol-Label bleiben bewusst
            # UNVERAENDERT in der settle-freien Form - nur der tatsaechliche
            # Exchange-Aufruf braucht die volle Form.
            binance_symbols = subscribed.get(ExchangeID.BINANCE, set())
            target = {(f"{symbol}:USDT", "1h") for symbol in binance_symbols}
            # Bestehende Zusatz-Subscription (BTC/USDT auf 4h) erhalten,
            # siehe urspruengliche LIVE_MARKET_DATA_SYMBOLS-Subscription-
            # Schleife weiter oben in dieser Funktion.
            if "BTC/USDT" in binance_symbols:
                target.add(("BTC/USDT:USDT", "4h"))
            await md_engine.reconcile_subscriptions(target, ExchangeID.BINANCE)

        asset_universe_engine = AssetUniverseEngine(
            binance_adapter=binance_adapter,
            strategy_registry=registry,
            subscribed_symbols={
                # LIVE_MARKET_DATA_SYMBOLS nutzt ccxt's Futures-Symbolform
                # ("BTC/USDT:USDT") - MarketInfo.symbol ist die
                # settle-freie kanonische Form ("BTC/USDT", siehe
                # Symbol.ccxt_symbol) - hier normalisiert, damit der
                # SUBSCRIBED-Abgleich tatsaechlich greift.
                ExchangeID.BINANCE: {s.split(":")[0] for s in LIVE_MARKET_DATA_SYMBOLS},
            },
            on_discovery=_on_asset_universe_discovery,
        )
        await asset_universe_engine.start()
        app.state.asset_universe_engine = asset_universe_engine

    log.info(
        "sgr.api.ready",
        role=role,
        tenant_id=config.tenant_id,
        host=config.api.host,
        port=config.api.port,
    )

    yield

    # --------------- Shutdown ---------------
    log.info("sgr.api.shutting_down", role=role)

    if role == "worker":
        await risk_engine.stop_kill_switch_remote_sync()
        await protection_watchdog.stop()
        await asset_universe_engine.stop()
        await worker_metrics_publisher.stop()
        await monitoring_engine.stop()
        await strategy_engine.stop()
        if md_engine.is_running:
            await md_engine.stop()
        # Baustein 7 (Shutdown Safety): noch offene/im Fill-Monitoring
        # befindliche Orders best-effort cancelln, BEVOR die
        # Exchange-Verbindungen geschlossen werden - danach waere kein
        # Cancel mehr moeglich.
        await execution_engine.shutdown()
        await pool.close_all()

    await feature_store.close()
    await bus.close()

    await close_db()

    log.info("sgr.api.stopped", role=role)


# ---------------------------------------------------------------------------
# App Factory
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _api_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Wrapper um lifespan(), der role="api" fest verdrahtet.

    FastAPI ruft den lifespan-Contextmanager immer mit genau einem
    Positionsargument (der App-Instanz) auf - ein zusaetzlicher Parameter
    wie role kann daher nicht direkt als lifespan= uebergeben werden.
    sgr-worker/main.py ruft lifespan() dagegen direkt mit role="worker"
    auf (siehe dort), ohne diesen Wrapper.
    """
    async with lifespan(app, role="api"):
        yield


def create_app() -> FastAPI:
    config = get_config()

    app = FastAPI(
        title="Project SGR",
        description="Institutional AI-powered Multi-Asset Trading System",
        version=config.version,
        docs_url="/docs" if config.environment != Environment.PRODUCTION else None,
        redoc_url="/redoc" if config.environment != Environment.PRODUCTION else None,
        lifespan=_api_lifespan,
    )

    app.state = AppState()  # type: ignore[assignment]

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.api.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request ID Middleware
    @app.middleware("http")
    async def add_request_id(request: Request, call_next: Any) -> Response:
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    # Request Timing
    @app.middleware("http")
    async def add_timing(request: Request, call_next: Any) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000
        response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"
        return response

    # Global Exception Handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        log.error(
            "api.unhandled_exception",
            path=request.url.path,
            method=request.method,
            error=str(exc),
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error",
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    # Mount Routers
    from sgr.api.routers import (
        health,
        market,
        orders,
        portfolio,
        reconciliation,
        risk,
        strategy,
        strategy_validation,
        system,
        trading,
        websocket,
    )
    from sgr.saas.routers import apikey_router, auth_router, billing_router

    app.include_router(health.router, tags=["health"])
    app.include_router(market.router, prefix="/api/v1/market", tags=["market"])
    app.include_router(portfolio.router, prefix="/api/v1/portfolio", tags=["portfolio"])
    app.include_router(risk.router, prefix="/api/v1/risk", tags=["risk"])
    app.include_router(strategy.router, prefix="/api/v1/strategy", tags=["strategy"])
    app.include_router(
        strategy_validation.router,
        prefix="/api/v1/strategy-validation",
        tags=["strategy-validation"],
    )
    app.include_router(orders.router, prefix="/api/v1/orders", tags=["orders"])
    app.include_router(system.router, prefix="/api/v1/system", tags=["system"])
    app.include_router(trading.router, prefix="/api/v1/trading", tags=["trading"])
    app.include_router(
        reconciliation.router, prefix="/api/v1/reconciliation", tags=["reconciliation"]
    )
    app.include_router(websocket.router, prefix="/ws", tags=["websocket"])

    # SaaS Layer
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(apikey_router, prefix="/api/v1")
    app.include_router(billing_router, prefix="/api/v1")

    # Prometheus Metrics Endpoint
    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Any:
        """
        Prometheus metrics endpoint.

        Kombiniert die lokale REGISTRY dieses Prozesses (in role="api"
        praktisch leer, da die API seit der Rollentrennung keine
        Trading-Engines mehr besitzt) mit den per Redis gepushten
        Snapshots aller sgr-worker-Prozesse (siehe sgr/monitoring/
        worker_metrics_bridge.py) - Prometheus muss dadurch nur diesen
        einen Endpunkt scrapen, obwohl die eigentlichen Trading-
        Metriken im Worker-Prozess entstehen, der selbst keinen HTTP-
        Port besitzt.
        """
        from prometheus_client import REGISTRY, generate_latest

        body = generate_latest(REGISTRY)

        redis_client = getattr(app.state.feature_store, "redis_client", None)
        if redis_client is not None:
            from sgr.monitoring.worker_metrics_bridge import collect_worker_metrics

            worker_body = await collect_worker_metrics(redis_client)
            if worker_body:
                body = body + b"\n" + worker_body

        return Response(body, media_type="text/plain; charset=utf-8")

    return app


# Singleton App-Instanz
app = create_app()
