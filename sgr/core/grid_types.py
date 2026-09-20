"""
SGR Futures Grid - Shared Domain Types
========================================
Domain-Types fuer Futures Grid Trading, geteilt zwischen Strategy Engine,
Risk Engine, Execution Engine, Portfolio-Bookkeeping, Backtesting und API.

Bewusst NICHT in sgr/core/types.py: Futures Grid ist eine zusaetzliche,
optionale Produktklasse (siehe ProductType.FUTURES_GRID dort) - ein
eigenes Modul haelt den bestehenden, zentralen Types-Katalog unveraendert
uebersichtlich und macht sichtbar, welche Types NUR fuer Futures Grid
relevant sind (kein bestehender Code-Pfad muss dieses Modul importieren).

Kapitalbindungs-/Risiko-Prinzip (siehe Aufgabenstellung "KEINE STRATEGIE
DARF DIE RISIKOSCHICHT UMGEHEN"): FuturesGridParameters ist reine
Konfiguration - ob ein Grid mit diesen Parametern tatsaechlich eroeffnet
werden darf, entscheidet ausschliesslich sgr.risk.grid_risk.GridRiskEngine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from sgr.core.types import (
    ExchangeID,
    GridDirection,
    GridSpacingMode,
    GridStatus,
    MarginMode,
    Symbol,
    TradingMode,
)

# ---------------------------------------------------------------------------
# Strategy Genome: Futures Grid Parameters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FuturesGridParameters:
    """
    Vollstaendiger Parametersatz eines Futures Grid - Teil des Strategy
    Genome (siehe sgr/strategy/futures_grid.py). Jede Mutation der
    Strategy Evolution Engine erzeugt eine neue Instanz dieser Klasse
    (frozen -> immutable, wie StrategyParameters in sgr/strategy/base.py).

    Felder 1:1 aus der Aufgabenstellung:
        grid_lower_price, grid_upper_price, grid_count, grid_spacing,
        grid_mode, long_or_short, leverage, margin_mode, position_size,
        max_notional, take_profit, stop_loss, maximum_holding_time,
        funding_cost_limit, volatility_limit, liquidity_limit,
        maximum_grid_exposure, maximum_open_grid_orders.
    """

    grid_lower_price: Decimal
    grid_upper_price: Decimal
    grid_count: int
    long_or_short: GridDirection
    leverage: Decimal = Decimal("1")
    margin_mode: MarginMode = MarginMode.ISOLATED
    grid_mode: GridSpacingMode = GridSpacingMode.ARITHMETIC
    # None = wird aus (upper-lower)/count bzw. dem geometrischen Verhaeltnis
    # abgeleitet (siehe compute_levels()) - explizit gesetzt nur, wenn eine
    # Mutation/Optimierung einen abweichenden Spacing-Wert erzwingen will.
    grid_spacing: Decimal | None = None
    # Notional pro einzelnem Grid-Level (nicht des gesamten Grids).
    position_size: Decimal = Decimal("0")
    max_notional: Decimal = Decimal("0")
    take_profit: Decimal | None = None  # Gesamt-Grid-Ziel (Preis oder PnL-Grenze, siehe Nutzung)
    stop_loss: Decimal | None = None
    maximum_holding_time: int | None = None  # Sekunden, None = unbegrenzt
    funding_cost_limit: float = 0.0006  # max. akzeptierter |funding rate| pro Intervall
    volatility_limit: float = 0.08  # max. akzeptierter ATR% des Preises
    liquidity_limit: Decimal = Decimal("0")  # min. erwartetes 24h-Quote-Volumen
    maximum_grid_exposure: Decimal = Decimal("0")  # 0 = wird von GridRiskEngine defaulted
    maximum_open_grid_orders: int = 0  # 0 = wird von GridRiskEngine defaulted

    def __post_init__(self) -> None:
        if self.grid_lower_price <= 0 or self.grid_upper_price <= 0:
            raise ValueError("grid_lower_price/grid_upper_price muessen > 0 sein")
        if self.grid_lower_price >= self.grid_upper_price:
            raise ValueError("grid_lower_price muss < grid_upper_price sein")
        if self.grid_count < 2:
            raise ValueError("grid_count muss >= 2 sein")
        if self.leverage < 1:
            raise ValueError("leverage muss >= 1 sein")
        if self.long_or_short == GridDirection.NEUTRAL:
            raise ValueError(
                "FuturesGridParameters.long_or_short darf nicht NEUTRAL sein - "
                "NEUTRAL bedeutet 'kein Grid', siehe GridDecision.direction"
            )

    def compute_levels(self) -> list[Decimal]:
        """Berechnet grid_count aufsteigend sortierte Preis-Levels
        zwischen grid_lower_price und grid_upper_price (inklusive)."""
        n = self.grid_count
        lo, hi = self.grid_lower_price, self.grid_upper_price

        if self.grid_mode == GridSpacingMode.GEOMETRIC:
            # Decimal unterstuetzt keine fraktionale Potenz nativ - das
            # Verhaeltnis wird ueber float berechnet (Praezisionsverlust
            # hier unkritisch, da nur die Level-PREISE betroffen sind,
            # nicht Order-Mengen/Notional-Berechnungen). Ergebnis wird
            # sofort wieder in Decimal ueberfuehrt.
            ratio = float(hi / lo) ** (1.0 / (n - 1))
            levels = [lo * Decimal(str(ratio**i)) for i in range(n)]
        else:
            step = (hi - lo) / Decimal(n - 1)
            levels = [lo + step * i for i in range(n)]

        return [round(level, 8) for level in levels]

    def effective_grid_spacing(self) -> Decimal:
        """Grid-Spacing als Preis-Abstand zwischen den zwei untersten
        Levels - explizit gesetzter Wert hat Vorrang vor der Ableitung."""
        if self.grid_spacing is not None:
            return self.grid_spacing
        levels = self.compute_levels()
        return levels[1] - levels[0] if len(levels) >= 2 else Decimal("0")

    def total_notional(self) -> Decimal:
        """Theoretische Gesamt-Kapitalbindung, wenn JEDES Level gleichzeitig
        gefuellt waere (Worst-Case fuer Exposure-Limits, siehe GridRiskEngine)."""
        return self.position_size * self.grid_count


# ---------------------------------------------------------------------------
# Grid Level / Order tracking
# ---------------------------------------------------------------------------


class GridLevelState(BaseModel):
    """Zustand eines einzelnen Grid-Preis-Levels."""

    model_config = {"frozen": False}

    index: int
    price: Decimal
    side: str  # "buy" | "sell" - abhaengig von long_or_short und Position zu diesem Level
    is_filled: bool = False
    cycle_count: int = 0  # wie oft dieses Level bereits gefuellt+wieder aufgefuellt wurde
    last_order_id: str | None = None
    last_filled_at: datetime | None = None
    # Tatsaechlich beim Oeffnen gefuellte Menge - MUSS beim Schliessen
    # dieser Cell wiederverwendet werden (nicht am Exit-Preis neu aus
    # position_size berechnet), sonst driftet grid.net_position_qty nie
    # zurueck auf 0 und die Order verkauft/kauft eine andere Menge, als
    # tatsaechlich gehalten wird (siehe GridController._fill_level()).
    quantity: Decimal = Decimal("0")


class GridState(BaseModel):
    """
    Vollstaendiger Laufzeit-Zustand einer Futures-Grid-Instanz.
    Wird von sgr.execution.grid_controller.GridController gehalten und
    (best-effort) ueber sgr.core.repositories.GridRepository persistiert.
    """

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str | None = None
    exchange: ExchangeID
    symbol: Symbol
    strategy_name: str
    trading_mode: TradingMode
    direction: GridDirection
    status: GridStatus = GridStatus.PENDING
    parameters: dict[str, Any] = Field(default_factory=dict)  # sanitisierte FuturesGridParameters
    levels: list[GridLevelState] = Field(default_factory=list)

    net_position_qty: Decimal = Decimal("0")  # aktuelle Netto-Exposure aus diesem Grid
    realized_pnl: Decimal = Decimal("0")  # Summe abgeschlossener Grid-Zyklen (Grid Capture)
    fees_paid: Decimal = Decimal("0")
    funding_paid: Decimal = Decimal("0")
    fills_count: int = 0
    # Letzter bekannter Preis - Basis fuer die Crossing-Erkennung in
    # GridController.on_price_tick() (ein Level darf nur ausgeloest
    # werden, wenn der Preis es seit dem letzten Tick TATSAECHLICH
    # UEBERQUERT hat, nicht schon weil "current_price <= level.price"
    # fuer JEDES noch nicht erreichte Level oberhalb des Startpreises
    # trivial wahr waere). Wird bei Grid-Eroeffnung auf den Eroeffnungs-
    # preis gesetzt.
    last_price: Decimal | None = None

    opened_at: datetime
    closed_at: datetime | None = None
    close_reason: str | None = None

    @property
    def open_orders_count(self) -> int:
        return sum(1 for level in self.levels if level.is_filled is False and level.last_order_id)

    @property
    def is_active(self) -> bool:
        return self.status in (GridStatus.PENDING, GridStatus.ACTIVE)


# ---------------------------------------------------------------------------
# Grid decision (Strategy -> GridController)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GridDecision:
    """
    Ergebnis einer GridTradingStrategy.evaluate()-Auswertung (siehe
    sgr/strategy/futures_grid.py). direction=NEUTRAL bedeutet "kein Grid
    eroeffnen" - ein haeufiges, legitimes Ergebnis, kein Fehler (siehe
    Aufgabenstellung: "Diese Regeln duerfen nicht als feste
    Gewinnversprechen implementiert werden").
    """

    direction: GridDirection
    parameters: FuturesGridParameters | None
    confidence: float
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Risk assessment
# ---------------------------------------------------------------------------


class GridRiskAssessment(BaseModel):
    """Ergebnis von GridRiskEngine.evaluate_new_grid()."""

    model_config = {"frozen": True}

    approved: bool
    reason: str | None = None
    adjusted_parameters: dict[str, Any] | None = None  # sanitisierte FuturesGridParameters
    warnings: list[str] = Field(default_factory=list)


class GridViolation(BaseModel):
    """Einzelne laufzeitbezogene Risikoverletzung eines aktiven Grids
    (siehe GridRiskEngine.check_ongoing_grid())."""

    model_config = {"frozen": True}

    code: str
    message: str
    severity: str = "hard"  # "hard" -> Grid muss geschlossen werden, "soft" -> Warnung
