"""
SGR Strategy Engine – Protocol & Base Types
============================================
Definiert den Vertrag für alle Strategien im Plugin-System.

Design-Prinzipien:
- Protocol (Duck Typing): keine Vererbung erzwungen
- Strategien sind zustandslos: kein interner Mutable-State
  (State wird außerhalb gespeichert – DB, Redis)
- Strategien kennen nur Features, nie rohe Candles
- Strategien entscheiden nur Richtung + Konfidenz, nie Positionsgröße
  (Sizing ist Aufgabe der Risk Engine)
- Jede Strategie deklariert welche Regime sie unterstützt
  → Strategy Selector aktiviert nur passende Strategien

Plugin-System:
    @StrategyRegistry.register
    class MyStrategy:
        ...

Validierungspfad (obligatorisch):
    Backtest → Walk-Forward → Paper (4 Wochen) → Live
    Status wird in DB gespeichert (StrategyModel).
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from sgr.core.types import MarketRegime, Signal, SignalDirection
from sgr.market_data.types import MarketContext


@dataclass(frozen=True)
class StrategyParameters:
    """
    Validierte Strategie-Parameter.
    Wird von der Strategie selbst definiert und validiert.
    Ermöglicht Parameter-Robustness-Tests (±20% Variation).
    """

    name: str
    version: str
    params: dict[str, Any] = field(default_factory=dict)

    def with_variation(self, factor: float) -> StrategyParameters:
        """Erstellt Parameter-Variante für Robustness-Tests."""
        varied = {}
        for k, v in self.params.items():
            if isinstance(v, (int, float)):
                varied[k] = type(v)(v * factor)
            else:
                varied[k] = v
        return StrategyParameters(
            name=self.name,
            version=self.version,
            params=varied,
        )


@dataclass(frozen=True)
class StrategyPerformance:
    """
    Performance-Metriken einer Strategie.
    Wird vom Learning Loop berechnet und in DB gespeichert.
    """

    strategy_name: str
    period_days: int
    total_trades: int
    win_rate: float
    profit_factor: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float
    cagr: float
    hit_rate: float
    expected_value: float  # avg PnL per trade
    computed_at: datetime

    @property
    def is_acceptable(self) -> bool:
        """Strategie erfüllt Mindestanforderungen für Live-Trading."""
        return (
            self.sharpe_ratio >= 1.0
            and self.profit_factor >= 1.3
            and self.max_drawdown <= 0.20
            and self.hit_rate >= 0.40
            and self.total_trades >= 30  # Statistische Signifikanz
        )

    @property
    def should_deactivate(self) -> bool:
        """Strategie unterschreitet Monitoring-Limits."""
        return self.sharpe_ratio < 0.5 or self.hit_rate < 0.35 or self.profit_factor < 1.0


@dataclass(frozen=True)
class ValidationStatus:
    """Aktueller Validierungsstatus einer Strategie."""

    backtest_passed: bool = False
    walk_forward_passed: bool = False
    paper_trading_passed: bool = False
    live_approved: bool = False
    notes: str = ""

    @property
    def can_go_live(self) -> bool:
        return self.backtest_passed and self.walk_forward_passed and self.paper_trading_passed


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class TradingStrategy(Protocol):
    """
    Contract für alle SGR-Strategien.

    Implementierung:
        class MyStrategy:
            name = "my_strategy"
            version = "1.0.0"
            supported_regimes = [MarketRegime.TRENDING_UP]

            def generate_signal(self, context: MarketContext) -> Signal | None:
                ...

    Keine Vererbung nötig – Duck Typing via Protocol.
    """

    name: str
    version: str
    supported_regimes: list[MarketRegime]

    @abstractmethod
    def generate_signal(self, context: MarketContext) -> Signal | None:
        """
        Generiert Trading Signal aus MarketContext.

        Args:
            context: Vollständiger Marktkontext (Features, Regime, Sentiment)

        Returns:
            Signal wenn Einstieg sinnvoll, None wenn kein Trade.
            Kein Signal ≠ Fehler. Strategien passen oft.

        DARF NICHT:
            - I/O machen (kein async)
            - State mutieren
            - Positionsgröße bestimmen (nur Richtung + Konfidenz)
            - Entscheiden ob Position offen ist (das macht Strategy Engine)
        """
        ...

    @abstractmethod
    def get_parameters(self) -> StrategyParameters:
        """Gibt aktuelle Parameter zurück (für Logging + Robustness-Tests)."""
        ...

    def validate_context(self, context: MarketContext) -> bool:
        """
        Prüft ob Context ausreichend Daten hat.
        Default: True. Strategien können überschreiben.
        """
        return True


# ---------------------------------------------------------------------------
# Base Class (optional, für Convenience)
# ---------------------------------------------------------------------------


class BaseStrategy:
    """
    Optionale Basisklasse mit Convenience-Methoden.
    Nicht erzwungen – Protocol ist der echte Vertrag.
    """

    name: str = "base"
    version: str = "0.0.0"
    supported_regimes: list[MarketRegime] = []

    def validate_context(self, context: MarketContext) -> bool:
        """Prüft Mindest-Feature-Anforderungen."""
        ind = context.primary.indicators
        return ind.rsi_14 is not None and ind.atr_14 is not None and context.primary.close > 0

    def get_parameters(self) -> StrategyParameters:
        return StrategyParameters(
            name=self.name,
            version=self.version,
            params={},
        )

    def _signal(
        self,
        context: MarketContext,
        direction: SignalDirection,
        confidence: float,
        metadata: dict[str, Any] | None = None,
    ) -> Signal:
        """Helper: erstellt Signal mit korrekten Feldern."""
        return Signal(
            timestamp=datetime.now(tz=UTC),
            strategy_name=self.name,
            symbol=context.symbol,
            direction=direction,
            confidence=confidence,
            regime=context.regime,
            metadata=metadata or {},
        )
