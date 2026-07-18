"""
SGR Strategy Engine
===================
Orchestriert Signal-Generierung über alle aktiven Strategien.

Verantwortlichkeiten:
    1. Aktive Strategien für aktuelles Regime abrufen (Registry)
    2. MarketContext aus FeatureStore zusammenbauen
    3. Jeden aktiven Strategie-Plugin aufrufen
    4. Signale aggregieren (Konfidenz-gewichtet)
    5. Bestes Signal an Risk Engine weiterleiten
    6. SignalEvent auf Event Bus publizieren

Signal-Aggregation:
    Mehrere Strategien können dasselbe Symbol handeln.
    Konsensus-Logik:
        - Mehrere LONG Signale → stärkstes (höchste Konfidenz) gewinnt
        - LONG + SHORT Signale → neutral (kein Trade)
        - Ein Signal mit Konfidenz > 0.80 → direkt weiterleiten

Entkopplung:
    Strategy Engine kennt weder Risk Engine noch Execution Engine.
    Sie publiziert nur SignalEvent auf den Event Bus.
    Risk Engine subscribed auf SignalEvent und reagiert.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sgr.core.event_bus import get_event_bus
from sgr.core.logging import get_logger
from sgr.core.types import (
    MarketRegime,
    Signal,
    SignalDirection,
    SignalEvent,
    TradingMode,
)
from sgr.market_data.feature_store import FeatureStore
from sgr.market_data.types import MarketContext
from sgr.strategy.registry import StrategyRegistry

log = get_logger(__name__)

# Minimale Konfidenz für Signal-Output
_MIN_SIGNAL_CONFIDENCE = 0.50


class StrategyEngine:
    """
    Haupt-Strategy-Engine.

    Lifecycle:
        engine = StrategyEngine(TradingMode.PAPER, feature_store)
        await engine.start()
        # Engine läuft im Hintergrund, subscribed auf CandleEvents
        await engine.stop()
    """

    def __init__(
        self,
        trading_mode: TradingMode,
        feature_store: FeatureStore,
        registry: StrategyRegistry | None = None,
    ) -> None:
        self._trading_mode = trading_mode
        self._feature_store = feature_store
        self._registry = registry or StrategyRegistry.get()
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """Startet Event-Listener für CandleEvents."""
        self._running = True
        log.info(
            "strategy_engine.started",
            trading_mode=self._trading_mode.value,
            active_strategies=len(self._registry.get_active()),
        )

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        log.info("strategy_engine.stopped")

    # ------------------------------------------------------------------
    # Main Entry Point
    # ------------------------------------------------------------------

    async def process(
        self,
        symbol_key: str,
        timeframe: str,
        regime: MarketRegime = MarketRegime.UNKNOWN,
    ) -> Signal | None:
        """
        Verarbeitet neuen Candle: Features laden → Strategien auswerten → Signal.

        Args:
            symbol_key: z.B. "binance:BTC/USDT"
            timeframe:  z.B. "1h"
            regime:     Aktuelles Marktregime (von ML Regime Detector)

        Returns:
            Bestes Signal oder None.
        """
        # 1. Features aus Store laden
        features = await self._feature_store.get_latest(symbol_key, timeframe)
        if features is None:
            log.debug(
                "strategy_engine.no_features",
                symbol_key=symbol_key,
                timeframe=timeframe,
            )
            return None

        # 2. MarketContext aufbauen
        features_with_regime = features.model_copy(update={"regime": regime})
        context = MarketContext(
            symbol=features.symbol,
            timestamp=features.timestamp,
            primary=features_with_regime,
            regime=regime,
        )

        # 3. Aktive Strategien für dieses Regime
        active = self._registry.get_active(regime=regime)
        if not active:
            log.debug(
                "strategy_engine.no_active_strategies",
                regime=regime.value,
            )
            return None

        # 4. Alle Strategien synchron auswerten (pure functions, kein I/O)
        signals: list[Signal] = []
        for strategy in active:
            try:
                signal = strategy.generate_signal(context)
                if signal and signal.confidence >= _MIN_SIGNAL_CONFIDENCE:
                    signals.append(signal)
                    log.debug(
                        "strategy_engine.signal_generated",
                        strategy=strategy.name,
                        direction=signal.direction.value,
                        confidence=f"{signal.confidence:.2%}",
                    )
            except Exception as e:
                log.error(
                    "strategy_engine.strategy_error",
                    strategy=strategy.name,
                    error=str(e),
                    exc_info=True,
                )

        if not signals:
            return None

        # 5. Signal-Aggregation
        best = self._aggregate(signals)
        if best is None:
            return None

        # 6. Event Bus
        await self._publish(best)

        log.info(
            "strategy_engine.signal_published",
            symbol=str(best.symbol),
            direction=best.direction.value,
            confidence=f"{best.confidence:.2%}",
            strategy=best.strategy_name,
            regime=regime.value,
        )

        return best

    # ------------------------------------------------------------------
    # Signal Aggregation
    # ------------------------------------------------------------------

    def _aggregate(self, signals: list[Signal]) -> Signal | None:
        """
        Aggregiert mehrere Signale zu einem.

        Regeln:
        - Widersprüchliche Richtungen (LONG + SHORT) → kein Signal
        - Einstimmig: stärkstes Signal (höchste Konfidenz) gewinnt
        - Konfidenz-Boost bei Konsensus (alle zeigen dieselbe Richtung)
        """
        if not signals:
            return None

        # Richtungen zählen
        longs = [s for s in signals if s.direction == SignalDirection.LONG]
        shorts = [s for s in signals if s.direction == SignalDirection.SHORT]
        closes = [s for s in signals if s.direction == SignalDirection.CLOSE]

        # Widerspruch → kein Signal
        if longs and shorts:
            log.debug(
                "strategy_engine.conflicting_signals",
                long_count=len(longs),
                short_count=len(shorts),
            )
            return None

        # Konsensus-Richtung bestimmen
        candidates = longs or shorts or closes
        if not candidates:
            return None

        # Bestes Signal (höchste Konfidenz)
        best = max(candidates, key=lambda s: s.confidence)

        # Konsensus-Boost: mehrere Strategien stimmen überein
        if len(candidates) > 1:
            avg_conf = sum(s.confidence for s in candidates) / len(candidates)
            boosted = min(best.confidence * 1.1 + avg_conf * 0.1, 1.0)
            # Neues Signal mit boost (Pydantic frozen → model_copy)
            best = best.model_copy(update={"confidence": boosted})

        return best

    async def _publish(self, signal: Signal) -> None:
        """Publiziert Signal auf Event Bus."""
        try:
            event = SignalEvent(
                timestamp=datetime.now(tz=UTC),
                signal=signal,
            )
            bus = get_event_bus()
            await bus.publish(event)
        except Exception as e:
            log.error("strategy_engine.publish_failed", error=str(e))
