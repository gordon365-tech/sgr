"""Tests für sgr.strategy.capital_allocation.CapitalAllocationEngine."""

from __future__ import annotations

from decimal import Decimal

from sgr.strategy.capital_allocation import AllocationCandidate, CapitalAllocationEngine
from sgr.strategy.grid_edge import GridEdgeMetrics


def _grid_metrics(**overrides) -> GridEdgeMetrics:
    base = dict(
        net_pnl=100,
        gross_pnl=120,
        fees=10,
        funding=10,
        slippage=5,
        win_rate=0.6,
        profit_factor=1.5,
        max_drawdown_pct=5,
        sharpe=1.5,
        sortino=2.0,
        grid_efficiency=0.83,
        capital_utilization=0.5,
        exposure_time=0.4,
        average_grid_capture=5,
        edge_stability=0.75,
        regime_compatibility=0.8,
    )
    base.update(overrides)
    return GridEdgeMetrics(**base)


class TestEdgeScore:
    def test_unvalidated_strategy_has_zero_score(self) -> None:
        c = AllocationCandidate("x", "spot", "binance", is_validated=False, directional_sharpe=3.0)
        assert c.edge_score() == 0.0

    def test_directional_score_penalized_by_drawdown(self) -> None:
        low_dd = AllocationCandidate(
            "a",
            "spot",
            "binance",
            is_validated=True,
            directional_sharpe=1.5,
            directional_max_drawdown_pct=5.0,
        )
        high_dd = AllocationCandidate(
            "b",
            "spot",
            "binance",
            is_validated=True,
            directional_sharpe=1.5,
            directional_max_drawdown_pct=35.0,
        )
        assert low_dd.edge_score() > high_dd.edge_score()

    def test_grid_score_penalized_by_low_efficiency(self) -> None:
        efficient = AllocationCandidate(
            "a",
            "futures_grid",
            "pionex",
            is_validated=True,
            grid_metrics=_grid_metrics(grid_efficiency=0.9),
        )
        inefficient = AllocationCandidate(
            "b",
            "futures_grid",
            "pionex",
            is_validated=True,
            grid_metrics=_grid_metrics(grid_efficiency=0.1),
        )
        assert efficient.edge_score() > inefficient.edge_score()

    def test_negative_sharpe_never_produces_negative_score(self) -> None:
        c = AllocationCandidate(
            "a",
            "futures_grid",
            "pionex",
            is_validated=True,
            grid_metrics=_grid_metrics(sharpe=-2.0),
        )
        assert c.edge_score() >= 0.0


class TestAllocation:
    def test_unvalidated_candidate_gets_zero_allocation(self) -> None:
        engine = CapitalAllocationEngine()
        c = AllocationCandidate("x", "spot", "binance", is_validated=False)

        results = engine.allocate([c], Decimal("10000"))

        assert results[0].allocated_capital == Decimal("0")
        assert "validiert" in results[0].reason

    def test_capital_distributed_proportionally_to_score(self) -> None:
        engine = CapitalAllocationEngine(max_single_strategy_fraction=1.0)
        strong = AllocationCandidate(
            "strong",
            "spot",
            "binance",
            is_validated=True,
            directional_sharpe=2.0,
            directional_max_drawdown_pct=5,
        )
        weak = AllocationCandidate(
            "weak",
            "spot",
            "binance",
            is_validated=True,
            directional_sharpe=0.5,
            directional_max_drawdown_pct=5,
        )

        results = {r.strategy_name: r for r in engine.allocate([strong, weak], Decimal("10000"))}

        assert results["strong"].allocated_capital > results["weak"].allocated_capital
        total_allocated = results["strong"].allocated_capital + results["weak"].allocated_capital
        assert total_allocated <= Decimal("10000")

    def test_single_strategy_cap_is_respected(self) -> None:
        engine = CapitalAllocationEngine(max_single_strategy_fraction=0.3)
        only_candidate = AllocationCandidate(
            "solo", "futures_grid", "pionex", is_validated=True, grid_metrics=_grid_metrics()
        )

        results = engine.allocate([only_candidate], Decimal("10000"))

        assert results[0].allocated_fraction <= 0.3
        assert results[0].allocated_capital <= Decimal("3000")

    def test_no_product_name_bias_grid_and_directional_compete_on_score_alone(self) -> None:
        """Kernanforderung: keine feste Bevorzugung aufgrund des
        Produktnamens - ein schwaches Grid darf NICHT mehr Kapital
        bekommen als eine starke direktionale Strategie."""
        engine = CapitalAllocationEngine(max_single_strategy_fraction=1.0)
        strong_directional = AllocationCandidate(
            "trend",
            "spot",
            "binance",
            is_validated=True,
            directional_sharpe=3.0,
            directional_max_drawdown_pct=3,
        )
        weak_grid = AllocationCandidate(
            "grid",
            "futures_grid",
            "pionex",
            is_validated=True,
            grid_metrics=_grid_metrics(sharpe=0.2, grid_efficiency=0.1, edge_stability=0.1),
        )

        results = {
            r.strategy_name: r
            for r in engine.allocate([strong_directional, weak_grid], Decimal("10000"))
        }

        assert results["trend"].allocated_capital > results["grid"].allocated_capital

    def test_below_threshold_candidates_receive_no_capital_and_reason(self) -> None:
        engine = CapitalAllocationEngine()
        c = AllocationCandidate(
            "grid",
            "futures_grid",
            "pionex",
            is_validated=True,
            grid_metrics=_grid_metrics(sharpe=0.0, grid_efficiency=0.0, edge_stability=0.0),
        )

        results = engine.allocate([c], Decimal("10000"), min_score_threshold=0.5)

        assert results[0].allocated_capital == Decimal("0")
        assert "Score" in results[0].reason
