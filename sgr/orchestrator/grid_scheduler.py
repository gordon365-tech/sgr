"""
SGR Grid Scheduler
===================
Bisher fehlendes Live/Paper-Wiring fuer Futures Grid (Phase D,
Architekturbericht "Futures-Grid-Strategie fuer SGR" - dokumentierter
offener Punkt seit docs/SGR_STRATEGY_REPORT.md Abschnitt 12, Punkt 1:
"Kein automatischer Live-Grid-Scheduler. is_active=True fuer eine
Grid-Strategie... startet KEIN automatisches Trading").

Getrennt von der direktionalen StrategyEngine/TradingOrchestrator (siehe
sgr/strategy/futures_grid.py Modul-Docstring: GridTradingStrategy.
evaluate() ist ein eigenes Protokoll, generate_signal() bleibt fuer
Grid-Strategien ein bewusster No-Op) - der GridScheduler ist das
Grid-Aequivalent zu TradingOrchestrator, ruft aber niemals
StrategyEngine.process() auf und wird von ihr nicht aufgerufen
(Null-Interferenz in beide Richtungen, siehe GATE 15).

Sicherheits-Prinzip (explizite Anweisung: "Der Scheduler darf nicht
einfach alle vorhandenen Grid-Strategien automatisch aktivieren"):
    1. Nur get_active_grid_strategies() (StrategyRegistry.is_active) -
       identische Aktivierungs-/Validierungslogik wie jede direktionale
       Strategie, keine Extra-Freigabe hier.
    2. ZUSAETZLICHER, expliziter Opt-in ueber die Umgebungsvariable
       GRID_SCHEDULER_ENABLED (Default: deaktiviert) - identisches
       Muster wie PAPER_STRESS_AUTO_RECOVERY_ENABLED (siehe
       sgr/api/main.py). Ohne dieses Flag ist der Scheduler
       konstruierbar (und Recovery/Wiring-Code lauffaehig getestet),
       aber JEDER on_candle_event()-Aufruf ist ein sofortiger No-Op -
       GARANTIERT kein automatisches Grid-Trading fuer Gordon/Sumo durch
       diese Aenderung allein, unabhaengig vom is_active-Status
       irgendeiner Grid-Strategie in der Registry.
    3. Symbol-Kill-Switch, Compliance, GridRiskEngine werden - wie bei
       jedem direktionalen Signal auch - NICHT umgangen (siehe
       GridController.open_grid()) - dieser Scheduler prueft den
       Symbol-Kill-Switch zusaetzlich VOR jeder Arbeit, ist aber nicht
       der einzige Schutz (GridController selbst prueft erneut).
    4. Ohne einen injizierten account_eligibility_provider wird KEIN
       Grid eroeffnet (fail-closed, keine erfundene Account-Freigabe) -
       siehe _resolve_account_eligibility().

Idempotenz: hoechstens EIN aktives Grid pro (Symbol, Strategie)
gleichzeitig - verhindert, dass zwei aufeinanderfolgende CandleEvents
fuer dasselbe Symbol ein zweites, ueberlappendes Grid eroeffnen. Bereits
aktive Grids fuer ein Symbol erhalten stattdessen den naechsten
Preis-Tick (on_price_tick()) statt eines neuen open_grid()-Versuchs.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from sgr.core.grid_types import GridDecision
from sgr.core.logging import get_logger
from sgr.core.types import GridDirection, MarketRegime
from sgr.market_data.types import MarketContext
from sgr.risk.grid_risk import GridPortfolioSnapshot
from sgr.risk.symbol_kill_switch import get_symbol_kill_switch
from sgr.strategy.futures_grid import get_active_grid_strategies

log = get_logger(__name__)

_ENV_FLAG = "GRID_SCHEDULER_ENABLED"


class GridScheduler:
    """
    Usage (siehe sgr/api/main.py):
        scheduler = GridScheduler(grid_controller, feature_store, trading_mode, tenant_id)
        bus.subscribe(
            CandleEvent, scheduler.on_candle_event,
            consumer_group=f"grid_scheduler:{tenant}", ...
        )
    """

    def __init__(
        self,
        grid_controller: Any,
        feature_store: Any,
        trading_mode: Any,
        tenant_id: str | None = None,
        account_eligibility_provider: Any = None,
        enabled: bool | None = None,
        portfolio_engine: Any = None,
    ) -> None:
        self._grid_controller = grid_controller
        self._feature_store = feature_store
        self._trading_mode = trading_mode
        self._tenant_id = tenant_id
        self._account_provider = account_eligibility_provider
        self._enabled = (
            enabled if enabled is not None else os.environ.get(_ENV_FLAG, "").lower() == "true"
        )
        self._open_grid_keys: set[str] = set()
        # Phase 9 (2026-09-24): optional, additiv - None (Default) fuehrt
        # dazu, dass _directional_exposure_usd() None liefert ("nicht
        # bestimmbar"), was GridRiskEngine bei aktiviertem
        # max_combined_exposure_usd fail-closed ablehnt (siehe dortigen
        # Docstring) - kein stiller Fallback auf 0.
        self._portfolio_engine = portfolio_engine

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _directional_exposure_usd(self) -> Any:
        """
        Summe der absoluten Notional-Exposure aller aktuell offenen
        direktionalen Positionen dieses Tenants (PortfolioEngine.positions,
        siehe Position.notional_value). None = nicht bestimmbar (kein
        portfolio_engine injiziert, oder ein Fehler beim Zugriff) - wird
        vom Aufrufer NIEMALS als 0 interpretiert (siehe GridRiskEngine.
        evaluate_new_grid() Docstring, fail-closed).
        """
        from decimal import Decimal

        if self._portfolio_engine is None:
            return None
        try:
            positions = list(self._portfolio_engine.positions)
        except Exception as e:
            log.warning("grid_scheduler.directional_exposure_lookup_failed", error=str(e))
            return None
        return sum((abs(p.notional_value) for p in positions), Decimal("0"))

    async def on_candle_event(self, event: Any) -> None:
        if not self._enabled:
            return
        try:
            candle = event.candle
        except Exception as e:
            log.error("grid_scheduler.on_candle_event_bad_event", error=str(e))
            return

        symbol = candle.symbol
        symbol_key = f"{symbol.exchange.value}:{symbol.ccxt_symbol}"

        if not get_symbol_kill_switch(tenant_id=self._tenant_id).is_active(symbol_key):
            return

        try:
            await self._process_symbol(candle, symbol, symbol_key)
        except Exception as e:
            log.error(
                "grid_scheduler.on_candle_event_failed",
                symbol_key=symbol_key,
                error=str(e),
                exc_info=True,
            )

    async def _process_symbol(self, candle: Any, symbol: Any, symbol_key: str) -> None:
        # 1. Bereits aktives Grid fuer dieses Symbol -> nur Preis-Tick,
        # niemals ein zweites Grid eroeffnen (Idempotenz).
        for grid in self._grid_controller.active_grids():
            if f"{grid.exchange.value}:{grid.symbol.ccxt_symbol}" == symbol_key:
                await self._grid_controller.on_price_tick(str(grid.id), candle.close)
                return

        # 2. Kein aktives Grid -> aktive Grid-Strategien evaluieren
        strategies = get_active_grid_strategies()
        if not strategies:
            return

        features = await self._feature_store.get_latest(symbol_key, candle.timeframe)
        if features is None:
            return
        regime = (
            await self._feature_store.get_latest_regime(symbol_key, candle.timeframe)
            or MarketRegime.UNKNOWN
        )
        features_with_regime = features.model_copy(update={"regime": regime})
        context = MarketContext(
            symbol=symbol,
            timestamp=candle.timestamp,
            primary=features_with_regime,
            regime=regime,
        )

        for strategy in strategies:
            open_key = f"{symbol_key}:{strategy.name}"
            if open_key in self._open_grid_keys:
                continue

            decision: GridDecision = strategy.evaluate(context)
            if decision.direction == GridDirection.NEUTRAL or decision.parameters is None:
                continue

            account = await self._resolve_account_eligibility()
            if account is None:
                log.warning(
                    "grid_scheduler.no_account_eligibility_provider_configured",
                    symbol_key=symbol_key,
                    strategy=strategy.name,
                )
                return

            snapshot = GridPortfolioSnapshot(
                open_grids=self._grid_controller.active_grids(),
                portfolio_value=Decimal("0"),
            )
            result = await self._grid_controller.open_grid(
                decision,
                symbol,
                strategy.name,
                account,
                snapshot,
                candle.close,
                directional_exposure_usd=self._directional_exposure_usd(),
            )
            if result.approved:
                self._open_grid_keys.add(open_key)
                log.info(
                    "grid_scheduler.grid_opened",
                    symbol_key=symbol_key,
                    strategy=strategy.name,
                    grid_id=str(result.grid.id) if result.grid else None,
                )
            else:
                log.info(
                    "grid_scheduler.grid_open_rejected",
                    symbol_key=symbol_key,
                    strategy=strategy.name,
                    reason=result.reason,
                )
            return  # ein Oeffnungsversuch pro Symbol/Tick reicht

    async def _resolve_account_eligibility(self) -> Any:
        if self._account_provider is None:
            return None
        if callable(self._account_provider):
            resolved = self._account_provider()
            if hasattr(resolved, "__await__"):
                return await resolved
            return resolved
        return self._account_provider
