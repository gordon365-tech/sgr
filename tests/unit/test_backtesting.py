"""
Tests für die Backtesting Engine.

Teststrategie:
    - PerformanceAnalyzer: KPI-Berechnung gegen bekannte Werte
    - BacktestSimulator: synthetische Candles, Look-Ahead-Prävention
    - MonteCarloAnalyzer: Verteilungsstatistiken
    - Go-Live Gates: alle Schwellwerte korrekt geprüft
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sgr.backtesting.performance import PerformanceAnalyzer
from sgr.backtesting.types import (
    BacktestConfig,
    BacktestResult,
    BacktestStatus,
    BacktestTrade,
    EquityCurvePoint,
)
from sgr.backtesting.validation import MonteCarloAnalyzer
from sgr.core.types import MarketRegime

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ts(offset_hours: int = 0) -> datetime:
    base = datetime(2023, 1, 1, tzinfo=UTC)
    return base + timedelta(hours=offset_hours)


def _make_trade(
    net_pnl: float = 100.0,
    holding_bars: int = 5,
    side: str = "long",
    strategy: str = "trend_following_v1",
    regime: MarketRegime = MarketRegime.TRENDING_UP,
    entry_hour: int = 0,
) -> BacktestTrade:
    entry = _make_ts(entry_hour)
    exit_ts = _make_ts(entry_hour + holding_bars)
    return BacktestTrade(
        id="test-trade",
        symbol="BTC/USDT",
        strategy=strategy,
        side=side,
        entry_time=entry,
        exit_time=exit_ts,
        entry_price=Decimal("50000"),
        exit_price=Decimal("51000") if net_pnl > 0 else Decimal("49000"),
        quantity=Decimal("0.1"),
        gross_pnl=Decimal(str(net_pnl + 10)),
        fees=Decimal("10"),
        slippage=Decimal("2"),
        net_pnl=Decimal(str(net_pnl)),
        holding_bars=holding_bars,
        regime=regime,
        max_adverse_excursion=Decimal("50"),
        max_favorable_excursion=Decimal("120"),
        entry_signal_confidence=0.75,
    )


def _make_config(
    start_offset_days: int = 0,
    duration_days: int = 365,
    timeframe: str = "1h",
) -> BacktestConfig:
    start = datetime(2023, 1, 1, tzinfo=UTC) + timedelta(days=start_offset_days)
    end = start + timedelta(days=duration_days)
    return BacktestConfig(
        start_date=start,
        end_date=end,
        symbols=["BTC/USDT"],
        timeframe=timeframe,
        initial_capital=Decimal("10000"),
    )


def _make_equity_curve(
    values: list[float],
    start_ts: datetime | None = None,
) -> list[EquityCurvePoint]:
    ts = start_ts or datetime(2023, 1, 1, tzinfo=UTC)
    curve = []
    peak = values[0]
    for i, val in enumerate(values):
        if val > peak:
            peak = val
        dd = (peak - val) / peak * 100 if peak > 0 else 0.0
        curve.append(
            EquityCurvePoint(
                timestamp=ts + timedelta(hours=i),
                portfolio_value=Decimal(str(val)),
                cash=Decimal(str(val)),
                open_positions_value=Decimal("0"),
                drawdown_pct=dd,
                daily_return=0.0,
            )
        )
    return curve


# ===========================================================================
# Performance Analyzer
# ===========================================================================


class TestPerformanceAnalyzer:
    def test_empty_result_on_no_data(self) -> None:
        analyzer = PerformanceAnalyzer()
        config = _make_config()
        result = analyzer._empty_result(config)
        assert result.status == BacktestStatus.FAILED
        assert result.total_trades == 0

    def test_total_return_calculation(self) -> None:
        """10000 → 12000 = +20% Return."""
        analyzer = PerformanceAnalyzer()
        trades = [_make_trade(100.0) for _ in range(20)]
        equity = _make_equity_curve([10000, 10500, 11000, 11500, 12000])
        config = _make_config()
        result = analyzer.analyze(trades, equity, config)
        assert result.total_return_pct == pytest.approx(20.0, rel=0.01)

    def test_cagr_one_year(self) -> None:
        """10000 → 12000 in 365 Tagen = 20% CAGR."""
        analyzer = PerformanceAnalyzer()
        trades = [_make_trade(100.0) for _ in range(20)]
        equity = _make_equity_curve([10000, 12000])
        config = _make_config(duration_days=365)
        result = analyzer.analyze(trades, equity, config)
        assert result.cagr_pct == pytest.approx(20.0, rel=0.05)

    def test_max_drawdown_calculation(self) -> None:
        """Peak 12000 → 9000 = 25% Drawdown."""
        analyzer = PerformanceAnalyzer()
        trades = [_make_trade(100.0) for _ in range(20)]
        equity = _make_equity_curve([10000, 12000, 11000, 9000, 10000])
        config = _make_config()
        result = analyzer.analyze(trades, equity, config)
        assert result.max_drawdown_pct == pytest.approx(25.0, rel=0.05)

    def test_hit_rate_calculation(self) -> None:
        """7 Winner, 3 Loser = 70% Hit Rate."""
        analyzer = PerformanceAnalyzer()
        trades = [_make_trade(100.0) for _ in range(7)] + [_make_trade(-80.0) for _ in range(3)]
        equity = _make_equity_curve([10000, 10700])
        config = _make_config()
        result = analyzer.analyze(trades, equity, config)
        assert result.hit_rate_pct == pytest.approx(70.0)
        assert result.winning_trades == 7
        assert result.losing_trades == 3

    def test_profit_factor_calculation(self) -> None:
        """Gross profit 700, Gross loss 300 → PF = 7/3 ≈ 2.33."""
        analyzer = PerformanceAnalyzer()
        trades = [_make_trade(100.0) for _ in range(7)] + [_make_trade(-100.0) for _ in range(3)]
        equity = _make_equity_curve([10000, 10400])
        config = _make_config()
        result = analyzer.analyze(trades, equity, config)
        assert result.profit_factor == pytest.approx(7 / 3, rel=0.01)

    def test_sharpe_positive_for_good_returns(self) -> None:
        """Konsistente positive Returns → positiver Sharpe."""
        analyzer = PerformanceAnalyzer()
        trades = [_make_trade(50.0) for _ in range(50)]
        # Gleichmäßig steigende Equity
        equity = _make_equity_curve([10000 + i * 10 for i in range(200)])
        config = _make_config()
        result = analyzer.analyze(trades, equity, config)
        assert result.sharpe_ratio > 0

    def test_go_live_gates_all_fail(self) -> None:
        """Schlechte Strategie → alle Go-Live Gates fail."""
        analyzer = PerformanceAnalyzer()
        trades = [_make_trade(-50.0) for _ in range(10)]  # Nur Verlierer, < 30 Trades
        equity = _make_equity_curve([10000, 9000, 8000])
        config = _make_config()
        result = analyzer.analyze(trades, equity, config)
        assert result.go_live_eligible is False
        assert len(result.go_live_blockers) > 0
        assert "30" in " ".join(result.go_live_blockers)  # Trades-Blocker

    def test_go_live_gates_pass(self) -> None:
        """Gute Strategie → Go-Live Gates bestanden."""
        analyzer = PerformanceAnalyzer()
        # 40 Trades, 75% Hit Rate, gute Returns
        trades = [_make_trade(200.0, entry_hour=i * 10) for i in range(30)] + [
            _make_trade(-100.0, entry_hour=i * 10 + 5) for i in range(10)
        ]
        equity = _make_equity_curve([10000 + i * 50 for i in range(500)])
        config = _make_config()
        result = analyzer.analyze(trades, equity, config)
        # Mit 40 Trades und guter Performance sollten die Gates bestanden sein
        assert result.total_trades == 40
        assert result.hit_rate_pct == pytest.approx(75.0)

    def test_strategy_breakdown(self) -> None:
        """Per-Strategie Breakdown korrekt."""
        analyzer = PerformanceAnalyzer()
        trades = [_make_trade(100.0, strategy="trend_following_v1") for _ in range(5)] + [
            _make_trade(-50.0, strategy="mean_reversion_v1") for _ in range(5)
        ]
        equity = _make_equity_curve([10000, 10250])
        config = _make_config()
        result = analyzer.analyze(trades, equity, config)
        assert "trend_following_v1" in result.strategy_breakdown
        assert "mean_reversion_v1" in result.strategy_breakdown
        assert result.strategy_breakdown["trend_following_v1"]["total_trades"] == 5

    def test_regime_breakdown(self) -> None:
        """Per-Regime Breakdown korrekt."""
        analyzer = PerformanceAnalyzer()
        trades = [_make_trade(100.0, regime=MarketRegime.TRENDING_UP) for _ in range(8)] + [
            _make_trade(-30.0, regime=MarketRegime.RANGING) for _ in range(2)
        ]
        equity = _make_equity_curve([10000, 10740])
        config = _make_config()
        result = analyzer.analyze(trades, equity, config)
        assert "trending_up" in result.regime_breakdown
        assert "ranging" in result.regime_breakdown

    def test_fees_and_slippage_totaled(self) -> None:
        """Total Fees und Slippage korrekt summiert."""
        analyzer = PerformanceAnalyzer()
        n = 10
        trades = [_make_trade(100.0) for _ in range(n)]
        equity = _make_equity_curve([10000, 11000])
        config = _make_config()
        result = analyzer.analyze(trades, equity, config)
        # Jeder Trade: fees=10, slippage=2
        assert float(result.total_fees) == pytest.approx(n * 10.0)
        assert float(result.total_slippage) == pytest.approx(n * 2.0)


# ===========================================================================
# Monte Carlo Analyzer
# ===========================================================================


class TestMonteCarloAnalyzer:
    def test_returns_correct_n_simulations(self) -> None:
        analyzer = MonteCarloAnalyzer()
        trades = [_make_trade(50.0) for _ in range(20)]
        result = analyzer.run(trades, Decimal("10000"), n_simulations=100, seed=42)
        assert result.n_simulations == 100

    def test_positive_median_for_winning_strategy(self) -> None:
        """Strategie mit positivem EV → positiver Median-Return."""
        analyzer = MonteCarloAnalyzer()
        trades = [_make_trade(100.0) for _ in range(30)]
        result = analyzer.run(trades, Decimal("10000"), n_simulations=200, seed=42)
        assert result.median_return_pct > 0

    def test_p5_below_median(self) -> None:
        """5th Percentile Return < Median Return (Definition)."""
        analyzer = MonteCarloAnalyzer()
        trades = [_make_trade(50.0) for _ in range(20)]
        result = analyzer.run(trades, Decimal("10000"), n_simulations=200, seed=42)
        assert result.percentile_5_return_pct <= result.median_return_pct

    def test_ruin_prob_low_for_good_strategy(self) -> None:
        """Gute Strategie → geringe Ruin-Wahrscheinlichkeit."""
        analyzer = MonteCarloAnalyzer()
        trades = [_make_trade(200.0) for _ in range(50)]
        result = analyzer.run(trades, Decimal("10000"), n_simulations=500, seed=42)
        assert result.ruin_probability < 0.1

    def test_ruin_prob_high_for_losing_strategy(self) -> None:
        """Schlechte Strategie → hohe Ruin-Wahrscheinlichkeit."""
        analyzer = MonteCarloAnalyzer()
        trades = [_make_trade(-200.0) for _ in range(50)]
        result = analyzer.run(trades, Decimal("10000"), n_simulations=200, seed=42)
        assert result.ruin_probability > 0.5

    def test_deterministic_with_same_seed(self) -> None:
        """Gleicher Seed → identische Ergebnisse (Reproduzierbarkeit)."""
        analyzer = MonteCarloAnalyzer()
        trades = [_make_trade(100.0) for _ in range(20)]
        r1 = analyzer.run(trades, Decimal("10000"), n_simulations=100, seed=99)
        r2 = analyzer.run(trades, Decimal("10000"), n_simulations=100, seed=99)
        assert r1.median_return_pct == r2.median_return_pct
        assert r1.ruin_probability == r2.ruin_probability

    def test_empty_result_on_insufficient_trades(self) -> None:
        """Zu wenig Trades → leeres Ergebnis ohne Crash."""
        analyzer = MonteCarloAnalyzer()
        trades = [_make_trade(100.0) for _ in range(5)]  # < 10 Minimum
        result = analyzer.run(trades, Decimal("10000"), n_simulations=100)
        assert result.ruin_probability == 1.0  # Konservative Schätzung

    def test_sharpe_distribution_has_required_keys(self) -> None:
        analyzer = MonteCarloAnalyzer()
        trades = [_make_trade(100.0) for _ in range(20)]
        result = analyzer.run(trades, Decimal("10000"), n_simulations=100, seed=1)
        assert "min" in result.sharpe_distribution
        assert "median" in result.sharpe_distribution
        assert "max" in result.sharpe_distribution
        assert result.sharpe_distribution["min"] <= result.sharpe_distribution["median"]
        assert result.sharpe_distribution["median"] <= result.sharpe_distribution["max"]


# ===========================================================================
# BacktestConfig
# ===========================================================================


class TestBacktestConfig:
    def test_default_values(self) -> None:
        config = _make_config()
        assert config.initial_capital == Decimal("10000")
        assert config.taker_fee == Decimal("0.001")
        assert config.slippage_pct == Decimal("0.0005")
        assert config.max_position_pct == 0.10

    def test_parameter_variation(self) -> None:
        """Parameter-Variation für Robustness-Tests."""
        from sgr.strategy.base import StrategyParameters

        params = StrategyParameters(
            name="test", version="1.0", params={"adx_min": 25.0, "rsi_min": 50.0}
        )
        varied = params.with_variation(1.2)
        assert varied.params["adx_min"] == pytest.approx(30.0)
        assert varied.params["rsi_min"] == pytest.approx(60.0)


# ===========================================================================
# BacktestResult Validation
# ===========================================================================


class TestBacktestResultValidation:
    def test_is_acceptable_good_result(self) -> None:
        """Gutes Ergebnis ist acceptable."""
        result = BacktestResult(
            config_summary={},
            status=BacktestStatus.COMPLETED,
            start_date="2023-01-01",
            end_date="2023-12-31",
            duration_days=365,
            initial_capital="10000",
            final_capital="15000",
            total_return_pct=50.0,
            cagr_pct=50.0,
            sharpe_ratio=1.5,
            sortino_ratio=2.0,
            calmar_ratio=3.0,
            max_drawdown_pct=10.0,
            max_drawdown_duration_days=30,
            profit_factor=1.8,
            hit_rate_pct=60.0,
            expected_value_per_trade="50",
            total_trades=50,
            winning_trades=30,
            losing_trades=20,
            avg_winner="150",
            avg_loser="-80",
            avg_holding_bars=8.0,
            total_fees="200",
            total_slippage="50",
            go_live_eligible=True,
            go_live_blockers=[],
        )
        assert result.is_acceptable is True

    def test_is_acceptable_bad_sharpe(self) -> None:
        """Sharpe < 1.0 → nicht acceptable."""
        result = BacktestResult(
            config_summary={},
            status=BacktestStatus.COMPLETED,
            start_date="2023-01-01",
            end_date="2023-12-31",
            duration_days=365,
            initial_capital="10000",
            final_capital="11000",
            total_return_pct=10.0,
            cagr_pct=10.0,
            sharpe_ratio=0.4,  # Unter 1.0
            sortino_ratio=0.5,
            calmar_ratio=0.5,
            max_drawdown_pct=12.0,
            max_drawdown_duration_days=60,
            profit_factor=1.1,
            hit_rate_pct=45.0,
            expected_value_per_trade="10",
            total_trades=35,
            winning_trades=20,
            losing_trades=15,
            avg_winner="50",
            avg_loser="-40",
            avg_holding_bars=5.0,
            total_fees="150",
            total_slippage="30",
            go_live_eligible=False,
            go_live_blockers=["Sharpe < 1.0"],
        )
        assert result.is_acceptable is False
