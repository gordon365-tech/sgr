"""
Tests für sgr.backtesting.grid_simulator.GridBacktestSimulator.

Deckt (siehe Aufgabenstellung "BACKTESTING"): Grid Levels, Long/Short,
Fees, Slippage, Funding, Leverage, Liquidation Risk, Stop Loss,
Maximum Holding Time, Range-Breakout-Schutz.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sgr.backtesting.grid_simulator import GridBacktestSimulator
from sgr.backtesting.grid_types import GridBacktestConfig
from sgr.core.grid_types import FuturesGridParameters
from sgr.core.types import Candle, ExchangeID, GridDirection, Symbol

SYMBOL = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX)


def _candle(ts, o, h, low, c, v=Decimal("10000")) -> Candle:
    return Candle(
        symbol=SYMBOL,
        timestamp=ts,
        timeframe="1h",
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        volume=v,
    )


def _flat_candles(n: int, price: Decimal, start=None) -> list[Candle]:
    start = start or datetime(2024, 1, 1, tzinfo=UTC)
    return [
        _candle(start + timedelta(hours=i), price, price + 1, price - 1, price) for i in range(n)
    ]


def _params(**overrides) -> FuturesGridParameters:
    base = dict(
        grid_lower_price=Decimal("90"),
        grid_upper_price=Decimal("110"),
        grid_count=5,  # levels: 90, 95, 100, 105, 110
        long_or_short=GridDirection.LONG,
        leverage=Decimal("2"),
        position_size=Decimal("50"),
        max_notional=Decimal("250"),
    )
    base.update(overrides)
    return FuturesGridParameters(**base)


class TestBasicLongCycle:
    def test_price_dip_and_recovery_produces_profitable_cycle(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        candles = [
            _candle(start, 100, 100, 100, 100),
            _candle(start + timedelta(hours=1), 100, 100, 94, 95),  # dips to buy level 95
            _candle(start + timedelta(hours=2), 95, 101, 95, 100),  # rises through 100
        ]
        config = GridBacktestConfig(
            symbol="BTC/USDT", timeframe="1h", strategy_name="test", parameters=_params()
        )
        sim = GridBacktestSimulator(config)
        result = sim.run(candles)

        assert result.fills_count >= 2
        assert len(result.trades) >= 1
        winning = [t for t in result.trades if t.net_pnl > 0]
        assert len(winning) >= 1
        assert all(t.side == "long" for t in result.trades)


class TestShortGrid:
    def test_short_grid_profits_from_upmove_then_reversal(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        candles = [
            _candle(start, 100, 100, 100, 100),
            _candle(start + timedelta(hours=1), 100, 106, 100, 105),  # rises to sell level 105
            _candle(start + timedelta(hours=2), 105, 105, 99, 100),  # falls back through 100
        ]
        config = GridBacktestConfig(
            symbol="BTC/USDT",
            timeframe="1h",
            strategy_name="test",
            parameters=_params(long_or_short=GridDirection.SHORT),
        )
        sim = GridBacktestSimulator(config)
        result = sim.run(candles)

        assert result.fills_count >= 2
        assert all(t.side == "short" for t in result.trades)
        assert any(t.net_pnl > 0 for t in result.trades)


class TestFeesAndSlippageAreApplied:
    def test_fees_and_slippage_reduce_net_pnl_below_gross(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        candles = [
            _candle(start, 100, 100, 100, 100),
            _candle(start + timedelta(hours=1), 100, 100, 94, 95),
            _candle(start + timedelta(hours=2), 95, 101, 95, 100),
        ]
        config = GridBacktestConfig(
            symbol="BTC/USDT",
            timeframe="1h",
            strategy_name="test",
            parameters=_params(),
            maker_fee=Decimal("0.001"),
            taker_fee=Decimal("0.001"),
            slippage_pct=Decimal("0.001"),
        )
        sim = GridBacktestSimulator(config)
        result = sim.run(candles)

        assert len(result.trades) >= 1
        for t in result.trades:
            assert t.fees > 0
            assert t.net_pnl < t.gross_pnl


class TestFundingAccounting:
    def test_funding_accrues_over_time_when_grid_is_holding_position(self) -> None:
        # Lange Flat-Serie mit einem initialen Dip, damit ein Level lange
        # offen bleibt und mehrere Funding-Intervalle durchlaeuft.
        start = datetime(2024, 1, 1, tzinfo=UTC)
        candles = [_candle(start, 100, 100, 100, 100)]
        candles.append(_candle(start + timedelta(hours=1), 100, 100, 94, 95))
        # 40 weitere flache Bars bei 95 (Level bleibt offen, kein Exit)
        for i in range(2, 42):
            candles.append(_candle(start + timedelta(hours=i), 95, 96, 94, 95))

        config = GridBacktestConfig(
            symbol="BTC/USDT",
            timeframe="1h",
            strategy_name="test",
            parameters=_params(),
            funding_interval_hours=8,
            assumed_funding_rate_per_interval=Decimal("0.001"),
        )
        sim = GridBacktestSimulator(config)
        result = sim.run(candles)

        assert result.total_funding_paid > 0

    def test_no_funding_when_grid_never_opens_a_position(self) -> None:
        candles = _flat_candles(30, Decimal("1000"))  # weit ausserhalb der Range [90,110]
        config = GridBacktestConfig(
            symbol="BTC/USDT",
            timeframe="1h",
            strategy_name="test",
            parameters=_params(),
        )
        sim = GridBacktestSimulator(config)
        result = sim.run(candles)

        assert result.total_funding_paid == 0


class TestStopLossAndTakeProfit:
    def test_stop_loss_force_closes_grid(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        candles = [
            _candle(start, 100, 100, 100, 100),
            _candle(start + timedelta(hours=1), 100, 100, 94, 95),  # open a level
            _candle(start + timedelta(hours=2), 95, 95, 79, 80),  # crash through stop_loss
        ]
        params = _params(stop_loss=Decimal("85"))
        config = GridBacktestConfig(
            symbol="BTC/USDT", timeframe="1h", strategy_name="test", parameters=params
        )
        sim = GridBacktestSimulator(config)
        result = sim.run(candles)

        assert result.close_reason == "stop_loss"

    def test_take_profit_force_closes_grid(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        candles = [
            _candle(start, 100, 100, 100, 100),
            _candle(start + timedelta(hours=1), 100, 100, 94, 95),
            _candle(start + timedelta(hours=2), 95, 121, 95, 120),  # spikes through take_profit
        ]
        params = _params(take_profit=Decimal("115"))
        config = GridBacktestConfig(
            symbol="BTC/USDT", timeframe="1h", strategy_name="test", parameters=params
        )
        sim = GridBacktestSimulator(config)
        result = sim.run(candles)

        assert result.close_reason == "take_profit"


class TestMaximumHoldingTime:
    def test_grid_force_closes_after_max_holding_time(self) -> None:
        candles = _flat_candles(100, Decimal("100"))
        params = _params(maximum_holding_time=3600 * 10)  # 10 Stunden
        config = GridBacktestConfig(
            symbol="BTC/USDT", timeframe="1h", strategy_name="test", parameters=params
        )
        sim = GridBacktestSimulator(config)
        result = sim.run(candles)

        assert result.close_reason == "max_holding_time"


class TestRangeBreakoutProtection:
    def test_grid_closes_when_price_leaves_range_with_buffer(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        candles = [_candle(start, 100, 100, 100, 100)]
        # Preis faellt weit unter die Range (90) + Buffer (0.5 * 20 = 10 -> 80)
        candles.append(_candle(start + timedelta(hours=1), 100, 100, 65, 70))
        config = GridBacktestConfig(
            symbol="BTC/USDT",
            timeframe="1h",
            strategy_name="test",
            parameters=_params(),
            range_breakout_buffer_factor=0.5,
        )
        sim = GridBacktestSimulator(config)
        result = sim.run(candles)

        assert result.close_reason == "range_breakout"


class TestLiquidationProtection:
    def test_high_leverage_triggers_liquidation_on_adverse_move(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        candles = [
            _candle(start, 100, 100, 100, 100),
            _candle(start + timedelta(hours=1), 100, 100, 94, 95),  # opens level at 95
            # Preis stuerzt weit ab - bei leverage=10 liegt die
            # Liquidationsnaeherung bei ~95*(1-1/10)=85.5
            _candle(start + timedelta(hours=2), 95, 95, 50, 60),
        ]
        params = _params(leverage=Decimal("10"))
        config = GridBacktestConfig(
            symbol="BTC/USDT", timeframe="1h", strategy_name="test", parameters=params
        )
        sim = GridBacktestSimulator(config)
        result = sim.run(candles)

        assert result.liquidated is True
        assert result.close_reason == "liquidation"

    def test_low_leverage_does_not_liquidate_on_same_move(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        candles = [
            _candle(start, 100, 100, 100, 100),
            _candle(start + timedelta(hours=1), 100, 100, 94, 95),
            _candle(start + timedelta(hours=2), 95, 95, 89, 90),  # kleine Bewegung
        ]
        params = _params(leverage=Decimal("1"))
        config = GridBacktestConfig(
            symbol="BTC/USDT", timeframe="1h", strategy_name="test", parameters=params
        )
        sim = GridBacktestSimulator(config)
        result = sim.run(candles)

        assert result.liquidated is False


class TestBacktestResultIntegration:
    def test_to_backtest_result_produces_standard_kpis(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=UTC)
        candles = [
            _candle(start, 100, 100, 100, 100),
            _candle(start + timedelta(hours=1), 100, 100, 94, 95),
            _candle(start + timedelta(hours=2), 95, 101, 95, 100),
        ]
        config = GridBacktestConfig(
            symbol="BTC/USDT",
            timeframe="1h",
            strategy_name="futures_grid_long_v1",
            parameters=_params(),
        )
        sim = GridBacktestSimulator(config)
        run_result = sim.run(candles)
        backtest_result = sim.to_backtest_result(run_result, candles)

        assert backtest_result.total_trades == len(run_result.trades)
        assert backtest_result.config_summary["strategy_names"] == ["futures_grid_long_v1"]

    def test_insufficient_data_returns_empty_result(self) -> None:
        config = GridBacktestConfig(
            symbol="BTC/USDT", timeframe="1h", strategy_name="test", parameters=_params()
        )
        sim = GridBacktestSimulator(config)
        result = sim.run([])

        assert result.close_reason == "insufficient_data"
        assert result.trades == []
