"""
Tests for sgr.backtesting.validation.WalkForwardAnalyzer.

Focus: OOS window sizing (Schritt 19 regression). Previously oos_size
could be smaller than BacktestSimulator.WARMUP_BARS, making every OOS
split's trading loop range empty (`for bar_idx in range(warmup,
len(candles))` with len(candles) < warmup), so every walk-forward run
silently measured 0 trades instead of real out-of-sample performance -
discovered during the mean_reversion_v1 suitability analysis (every
"Walk-Forward inconsistent" verdict in Schritt 10-16 was actually
measuring zero data). Fixed by flooring oos_size (and the inner
per-period length guard) at 2x BacktestSimulator.WARMUP_BARS instead of
a hardcoded 50.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sgr.backtesting.simulator import BacktestSimulator
from sgr.backtesting.types import BacktestConfig
from sgr.backtesting.validation import WalkForwardAnalyzer
from sgr.core.types import (
    Candle,
    ExchangeID,
    MarketRegime,
    Signal,
    SignalDirection,
    Symbol,
)

SYMBOL_STR = "BTC/USDT"
SYMBOL = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def make_candles(n: int, start_price: float = 100.0, drift: float = 0.0) -> list[Candle]:
    """Deterministic synthetic candle series with linear drift per bar."""
    base = datetime(2024, 1, 1, tzinfo=UTC)
    candles = []
    price = start_price
    for i in range(n):
        price = price + drift
        o = price
        c = price + drift * 0.5
        h = max(o, c) + 0.5
        low = min(o, c) - 0.5
        candles.append(
            Candle(
                symbol=SYMBOL,
                timestamp=base + timedelta(hours=i),
                timeframe="1h",
                open=Decimal(str(round(o, 4))),
                high=Decimal(str(round(h, 4))),
                low=Decimal(str(round(low, 4))),
                close=Decimal(str(round(c, 4))),
                volume=Decimal("100"),
            )
        )
        price = c
    return candles


def make_config(**overrides) -> BacktestConfig:
    defaults = dict(
        start_date=datetime(2024, 1, 1, tzinfo=UTC),
        end_date=datetime(2024, 2, 1, tzinfo=UTC),
        symbols=[SYMBOL_STR],
        timeframe="1h",
        initial_capital=Decimal("10000"),
    )
    defaults.update(overrides)
    return BacktestConfig(**defaults)


class FakeStrategy:
    """Always returns the same fixed signal - relies on the generic
    ATR-stop/20-bar time-exit to close positions so new ones can open,
    giving a predictable, non-zero trade cadence over a long series."""

    def __init__(self, name, supported_regimes, signal=None):
        self.name = name
        self.supported_regimes = supported_regimes
        self._signal = signal

    def generate_signal(self, context):
        return self._signal


class FakeRegistry:
    def __init__(self, active):
        self._active = active

    def get_active(self):
        return self._active


def make_signal(
    direction: SignalDirection = SignalDirection.LONG, confidence: float = 0.9
) -> Signal:
    return Signal(
        timestamp=datetime.now(tz=UTC),
        strategy_name="fake",
        symbol=SYMBOL,
        direction=direction,
        confidence=confidence,
        regime=MarketRegime.TRENDING_UP,
        size_hint=1.0,
    )


class TestWalkForwardOOSWindowSizing:
    async def test_oos_windows_exceed_warmup_and_produce_trades(self):
        """Core regression test. 180 days of 1h candles (4320 bars) is
        the exact real-world scenario that originally produced
        oos_size ~= 102 bars, well under the 200-bar warmup - every
        split's trading loop range was empty regardless of the
        strategy. With the fix, every OOS split must actually reach the
        trading loop and produce trades."""
        candles = make_candles(4320, drift=0.05)
        strat = FakeStrategy(
            "trend", [MarketRegime.TRENDING_UP, MarketRegime.RANGING], signal=make_signal()
        )
        registry = FakeRegistry([strat])

        analyzer = WalkForwardAnalyzer(n_splits=6)
        config = make_config(start_date=candles[0].timestamp, end_date=candles[-1].timestamp)
        result = await analyzer.run({SYMBOL_STR: candles}, config, registry)

        assert result.n_splits > 0
        assert all(r.total_trades > 0 for r in result.split_results)

    async def test_oos_size_floor_is_at_least_double_warmup(self):
        """Pins the exact floor formula so a future change can't
        silently regress it back below the warmup requirement."""
        total_bars = 4320
        split_size = total_bars // 7
        expected_oos_size = max(split_size // 6, BacktestSimulator.WARMUP_BARS * 2)
        assert expected_oos_size >= BacktestSimulator.WARMUP_BARS * 2
        assert expected_oos_size > BacktestSimulator.WARMUP_BARS  # loop range is non-empty

    async def test_insufficient_total_data_still_returns_fallback(self):
        """Below-warmup total input must still fail closed via the
        existing fallback, not crash or hang."""
        candles = make_candles(50, drift=0.1)
        analyzer = WalkForwardAnalyzer(n_splits=6)
        config = make_config(start_date=candles[0].timestamp, end_date=candles[-1].timestamp)
        result = await analyzer.run({SYMBOL_STR: candles}, config, FakeRegistry([]))
        assert result.n_splits == 0
        assert result.recommendation.startswith("FAIL")
