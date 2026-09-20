"""
SGR Futures Grid Strategies
============================
Futures Grid als First-Class Strategy/Product-Klasse (siehe
Aufgabenstellung). Enthaelt drei konkrete Strategien:

    LongFuturesGridStrategy     - handelt ausschliesslich Long-Grids
    ShortFuturesGridStrategy    - handelt ausschliesslich Short-Grids
    AdaptiveFuturesGridStrategy - entscheidet selbst Long/Short/kein Grid,
                                  basierend auf GridSuitabilityScore

Architektur-Entscheidung: eigenes Protocol statt TradingStrategy
    TradingStrategy.generate_signal() (sgr/strategy/base.py) gibt GENAU
    EIN Signal fuer GENAU EINE Order zurueck - ein Grid besteht dagegen
    aus mehreren gleichzeitig aktiven Preis-Levels/Orders. Ein Grid in
    ein einzelnes Signal zu zwingen wuerde entweder Informationen
    verlieren oder das bestehende Signal/RiskAssessment/OrderRequest-
    Format kuenstlich ueberladen (Regressionsrisiko fuer ALLE
    bestehenden Strategien, die dieses Format nutzen).

    Stattdessen implementieren Grid-Strategien zusaetzlich
    GridTradingStrategy.evaluate() (eigener Vertrag, siehe unten) UND
    bleiben gleichzeitig gueltige TradingStrategy-Instanzen (gleiche
    Attribute: name/version/supported_regimes, generate_signal() als
    bewusster No-Op). Dadurch:
    - Registrierung/Aktivierung/Deaktivierung/Validierungsstatus laeuft
      unveraendert ueber StrategyRegistry (dieselbe strenge Pipeline wie
      jede andere Strategie, siehe Aufgabenstellung).
    - StrategyEngine.process()/BacktestSimulator._generate_signal() rufen
      generate_signal() weiterhin unveraendert auf und erhalten IMMER
      None von einer Grid-Strategie - Null Interferenz mit dem
      bestehenden Directional-Signal-Pfad.
    - sgr.execution.grid_controller.GridController ruft stattdessen
      explizit evaluate() auf, ausschliesslich fuer Strategien, die das
      GridTradingStrategy-Protocol erfuellen (siehe
      sgr.strategy.registry_helpers.get_active_grid_strategies()).
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from sgr.core.grid_types import FuturesGridParameters, GridDecision
from sgr.core.types import ExchangeID, GridDirection, MarketRegime, ProductType
from sgr.market_data.grid_suitability import GridSuitabilityScore, compute_grid_suitability
from sgr.market_data.types import MarketContext
from sgr.strategy.base import BaseStrategy, StrategyParameters
from sgr.strategy.registry import StrategyRegistry

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class GridTradingStrategy(Protocol):
    """Zusaetzlicher Vertrag fuer Grid-Strategien - siehe Modul-Docstring."""

    name: str
    version: str
    supported_regimes: list[MarketRegime]
    supported_product_types: list[ProductType]
    supported_exchanges: list[ExchangeID]

    @abstractmethod
    def evaluate(self, context: MarketContext) -> GridDecision:
        """
        Bewertet, ob JETZT ein Futures Grid fuer dieses Symbol eroeffnet
        werden sollte. Liefert IMMER eine GridDecision zurueck (nie None) -
        GridDecision.direction == NEUTRAL ist das korrekte Ergebnis, wenn
        kein Grid sinnvoll ist. Rein synchron, kein I/O (wie
        TradingStrategy.generate_signal()).
        """
        ...


# ---------------------------------------------------------------------------
# Gemeinsame Basis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GridStrategyDefaults:
    """Konfigurierbare Default-Werte fuer die Parameter-Ableitung aus dem
    aktuellen Marktkontext (kein Gewinnversprechen - reine Heuristik fuer
    einen plausiblen Startpunkt, der anschliessend durch GridRiskEngine
    und die Walk-Forward-/Paper-Trading-Pipeline geprueft wird)."""

    grid_count: int = 10
    range_atr_multiplier: Decimal = Decimal("3.0")  # Range-Breite = ATR * Multiplier
    leverage: Decimal = Decimal("2")
    position_size_usd: Decimal = Decimal("20")
    take_profit_pct: Decimal = Decimal("0.03")  # Gesamt-Grid, relativ zur Range-Mitte
    stop_loss_pct: Decimal = Decimal("0.08")
    maximum_holding_time_seconds: int = 60 * 60 * 24 * 3  # 3 Tage


class _BaseFuturesGridStrategy(BaseStrategy):
    """
    Gemeinsame Basis fuer alle Futures-Grid-Strategien. Erfuellt sowohl
    TradingStrategy (via BaseStrategy) als auch GridTradingStrategy.
    """

    supported_regimes: list[MarketRegime] = [
        MarketRegime.RANGING,
        MarketRegime.LOW_VOLATILITY,
        MarketRegime.TRENDING_UP,
        MarketRegime.TRENDING_DOWN,
    ]
    supported_product_types: list[ProductType] = [ProductType.FUTURES_GRID]
    supported_exchanges: list[ExchangeID] = [ExchangeID.PIONEX, ExchangeID.BINANCE]

    _fixed_direction: GridDirection | None = None  # None = adaptive
    _defaults = GridStrategyDefaults()

    def generate_signal(self, context: MarketContext) -> None:
        """
        Bewusster No-Op (siehe Modul-Docstring) - Grid-Strategien werden
        NICHT ueber den klassischen Directional-Signal-Pfad ausgefuehrt.
        """
        return None

    def get_parameters(self) -> StrategyParameters:
        direction = self._fixed_direction.value if self._fixed_direction else "adaptive"
        return StrategyParameters(
            name=self.name,
            version=self.version,
            params={"fixed_direction": direction},
        )

    def evaluate(self, context: MarketContext) -> GridDecision:
        suitability = compute_grid_suitability(context)

        direction = self._decide_direction(suitability)
        if direction == GridDirection.NEUTRAL:
            return GridDecision(
                direction=GridDirection.NEUTRAL,
                parameters=None,
                confidence=suitability.composite,
                reasons=suitability.reasons,
            )

        parameters = self._build_parameters(context, direction)
        if parameters is None:
            return GridDecision(
                direction=GridDirection.NEUTRAL,
                parameters=None,
                confidence=suitability.composite,
                reasons=[*suitability.reasons, "Parameter-Ableitung fehlgeschlagen (ATR fehlt)"],
            )

        return GridDecision(
            direction=direction,
            parameters=parameters,
            confidence=suitability.composite,
            reasons=suitability.reasons,
        )

    def _decide_direction(self, suitability: GridSuitabilityScore) -> GridDirection:
        """Von Subklassen ueberschrieben - siehe Long/Short/Adaptive unten."""
        raise NotImplementedError

    def _build_parameters(
        self, context: MarketContext, direction: GridDirection
    ) -> FuturesGridParameters | None:
        """
        Leitet einen plausiblen Startparametersatz aus dem aktuellen
        Marktkontext ab (ATR-basierte Range, siehe GridStrategyDefaults).
        Liefert None, wenn die notwendigen Indikatoren (ATR) fehlen -
        der Aufrufer behandelt das als NEUTRAL, kein Fehler.
        """
        atr = context.primary.indicators.atr_14
        price = context.primary.close
        if atr is None or atr <= 0 or price <= 0:
            return None

        half_range = atr * self._defaults.range_atr_multiplier
        lower = max(price - half_range, price * Decimal("0.5"))
        upper = price + half_range

        return FuturesGridParameters(
            grid_lower_price=lower,
            grid_upper_price=upper,
            grid_count=self._defaults.grid_count,
            long_or_short=direction,
            leverage=self._defaults.leverage,
            position_size=self._defaults.position_size_usd,
            max_notional=self._defaults.position_size_usd * self._defaults.grid_count,
            take_profit=price * (Decimal("1") + self._defaults.take_profit_pct)
            if direction == GridDirection.LONG
            else price * (Decimal("1") - self._defaults.take_profit_pct),
            stop_loss=price * (Decimal("1") - self._defaults.stop_loss_pct)
            if direction == GridDirection.LONG
            else price * (Decimal("1") + self._defaults.stop_loss_pct),
            maximum_holding_time=self._defaults.maximum_holding_time_seconds,
        )


# ---------------------------------------------------------------------------
# Konkrete Strategien
# ---------------------------------------------------------------------------


@StrategyRegistry.register
class LongFuturesGridStrategy(_BaseFuturesGridStrategy):
    """
    Long Futures Grid: arbeitet innerhalb einer Preisrange und versucht,
    von wiederkehrenden Bewegungen auf der Long-Seite zu profitieren.
    Handelt NUR, wenn GridSuitabilityScore.is_suitable True ist - keine
    feste Aktivierung unabhaengig vom Marktzustand.
    """

    name = "futures_grid_long_v1"
    version = "1.0.0"
    _fixed_direction = GridDirection.LONG

    def _decide_direction(self, suitability: GridSuitabilityScore) -> GridDirection:
        if not suitability.is_suitable:
            return GridDirection.NEUTRAL
        return GridDirection.LONG


@StrategyRegistry.register
class ShortFuturesGridStrategy(_BaseFuturesGridStrategy):
    """
    Short Futures Grid: arbeitet innerhalb einer Preisrange und versucht,
    von wiederkehrenden Bewegungen auf der Short-Seite zu profitieren.
    """

    name = "futures_grid_short_v1"
    version = "1.0.0"
    _fixed_direction = GridDirection.SHORT

    def _decide_direction(self, suitability: GridSuitabilityScore) -> GridDirection:
        if not suitability.is_suitable:
            return GridDirection.NEUTRAL
        return GridDirection.SHORT


@StrategyRegistry.register
class AdaptiveFuturesGridStrategy(_BaseFuturesGridStrategy):
    """
    Adaptive Futures Grid: entscheidet selbst, ob ein Long Grid, Short
    Grid oder gar kein Grid sinnvoll ist - abhaengig vom Market State
    (siehe GridSuitabilityScore.suggested_direction). Keine feste
    Vorliebe fuer eine Richtung; bei ungeeigneten Marktbedingungen
    (hohe Volatilitaet, illiquide, ungewoehnliche Funding Rate, zu
    geringe erwartete Edge) wird explizit KEIN Grid vorgeschlagen.
    """

    name = "futures_grid_adaptive_v1"
    version = "1.0.0"
    _fixed_direction = None

    def _decide_direction(self, suitability: GridSuitabilityScore) -> GridDirection:
        if not suitability.is_suitable:
            return GridDirection.NEUTRAL
        return suitability.suggested_direction


# ---------------------------------------------------------------------------
# Registry-Hilfsfunktion (Grid-Strategien aus der gemeinsamen Registry filtern)
# ---------------------------------------------------------------------------


def get_active_grid_strategies() -> list[GridTradingStrategy]:
    """
    Gibt alle aktiven, registrierten Strategien zurueck, die das
    GridTradingStrategy-Protocol erfuellen (isinstance-Check via
    @runtime_checkable - prueft strukturell auf evaluate()+die noetigen
    Attribute, kein Vererbungszwang). Genutzt von
    sgr.execution.grid_controller - NICHT von StrategyEngine.process()
    (das wuerde generate_signal() aufrufen, siehe Modul-Docstring).
    """
    registry = StrategyRegistry.get()
    result: list[GridTradingStrategy] = []
    for strategy in registry.get_active():
        if isinstance(strategy, GridTradingStrategy):
            result.append(strategy)
    return result


__all__ = [
    "GridTradingStrategy",
    "GridStrategyDefaults",
    "LongFuturesGridStrategy",
    "ShortFuturesGridStrategy",
    "AdaptiveFuturesGridStrategy",
    "get_active_grid_strategies",
]
