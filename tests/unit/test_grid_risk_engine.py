"""Tests für sgr.risk.grid_risk.GridRiskEngine (eigenes Grid-Risikoprofil)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sgr.core.grid_types import FuturesGridParameters, GridState
from sgr.core.types import ExchangeID, GridDirection, GridStatus, Symbol, TradingMode
from sgr.risk.grid_risk import GridPortfolioSnapshot, GridRiskEngine, GridRiskLimitsConfig


def _params(**overrides) -> FuturesGridParameters:
    base = dict(
        grid_lower_price=Decimal("90"),
        grid_upper_price=Decimal("110"),
        grid_count=5,
        long_or_short=GridDirection.LONG,
        leverage=Decimal("2"),
        position_size=Decimal("50"),
        max_notional=Decimal("250"),
    )
    base.update(overrides)
    return FuturesGridParameters(**base)


def _snapshot(open_grids: list | None = None) -> GridPortfolioSnapshot:
    return GridPortfolioSnapshot(open_grids=open_grids or [], portfolio_value=Decimal("10000"))


def _open_grid(notional: str) -> GridState:
    symbol = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX)
    return GridState(
        exchange=ExchangeID.PIONEX,
        symbol=symbol,
        strategy_name="futures_grid_long_v1",
        trading_mode=TradingMode.PAPER,
        direction=GridDirection.LONG,
        status=GridStatus.ACTIVE,
        parameters={"total_notional": notional},
        opened_at=datetime.now(tz=UTC),
    )


class TestApprovalHappyPath:
    def test_reasonable_grid_is_approved(self) -> None:
        engine = GridRiskEngine()
        result = engine.evaluate_new_grid(_params(), _snapshot(), current_price=Decimal("100"))

        assert result.approved is True


class TestMaxOpenGrids:
    def test_rejects_when_max_open_grids_reached(self) -> None:
        limits = GridRiskLimitsConfig(max_open_grids=1)
        engine = GridRiskEngine(limits)
        snapshot = _snapshot([_open_grid("100")])

        result = engine.evaluate_new_grid(_params(), snapshot, current_price=Decimal("100"))

        assert result.approved is False
        assert "offener Grids" in result.reason


class TestLeverageLimit:
    def test_rejects_leverage_above_limit(self) -> None:
        limits = GridRiskLimitsConfig(max_leverage=Decimal("3"))
        engine = GridRiskEngine(limits)

        result = engine.evaluate_new_grid(
            _params(leverage=Decimal("10")), _snapshot(), current_price=Decimal("100")
        )

        assert result.approved is False
        assert "Leverage" in result.reason


class TestMaxGridOrders:
    def test_rejects_grid_count_above_limit(self) -> None:
        limits = GridRiskLimitsConfig(max_grid_orders=3)
        engine = GridRiskEngine(limits)

        result = engine.evaluate_new_grid(
            _params(grid_count=10), _snapshot(), current_price=Decimal("100")
        )

        assert result.approved is False
        assert "grid_count" in result.reason


class TestMaximumGridExposure:
    def test_rejects_single_grid_over_position_cap(self) -> None:
        limits = GridRiskLimitsConfig(max_grid_position_usd=Decimal("100"))
        engine = GridRiskEngine(limits)

        result = engine.evaluate_new_grid(
            _params(position_size=Decimal("50"), grid_count=5),  # total_notional=250
            _snapshot(),
            current_price=Decimal("100"),
        )

        assert result.approved is False
        assert "Grid-Notional" in result.reason

    def test_rejects_when_portfolio_wide_exposure_would_be_exceeded(self) -> None:
        limits = GridRiskLimitsConfig(
            max_grid_exposure_usd=Decimal("300"), max_grid_position_usd=Decimal("10000")
        )
        engine = GridRiskEngine(limits)
        snapshot = _snapshot([_open_grid("200")])

        result = engine.evaluate_new_grid(
            _params(position_size=Decimal("50"), grid_count=5),  # +250 notional
            snapshot,
            current_price=Decimal("100"),
        )

        assert result.approved is False
        assert "Portfolio-weite" in result.reason


class TestLiquidityAndVolatilityLimits:
    def test_rejects_illiquid_market(self) -> None:
        limits = GridRiskLimitsConfig(min_liquidity_usd=Decimal("1000000"))
        engine = GridRiskEngine(limits)

        result = engine.evaluate_new_grid(
            _params(), _snapshot(), current_price=Decimal("100"), liquidity_usd=Decimal("1000")
        )

        assert result.approved is False
        assert "Liquiditaet" in result.reason

    def test_rejects_extreme_volatility(self) -> None:
        limits = GridRiskLimitsConfig(max_volatility_atr_pct=0.05)
        engine = GridRiskEngine(limits)

        result = engine.evaluate_new_grid(
            _params(), _snapshot(), current_price=Decimal("100"), volatility_atr_pct=0.20
        )

        assert result.approved is False
        assert "ATR" in result.reason


class TestFundingCostLimit:
    def test_rejects_unusual_funding_rate(self) -> None:
        limits = GridRiskLimitsConfig(max_funding_cost_pct=0.0005)
        engine = GridRiskEngine(limits)

        result = engine.evaluate_new_grid(
            _params(),
            _snapshot(),
            current_price=Decimal("100"),
            funding_rate_annualized_pct=500.0,
        )

        assert result.approved is False
        assert "Funding" in result.reason


class TestLiquidationDistance:
    def test_rejects_leverage_too_close_to_liquidation(self) -> None:
        limits = GridRiskLimitsConfig(max_liquidation_distance_pct=0.5, max_leverage=Decimal("10"))
        engine = GridRiskEngine(limits)

        # leverage=2 -> Liquidationsdistanz = 1/2 = 50%, genau am Limit
        # von 50% - leverage=3 unterschreitet es (1/3 ≈ 33%).
        result = engine.evaluate_new_grid(
            _params(leverage=Decimal("3")), _snapshot(), current_price=Decimal("100")
        )

        assert result.approved is False
        assert "Liquidationsdistanz" in result.reason


class TestOngoingGridChecks:
    def test_max_grid_loss_triggers_hard_violation(self) -> None:
        limits = GridRiskLimitsConfig(max_grid_loss_usd=Decimal("50"))
        engine = GridRiskEngine(limits)
        grid = _open_grid("250")
        grid.realized_pnl = Decimal("-100")

        violations = engine.check_ongoing_grid(grid, current_price=Decimal("100"))

        codes = [v.code for v in violations]
        assert "max_grid_loss_exceeded" in codes
        assert all(v.severity == "hard" for v in violations if v.code == "max_grid_loss_exceeded")

    def test_no_violations_for_healthy_grid(self) -> None:
        engine = GridRiskEngine()
        grid = _open_grid("100")

        violations = engine.check_ongoing_grid(grid, current_price=Decimal("100"))

        assert violations == []

    def test_stop_loss_hit_for_long_grid(self) -> None:
        engine = GridRiskEngine()
        grid = _open_grid("100")
        grid.parameters["stop_loss"] = "95"

        violations = engine.check_ongoing_grid(grid, current_price=Decimal("94"))

        assert any(v.code == "stop_loss_hit" for v in violations)

    def test_funding_violation_during_runtime(self) -> None:
        limits = GridRiskLimitsConfig(max_funding_cost_pct=0.0005)
        engine = GridRiskEngine(limits)
        grid = _open_grid("100")

        violations = engine.check_ongoing_grid(
            grid, current_price=Decimal("100"), funding_rate_annualized_pct=500.0
        )

        assert any(v.code == "funding_cost_limit_exceeded" for v in violations)
