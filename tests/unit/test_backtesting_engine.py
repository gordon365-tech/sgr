"""
Tests für sgr.backtesting.engine – BacktestingEngine / FullValidationReport.
Coverage-Ziel: ~33% -> 100%.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sgr.backtesting.engine import BacktestingEngine, FullValidationReport
from sgr.backtesting.types import (
    BacktestResult,
    BacktestStatus,
    MonteCarloResult,
    WalkForwardResult,
)

# ===========================================================================
# Helpers
# ===========================================================================


def make_backtest_result(
    *,
    total_trades: int = 30,
    sharpe_ratio: float = 1.5,
    max_drawdown_pct: float = 10.0,
    profit_factor: float = 1.5,
    go_live_blockers: list[str] | None = None,
) -> BacktestResult:
    return BacktestResult(
        config_summary={},
        status=BacktestStatus.COMPLETED,
        start_date="2022-01-01",
        end_date="2023-12-31",
        duration_days=730,
        initial_capital="10000",
        final_capital="15000",
        total_return_pct=50.0,
        cagr_pct=20.0,
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=1.8,
        calmar_ratio=2.0,
        max_drawdown_pct=max_drawdown_pct,
        max_drawdown_duration_days=15,
        profit_factor=profit_factor,
        hit_rate_pct=55.0,
        expected_value_per_trade="50",
        total_trades=total_trades,
        winning_trades=17,
        losing_trades=13,
        avg_winner="100",
        avg_loser="60",
        avg_holding_bars=12.0,
        total_fees="20",
        total_slippage="5",
        go_live_blockers=go_live_blockers or [],
    )


def make_walk_forward_result(
    *,
    is_consistent: bool = True,
    degradation_factor: float = 0.9,
) -> WalkForwardResult:
    return WalkForwardResult(
        n_splits=6,
        split_results=[],
        is_consistent=is_consistent,
        consistency_score=0.8,
        in_sample_sharpe=1.6,
        out_of_sample_sharpe=1.4,
        degradation_factor=degradation_factor,
        recommendation="Proceed",
    )


def make_monte_carlo_result(
    *,
    p95_drawdown: float = 15.0,
    ruin_probability: float = 0.01,
    p5_return: float = -5.0,
) -> MonteCarloResult:
    return MonteCarloResult(
        n_simulations=1000,
        median_return_pct=25.0,
        percentile_5_return_pct=p5_return,
        percentile_95_return_pct=60.0,
        median_max_drawdown_pct=10.0,
        percentile_95_max_drawdown_pct=p95_drawdown,
        ruin_probability=ruin_probability,
        sharpe_distribution={"min": 0.5, "p25": 1.0, "median": 1.5, "p75": 2.0, "max": 2.5},
    )


class FakeCandle:
    def __init__(self, timestamp: datetime) -> None:
        self.timestamp = timestamp


# ===========================================================================
# FullValidationReport.to_dict
# ===========================================================================


class TestFullValidationReportToDict:
    def test_to_dict_without_wf_and_mc(self) -> None:
        backtest = make_backtest_result()
        report = FullValidationReport(
            strategy_names=["trend_v1"],
            symbols=["BTC/USDT"],
            timeframe="1h",
            start_date="2022-01-01T00:00:00",
            end_date="2023-12-31T00:00:00",
            backtest=backtest,
            go_live_decision="GO",
            decision_summary="All good",
        )

        result = report.to_dict()

        assert result["go_live_decision"] == "GO"
        assert result["walk_forward"] is None
        assert result["monte_carlo"] is None
        assert result["backtest_kpis"]["sharpe_ratio"] == backtest.sharpe_ratio
        assert "→" in result["period"]

    def test_to_dict_with_wf_and_mc(self) -> None:
        backtest = make_backtest_result()
        wf = make_walk_forward_result()
        mc = make_monte_carlo_result()
        report = FullValidationReport(
            strategy_names=["trend_v1"],
            symbols=["BTC/USDT"],
            timeframe="1h",
            start_date="2022-01-01T00:00:00",
            end_date="2023-12-31T00:00:00",
            backtest=backtest,
            walk_forward=wf,
            monte_carlo=mc,
            go_live_decision="GO",
        )

        result = report.to_dict()

        assert result["walk_forward"]["is_consistent"] is True
        assert result["walk_forward"]["recommendation"] == "Proceed"
        assert result["monte_carlo"]["n_simulations"] == 1000
        assert result["monte_carlo"]["ruin_probability"] == 0.01


# ===========================================================================
# BacktestingEngine.run_full_validation
# ===========================================================================


class TestRunFullValidation:
    def _build_engine_with_mocks(
        self,
        *,
        registry_is_active: bool = False,
        candles_by_symbol: dict[str, list] | None = None,
    ) -> tuple[BacktestingEngine, dict]:
        engine = BacktestingEngine()

        mock_registry = MagicMock()
        mock_registry.is_active.return_value = registry_is_active
        mock_registry.activate = AsyncMock()

        engine._loader = AsyncMock()
        engine._loader.load_from_exchange = AsyncMock(
            side_effect=lambda symbol, **kwargs: (candles_by_symbol or {}).get(symbol, [])
        )

        engine._analyzer = MagicMock()
        engine._wf_analyzer = AsyncMock()
        engine._mc_analyzer = MagicMock()

        return engine, {"registry": mock_registry}

    @pytest.mark.asyncio
    async def test_no_data_loaded_returns_no_go(self) -> None:
        engine, mocks = self._build_engine_with_mocks(candles_by_symbol={"BTC/USDT": []})
        empty_result = make_backtest_result(total_trades=0)
        engine._analyzer._empty_result.return_value = empty_result

        with patch("sgr.backtesting.engine.StrategyRegistry.get", return_value=mocks["registry"]):
            report = await engine.run_full_validation(
                strategy_names=["trend_v1"],
                symbols=["BTC/USDT"],
                timeframe="1h",
                start_date=datetime(2022, 1, 1),
                end_date=datetime(2023, 1, 1),
                exchange_pool=MagicMock(),
            )

        assert report.go_live_decision == "NO-GO"
        assert report.decision_summary == "No historical data available."
        assert report.blockers == ["Data loading failed"]
        mocks["registry"].activate.assert_awaited_once_with("trend_v1")

    @pytest.mark.asyncio
    async def test_activates_only_inactive_strategies(self) -> None:
        engine, mocks = self._build_engine_with_mocks(
            registry_is_active=True, candles_by_symbol={"BTC/USDT": []}
        )
        engine._analyzer._empty_result.return_value = make_backtest_result(total_trades=0)

        with patch("sgr.backtesting.engine.StrategyRegistry.get", return_value=mocks["registry"]):
            await engine.run_full_validation(
                strategy_names=["trend_v1"],
                symbols=["BTC/USDT"],
                timeframe="1h",
                start_date=datetime(2022, 1, 1),
                end_date=datetime(2023, 1, 1),
                exchange_pool=MagicMock(),
            )

        mocks["registry"].activate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_full_path_go_decision_with_wf_and_mc(self) -> None:
        candle = FakeCandle(datetime(2022, 1, 1))
        engine, mocks = self._build_engine_with_mocks(candles_by_symbol={"BTC/USDT": [candle]})

        backtest_result = make_backtest_result(total_trades=30, sharpe_ratio=2.0)
        engine._analyzer.analyze.return_value = backtest_result

        wf_result = make_walk_forward_result(is_consistent=True)
        engine._wf_analyzer.run = AsyncMock(return_value=wf_result)

        mc_result = make_monte_carlo_result()
        engine._mc_analyzer.run.return_value = mc_result

        trades = [MagicMock()]
        equity_curve = [MagicMock()]

        mock_simulator = AsyncMock()
        mock_simulator.run = AsyncMock(return_value=(trades, equity_curve))

        with (
            patch("sgr.backtesting.engine.StrategyRegistry.get", return_value=mocks["registry"]),
            patch("sgr.backtesting.engine.BacktestSimulator", return_value=mock_simulator),
        ):
            report = await engine.run_full_validation(
                strategy_names=["trend_v1"],
                symbols=["BTC/USDT"],
                timeframe="1h",
                start_date=datetime(2022, 1, 1),
                end_date=datetime(2023, 1, 1),
                exchange_pool=MagicMock(),
            )

        assert report.go_live_decision == "GO"
        assert report.walk_forward is wf_result
        assert report.monte_carlo is mc_result
        engine._wf_analyzer.run.assert_awaited_once()
        engine._mc_analyzer.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_walk_forward_when_too_few_trades(self) -> None:
        candle = FakeCandle(datetime(2022, 1, 1))
        engine, mocks = self._build_engine_with_mocks(candles_by_symbol={"BTC/USDT": [candle]})
        backtest_result = make_backtest_result(total_trades=5)
        engine._analyzer.analyze.return_value = backtest_result
        engine._mc_analyzer.run.return_value = make_monte_carlo_result()

        mock_simulator = AsyncMock()
        mock_simulator.run = AsyncMock(return_value=([MagicMock()], [MagicMock()]))

        with (
            patch("sgr.backtesting.engine.StrategyRegistry.get", return_value=mocks["registry"]),
            patch("sgr.backtesting.engine.BacktestSimulator", return_value=mock_simulator),
        ):
            report = await engine.run_full_validation(
                strategy_names=["trend_v1"],
                symbols=["BTC/USDT"],
                timeframe="1h",
                start_date=datetime(2022, 1, 1),
                end_date=datetime(2023, 1, 1),
                exchange_pool=MagicMock(),
            )

        assert report.walk_forward is None
        engine._wf_analyzer.run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_monte_carlo_when_no_trades(self) -> None:
        candle = FakeCandle(datetime(2022, 1, 1))
        engine, mocks = self._build_engine_with_mocks(candles_by_symbol={"BTC/USDT": [candle]})
        backtest_result = make_backtest_result(total_trades=25)
        engine._analyzer.analyze.return_value = backtest_result

        mock_simulator = AsyncMock()
        mock_simulator.run = AsyncMock(return_value=([], [MagicMock()]))

        with (
            patch("sgr.backtesting.engine.StrategyRegistry.get", return_value=mocks["registry"]),
            patch("sgr.backtesting.engine.BacktestSimulator", return_value=mock_simulator),
        ):
            report = await engine.run_full_validation(
                strategy_names=["trend_v1"],
                symbols=["BTC/USDT"],
                timeframe="1h",
                start_date=datetime(2022, 1, 1),
                end_date=datetime(2023, 1, 1),
                exchange_pool=MagicMock(),
                run_walk_forward=False,
            )

        assert report.monte_carlo is None
        engine._mc_analyzer.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_walk_forward_and_monte_carlo_flags_disabled(self) -> None:
        candle = FakeCandle(datetime(2022, 1, 1))
        engine, mocks = self._build_engine_with_mocks(candles_by_symbol={"BTC/USDT": [candle]})
        backtest_result = make_backtest_result(total_trades=30)
        engine._analyzer.analyze.return_value = backtest_result

        mock_simulator = AsyncMock()
        mock_simulator.run = AsyncMock(return_value=([MagicMock()], [MagicMock()]))

        with (
            patch("sgr.backtesting.engine.StrategyRegistry.get", return_value=mocks["registry"]),
            patch("sgr.backtesting.engine.BacktestSimulator", return_value=mock_simulator),
        ):
            report = await engine.run_full_validation(
                strategy_names=["trend_v1"],
                symbols=["BTC/USDT"],
                timeframe="1h",
                start_date=datetime(2022, 1, 1),
                end_date=datetime(2023, 1, 1),
                exchange_pool=MagicMock(),
                run_walk_forward=False,
                run_monte_carlo=False,
            )

        assert report.walk_forward is None
        assert report.monte_carlo is None
        engine._wf_analyzer.run.assert_not_awaited()
        engine._mc_analyzer.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_symbols_loaded(self) -> None:
        candle = FakeCandle(datetime(2022, 1, 1))
        engine, mocks = self._build_engine_with_mocks(
            candles_by_symbol={"BTC/USDT": [candle], "ETH/USDT": [candle]}
        )
        backtest_result = make_backtest_result(total_trades=30)
        engine._analyzer.analyze.return_value = backtest_result
        engine._mc_analyzer.run.return_value = make_monte_carlo_result()

        mock_simulator = AsyncMock()
        mock_simulator.run = AsyncMock(return_value=([MagicMock()], [MagicMock()]))

        with (
            patch("sgr.backtesting.engine.StrategyRegistry.get", return_value=mocks["registry"]),
            patch("sgr.backtesting.engine.BacktestSimulator", return_value=mock_simulator),
        ):
            report = await engine.run_full_validation(
                strategy_names=["trend_v1"],
                symbols=["BTC/USDT", "ETH/USDT"],
                timeframe="1h",
                start_date=datetime(2022, 1, 1),
                end_date=datetime(2023, 1, 1),
                exchange_pool=MagicMock(),
                run_walk_forward=False,
            )

        assert engine._loader.load_from_exchange.await_count == 2
        assert report.symbols == ["BTC/USDT", "ETH/USDT"]


# ===========================================================================
# BacktestingEngine.run_quick_backtest
# ===========================================================================


class TestRunQuickBacktest:
    @pytest.mark.asyncio
    async def test_empty_candles_returns_empty_result(self) -> None:
        engine = BacktestingEngine()
        mock_registry = MagicMock()
        mock_registry.is_active.return_value = False
        mock_registry.activate = AsyncMock()
        engine._analyzer = MagicMock()
        empty_result = make_backtest_result(total_trades=0)
        engine._analyzer._empty_result.return_value = empty_result

        with patch("sgr.backtesting.engine.StrategyRegistry.get", return_value=mock_registry):
            result = await engine.run_quick_backtest(
                strategy_names=["trend_v1"],
                candles=[],
                symbol="BTC/USDT",
                timeframe="1h",
            )

        assert result is empty_result
        mock_registry.activate.assert_awaited_once_with("trend_v1")

    @pytest.mark.asyncio
    async def test_with_candles_runs_simulator_and_analyzer(self) -> None:
        engine = BacktestingEngine()
        mock_registry = MagicMock()
        mock_registry.is_active.return_value = True
        mock_registry.activate = AsyncMock()
        engine._analyzer = MagicMock()
        expected_result = make_backtest_result(total_trades=15)
        engine._analyzer.analyze.return_value = expected_result

        candles = [
            FakeCandle(datetime(2022, 1, 1)),
            FakeCandle(datetime(2022, 1, 2)),
        ]

        mock_simulator = AsyncMock()
        mock_simulator.run = AsyncMock(return_value=([MagicMock()], [MagicMock()]))

        with (
            patch("sgr.backtesting.engine.StrategyRegistry.get", return_value=mock_registry),
            patch("sgr.backtesting.engine.BacktestSimulator", return_value=mock_simulator),
        ):
            result = await engine.run_quick_backtest(
                strategy_names=["trend_v1"],
                candles=candles,
                symbol="BTC/USDT",
                timeframe="1h",
                initial_capital=Decimal("5000"),
            )

        assert result is expected_result
        mock_registry.activate.assert_not_awaited()
        mock_simulator.run.assert_awaited_once()


# ===========================================================================
# BacktestingEngine._make_decision
# ===========================================================================


class TestMakeDecision:
    def test_go_when_no_blockers(self) -> None:
        engine = BacktestingEngine()
        backtest = make_backtest_result(go_live_blockers=[])

        report = engine._make_decision(
            strategy_names=["trend_v1"],
            symbols=["BTC/USDT"],
            timeframe="1h",
            start_date="2022-01-01",
            end_date="2023-01-01",
            backtest=backtest,
            walk_forward=None,
            monte_carlo=None,
        )

        assert report.go_live_decision == "GO"
        assert "Paper Trading" in report.decision_summary
        assert report.blockers == []

    def test_no_go_when_backtest_has_blockers_and_low_sharpe(self) -> None:
        engine = BacktestingEngine()
        backtest = make_backtest_result(
            go_live_blockers=["Sharpe too low", "Drawdown too high", "Hit rate too low"],
            sharpe_ratio=0.3,
        )

        report = engine._make_decision(
            strategy_names=["trend_v1"],
            symbols=["BTC/USDT"],
            timeframe="1h",
            start_date="2022-01-01",
            end_date="2023-01-01",
            backtest=backtest,
            walk_forward=None,
            monte_carlo=None,
        )

        assert report.go_live_decision == "NO-GO"
        assert "Do not proceed to Live Trading" in report.decision_summary

    def test_conditional_when_few_blockers_and_decent_sharpe(self) -> None:
        engine = BacktestingEngine()
        backtest = make_backtest_result(
            go_live_blockers=["Minor issue"],
            sharpe_ratio=0.9,
        )

        report = engine._make_decision(
            strategy_names=["trend_v1"],
            symbols=["BTC/USDT"],
            timeframe="1h",
            start_date="2022-01-01",
            end_date="2023-01-01",
            backtest=backtest,
            walk_forward=None,
            monte_carlo=None,
        )

        assert report.go_live_decision == "CONDITIONAL"
        assert "Marginal result" in report.decision_summary

    def test_walk_forward_inconsistent_adds_blocker(self) -> None:
        engine = BacktestingEngine()
        backtest = make_backtest_result(go_live_blockers=[])
        wf = make_walk_forward_result(is_consistent=False, degradation_factor=0.3)

        report = engine._make_decision(
            strategy_names=["trend_v1"],
            symbols=["BTC/USDT"],
            timeframe="1h",
            start_date="2022-01-01",
            end_date="2023-01-01",
            backtest=backtest,
            walk_forward=wf,
            monte_carlo=None,
        )

        assert any("Walk-Forward inconsistent" in b for b in report.blockers)
        assert report.go_live_decision != "GO"

    def test_walk_forward_consistent_but_degraded_adds_warning(self) -> None:
        engine = BacktestingEngine()
        backtest = make_backtest_result(go_live_blockers=[])
        wf = make_walk_forward_result(is_consistent=True, degradation_factor=0.5)

        report = engine._make_decision(
            strategy_names=["trend_v1"],
            symbols=["BTC/USDT"],
            timeframe="1h",
            start_date="2022-01-01",
            end_date="2023-01-01",
            backtest=backtest,
            walk_forward=wf,
            monte_carlo=None,
        )

        assert report.blockers == []
        assert any("degradation" in w for w in report.warnings)
        assert report.go_live_decision == "GO"

    def test_walk_forward_consistent_no_degradation_no_warning(self) -> None:
        engine = BacktestingEngine()
        backtest = make_backtest_result(go_live_blockers=[])
        wf = make_walk_forward_result(is_consistent=True, degradation_factor=0.95)

        report = engine._make_decision(
            strategy_names=["trend_v1"],
            symbols=["BTC/USDT"],
            timeframe="1h",
            start_date="2022-01-01",
            end_date="2023-01-01",
            backtest=backtest,
            walk_forward=wf,
            monte_carlo=None,
        )

        assert report.warnings == []
        assert report.blockers == []

    def test_monte_carlo_high_drawdown_adds_blocker(self) -> None:
        engine = BacktestingEngine()
        backtest = make_backtest_result(go_live_blockers=[])
        mc = make_monte_carlo_result(p95_drawdown=30.0)

        report = engine._make_decision(
            strategy_names=["trend_v1"],
            symbols=["BTC/USDT"],
            timeframe="1h",
            start_date="2022-01-01",
            end_date="2023-01-01",
            backtest=backtest,
            walk_forward=None,
            monte_carlo=mc,
        )

        assert any("P95 Drawdown" in b for b in report.blockers)

    def test_monte_carlo_high_ruin_probability_adds_blocker(self) -> None:
        engine = BacktestingEngine()
        backtest = make_backtest_result(go_live_blockers=[])
        mc = make_monte_carlo_result(ruin_probability=0.1)

        report = engine._make_decision(
            strategy_names=["trend_v1"],
            symbols=["BTC/USDT"],
            timeframe="1h",
            start_date="2022-01-01",
            end_date="2023-01-01",
            backtest=backtest,
            walk_forward=None,
            monte_carlo=mc,
        )

        assert any("Ruin probability" in b for b in report.blockers)

    def test_monte_carlo_bad_p5_return_adds_warning(self) -> None:
        engine = BacktestingEngine()
        backtest = make_backtest_result(go_live_blockers=[])
        mc = make_monte_carlo_result(p5_return=-25.0)

        report = engine._make_decision(
            strategy_names=["trend_v1"],
            symbols=["BTC/USDT"],
            timeframe="1h",
            start_date="2022-01-01",
            end_date="2023-01-01",
            backtest=backtest,
            walk_forward=None,
            monte_carlo=mc,
        )

        assert any("P5 scenario" in w for w in report.warnings)

    def test_monte_carlo_good_result_no_blockers_or_warnings(self) -> None:
        engine = BacktestingEngine()
        backtest = make_backtest_result(go_live_blockers=[])
        mc = make_monte_carlo_result(p95_drawdown=10.0, ruin_probability=0.0, p5_return=5.0)

        report = engine._make_decision(
            strategy_names=["trend_v1"],
            symbols=["BTC/USDT"],
            timeframe="1h",
            start_date="2022-01-01",
            end_date="2023-01-01",
            backtest=backtest,
            walk_forward=None,
            monte_carlo=mc,
        )

        assert report.blockers == []
        assert report.warnings == []
        assert report.go_live_decision == "GO"
