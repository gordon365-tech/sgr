"""
SGR SaaS Layer – Domain Types
==============================
Alle Types für Multi-Tenant SaaS-Betrieb.

Geschäftsmodell:
    - 5% Performance Fee auf realisierte Gewinne (High-Water-Mark)
    - Keine Fixkosten (nur Erfolgsbasiert)
    - Optional später: SaaS-Tiers (Pro / Enterprise / White-Label)

High-Water-Mark Prinzip:
    Fee wird nur auf NEW HIGHS gezahlt.
    Verliert User 20%, muss er diese 20% erst zurückgewinnen
    bevor neue Fees anfallen.
    Standard in Hedge Fonds.

Tenant-Isolation:
    Jeder User hat:
    - Isolierte Trading-Accounts (eigene API Keys)
    - Eigene Portfolio-Engine-Instanz
    - Eigene Risk-Engine mit eigenem Kill Switch
    - Row-Level Security in PostgreSQL
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SubscriptionTier(StrEnum):
    FREE = "free"  # Paper Trading only, kein Live
    PRO = "pro"  # Live Trading, 5% Performance Fee
    ENTERPRISE = "enterprise"  # Eigene Risk-Limits, White-Label
    WHITE_LABEL = "white_label"  # Vollständige API-Lizenz


class BillingStatus(StrEnum):
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class FeeStatus(StrEnum):
    PENDING = "pending"  # Fee berechnet, noch nicht eingezogen
    INVOICED = "invoiced"  # Rechnung erstellt
    PAID = "paid"  # Bezahlt
    WAIVED = "waived"  # Erlassen (z.B. technischer Fehler)


@dataclass
class HighWaterMark:
    """
    High-Water-Mark für Performance Fee Berechnung.
    Fee fällt nur an wenn Portfolio-Wert neues Allzeithoch erreicht.

    Beispiel:
        Start:     HWM = 10.000 USDT
        +30%:      HWM = 13.000 USDT  → Fee auf 3.000 USDT Gewinn
        -20%:      Kein Fee, HWM bleibt bei 13.000
        +10%:      Kein Fee (12.100 < 13.000 HWM)
        +15%:      Jetzt 13.915 > 13.000  → Fee nur auf 915 USDT
    """

    user_id: str
    current_hwm: Decimal  # Aktueller High-Water-Mark Wert
    last_fee_date: datetime | None = None  # Wann zuletzt Fee berechnet
    cumulative_fees_paid: Decimal = Decimal("0")
    currency: str = "USDT"

    def calculate_new_high(self, current_value: Decimal) -> Decimal:
        """Berechnet auf wie viel Gewinn Fee anfällt."""
        if current_value <= self.current_hwm:
            return Decimal("0")
        return current_value - self.current_hwm

    def update_hwm(self, new_value: Decimal) -> None:
        if new_value > self.current_hwm:
            self.current_hwm = new_value


@dataclass(frozen=True)
class PerformanceFeeCalculation:
    """
    Ergebnis einer Fee-Berechnung.
    Immutable – wird als Audit-Record gespeichert.
    """

    user_id: str
    period_start: datetime
    period_end: datetime
    portfolio_value_start: Decimal
    portfolio_value_end: Decimal
    high_water_mark: Decimal
    profit_above_hwm: Decimal  # Gewinn über HWM
    fee_rate: Decimal  # 0.05 = 5%
    fee_amount: Decimal  # profit_above_hwm * fee_rate
    status: FeeStatus
    calculation_details: dict[str, Any] = field(default_factory=dict)


class TenantConfig(BaseModel):
    """
    Tenant-spezifische Konfiguration.
    Überschreibt System-Defaults für Enterprise/White-Label.
    """

    user_id: str
    tier: SubscriptionTier = SubscriptionTier.FREE
    performance_fee_rate: Decimal = Decimal("0.05")  # 5%
    max_leverage: Decimal = Decimal("3.0")
    max_portfolio_drawdown: float = 0.15
    daily_loss_limit: float = 0.05
    max_open_positions: int = 10
    allowed_exchanges: list[str] = Field(default_factory=lambda: ["pionex"])
    is_live_trading_enabled: bool = False
    custom_branding: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PortfolioSnapshot(BaseModel):
    """Periodischer Portfolio-Snapshot für Fee-Berechnung und Reporting."""

    user_id: str
    snapshot_date: datetime
    portfolio_value: Decimal
    cash: Decimal
    unrealized_pnl: Decimal
    realized_pnl_period: Decimal  # PnL seit letztem Snapshot
    total_fees_period: Decimal
    performance_fee_period: Decimal
    high_water_mark: Decimal
    trading_mode: str


class Invoice(BaseModel):
    """Rechnung für Performance Fee."""

    id: str
    user_id: str
    period_start: datetime
    period_end: datetime
    performance_fee: Decimal
    status: FeeStatus
    issued_at: datetime
    paid_at: datetime | None = None
    stripe_invoice_id: str | None = None
    line_items: list[dict[str, Any]] = Field(default_factory=list)
