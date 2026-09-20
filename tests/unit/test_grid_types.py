"""Tests für sgr.core.grid_types (Strategy Genome / GridState)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sgr.core.grid_types import FuturesGridParameters, GridLevelState, GridState
from sgr.core.types import (
    ExchangeID,
    GridDirection,
    GridSpacingMode,
    GridStatus,
    Symbol,
    TradingMode,
)


class TestFuturesGridParametersValidation:
    def test_rejects_lower_price_zero(self) -> None:
        with pytest.raises(ValueError, match="muessen > 0"):
            FuturesGridParameters(
                grid_lower_price=Decimal("0"),
                grid_upper_price=Decimal("100"),
                grid_count=5,
                long_or_short=GridDirection.LONG,
            )

    def test_rejects_lower_gte_upper(self) -> None:
        with pytest.raises(ValueError, match="muss < "):
            FuturesGridParameters(
                grid_lower_price=Decimal("100"),
                grid_upper_price=Decimal("100"),
                grid_count=5,
                long_or_short=GridDirection.LONG,
            )

    def test_rejects_grid_count_below_two(self) -> None:
        with pytest.raises(ValueError, match="grid_count"):
            FuturesGridParameters(
                grid_lower_price=Decimal("100"),
                grid_upper_price=Decimal("200"),
                grid_count=1,
                long_or_short=GridDirection.LONG,
            )

    def test_rejects_leverage_below_one(self) -> None:
        with pytest.raises(ValueError, match="leverage"):
            FuturesGridParameters(
                grid_lower_price=Decimal("100"),
                grid_upper_price=Decimal("200"),
                grid_count=5,
                long_or_short=GridDirection.LONG,
                leverage=Decimal("0.5"),
            )

    def test_rejects_neutral_direction(self) -> None:
        """NEUTRAL bedeutet 'kein Grid' - ein FuturesGridParameters-Objekt
        mit NEUTRAL waere ein Widerspruch in sich (siehe GridDecision fuer
        den korrekten Weg, 'kein Grid' auszudruecken)."""
        with pytest.raises(ValueError, match="NEUTRAL"):
            FuturesGridParameters(
                grid_lower_price=Decimal("100"),
                grid_upper_price=Decimal("200"),
                grid_count=5,
                long_or_short=GridDirection.NEUTRAL,
            )

    def test_immutable(self) -> None:
        params = FuturesGridParameters(
            grid_lower_price=Decimal("100"),
            grid_upper_price=Decimal("200"),
            grid_count=5,
            long_or_short=GridDirection.LONG,
        )
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            params.grid_count = 10  # type: ignore[misc]


class TestComputeLevels:
    def test_arithmetic_levels_are_evenly_spaced(self) -> None:
        params = FuturesGridParameters(
            grid_lower_price=Decimal("100"),
            grid_upper_price=Decimal("200"),
            grid_count=5,
            long_or_short=GridDirection.LONG,
            grid_mode=GridSpacingMode.ARITHMETIC,
        )
        levels = params.compute_levels()

        assert len(levels) == 5
        assert levels[0] == Decimal("100.00000000")
        assert levels[-1] == Decimal("200.00000000")
        spacings = [levels[i + 1] - levels[i] for i in range(len(levels) - 1)]
        assert len(set(spacings)) == 1  # alle Abstaende identisch

    def test_geometric_levels_have_constant_ratio(self) -> None:
        params = FuturesGridParameters(
            grid_lower_price=Decimal("100"),
            grid_upper_price=Decimal("200"),
            grid_count=5,
            long_or_short=GridDirection.LONG,
            grid_mode=GridSpacingMode.GEOMETRIC,
        )
        levels = params.compute_levels()

        assert levels[0] == Decimal("100.00000000")
        ratios = [float(levels[i + 1] / levels[i]) for i in range(len(levels) - 1)]
        assert max(ratios) - min(ratios) < 1e-6

    def test_effective_grid_spacing_uses_explicit_value_when_set(self) -> None:
        params = FuturesGridParameters(
            grid_lower_price=Decimal("100"),
            grid_upper_price=Decimal("200"),
            grid_count=5,
            long_or_short=GridDirection.LONG,
            grid_spacing=Decimal("42"),
        )
        assert params.effective_grid_spacing() == Decimal("42")

    def test_effective_grid_spacing_derived_when_not_set(self) -> None:
        params = FuturesGridParameters(
            grid_lower_price=Decimal("100"),
            grid_upper_price=Decimal("200"),
            grid_count=5,
            long_or_short=GridDirection.LONG,
        )
        assert params.effective_grid_spacing() == Decimal("25.00000000")

    def test_total_notional(self) -> None:
        params = FuturesGridParameters(
            grid_lower_price=Decimal("100"),
            grid_upper_price=Decimal("200"),
            grid_count=5,
            long_or_short=GridDirection.LONG,
            position_size=Decimal("20"),
        )
        assert params.total_notional() == Decimal("100")


class TestGridState:
    def _make_state(self) -> GridState:
        symbol = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX)
        return GridState(
            exchange=ExchangeID.PIONEX,
            symbol=symbol,
            strategy_name="futures_grid_long_v1",
            trading_mode=TradingMode.PAPER,
            direction=GridDirection.LONG,
            status=GridStatus.ACTIVE,
            levels=[
                GridLevelState(
                    index=0, price=Decimal("100"), side="buy", is_filled=True, last_order_id="x"
                ),
                GridLevelState(index=1, price=Decimal("110"), side="buy"),
            ],
            opened_at=datetime.now(tz=UTC),
        )

    def test_is_active_true_for_pending_and_active(self) -> None:
        state = self._make_state()
        assert state.is_active is True
        state.status = GridStatus.PENDING
        assert state.is_active is True
        state.status = GridStatus.CLOSED
        assert state.is_active is False

    def test_open_orders_count(self) -> None:
        state = self._make_state()
        # index 0 ist filled aber last_order_id gesetzt -> zaehlt laut
        # Definition (is_filled False + last_order_id) nicht; hier beide
        # Level sind entweder gefuellt oder haben keine Order.
        assert state.open_orders_count == 0
