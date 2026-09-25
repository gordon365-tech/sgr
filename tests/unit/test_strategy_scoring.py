"""Tests fuer sgr.strategy.strategy_scoring.score_validation()."""

from __future__ import annotations

from sgr.backtesting.types import BacktestResult, BacktestStatus, WalkForwardResult
from sgr.strategy.strategy_scoring import score_validation


def _backtest(
    *,
    sharpe: float = 1.5,
    profit_factor: float = 1.5,
    max_dd: float = 10.0,
    hit_rate: float = 50.0,
    trades: int = 60,
    trades_list: list[dict] | None = None,
) -> BacktestResult:
    return BacktestResult(
        config_summary={},
        status=BacktestStatus.COMPLETED,
        start_date="2026-01-01",
        end_date="2026-06-01",
        duration_days=150,
        initial_capital="10000",
        final_capital="11000",
        total_return_pct=10.0,
        cagr_pct=20.0,
        sharpe_ratio=sharpe,
        sortino_ratio=sharpe,
        calmar_ratio=1.0,
        max_drawdown_pct=max_dd,
        max_drawdown_duration_days=5,
        profit_factor=profit_factor,
        hit_rate_pct=hit_rate,
        expected_value_per_trade="10",
        total_trades=trades,
        winning_trades=int(trades * hit_rate / 100),
        losing_trades=trades - int(trades * hit_rate / 100),
        avg_winner="50",
        avg_loser="30",
        avg_holding_bars=10.0,
        total_fees="10",
        total_slippage="5",
        trades=trades_list or [],
        go_live_eligible=True,
        go_live_blockers=[],
    )


def _wf(
    *, consistent: bool = True, consistency_score: float = 0.8, degradation: float = 0.8
) -> WalkForwardResult:
    return WalkForwardResult(
        n_splits=6,
        split_results=[],
        is_consistent=consistent,
        consistency_score=consistency_score,
        in_sample_sharpe=1.5,
        out_of_sample_sharpe=1.2,
        degradation_factor=degradation,
        recommendation="PASS",
    )


class TestGates:
    def test_failing_backtest_gate_scores_zero(self) -> None:
        result = score_validation(_backtest(sharpe=0.2), _wf())
        assert result.score == 0.0
        assert not result.passes_gates

    def test_missing_walk_forward_scores_zero(self) -> None:
        result = score_validation(_backtest(), None)
        assert result.score == 0.0
        assert not result.passes_gates

    def test_inconsistent_walk_forward_scores_zero(self) -> None:
        result = score_validation(_backtest(), _wf(consistent=False))
        assert result.score == 0.0
        assert not result.passes_gates

    def test_passing_gates_yields_positive_score(self) -> None:
        result = score_validation(_backtest(), _wf())
        assert result.passes_gates
        assert result.score > 0.0
        assert result.score <= 100.0


class TestNotReturnDominated:
    """Phase 8: 'darf nicht ausschliesslich auf Return basieren' /
    'hohe Rendite und extremem Drawdown darf nicht automatisch gewinnen'."""

    def test_high_drawdown_scores_lower_than_low_drawdown_at_equal_sharpe(self) -> None:
        low_dd = score_validation(_backtest(max_dd=2.0), _wf())
        high_dd = score_validation(_backtest(max_dd=19.0), _wf())
        assert low_dd.score > high_dd.score

    def test_higher_sharpe_scores_higher_at_equal_drawdown(self) -> None:
        low_sharpe = score_validation(_backtest(sharpe=1.0), _wf())
        high_sharpe = score_validation(_backtest(sharpe=2.8), _wf())
        assert high_sharpe.score > low_sharpe.score

    def test_minimal_trade_count_scores_lower_than_ample_trade_count(self) -> None:
        """'Eine Strategie mit nur wenigen zufaelligen Trades darf
        ebenfalls nicht als robust bewertet werden' - bei identischem
        Sharpe/Drawdown muss mehr Trades einen hoeheren Score geben."""
        few_trades = score_validation(_backtest(trades=30), _wf())
        many_trades = score_validation(_backtest(trades=130), _wf())
        assert many_trades.score > few_trades.score


class TestRobustness:
    def test_profit_entirely_from_single_trade_lowers_robustness(self) -> None:
        trades_dependent = [
            {"net_pnl": "1000", "entry_time": "2026-01-01T00:00:00+00:00"},
            {"net_pnl": "-10", "entry_time": "2026-01-02T00:00:00+00:00"},
            {"net_pnl": "-10", "entry_time": "2026-01-03T00:00:00+00:00"},
        ]
        trades_distributed = [
            {"net_pnl": "300", "entry_time": "2026-01-01T00:00:00+00:00"},
            {"net_pnl": "300", "entry_time": "2026-01-02T00:00:00+00:00"},
            {"net_pnl": "300", "entry_time": "2026-01-03T00:00:00+00:00"},
        ]
        dependent = score_validation(_backtest(trades_list=trades_dependent), _wf())
        distributed = score_validation(_backtest(trades_list=trades_distributed), _wf())
        assert dependent.robustness_score < distributed.robustness_score
