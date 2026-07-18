"""
SGR Risk Engine – Domain Types
================================
Alle Typen die vom Risk Engine intern und nach außen verwendet werden.

Hierarchie der Limit-Typen:
    HardLimit  → sofortiger Kill Switch bei Verletzung
    SoftLimit  → Warnung + Positionsgrößen-Reduktion
    MonitorLimit → nur Alert, kein automatischer Eingriff

Kein Typ hier enthält Business Logic.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from sgr.core.types import AlertSeverity, TradingMode


class LimitType(StrEnum):
    HARD = "hard"  # Kill Switch
    SOFT = "soft"  # Reduktion
    MONITOR = "monitor"  # Alert only


class LimitStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    BREACHED = "breached"


class LimitCheck(BaseModel):
    """Ergebnis einer einzelnen Limit-Prüfung."""

    model_config = {"frozen": True}

    name: str
    limit_type: LimitType
    status: LimitStatus
    current_value: float
    limit_value: float
    message: str
    reduction_factor: float = 1.0  # 1.0 = keine Reduktion, 0.5 = halbieren


class RiskReport(BaseModel):
    """
    Vollständiger Risk-Report für einen Zeitpunkt.
    Aggregiert alle Limit-Checks.
    """

    model_config = {"frozen": True}

    id: UUID = Field(default_factory=uuid4)
    timestamp: datetime
    trading_mode: TradingMode

    checks: list[LimitCheck]
    kill_switch_triggered: bool = False
    kill_switch_reason: str | None = None

    # Aktuelle Portfolio-Metriken
    portfolio_value: Decimal
    daily_pnl: Decimal
    daily_pnl_pct: float
    drawdown_from_peak: float
    var_95: float
    expected_shortfall: float
    portfolio_heat: float
    active_positions: int
    correlation_exposure: float

    @property
    def has_hard_breach(self) -> bool:
        return any(
            c.status == LimitStatus.BREACHED and c.limit_type == LimitType.HARD for c in self.checks
        )

    @property
    def has_soft_breach(self) -> bool:
        return any(
            c.status == LimitStatus.BREACHED and c.limit_type == LimitType.SOFT for c in self.checks
        )

    @property
    def min_reduction_factor(self) -> float:
        """Kleinstes Reduktions-Faktor über alle breached Soft Limits."""
        breached = [
            c.reduction_factor
            for c in self.checks
            if c.status == LimitStatus.BREACHED and c.limit_type == LimitType.SOFT
        ]
        return min(breached, default=1.0)

    @property
    def overall_severity(self) -> AlertSeverity:
        if self.kill_switch_triggered:
            return AlertSeverity.KILL_SWITCH
        if self.has_hard_breach:
            return AlertSeverity.CRITICAL
        if self.has_soft_breach:
            return AlertSeverity.WARNING
        return AlertSeverity.INFO


class PositionSizeResult(BaseModel):
    """Ergebnis der Positionsgrößen-Berechnung."""

    model_config = {"frozen": True}

    signal_id: UUID
    approved_quantity: Decimal
    original_quantity: Decimal
    reduction_reason: str | None = None
    var_contribution: float  # Wie viel VaR diese Position beiträgt (%)
    heat_contribution: float  # Wie viel Portfolio-Heat (%)


class KillSwitchState(BaseModel):
    """Zustand des Kill Switch."""

    is_active: bool = False
    triggered_at: datetime | None = None
    reason: str | None = None
    trading_mode: TradingMode = TradingMode.PAPER
    manual_reset_required: bool = True  # Kein Auto-Reset

    def trigger(self, reason: str, trading_mode: TradingMode) -> None:
        self.is_active = True
        self.triggered_at = datetime.now()
        self.reason = reason
        self.trading_mode = trading_mode

    def reset(self) -> None:
        """Nur manuell aufrufbar – kein automatischer Reset."""
        self.is_active = False
        self.triggered_at = None
        self.reason = None
