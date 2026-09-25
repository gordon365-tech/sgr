"""
SGR Performance Fee Engine
===========================
Berechnet und verwaltet Performance-Fees nach High-Water-Mark Prinzip.

Geschäftsregel:
    5% Performance Fee auf realisierte Gewinne über HWM.
    Monatliche Abrechnung (letzter Kalendertag).
    Nur auf LIVE-Trading Gewinne (Paper Trading kostenlos).

High-Water-Mark Ablauf:
    Monat 1: Start 10.000 → Ende 12.000 → HWM=12.000 → Fee: 2.000 * 5% = 100 USDT
    Monat 2: Start 12.000 → Ende 11.000 → HWM=12.000 → Fee: 0 (kein neues Hoch)
    Monat 3: Start 11.000 → Ende 13.000 → HWM=13.000 → Fee: (13.000-12.000) * 5% = 50 USDT

Transparenz:
    Jeder Fee-Schritt wird als PerformanceFeeCalculation gespeichert.
    Vollständiger Audit-Trail: User kann jede Berechnung nachvollziehen.
    Reports verfügbar über /api/v1/billing/performance-report

Fee-Einzug:
    MVP: Manuell (User überweist auf SGR-Konto)
    V2: Stripe Integration (automatischer Einzug)
    V3: On-Chain (Smart Contract für trustless Fee)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sgr.core.logging import get_logger
from sgr.saas.types import (
    FeeStatus,
    HighWaterMark,
    Invoice,
    PerformanceFeeCalculation,
    PortfolioSnapshot,
)

log = get_logger(__name__)

# Monatliche Fee-Rate
DEFAULT_FEE_RATE = Decimal("0.05")  # 5%
MIN_FEE_AMOUNT = Decimal("1.00")  # Keine Micro-Fees unter 1 USDT


class PerformanceFeeEngine:
    """
    Berechnet und verwaltet Performance-Fees.

    State:
        HWM wird in Redis + DB gespeichert (doppelte Sicherheit).
        Bei Diskrepanz: DB-Wert ist Quelle der Wahrheit.
    """

    def __init__(self) -> None:
        self._hwm_cache: dict[str, HighWaterMark] = {}

    # ------------------------------------------------------------------
    # HWM Management
    # ------------------------------------------------------------------

    async def get_hwm(self, user_id: str, initial_capital: Decimal) -> HighWaterMark:
        """
        Lädt High-Water-Mark für User.
        Falls nicht vorhanden: initialisiert mit initial_capital.
        """
        if user_id in self._hwm_cache:
            return self._hwm_cache[user_id]

        # Aus DB laden (vereinfacht – in Produktion aus portfolio_snapshots)
        hwm = HighWaterMark(
            user_id=user_id,
            current_hwm=initial_capital,
        )
        self._hwm_cache[user_id] = hwm
        return hwm

    async def update_hwm(self, user_id: str, new_portfolio_value: Decimal) -> None:
        """Aktualisiert HWM wenn neues Allzeithoch."""
        hwm = self._hwm_cache.get(user_id)
        if hwm and new_portfolio_value > hwm.current_hwm:
            hwm.update_hwm(new_portfolio_value)
            log.info(
                "fee_engine.hwm_updated",
                user_id=user_id,
                new_hwm=str(new_portfolio_value),
            )

    # ------------------------------------------------------------------
    # Fee Berechnung
    # ------------------------------------------------------------------

    def calculate_fee(
        self,
        user_id: str,
        period_start: datetime,
        period_end: datetime,
        portfolio_value_start: Decimal,
        portfolio_value_end: Decimal,
        hwm: HighWaterMark,
        fee_rate: Decimal = DEFAULT_FEE_RATE,
    ) -> PerformanceFeeCalculation:
        """
        Berechnet Performance Fee für eine Periode.

        Logic:
            1. Neues Hoch gegenüber HWM berechnen
            2. Fee = Gewinn_über_HWM * fee_rate
            3. HWM updaten
        """
        profit_above_hwm = hwm.calculate_new_high(portfolio_value_end)

        if profit_above_hwm <= 0:
            fee_amount = Decimal("0")
            status = FeeStatus.PENDING  # Kein Fee in dieser Periode
        else:
            raw_fee = profit_above_hwm * fee_rate
            # Auf 2 Dezimalstellen runden (USDT Cent)
            fee_amount = raw_fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            # Minimum-Fee-Schwelle
            if fee_amount < MIN_FEE_AMOUNT:
                fee_amount = Decimal("0")
                profit_above_hwm = Decimal("0")

            status = FeeStatus.PENDING if fee_amount > 0 else FeeStatus.PENDING

        calculation = PerformanceFeeCalculation(
            user_id=user_id,
            period_start=period_start,
            period_end=period_end,
            portfolio_value_start=portfolio_value_start,
            portfolio_value_end=portfolio_value_end,
            high_water_mark=hwm.current_hwm,
            profit_above_hwm=profit_above_hwm,
            fee_rate=fee_rate,
            fee_amount=fee_amount,
            status=status,
            calculation_details={
                "hwm_before": str(hwm.current_hwm),
                "hwm_after": str(max(hwm.current_hwm, portfolio_value_end)),
                "portfolio_growth_pct": float(
                    (portfolio_value_end - portfolio_value_start) / portfolio_value_start * 100
                )
                if portfolio_value_start > 0
                else 0.0,
            },
        )

        # HWM updaten
        hwm.update_hwm(portfolio_value_end)

        if fee_amount > 0:
            log.info(
                "fee_engine.fee_calculated",
                user_id=user_id,
                period=f"{period_start.date()} → {period_end.date()}",
                profit_above_hwm=str(profit_above_hwm),
                fee_amount=str(fee_amount),
                fee_rate=f"{float(fee_rate):.0%}",
            )

        return calculation

    # ------------------------------------------------------------------
    # Monthly Settlement
    # ------------------------------------------------------------------

    async def run_monthly_settlement(
        self,
        user_id: str,
        current_portfolio_value: Decimal,
        initial_capital: Decimal,
        fee_rate: Decimal = DEFAULT_FEE_RATE,
    ) -> PerformanceFeeCalculation:
        """
        Führt monatliche Fee-Abrechnung durch.
        Wird am letzten Tag des Monats automatisch aufgerufen.

        In Produktion: Cronjob oder Celery Task.
        """
        now = datetime.now(tz=UTC)
        hwm = await self.get_hwm(user_id, initial_capital)

        # Snapshot des letzten Monats als Start-Wert
        period_start_value = hwm.current_hwm  # Vereinfachung: HWM als Start

        calculation = self.calculate_fee(
            user_id=user_id,
            period_start=hwm.last_fee_date or now,
            period_end=now,
            portfolio_value_start=period_start_value,
            portfolio_value_end=current_portfolio_value,
            hwm=hwm,
            fee_rate=fee_rate,
        )

        hwm.last_fee_date = now
        hwm.cumulative_fees_paid += calculation.fee_amount

        return calculation

    # ------------------------------------------------------------------
    # Invoice Generation
    # ------------------------------------------------------------------

    def generate_invoice(
        self,
        calculation: PerformanceFeeCalculation,
    ) -> Invoice:
        """
        Erstellt Rechnung aus Fee-Berechnung.
        """
        return Invoice(
            id=str(uuid.uuid4()),
            user_id=calculation.user_id,
            period_start=calculation.period_start,
            period_end=calculation.period_end,
            performance_fee=calculation.fee_amount,
            status=FeeStatus.INVOICED,
            issued_at=datetime.now(tz=UTC),
            line_items=[
                {
                    "description": f"Performance Fee {float(calculation.fee_rate):.0%}",
                    "profit_above_hwm": str(calculation.profit_above_hwm),
                    "fee_rate": str(calculation.fee_rate),
                    "fee_amount": str(calculation.fee_amount),
                    "high_water_mark": str(calculation.high_water_mark),
                    "period": (
                        f"{calculation.period_start.date()} – {calculation.period_end.date()}"
                    ),
                }
            ],
        )

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def generate_performance_report(
        self,
        user_id: str,
        calculations: list[PerformanceFeeCalculation],
        snapshots: list[PortfolioSnapshot],
    ) -> dict[str, Any]:
        """
        Vollständiger Performance-Report für Billing-Dashboard.
        Zeigt HWM-Verlauf, Fees, Portfolio-Entwicklung.
        """
        total_fees_paid = sum(c.fee_amount for c in calculations if c.status == FeeStatus.PAID)
        total_fees_pending = sum(
            c.fee_amount for c in calculations if c.status == FeeStatus.PENDING
        )

        # Portfolio-Entwicklung
        portfolio_history = [
            {
                "date": s.snapshot_date.isoformat(),
                "value": str(s.portfolio_value),
                "hwm": str(s.high_water_mark),
                "fee_period": str(s.performance_fee_period),
            }
            for s in snapshots
        ]

        # Fee-Perioden
        fee_periods = [
            {
                "period": f"{c.period_start.date()} – {c.period_end.date()}",
                "portfolio_end": str(c.portfolio_value_end),
                "hwm": str(c.high_water_mark),
                "profit_above_hwm": str(c.profit_above_hwm),
                "fee_amount": str(c.fee_amount),
                "fee_rate": f"{float(c.fee_rate):.0%}",
                "status": c.status.value,
            }
            for c in calculations
        ]

        return {
            "user_id": user_id,
            "report_generated_at": datetime.now(tz=UTC).isoformat(),
            "summary": {
                "total_fees_paid_usdt": str(total_fees_paid),
                "total_fees_pending_usdt": str(total_fees_pending),
                "fee_rate": "5%",
                "fee_model": "High-Water-Mark Performance Fee",
                "billing_cycle": "Monthly",
            },
            "portfolio_history": portfolio_history,
            "fee_periods": fee_periods,
        }
