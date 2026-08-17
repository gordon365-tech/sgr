"""
SGR Trading Orchestrator
=========================
Koordiniert den vollständigen Trading-Zyklus von Signal bis Portfolio-Update.

Warum dieses Modul nötig war:
    Vor Einführung dieses Orchestrators publizierten StrategyEngine
    (SignalEvent) und ExecutionEngine (OrderFilledEvent) Events auf den
    Event Bus, auf die jedoch nirgends im System subscribed wurde.
    RiskEngine.evaluate() und RiskEngine.build_order_request() existierten
    vollständig implementiert, wurden aber nie aufgerufen.
    ExecutionEngine wurde nirgends instanziiert. PortfolioEngine.on_order_filled()
    wurde nie erreicht. Es gab keinen Pfad, auf dem ein Signal jemals zu
    einer Order werden konnte.

Architekturentscheidung: Hybrid statt rein event-driven
    Der Zyklus läuft über direkte, awaited Methodenaufrufe
    (Strategy → Risk → Execution → Portfolio). Das macht den Ablauf
    deterministisch, synchron nachvollziehbar und ohne Redis testbar.
    Der Event Bus wird zusätzlich (additiv) für Audit/Monitoring bedient:
    RiskApprovedEvent, RiskRejectedEvent, TradingCycleCompletedEvent,
    TradingCycleFailedEvent. Bus-Fehler dürfen laut Projektgrundsatz
    ("Fail-safe: DB/Bus-Fehler dürfen Trading-Ergebnisse nie beeinflussen")
    niemals den eigentlichen Trading-Ausgang verändern – jede Publish-Stelle
    ist deshalb einzeln try/except-isoliert.

    Damit bleibt exakt eine Quelle der Wahrheit für den Kontrollfluss
    (keine parallele State Machine über Events), während das bereits
    vorhandene Event-Bus-Setup weiterhin für Beobachtbarkeit genutzt wird.

Zyklus:
    StrategyEngine.process()        → Signal | None
    RiskEngine.evaluate()           → RiskAssessment
    RiskEngine.build_order_request()→ OrderRequest (nur bei APPROVED/REDUCED)
    ExecutionEngine.execute()       → OrderResult
    PortfolioEngine.on_order_filled()  (nur bei OrderStatus.FILLED)

Kein Schritt überspringt Risk Management. Kein Signal erreicht Execution
ohne RiskEngine.evaluate(). Die Reihenfolge ist fix und kann nicht durch
Konfiguration umgangen werden.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sgr.core.event_bus import get_event_bus
from sgr.core.logging import get_logger
from sgr.core.types import (
    MarketRegime,
    OrderStatus,
    RiskApprovedEvent,
    RiskAssessment,
    RiskDecision,
    RiskRejectedEvent,
    Signal,
    TradingCycleCompletedEvent,
    TradingCycleFailedEvent,
    TradingCycleResult,
    TradingCycleStatus,
    TradingMode,
)

log = get_logger(__name__)


class TradingOrchestrator:
    """
    Koordiniert einen vollständigen Trading-Zyklus für ein Symbol/Timeframe.

    Usage:
        orchestrator = TradingOrchestrator(
            strategy_engine=strategy_engine,
            risk_engine=risk_engine,
            execution_engine=execution_engine,
            portfolio_engine=portfolio_engine,
            feature_store=feature_store,
            trading_mode=TradingMode.PAPER,
        )
        result = await orchestrator.run_cycle("pionex:BTC/USDT", "1h")
    """

    def __init__(
        self,
        strategy_engine: Any,
        risk_engine: Any,
        execution_engine: Any,
        portfolio_engine: Any,
        feature_store: Any,
        trading_mode: TradingMode,
    ) -> None:
        self._strategy_engine = strategy_engine
        self._risk_engine = risk_engine
        self._execution_engine = execution_engine
        self._portfolio_engine = portfolio_engine
        self._feature_store = feature_store
        self._trading_mode = trading_mode

    # ------------------------------------------------------------------
    # Main Entry Point
    # ------------------------------------------------------------------

    async def run_cycle(
        self,
        symbol_key: str,
        timeframe: str,
        regime: MarketRegime = MarketRegime.UNKNOWN,
    ) -> TradingCycleResult:
        """
        Führt einen vollständigen Trading-Zyklus aus.
        Fail-Safe: jede unerwartete Exception → TradingCycleStatus.FAILED,
        niemals eine unbehandelte Exception nach außen.
        """
        started_at = datetime.now(tz=UTC)
        try:
            result = await self._run_cycle_internal(symbol_key, timeframe, regime, started_at)
        except Exception as e:
            log.error(
                "orchestrator.cycle.unexpected_error",
                symbol_key=symbol_key,
                timeframe=timeframe,
                error=str(e),
                exc_info=True,
            )
            result = TradingCycleResult(
                started_at=started_at,
                completed_at=datetime.now(tz=UTC),
                status=TradingCycleStatus.FAILED,
                symbol_key=symbol_key,
                timeframe=timeframe,
                error=f"Orchestrator error: {e}",
            )
            await self._publish_cycle_failed(symbol_key, timeframe, str(e))
            return result

        await self._publish_cycle_completed(result)
        return result

    async def _run_cycle_internal(
        self,
        symbol_key: str,
        timeframe: str,
        regime: MarketRegime,
        started_at: datetime,
    ) -> TradingCycleResult:
        # 1. Signal generation (StrategyEngine publiziert SignalEvent bereits
        #    selbst additiv - siehe strategy/engine.py _publish())
        signal = await self._strategy_engine.process(symbol_key, timeframe, regime)
        if signal is None:
            return self._result(
                TradingCycleStatus.NO_SIGNAL, started_at, symbol_key, timeframe
            )

        # 2. Aktuelle Marktdaten für Risk Engine (Preis + ATR)
        features = await self._feature_store.get_latest(symbol_key, timeframe)
        if features is None:
            # Race Condition: Signal wurde aus Features gebaut, die zwischen
            # StrategyEngine.process() und hier invalidiert wurden.
            # Fail-safe: kein Trade ohne verifizierten aktuellen Preis.
            log.warning(
                "orchestrator.cycle.features_unavailable",
                symbol_key=symbol_key,
                timeframe=timeframe,
                signal_id=str(signal.id),
            )
            return self._result(
                TradingCycleStatus.FAILED,
                started_at,
                symbol_key,
                timeframe,
                signal=signal,
                error="Market features unavailable after signal generation",
            )

        current_price = features.close
        atr = features.indicators.atr_14

        # 3. Portfolio State laden (Single Source of Truth: PortfolioEngine)
        open_positions = self._portfolio_engine.positions
        portfolio_value = self._portfolio_engine.portfolio_value
        available_capital = self._portfolio_engine.cash

        # 4. Risk Engine (fail-safe intern: Fehler → REJECTED)
        assessment = await self._risk_engine.evaluate(
            signal=signal,
            open_positions=open_positions,
            portfolio_value=portfolio_value,
            available_capital=available_capital,
            current_price=current_price,
            atr=atr,
        )

        if assessment.decision == RiskDecision.REJECTED:
            await self._publish_risk_rejected(assessment)
            return self._result(
                TradingCycleStatus.RISK_REJECTED,
                started_at,
                symbol_key,
                timeframe,
                signal=signal,
                assessment=assessment,
            )

        # 5. Order Request bauen (nur erreichbar bei APPROVED/REDUCED)
        order_request = self._risk_engine.build_order_request(
            signal, assessment, current_price=current_price
        )

        await self._publish_risk_approved(assessment, order_request)

        # 6. Execution (fail-safe intern: Fehler → REJECTED OrderResult)
        order_result = await self._execution_engine.execute(order_request)

        # 7. Portfolio State aktualisieren (deterministisch, direkter Aufruf -
        #    NICHT über Event Bus, um Doppelverarbeitung/Race auszuschließen)
        if order_result.status == OrderStatus.FILLED:
            await self._portfolio_engine.on_order_filled(order_result)
            status = TradingCycleStatus.ORDER_FILLED
        else:
            status = TradingCycleStatus.ORDER_NOT_FILLED

        return self._result(
            status,
            started_at,
            symbol_key,
            timeframe,
            signal=signal,
            assessment=assessment,
            order_request=order_request,
            order_result=order_result,
        )

    # ------------------------------------------------------------------
    # Event-Driven Trigger (additiv, optional)
    # ------------------------------------------------------------------

    async def on_candle_event(self, event: Any) -> None:
        """
        Handler für CandleEvent-Subscription auf dem Event Bus.
        Ermöglicht automatisches Auslösen eines Zyklus bei neuen Candles,
        ohne dass run_cycle() direkt vom Aufrufer für jeden Candle
        aufgerufen werden muss. Fail-safe: Fehler hier dürfen die
        Candle-Verarbeitung im Market Data Engine nicht stören.
        """
        try:
            candle = event.candle
            symbol_key = f"{candle.symbol.exchange.value}:{candle.symbol.ccxt_symbol}"
            await self.run_cycle(symbol_key, candle.timeframe)
        except Exception as e:
            log.error("orchestrator.on_candle_event.error", error=str(e), exc_info=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _result(
        self,
        status: TradingCycleStatus,
        started_at: datetime,
        symbol_key: str,
        timeframe: str,
        signal: Signal | None = None,
        assessment: RiskAssessment | None = None,
        order_request: Any = None,
        order_result: Any = None,
        error: str | None = None,
    ) -> TradingCycleResult:
        return TradingCycleResult(
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            status=status,
            symbol_key=symbol_key,
            timeframe=timeframe,
            signal=signal,
            assessment=assessment,
            order_request=order_request,
            order_result=order_result,
            error=error,
        )

    async def _publish_risk_approved(self, assessment: RiskAssessment, order_request: Any) -> None:
        try:
            event = RiskApprovedEvent(
                timestamp=datetime.now(tz=UTC),
                assessment=assessment,
                order_request=order_request,
            )
            await get_event_bus().publish(event)
        except Exception as e:
            log.error("orchestrator.publish_risk_approved_failed", error=str(e))

    async def _publish_risk_rejected(self, assessment: RiskAssessment) -> None:
        try:
            event = RiskRejectedEvent(
                timestamp=datetime.now(tz=UTC),
                assessment=assessment,
            )
            await get_event_bus().publish(event)
        except Exception as e:
            log.error("orchestrator.publish_risk_rejected_failed", error=str(e))

    async def _publish_cycle_completed(self, result: TradingCycleResult) -> None:
        try:
            event = TradingCycleCompletedEvent(
                timestamp=datetime.now(tz=UTC),
                result=result,
            )
            await get_event_bus().publish(event)
        except Exception as e:
            log.error("orchestrator.publish_cycle_completed_failed", error=str(e))

    async def _publish_cycle_failed(self, symbol_key: str, timeframe: str, error: str) -> None:
        try:
            event = TradingCycleFailedEvent(
                timestamp=datetime.now(tz=UTC),
                symbol_key=symbol_key,
                timeframe=timeframe,
                error=error,
            )
            await get_event_bus().publish(event)
        except Exception as e:
            log.error("orchestrator.publish_cycle_failed_failed", error=str(e))
