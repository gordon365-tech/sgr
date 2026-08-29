"""
Tests für sgr.saas.fees.PerformanceFeeEngine.

Coverage-Ziel: 62% -> hoch.

Teststrategie: Reine Business-Logik ohne externe I/O (HWM-Cache ist
in-memory, DB-Anbindung ist laut Docstring vereinfacht/noch nicht
implementiert) - alles wird direkt mit echten Decimal-Werten getestet,
kein Mocking notwendig.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sgr.saas.fees import DEFAULT_FEE_RATE, MIN_FEE_AMOUNT, PerformanceFeeEngine
from sgr.saas.types import (
    FeeStatus,
    HighWaterMark,
    PerformanceFeeCalculation,
    PortfolioSnapshot,
)


@pytest.fixture
def engine() -> PerformanceFeeEngine:
    return PerformanceFeeEngine()


# ---------------------------------------------------------------------
# get_hwm() / update_hwm()
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestHWMManagement:
    async def test_get_hwm_initializes_new_user(self, engine: PerformanceFeeEngine) -> None:
        hwm = await engine.get_hwm("user-1", Decimal("10000"))

        assert hwm.user_id == "user-1"
        assert hwm.current_hwm == Decimal("10000")

    async def test_get_hwm_returns_cached_value_on_second_call(
        self, engine: PerformanceFeeEngine
    ) -> None:
        first = await engine.get_hwm("user-1", Decimal("10000"))
        first.current_hwm = Decimal("15000")  # Mutate cached instance.

        second = await engine.get_hwm("user-1", Decimal("999999"))  # Ignored, cache hit.

        assert second is first
        assert second.current_hwm == Decimal("15000")

    async def test_update_hwm_raises_when_new_value_higher(
        self, engine: PerformanceFeeEngine
    ) -> None:
        await engine.get_hwm("user-1", Decimal("10000"))

        await engine.update_hwm("user-1", Decimal("12000"))

        cached = await engine.get_hwm("user-1", Decimal("0"))
        assert cached.current_hwm == Decimal("12000")

    async def test_update_hwm_noop_when_new_value_lower(
        self, engine: PerformanceFeeEngine
    ) -> None:
        await engine.get_hwm("user-1", Decimal("10000"))

        await engine.update_hwm("user-1", Decimal("8000"))

        cached = await engine.get_hwm("user-1", Decimal("0"))
        assert cached.current_hwm == Decimal("10000")

    async def test_update_hwm_noop_for_unknown_user(self, engine: PerformanceFeeEngine) -> None:
        # No cached HWM exists for this user - should not raise.
        await engine.update_hwm("never-seen-user", Decimal("50000"))


# ---------------------------------------------------------------------
# calculate_fee()
# ---------------------------------------------------------------------


class TestCalculateFee:
    def _period(self) -> tuple[datetime, datetime]:
        end = datetime.now(tz=UTC)
        start = end - timedelta(days=30)
        return start, end

    def test_calculate_fee_charges_on_new_high(self, engine: PerformanceFeeEngine) -> None:
        start, end = self._period()
        hwm = HighWaterMark(user_id="user-1", current_hwm=Decimal("10000"))

        calc = engine.calculate_fee(
            user_id="user-1",
            period_start=start,
            period_end=end,
            portfolio_value_start=Decimal("10000"),
            portfolio_value_end=Decimal("12000"),
            hwm=hwm,
        )

        assert calc.profit_above_hwm == Decimal("2000")
        assert calc.fee_amount == Decimal("100.00")  # 5% of 2000
        assert hwm.current_hwm == Decimal("12000")  # HWM updated.

    def test_calculate_fee_zero_when_below_hwm(self, engine: PerformanceFeeEngine) -> None:
        start, end = self._period()
        hwm = HighWaterMark(user_id="user-1", current_hwm=Decimal("12000"))

        calc = engine.calculate_fee(
            user_id="user-1",
            period_start=start,
            period_end=end,
            portfolio_value_start=Decimal("12000"),
            portfolio_value_end=Decimal("11000"),
            hwm=hwm,
        )

        assert calc.fee_amount == Decimal("0")
        assert calc.status == FeeStatus.PENDING
        assert hwm.current_hwm == Decimal("12000")  # Unchanged, no new high.

    def test_calculate_fee_below_minimum_threshold_is_waived(
        self, engine: PerformanceFeeEngine
    ) -> None:
        start, end = self._period()
        hwm = HighWaterMark(user_id="user-1", current_hwm=Decimal("10000"))

        # Profit of 10 USDT * 5% = 0.50 USDT, below MIN_FEE_AMOUNT (1.00).
        calc = engine.calculate_fee(
            user_id="user-1",
            period_start=start,
            period_end=end,
            portfolio_value_start=Decimal("10000"),
            portfolio_value_end=Decimal("10010"),
            hwm=hwm,
        )

        assert calc.fee_amount == Decimal("0")
        assert calc.profit_above_hwm == Decimal("0")

    def test_calculate_fee_uses_custom_fee_rate(self, engine: PerformanceFeeEngine) -> None:
        start, end = self._period()
        hwm = HighWaterMark(user_id="user-1", current_hwm=Decimal("10000"))

        calc = engine.calculate_fee(
            user_id="user-1",
            period_start=start,
            period_end=end,
            portfolio_value_start=Decimal("10000"),
            portfolio_value_end=Decimal("12000"),
            hwm=hwm,
            fee_rate=Decimal("0.10"),
        )

        assert calc.fee_amount == Decimal("200.00")  # 10% of 2000

    def test_calculate_fee_computes_growth_percentage(
        self, engine: PerformanceFeeEngine
    ) -> None:
        start, end = self._period()
        hwm = HighWaterMark(user_id="user-1", current_hwm=Decimal("10000"))

        calc = engine.calculate_fee(
            user_id="user-1",
            period_start=start,
            period_end=end,
            portfolio_value_start=Decimal("10000"),
            portfolio_value_end=Decimal("12000"),
            hwm=hwm,
        )

        assert calc.calculation_details["portfolio_growth_pct"] == pytest.approx(20.0)

    def test_calculate_fee_growth_percentage_zero_when_start_value_zero(
        self, engine: PerformanceFeeEngine
    ) -> None:
        start, end = self._period()
        hwm = HighWaterMark(user_id="user-1", current_hwm=Decimal("0"))

        calc = engine.calculate_fee(
            user_id="user-1",
            period_start=start,
            period_end=end,
            portfolio_value_start=Decimal("0"),
            portfolio_value_end=Decimal("1000"),
            hwm=hwm,
        )

        assert calc.calculation_details["portfolio_growth_pct"] == 0.0

    def test_calculate_fee_default_rate_constant(self) -> None:
        assert DEFAULT_FEE_RATE == Decimal("0.05")
        assert MIN_FEE_AMOUNT == Decimal("1.00")


# ---------------------------------------------------------------------
# run_monthly_settlement()
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestRunMonthlySettlement:
    async def test_run_monthly_settlement_charges_fee_and_updates_cumulative(
        self, engine: PerformanceFeeEngine
    ) -> None:
        await engine.get_hwm("user-1", Decimal("10000"))

        calc = await engine.run_monthly_settlement(
            user_id="user-1",
            current_portfolio_value=Decimal("12000"),
            initial_capital=Decimal("10000"),
        )

        assert calc.fee_amount == Decimal("100.00")

        hwm = await engine.get_hwm("user-1", Decimal("0"))
        assert hwm.cumulative_fees_paid == Decimal("100.00")
        assert hwm.last_fee_date is not None

    async def test_run_monthly_settlement_uses_existing_hwm_as_start(
        self, engine: PerformanceFeeEngine
    ) -> None:
        await engine.get_hwm("user-1", Decimal("10000"))
        await engine.run_monthly_settlement(
            user_id="user-1",
            current_portfolio_value=Decimal("12000"),
            initial_capital=Decimal("10000"),
        )

        # Second settlement: no growth beyond the now-updated HWM.
        second = await engine.run_monthly_settlement(
            user_id="user-1",
            current_portfolio_value=Decimal("11000"),
            initial_capital=Decimal("10000"),
        )

        assert second.fee_amount == Decimal("0")


# ---------------------------------------------------------------------
# generate_invoice()
# ---------------------------------------------------------------------


class TestGenerateInvoice:
    def test_generate_invoice_from_calculation(self, engine: PerformanceFeeEngine) -> None:
        now = datetime.now(tz=UTC)
        calc = PerformanceFeeCalculation(
            user_id="user-1",
            period_start=now - timedelta(days=30),
            period_end=now,
            portfolio_value_start=Decimal("10000"),
            portfolio_value_end=Decimal("12000"),
            high_water_mark=Decimal("12000"),
            profit_above_hwm=Decimal("2000"),
            fee_rate=Decimal("0.05"),
            fee_amount=Decimal("100.00"),
            status=FeeStatus.PENDING,
        )

        invoice = engine.generate_invoice(calc)

        assert invoice.user_id == "user-1"
        assert invoice.performance_fee == Decimal("100.00")
        assert invoice.status == FeeStatus.INVOICED
        assert len(invoice.line_items) == 1
        assert invoice.line_items[0]["fee_amount"] == "100.00"


# ---------------------------------------------------------------------
# generate_performance_report()
# ---------------------------------------------------------------------


class TestGeneratePerformanceReport:
    def _make_calc(self, status: FeeStatus, fee_amount: Decimal) -> PerformanceFeeCalculation:
        now = datetime.now(tz=UTC)
        return PerformanceFeeCalculation(
            user_id="user-1",
            period_start=now - timedelta(days=30),
            period_end=now,
            portfolio_value_start=Decimal("10000"),
            portfolio_value_end=Decimal("12000"),
            high_water_mark=Decimal("12000"),
            profit_above_hwm=Decimal("2000"),
            fee_rate=Decimal("0.05"),
            fee_amount=fee_amount,
            status=status,
        )

    def _make_snapshot(self) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            user_id="user-1",
            snapshot_date=datetime.now(tz=UTC),
            portfolio_value=Decimal("12000"),
            cash=Decimal("2000"),
            unrealized_pnl=Decimal("500"),
            realized_pnl_period=Decimal("1500"),
            total_fees_period=Decimal("100"),
            performance_fee_period=Decimal("100"),
            high_water_mark=Decimal("12000"),
            trading_mode="live",
        )

    def test_report_sums_paid_and_pending_fees_separately(
        self, engine: PerformanceFeeEngine
    ) -> None:
        calcs = [
            self._make_calc(FeeStatus.PAID, Decimal("100.00")),
            self._make_calc(FeeStatus.PENDING, Decimal("50.00")),
        ]

        report = engine.generate_performance_report("user-1", calcs, [])

        assert report["summary"]["total_fees_paid_usdt"] == "100.00"
        assert report["summary"]["total_fees_pending_usdt"] == "50.00"

    def test_report_includes_portfolio_history(self, engine: PerformanceFeeEngine) -> None:
        snapshot = self._make_snapshot()

        report = engine.generate_performance_report("user-1", [], [snapshot])

        assert len(report["portfolio_history"]) == 1
        assert report["portfolio_history"][0]["value"] == "12000"

    def test_report_includes_fee_periods(self, engine: PerformanceFeeEngine) -> None:
        calcs = [self._make_calc(FeeStatus.PAID, Decimal("100.00"))]

        report = engine.generate_performance_report("user-1", calcs, [])

        assert len(report["fee_periods"]) == 1
        assert report["fee_periods"][0]["fee_amount"] == "100.00"
        assert report["fee_periods"][0]["status"] == FeeStatus.PAID.value

    def test_report_with_no_data_returns_zero_summary(
        self, engine: PerformanceFeeEngine
    ) -> None:
        report = engine.generate_performance_report("user-1", [], [])

        assert report["summary"]["total_fees_paid_usdt"] == "0"
        assert report["summary"]["total_fees_pending_usdt"] == "0"
        assert report["portfolio_history"] == []
        assert report["fee_periods"] == []
        assert report["user_id"] == "user-1"
