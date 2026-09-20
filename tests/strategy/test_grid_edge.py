"""Tests für sgr.strategy.grid_edge (Grid Edge Engine)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sgr.backtesting.grid_simulator import GridBacktestSimulator
from sgr.backtesting.grid_types import GridBacktestConfig
from sgr.core.grid_types import FuturesGridParameters
from sgr.core.types import Candle, ExchangeID, GridDirection, Symbol
from sgr.strategy.grid_edge import GridEdgeMetrics, compute_grid_edge_metrics, is_edge_confirmed

SYMBOL = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX)


def _candle(ts, o, h, low, c) -> Candle:
    return Candle(
        symbol=SYMBOL,
        timestamp=ts,
        timeframe="1h",
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        volume=Decimal("10000"),
    )


def _oscillating_candles(n: int) -> list[Candle]:
    """Erzeugt eine bewusst grid-freundliche, oszillierende Serie
    zwischen ~90 und ~110, damit mehrere profitable Zyklen entstehen."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles = []
    price = 100.0
    direction = -1
    for i in range(n):
        price += direction * 2.0
        if price <= 91:
            direction = 1
        elif price >= 109:
            direction = -1
        o = price
        h = price + 1
        low = price - 1
        c = price
        candles.append(_candle(start + timedelta(hours=i), o, h, low, c))
    return candles


def _params() -> FuturesGridParameters:
    return FuturesGridParameters(
        grid_lower_price=Decimal("90"),
        grid_upper_price=Decimal("110"),
        grid_count=11,
        long_or_short=GridDirection.LONG,
        leverage=Decimal("2"),
        position_size=Decimal("50"),
        max_notional=Decimal("550"),
    )


def _run() -> tuple:
    config = GridBacktestConfig(
        symbol="BTC/USDT",
        timeframe="1h",
        strategy_name="futures_grid_long_v1",
        parameters=_params(),
        maker_fee=Decimal("0.0002"),
        taker_fee=Decimal("0.0005"),
        slippage_pct=Decimal("0.0001"),
    )
    sim = GridBacktestSimulator(config)
    candles = _oscillating_candles(300)
    run_result = sim.run(candles)
    backtest_result = sim.to_backtest_result(run_result, candles)
    return run_result, backtest_result


class TestComputeGridEdgeMetrics:
    def test_produces_all_required_fields(self) -> None:
        run_result, backtest_result = _run()
        metrics = compute_grid_edge_metrics(backtest_result, run_result, grid_count=11)

        assert isinstance(metrics, GridEdgeMetrics)
        assert metrics.net_pnl <= metrics.gross_pnl  # Fees/Funding koennen nur abziehen
        assert 0.0 <= metrics.grid_efficiency <= 1.0 or metrics.grid_efficiency == 0.0
        assert 0.0 <= metrics.capital_utilization <= 1.0
        assert 0.0 <= metrics.exposure_time <= 1.0
        assert 0.0 <= metrics.regime_compatibility <= 1.0
        assert 0.0 <= metrics.edge_stability <= 1.0

    def test_zero_trades_yields_zero_metrics_without_crashing(self) -> None:
        config = GridBacktestConfig(
            symbol="BTC/USDT",
            timeframe="1h",
            strategy_name="test",
            parameters=FuturesGridParameters(
                grid_lower_price=Decimal("1"),
                grid_upper_price=Decimal("2"),
                grid_count=3,
                long_or_short=GridDirection.LONG,
                position_size=Decimal("10"),
            ),
        )
        sim = GridBacktestSimulator(config)
        # Preis bewegt sich weit ausserhalb der Range -> keine Fills.
        candles = [
            _candle(datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=i), 1000, 1001, 999, 1000)
            for i in range(30)
        ]
        run_result = sim.run(candles)
        backtest_result = sim.to_backtest_result(run_result, candles)

        metrics = compute_grid_edge_metrics(backtest_result, run_result, grid_count=3)

        assert metrics.net_pnl == 0.0
        assert metrics.average_grid_capture == 0.0


class TestIsEdgeConfirmed:
    def test_insufficient_trades_blocks_confirmation(self) -> None:
        metrics = GridEdgeMetrics(
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
            grid_efficiency=0.8,
            capital_utilization=0.5,
            exposure_time=0.4,
            average_grid_capture=5,
            edge_stability=0.75,
            regime_compatibility=0.8,
        )
        confirmed, blockers = is_edge_confirmed(metrics, n_trades=5, min_trades=20)

        assert confirmed is False
        assert any("Zyklen" in b for b in blockers)

    def test_negative_net_pnl_blocks_confirmation(self) -> None:
        metrics = GridEdgeMetrics(
            net_pnl=-10,
            gross_pnl=5,
            fees=10,
            funding=5,
            slippage=2,
            win_rate=0.3,
            profit_factor=0.5,
            max_drawdown_pct=15,
            sharpe=-1.0,
            sortino=-1.5,
            grid_efficiency=0.0,
            capital_utilization=0.3,
            exposure_time=0.2,
            average_grid_capture=-1,
            edge_stability=0.25,
            regime_compatibility=0.3,
        )
        confirmed, blockers = is_edge_confirmed(metrics, n_trades=30)

        assert confirmed is False
        assert any("Net PnL" in b for b in blockers)

    def test_low_grid_efficiency_blocks_despite_positive_pnl(self) -> None:
        """Kern-Anforderung: PnL allein reicht nicht, wenn fast der
        gesamte Bruttogewinn in Kosten aufgeht."""
        metrics = GridEdgeMetrics(
            net_pnl=1.0,
            gross_pnl=100.0,
            fees=90.0,
            funding=8.0,
            slippage=1.0,
            win_rate=0.55,
            profit_factor=1.05,
            max_drawdown_pct=5,
            sharpe=1.2,
            sortino=1.5,
            grid_efficiency=0.01,
            capital_utilization=0.5,
            exposure_time=0.5,
            average_grid_capture=0.1,
            edge_stability=0.6,
            regime_compatibility=0.7,
        )
        confirmed, blockers = is_edge_confirmed(metrics, n_trades=50)

        assert confirmed is False
        assert any("Grid Efficiency" in b for b in blockers)

    def test_well_rounded_metrics_are_confirmed(self) -> None:
        metrics = GridEdgeMetrics(
            net_pnl=200,
            gross_pnl=250,
            fees=30,
            funding=20,
            slippage=5,
            win_rate=0.65,
            profit_factor=1.8,
            max_drawdown_pct=4,
            sharpe=1.5,
            sortino=2.0,
            grid_efficiency=0.8,
            capital_utilization=0.6,
            exposure_time=0.5,
            average_grid_capture=4,
            edge_stability=0.75,
            regime_compatibility=0.8,
        )
        confirmed, blockers = is_edge_confirmed(metrics, n_trades=50)

        assert confirmed is True
        assert blockers == []
