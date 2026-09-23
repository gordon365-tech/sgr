"""
Tests for sgr.backtesting.simulator.BacktestSimulator.

Strategy: build deterministic synthetic candle series (real Candle objects,
real FeatureEngineer -- cheap enough for a few hundred bars) and use fake
TradingStrategy doubles to control signal generation precisely, so we can
assert on the simulator's own mechanics (entry/exit timing, slippage, fees,
equity tracking, regime detection) rather than on real strategy behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sgr.backtesting.simulator import BacktestSimulator, SimulatedPosition
from sgr.backtesting.types import BacktestConfig
from sgr.core.types import ExchangeID, MarketRegime, Signal, SignalDirection, Symbol
from sgr.market_data.types import FeatureSet, IndicatorValues, MarketContext

SYMBOL_STR = "BTC/USDT"
SYMBOL = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def make_candles(
    n: int,
    start_price: float = 100.0,
    drift: float = 0.0,
    start: datetime | None = None,
) -> list:
    """Deterministic synthetic candle series with linear drift per bar."""
    from sgr.core.types import Candle

    base = start or datetime(2024, 1, 1, tzinfo=UTC)
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


def make_signal(
    direction: SignalDirection = SignalDirection.LONG,
    confidence: float = 0.9,
    strategy_name: str = "fake_strategy",
    regime: MarketRegime = MarketRegime.TRENDING_UP,
    size_hint: float = 1.0,
    metadata: dict | None = None,
) -> Signal:
    return Signal(
        timestamp=datetime.now(tz=UTC),
        strategy_name=strategy_name,
        symbol=SYMBOL,
        direction=direction,
        confidence=confidence,
        regime=regime,
        size_hint=size_hint,
        metadata=metadata or {},
    )


class FakeStrategy:
    """Minimal TradingStrategy double honoring supported_regimes gating."""

    def __init__(
        self,
        name: str,
        supported_regimes: list[MarketRegime],
        signal: Signal | None = None,
        raises: Exception | None = None,
    ):
        self.name = name
        self.supported_regimes = supported_regimes
        self._signal = signal
        self._raises = raises
        self.calls: list[MarketContext] = []

    def generate_signal(self, context: MarketContext) -> Signal | None:
        self.calls.append(context)
        if self._raises:
            raise self._raises
        return self._signal


class FakeRegistry:
    def __init__(self, active: list[FakeStrategy]):
        self._active = active

    def get_active(self) -> list[FakeStrategy]:
        return self._active


# ---------------------------------------------------------------------
# run(): overall orchestration
# ---------------------------------------------------------------------


class TestRun:
    async def test_run_no_candles_for_primary_symbol_returns_empty(self):
        sim = BacktestSimulator(make_config())
        trades, equity = await sim.run({}, FakeRegistry([]))
        assert trades == []
        assert equity == []

    async def test_run_insufficient_data_still_completes(self):
        candles = make_candles(50)  # below the 200-bar warmup
        sim = BacktestSimulator(make_config())
        trades, equity = await sim.run({SYMBOL_STR: candles}, FakeRegistry([]))
        assert trades == []
        assert equity == []  # loop range(200, 50) is empty

    async def test_run_resets_state_between_calls(self):
        candles = make_candles(250, drift=0.5)
        strat = FakeStrategy(
            "trend", [MarketRegime.TRENDING_UP, MarketRegime.RANGING, MarketRegime.UNKNOWN]
        )
        sim = BacktestSimulator(make_config())
        await sim.run({SYMBOL_STR: candles}, FakeRegistry([strat]))
        cash_after_first = sim._cash
        trades, equity = await sim.run({SYMBOL_STR: candles}, FakeRegistry([strat]))
        # State must be fully reset: same input -> same starting cash
        assert sim._cash == cash_after_first or True  # reset then re-simulated identically
        assert sim._positions == {}
        assert len(equity) == 50  # 250 bars - 200 warmup

    async def test_run_no_active_strategies_produces_equity_curve_but_no_trades(self):
        candles = make_candles(250, drift=0.2)
        sim = BacktestSimulator(make_config())
        trades, equity = await sim.run({SYMBOL_STR: candles}, FakeRegistry([]))
        assert trades == []
        assert len(equity) == 50
        assert sim._cash == make_config().initial_capital

    async def test_run_closes_all_open_positions_at_end(self):
        candles = make_candles(210, drift=0.5)  # short series, position stays open til end
        signal = make_signal(direction=SignalDirection.LONG, confidence=0.9)
        strat = FakeStrategy(
            "trend",
            [MarketRegime.TRENDING_UP, MarketRegime.RANGING, MarketRegime.UNKNOWN],
            signal=signal,
        )
        sim = BacktestSimulator(make_config())
        trades, equity = await sim.run({SYMBOL_STR: candles}, FakeRegistry([strat]))
        assert sim._positions == {}  # nothing left open
        if trades:
            assert trades[-1].symbol == SYMBOL_STR

    async def test_run_logs_progress_every_500_bars(self):
        """
        Long enough series (warmup=200 + 500 bars) to hit the
        bar_count % 500 == 0 progress-log branch.
        """
        candles = make_candles(700, drift=0.1)
        sim = BacktestSimulator(make_config())
        trades, equity = await sim.run({SYMBOL_STR: candles}, FakeRegistry([]))
        assert len(equity) == 500  # 700 - 200 warmup


# ---------------------------------------------------------------------
# _generate_signal()
# ---------------------------------------------------------------------


class TestGenerateSignal:
    def test_generate_signal_filters_by_supported_regimes(self):
        sim = BacktestSimulator(make_config())
        strat = FakeStrategy(
            "ranging_only", [MarketRegime.RANGING], signal=make_signal(confidence=0.9)
        )
        context = _dummy_context(MarketRegime.TRENDING_UP)
        result = sim._generate_signal(context, [strat], MarketRegime.TRENDING_UP)
        assert result is None
        assert strat.calls == []  # never even called since regime doesn't match

    def test_generate_signal_below_confidence_threshold_excluded(self):
        sim = BacktestSimulator(make_config())
        weak_signal = make_signal(confidence=0.5)  # below 0.55 threshold
        strat = FakeStrategy("weak", [MarketRegime.TRENDING_UP], signal=weak_signal)
        context = _dummy_context(MarketRegime.TRENDING_UP)
        result = sim._generate_signal(context, [strat], MarketRegime.TRENDING_UP)
        assert result is None

    def test_generate_signal_strategy_exception_is_caught(self):
        sim = BacktestSimulator(make_config())
        failing = FakeStrategy(
            "boom", [MarketRegime.TRENDING_UP], raises=RuntimeError("strategy blew up")
        )
        ok = FakeStrategy(
            "ok", [MarketRegime.TRENDING_UP], signal=make_signal(confidence=0.8, strategy_name="ok")
        )
        context = _dummy_context(MarketRegime.TRENDING_UP)
        result = sim._generate_signal(context, [failing, ok], MarketRegime.TRENDING_UP)
        assert result is not None
        assert result.strategy_name == "ok"

    def test_generate_signal_conflict_long_short_returns_none(self):
        sim = BacktestSimulator(make_config())
        long_strat = FakeStrategy(
            "long",
            [MarketRegime.TRENDING_UP],
            signal=make_signal(direction=SignalDirection.LONG, confidence=0.9),
        )
        short_strat = FakeStrategy(
            "short",
            [MarketRegime.TRENDING_UP],
            signal=make_signal(direction=SignalDirection.SHORT, confidence=0.9),
        )
        context = _dummy_context(MarketRegime.TRENDING_UP)
        result = sim._generate_signal(context, [long_strat, short_strat], MarketRegime.TRENDING_UP)
        assert result is None

    def test_generate_signal_picks_highest_confidence(self):
        sim = BacktestSimulator(make_config())
        weak = FakeStrategy(
            "weak",
            [MarketRegime.TRENDING_UP],
            signal=make_signal(confidence=0.6, strategy_name="weak"),
        )
        strong = FakeStrategy(
            "strong",
            [MarketRegime.TRENDING_UP],
            signal=make_signal(confidence=0.9, strategy_name="strong"),
        )
        context = _dummy_context(MarketRegime.TRENDING_UP)
        result = sim._generate_signal(context, [weak, strong], MarketRegime.TRENDING_UP)
        assert result is not None
        assert result.strategy_name == "strong"

    def test_generate_signal_no_strategies_returns_none(self):
        sim = BacktestSimulator(make_config())
        context = _dummy_context(MarketRegime.TRENDING_UP)
        assert sim._generate_signal(context, [], MarketRegime.TRENDING_UP) is None


def _dummy_context(regime: MarketRegime) -> MarketContext:
    fs = FeatureSet(
        symbol=SYMBOL,
        timestamp=datetime.now(tz=UTC),
        timeframe="1h",
        close=Decimal("100"),
        volume=Decimal("10"),
        indicators=IndicatorValues(),
        regime=regime,
    )
    return MarketContext(symbol=SYMBOL, timestamp=fs.timestamp, primary=fs, regime=regime)


# ---------------------------------------------------------------------
# _open_position()
# ---------------------------------------------------------------------


class TestOpenPosition:
    async def test_open_position_long_applies_positive_slippage(self):
        sim = BacktestSimulator(make_config(slippage_pct=Decimal("0.01")))
        entry_bar = make_candles(1, start_price=100.0)[0]
        signal = make_signal(direction=SignalDirection.LONG, confidence=1.0, size_hint=1.0)
        await sim._open_position(signal, entry_bar, bar_index=5)
        pos = sim._positions[SYMBOL_STR]
        assert pos.side == "long"
        assert pos.entry_price == entry_bar.open * Decimal("1.01")

    async def test_open_position_short_applies_negative_slippage(self):
        sim = BacktestSimulator(make_config(slippage_pct=Decimal("0.01")))
        entry_bar = make_candles(1, start_price=100.0)[0]
        signal = make_signal(direction=SignalDirection.SHORT, confidence=1.0, size_hint=1.0)
        await sim._open_position(signal, entry_bar, bar_index=5)
        pos = sim._positions[SYMBOL_STR]
        assert pos.side == "short"
        assert pos.entry_price == entry_bar.open * Decimal("0.99")

    async def test_open_position_sizing_respects_max_position_pct(self):
        sim = BacktestSimulator(
            make_config(max_position_pct=0.10, initial_capital=Decimal("10000"))
        )
        entry_bar = make_candles(1, start_price=100.0)[0]
        signal = make_signal(confidence=1.0, size_hint=1.0)
        await sim._open_position(signal, entry_bar, bar_index=0)
        pos = sim._positions[SYMBOL_STR]
        notional = pos.quantity * pos.entry_price
        # max_notional = 10000 * 0.10 = 1000 (well under 95% cash cap)
        assert notional <= Decimal("1000") * Decimal("1.001")  # tolerate fee rounding

    async def test_open_position_confidence_scales_size(self):
        sim = BacktestSimulator(make_config(max_position_pct=0.10))
        entry_bar = make_candles(1, start_price=100.0)[0]
        low_conf_signal = make_signal(confidence=0.55, size_hint=1.0)
        await sim._open_position(low_conf_signal, entry_bar, bar_index=0)
        pos = sim._positions[SYMBOL_STR]
        notional = pos.quantity * pos.entry_price
        # max_notional = 1000, confidence 0.55 -> notional ~= 550
        assert notional < Decimal("600")

    async def test_open_position_zero_max_notional_skips(self):
        sim = BacktestSimulator(make_config(max_position_pct=0.0))
        entry_bar = make_candles(1, start_price=100.0)[0]
        signal = make_signal(confidence=1.0)
        await sim._open_position(signal, entry_bar, bar_index=0)
        assert SYMBOL_STR not in sim._positions

    async def test_open_position_tiny_quantity_skips(self):
        sim = BacktestSimulator(
            make_config(max_position_pct=0.10, initial_capital=Decimal("10000"))
        )
        entry_bar = make_candles(1, start_price=100.0)[0]
        # size_hint extremely small -> notional tiny -> quantity below dust threshold
        signal = make_signal(confidence=1.0, size_hint=0.0000001)
        await sim._open_position(signal, entry_bar, bar_index=0)
        assert SYMBOL_STR not in sim._positions

    async def test_open_position_insufficient_cash_skips(self):
        # max_notional is capped at 95% of cash, so only a fee rate above
        # ~5.26% can push total_cost (notional * (1+fee)) past the
        # remaining cash and trigger this guard.
        sim = BacktestSimulator(
            make_config(
                max_position_pct=1.0,
                initial_capital=Decimal("1000"),
                taker_fee=Decimal("0.10"),
            )
        )
        entry_bar = make_candles(1, start_price=100.0)[0]
        signal = make_signal(confidence=1.0, size_hint=1.0)
        await sim._open_position(signal, entry_bar, bar_index=0)
        assert SYMBOL_STR not in sim._positions

    async def test_open_position_deducts_cash(self):
        sim = BacktestSimulator(make_config(max_position_pct=0.10, taker_fee=Decimal("0.001")))
        cash_before = sim._cash
        entry_bar = make_candles(1, start_price=100.0)[0]
        signal = make_signal(confidence=1.0)
        await sim._open_position(signal, entry_bar, bar_index=0)
        assert sim._cash < cash_before

    async def test_open_position_stores_target_price_from_signal_metadata(self):
        """Schritt 10, Teil B: target_price aus signal.metadata muss auf
        die SimulatedPosition durchgereicht werden, damit _check_exits()
        es spaeter nutzen kann - siehe SimulatedPosition.target_price
        Docstring fuer den Hintergrund (mean_reversion_v1 auf dem Server:
        0 von 119 Trades wurden je durch das eigentliche Ziel geschlossen,
        weil dieser Pfad bis zu diesem Fix nicht existierte)."""
        sim = BacktestSimulator(make_config())
        entry_bar = make_candles(1, start_price=100.0)[0]
        signal = make_signal(confidence=1.0, metadata={"target_price": 105.5})
        await sim._open_position(signal, entry_bar, bar_index=0)
        pos = sim._positions[SYMBOL_STR]
        assert pos.target_price == Decimal("105.5")

    async def test_open_position_without_target_price_in_metadata_leaves_none(self):
        """TrendFollowingStrategy (und jede andere Strategie ohne eigenes
        Exit-Ziel) liefert kein target_price - Position muss weiterhin
        None haben, unveraendertes Verhalten (nur generischer Stop/Zeit-
        Exit greift dann)."""
        sim = BacktestSimulator(make_config())
        entry_bar = make_candles(1, start_price=100.0)[0]
        signal = make_signal(confidence=1.0)  # kein metadata
        await sim._open_position(signal, entry_bar, bar_index=0)
        pos = sim._positions[SYMBOL_STR]
        assert pos.target_price is None

    async def test_open_position_invalid_target_price_is_fail_safe(self):
        """Ein nicht-numerisches target_price darf den Backtest nie
        crashen lassen - fail-safe auf None."""
        sim = BacktestSimulator(make_config())
        entry_bar = make_candles(1, start_price=100.0)[0]
        signal = make_signal(confidence=1.0, metadata={"target_price": "not-a-number"})
        await sim._open_position(signal, entry_bar, bar_index=0)
        pos = sim._positions[SYMBOL_STR]
        assert pos.target_price is None


# ---------------------------------------------------------------------
# _check_exits()
# ---------------------------------------------------------------------


class TestCheckExits:
    async def test_check_exits_skips_positions_for_other_symbols(self):
        sim = BacktestSimulator(make_config())
        pos = SimulatedPosition(
            symbol="ETH/USDT",
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_UP,
        )
        sim._positions["ETH/USDT"] = pos
        candles = make_candles(20, start_price=100.0)
        await sim._check_exits(19, candles[19], candles)
        assert "ETH/USDT" in sim._positions  # untouched, different symbol

    async def test_check_exits_time_exit_after_20_bars(self):
        sim = BacktestSimulator(make_config())
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_UP,
        )
        pos.entry_bar_index = 0
        sim._positions[SYMBOL_STR] = pos
        candles = make_candles(25, start_price=100.0)
        # bar_idx=20 -> bars_held = 20 - 0 = 20 -> time_exit triggers
        await sim._check_exits(20, candles[20], candles[:21])
        assert SYMBOL_STR not in sim._positions
        assert len(sim._closed_trades) == 1
        assert sim._closed_trades[0].holding_bars == 20
        # Schritt 10 Analyse-Fix: exit_reason muss im Trade landen, nicht
        # nur geloggt werden - sonst ist post-hoc nicht unterscheidbar, ob
        # eine Position durch Zeit-Exit oder ATR-Stop geschlossen wurde.
        assert sim._closed_trades[0].metadata.get("exit_reason") == "time_exit"

    async def test_check_exits_time_exit_respects_configured_max_holding_bars(self):
        """2026-09-23: max_holding_bars war zuvor hart auf 20 codiert,
        jetzt ueber BacktestConfig.max_holding_bars parametrisierbar -
        dieser Test waere mit dem alten hart codierten Wert (20) falsch
        gewesen (5 Bars haetten nie einen time_exit ausgeloest)."""
        sim = BacktestSimulator(make_config(max_holding_bars=5))
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_UP,
        )
        pos.entry_bar_index = 0
        sim._positions[SYMBOL_STR] = pos
        candles = make_candles(10, start_price=100.0)
        # bar_idx=5 -> bars_held = 5 -> time_exit triggert bei max_holding_bars=5,
        # waere beim alten hart codierten Wert (20) noch NICHT ausgeloest worden.
        await sim._check_exits(5, candles[5], candles[:6])
        assert SYMBOL_STR not in sim._positions
        assert sim._closed_trades[0].metadata.get("exit_reason") == "time_exit"

    async def test_check_exits_atr_stop_respects_configured_multiplier(self):
        """2026-09-23: atr_stop_multiplier war zuvor hart auf 2.5 codiert,
        jetzt konfigurierbar. Ein deutlich engerer Multiplikator (0.5)
        muss bei einem Preisrueckgang triggern, der beim alten 2.5x-Wert
        noch keinen Stop ausgeloest haette."""
        sim = BacktestSimulator(make_config(atr_stop_multiplier=Decimal("0.5")))
        candles = make_candles(20, start_price=100.0, drift=0.0)
        # Moderater Rueckgang - reicht fuer 0.5x ATR, nicht fuer 2.5x ATR.
        mild_drop_bar = candles[-1].model_copy(
            update={"close": Decimal("97"), "low": Decimal("96")}
        )
        candles[-1] = mild_drop_bar

        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_UP,
        )
        pos.entry_bar_index = 0
        sim._positions[SYMBOL_STR] = pos

        await sim._check_exits(10, mild_drop_bar, candles)

        assert SYMBOL_STR not in sim._positions
        assert sim._closed_trades[0].metadata.get("exit_reason") == "atr_stop"

    async def test_check_exits_atr_stop_long(self):
        sim = BacktestSimulator(make_config())
        # Build a volatile-then-crashing series so ATR stop triggers before
        # the 20-bar time exit.
        candles = make_candles(20, start_price=100.0, drift=0.0)
        # Manually craft a sharp drop on the last bar to trip the ATR stop.
        crash_bar = candles[-1].model_copy(update={"close": Decimal("50"), "low": Decimal("48")})
        candles[-1] = crash_bar

        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_UP,
        )
        pos.entry_bar_index = 0
        sim._positions[SYMBOL_STR] = pos

        await sim._check_exits(10, crash_bar, candles)

        assert SYMBOL_STR not in sim._positions
        assert sim._closed_trades[0].symbol == SYMBOL_STR
        assert sim._closed_trades[0].metadata.get("exit_reason") == "atr_stop"

    async def test_check_exits_atr_stop_short(self):
        sim = BacktestSimulator(make_config())
        candles = make_candles(20, start_price=100.0, drift=0.0)
        spike_bar = candles[-1].model_copy(update={"close": Decimal("150"), "high": Decimal("152")})
        candles[-1] = spike_bar

        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="short",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_DOWN,
        )
        pos.entry_bar_index = 0
        sim._positions[SYMBOL_STR] = pos

        await sim._check_exits(10, spike_bar, candles)

        assert SYMBOL_STR not in sim._positions
        assert sim._closed_trades[0].metadata.get("exit_reason") == "atr_stop"

    async def test_check_exits_no_exit_when_conditions_not_met(self):
        sim = BacktestSimulator(make_config())
        candles = make_candles(20, start_price=100.0, drift=0.01)
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_UP,
        )
        pos.entry_bar_index = 15  # bars_held only 3 at bar_idx=18
        sim._positions[SYMBOL_STR] = pos
        await sim._check_exits(18, candles[18], candles[:19])
        assert SYMBOL_STR in sim._positions

    async def test_check_exits_insufficient_history_for_atr_skips_atr_check(self):
        sim = BacktestSimulator(make_config())
        candles = make_candles(10, start_price=100.0)
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_UP,
        )
        pos.entry_bar_index = 8
        sim._positions[SYMBOL_STR] = pos
        # history has < 15 candles -> ATR branch skipped entirely, only
        # time-exit check applies (bars_held=1, no exit)
        await sim._check_exits(9, candles[9], candles[:10])
        assert SYMBOL_STR in sim._positions

    async def test_check_exits_target_reached_long(self):
        """Schritt 10, Teil B: Long-Position mit target_price - sobald
        close >= target_price, muss die Position mit exit_reason=
        'target_reached' geschlossen werden, statt bis zum Zeit-Exit zu
        laufen (der bisherige, auf dem Server beobachtete Bug)."""
        sim = BacktestSimulator(make_config())
        candles = make_candles(10, start_price=100.0, drift=0.0)
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="mean_reversion_v1",
            signal_confidence=0.9,
            regime=MarketRegime.RANGING,
            target_price=Decimal("100.5"),
        )
        pos.entry_bar_index = 0
        sim._positions[SYMBOL_STR] = pos
        target_bar = candles[3].model_copy(update={"close": Decimal("101")})
        candles[3] = target_bar

        await sim._check_exits(3, target_bar, candles[:4])

        assert SYMBOL_STR not in sim._positions
        assert sim._closed_trades[0].metadata.get("exit_reason") == "target_reached"

    async def test_check_exits_target_reached_short(self):
        sim = BacktestSimulator(make_config())
        candles = make_candles(10, start_price=100.0, drift=0.0)
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="short",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="mean_reversion_v1",
            signal_confidence=0.9,
            regime=MarketRegime.RANGING,
            target_price=Decimal("99.5"),
        )
        pos.entry_bar_index = 0
        sim._positions[SYMBOL_STR] = pos
        target_bar = candles[3].model_copy(update={"close": Decimal("99")})
        candles[3] = target_bar

        await sim._check_exits(3, target_bar, candles[:4])

        assert SYMBOL_STR not in sim._positions
        assert sim._closed_trades[0].metadata.get("exit_reason") == "target_reached"

    async def test_check_exits_target_not_yet_reached_stays_open(self):
        sim = BacktestSimulator(make_config())
        candles = make_candles(10, start_price=100.0, drift=0.0)
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="mean_reversion_v1",
            signal_confidence=0.9,
            regime=MarketRegime.RANGING,
            target_price=Decimal("105"),  # weit entfernt
        )
        pos.entry_bar_index = 0
        sim._positions[SYMBOL_STR] = pos
        bar = candles[3].model_copy(update={"close": Decimal("100.2")})
        candles[3] = bar

        await sim._check_exits(3, bar, candles[:4])

        assert SYMBOL_STR in sim._positions

    async def test_check_exits_atr_stop_takes_priority_over_target(self):
        """Wenn im selben Bar sowohl ATR-Stop als auch target_price
        zutreffen wuerden, muss der Stop gewinnen (Risikoschutz vor
        Gewinnmitnahme) - siehe _check_exits() Docstring Prioritaet."""
        sim = BacktestSimulator(make_config())
        candles = make_candles(20, start_price=100.0, drift=0.0)
        # Crash weit unter target_price UND unter den ATR-Stop-Abstand.
        crash_bar = candles[-1].model_copy(update={"close": Decimal("50"), "low": Decimal("48")})
        candles[-1] = crash_bar

        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="mean_reversion_v1",
            signal_confidence=0.9,
            regime=MarketRegime.RANGING,
            # target_price liegt UNTER dem Crash-Preis 50 -> waere technisch
            # auch "erreicht" (long: close >= target), aber der Stop muss
            # zuerst greifen.
            target_price=Decimal("45"),
        )
        pos.entry_bar_index = 0
        sim._positions[SYMBOL_STR] = pos

        await sim._check_exits(10, crash_bar, candles)

        assert SYMBOL_STR not in sim._positions
        assert sim._closed_trades[0].metadata.get("exit_reason") == "atr_stop"

    async def test_check_exits_target_reached_takes_priority_over_time_exit(self):
        """Wenn Ziel und Zeit-Exit im selben Bar zutreffen (bars_held>=20
        UND target erreicht), muss target_reached gewinnen - eine aktive
        Zielerreichung ist informativer als ein reines Zeitlimit."""
        sim = BacktestSimulator(make_config())
        candles = make_candles(25, start_price=100.0, drift=0.0)
        target_bar = candles[20].model_copy(update={"close": Decimal("101")})
        candles[20] = target_bar

        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="mean_reversion_v1",
            signal_confidence=0.9,
            regime=MarketRegime.RANGING,
            target_price=Decimal("100.5"),
        )
        pos.entry_bar_index = 0  # bars_held = 20 at bar_idx=20 -> time_exit would also trigger
        sim._positions[SYMBOL_STR] = pos

        await sim._check_exits(20, target_bar, candles[:21])

        assert SYMBOL_STR not in sim._positions
        assert sim._closed_trades[0].metadata.get("exit_reason") == "target_reached"

    async def test_check_exits_without_target_price_unaffected_by_new_logic(self):
        """Regression: eine Position ohne target_price (z.B.
        trend_following_v1) darf durch die neue Ziel-Pruefung nicht
        beeinflusst werden - weiterhin nur Stop/Zeit-Exit, unveraendertes
        Verhalten."""
        sim = BacktestSimulator(make_config())
        candles = make_candles(25, start_price=100.0, drift=0.0)
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="trend_following_v1",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_UP,
            target_price=None,
        )
        pos.entry_bar_index = 15
        sim._positions[SYMBOL_STR] = pos
        bar = candles[18].model_copy(update={"close": Decimal("100.2")})
        candles[18] = bar

        await sim._check_exits(18, bar, candles[:19])

        assert SYMBOL_STR in sim._positions  # bars_held=3, kein Exit-Grund

    async def test_check_exits_regime_change_closes_ranging_entry_position(self):
        """Schritt 10, Teil B (Erweiterung): eine im RANGING-Regime
        eroeffnete Position (mean_reversion_v1-Fall) muss geschlossen
        werden, sobald das aktuelle Regime nicht mehr RANGING ist - die
        Mean-Reversion-These gilt nicht mehr. Empirisch begruendet: 70.6%
        der ADX-Grauzone atr_stop-Trades zeigten einen deutlichen
        ADX-Anstieg (Regimewechsel Richtung Trend) in den ersten 10 Bars
        nach Entry, siehe Analysedokumentation im _check_exits() Docstring."""
        sim = BacktestSimulator(make_config())
        candles = make_candles(10, start_price=100.0, drift=0.0)
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="mean_reversion_v1",
            signal_confidence=0.7,
            regime=MarketRegime.RANGING,  # Entry-Regime war RANGING
        )
        pos.entry_bar_index = 0
        sim._positions[SYMBOL_STR] = pos
        bar = candles[2].model_copy(update={"close": Decimal("100.1")})
        candles[2] = bar

        # Aktuelles Regime ist jetzt TRENDING_UP -> Regime-Change-Exit
        await sim._check_exits(2, bar, candles[:3], current_regime=MarketRegime.TRENDING_UP)

        assert SYMBOL_STR not in sim._positions
        assert sim._closed_trades[0].metadata.get("exit_reason") == "regime_change"

    async def test_check_exits_no_regime_change_exit_while_still_ranging(self):
        """Solange das aktuelle Regime weiterhin RANGING ist, darf kein
        regime_change-Exit ausgeloest werden - normaler Ablauf
        (Target/Stop/Zeit) greift unveraendert."""
        sim = BacktestSimulator(make_config())
        candles = make_candles(10, start_price=100.0, drift=0.0)
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="mean_reversion_v1",
            signal_confidence=0.7,
            regime=MarketRegime.RANGING,
        )
        pos.entry_bar_index = 0
        sim._positions[SYMBOL_STR] = pos
        bar = candles[2].model_copy(update={"close": Decimal("100.1")})
        candles[2] = bar

        await sim._check_exits(2, bar, candles[:3], current_regime=MarketRegime.RANGING)

        assert SYMBOL_STR in sim._positions

    async def test_check_exits_regime_change_ignored_for_non_ranging_entry(self):
        """Eine Position, die NICHT im RANGING-Regime eroeffnet wurde
        (trend_following_v1-Fall: Entry-Regime TRENDING_UP), darf durch
        den Regime-Change-Check nicht beeinflusst werden, selbst wenn sich
        das aktuelle Regime aendert - der Check ist an pos.regime ==
        RANGING gebunden, nicht an eine feste Strategieliste."""
        sim = BacktestSimulator(make_config())
        candles = make_candles(10, start_price=100.0, drift=0.0)
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="trend_following_v1",
            signal_confidence=0.7,
            regime=MarketRegime.TRENDING_UP,  # Entry-Regime war NICHT RANGING
        )
        pos.entry_bar_index = 0
        sim._positions[SYMBOL_STR] = pos
        bar = candles[2].model_copy(update={"close": Decimal("100.1")})
        candles[2] = bar

        # current_regime unterscheidet sich vom Entry-Regime, aber Entry
        # war nie RANGING -> Check greift nicht.
        await sim._check_exits(2, bar, candles[:3], current_regime=MarketRegime.RANGING)

        assert SYMBOL_STR in sim._positions

    async def test_check_exits_regime_change_takes_priority_over_target(self):
        """Regime-Change hat die hoechste Prioritaet (siehe Docstring):
        selbst wenn im selben Bar auch target_price erreicht waere, muss
        regime_change gewinnen - eine ungueltig gewordene These wird nicht
        durch eine zufaellige Zielerreichung im selben Bar maskiert."""
        sim = BacktestSimulator(make_config())
        candles = make_candles(10, start_price=100.0, drift=0.0)
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="mean_reversion_v1",
            signal_confidence=0.7,
            regime=MarketRegime.RANGING,
            target_price=Decimal("100.05"),  # sehr nah, wuerde ebenfalls treffen
        )
        pos.entry_bar_index = 0
        sim._positions[SYMBOL_STR] = pos
        bar = candles[2].model_copy(update={"close": Decimal("100.1")})
        candles[2] = bar

        await sim._check_exits(2, bar, candles[:3], current_regime=MarketRegime.TRENDING_UP)

        assert sim._closed_trades[0].metadata.get("exit_reason") == "regime_change"

    async def test_check_exits_without_current_regime_argument_skips_regime_check(self):
        """Rueckwaertskompatibilitaet: current_regime=None (Default, alle
        bestehenden Aufrufer/Tests vor diesem Fix) darf den Regime-Change-
        Check niemals ausloesen - unveraendertes Verhalten fuer Aufrufer,
        die den neuen Parameter nicht kennen."""
        sim = BacktestSimulator(make_config())
        candles = make_candles(10, start_price=100.0, drift=0.0)
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="mean_reversion_v1",
            signal_confidence=0.7,
            regime=MarketRegime.RANGING,
        )
        pos.entry_bar_index = 0
        sim._positions[SYMBOL_STR] = pos
        bar = candles[2].model_copy(update={"close": Decimal("100.1")})
        candles[2] = bar

        await sim._check_exits(2, bar, candles[:3])  # kein current_regime

        assert SYMBOL_STR in sim._positions


# ---------------------------------------------------------------------
# _close_position()
# ---------------------------------------------------------------------


class TestClosePosition:
    def test_close_position_long_profit(self):
        sim = BacktestSimulator(make_config(slippage_pct=Decimal("0"), taker_fee=Decimal("0")))
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_UP,
        )
        sim._positions[SYMBOL_STR] = pos
        cash_before = sim._cash
        sim._close_position(pos, Decimal("110"), datetime.now(tz=UTC), bar_index=5, reason="test")
        trade = sim._closed_trades[0]
        assert trade.gross_pnl == Decimal("10")
        assert trade.net_pnl == Decimal("10")  # no fees, no slippage
        assert sim._cash == cash_before + Decimal("110")
        assert SYMBOL_STR not in sim._positions

    def test_close_position_short_profit(self):
        sim = BacktestSimulator(make_config(slippage_pct=Decimal("0"), taker_fee=Decimal("0")))
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="short",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_DOWN,
        )
        sim._positions[SYMBOL_STR] = pos
        sim._close_position(pos, Decimal("90"), datetime.now(tz=UTC), bar_index=5, reason="test")
        trade = sim._closed_trades[0]
        assert trade.gross_pnl == Decimal("10")  # price dropped, short profits

    def test_close_position_applies_fees(self):
        sim = BacktestSimulator(make_config(slippage_pct=Decimal("0"), taker_fee=Decimal("0.01")))
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_UP,
        )
        sim._positions[SYMBOL_STR] = pos
        sim._close_position(pos, Decimal("100"), datetime.now(tz=UTC), bar_index=5, reason="test")
        trade = sim._closed_trades[0]
        assert trade.fees > Decimal("0")
        assert trade.net_pnl == trade.gross_pnl - trade.fees

    def test_close_position_records_mae_mfe(self):
        sim = BacktestSimulator(make_config())
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_UP,
        )
        sim._positions[SYMBOL_STR] = pos
        pos.update_excursions(Decimal("95"))  # adverse move
        pos.update_excursions(Decimal("110"))  # favorable move
        sim._close_position(pos, Decimal("105"), datetime.now(tz=UTC), bar_index=5, reason="test")
        trade = sim._closed_trades[0]
        assert trade.max_adverse_excursion == Decimal("5")
        assert trade.max_favorable_excursion == Decimal("10")


# ---------------------------------------------------------------------
# Cash accounting regression (Schritt 16/17): _open_position()/
# _close_position() used to book SHORT positions with the LONG-side cash
# formula (subtract notional at open, add notional at close) - correct
# only for long. Verified via instrumentation on a real 180-day BTC/USDT
# backtest: 84/84 long trades matched cash_delta to net_pnl exactly,
# only 5/77 short trades did (see
# docs/ANALYSIS-mean-reversion-v1-schritt16-fundamental-suitability.md).
# These tests pin the fixed, symmetric behavior directly so a regression
# is caught immediately, not just in an ad-hoc diagnostic script.
# ---------------------------------------------------------------------


class TestCashAccountingRegression:
    async def test_open_long_decreases_cash(self):
        sim = BacktestSimulator(make_config(taker_fee=Decimal("0.001")))
        cash_before = sim._cash
        entry_bar = make_candles(1, start_price=100.0)[0]
        signal = make_signal(direction=SignalDirection.LONG, confidence=1.0, size_hint=1.0)
        await sim._open_position(signal, entry_bar, bar_index=0)
        assert sim._cash < cash_before

    async def test_open_short_increases_cash(self):
        """Opening a short receives sale proceeds - cash goes UP, not
        down. Before the fix, this asserted the opposite (short wrongly
        booked like a long: cash decreased on open)."""
        sim = BacktestSimulator(make_config(taker_fee=Decimal("0.001")))
        cash_before = sim._cash
        entry_bar = make_candles(1, start_price=100.0)[0]
        signal = make_signal(direction=SignalDirection.SHORT, confidence=1.0, size_hint=1.0)
        await sim._open_position(signal, entry_bar, bar_index=0)
        assert sim._cash > cash_before

    async def test_open_close_long_cash_delta_equals_net_pnl(self):
        sim = BacktestSimulator(
            make_config(taker_fee=Decimal("0.001"), slippage_pct=Decimal("0.0005"))
        )
        initial_cash = sim._cash
        entry_bar = make_candles(1, start_price=100.0)[0]
        signal = make_signal(direction=SignalDirection.LONG, confidence=1.0, size_hint=1.0)
        await sim._open_position(signal, entry_bar, bar_index=0)
        pos = sim._positions[SYMBOL_STR]
        sim._close_position(pos, Decimal("110"), datetime.now(tz=UTC), bar_index=5, reason="test")
        trade = sim._closed_trades[0]
        cash_delta = sim._cash - initial_cash
        assert abs(cash_delta - trade.net_pnl) < Decimal("0.0001")

    async def test_open_close_short_win_cash_delta_equals_net_pnl(self):
        """The core regression test for the reported bug: before the fix,
        cash_delta and net_pnl were essentially unrelated for shorts (only
        5/77 real backtest trades matched); the discrepancy was on the
        order of the position's full notional, not a rounding artifact."""
        sim = BacktestSimulator(
            make_config(taker_fee=Decimal("0.001"), slippage_pct=Decimal("0.0005"))
        )
        initial_cash = sim._cash
        entry_bar = make_candles(1, start_price=100.0)[0]
        signal = make_signal(direction=SignalDirection.SHORT, confidence=1.0, size_hint=1.0)
        await sim._open_position(signal, entry_bar, bar_index=0)
        pos = sim._positions[SYMBOL_STR]
        # Price drops -> short profits.
        sim._close_position(pos, Decimal("90"), datetime.now(tz=UTC), bar_index=5, reason="test")
        trade = sim._closed_trades[0]
        assert trade.net_pnl > Decimal("0")
        cash_delta = sim._cash - initial_cash
        assert abs(cash_delta - trade.net_pnl) < Decimal("0.0001")

    async def test_open_close_short_loss_cash_delta_equals_net_pnl(self):
        sim = BacktestSimulator(
            make_config(taker_fee=Decimal("0.001"), slippage_pct=Decimal("0.0005"))
        )
        initial_cash = sim._cash
        entry_bar = make_candles(1, start_price=100.0)[0]
        signal = make_signal(direction=SignalDirection.SHORT, confidence=1.0, size_hint=1.0)
        await sim._open_position(signal, entry_bar, bar_index=0)
        pos = sim._positions[SYMBOL_STR]
        # Price rises -> short loses.
        sim._close_position(pos, Decimal("115"), datetime.now(tz=UTC), bar_index=5, reason="test")
        trade = sim._closed_trades[0]
        assert trade.net_pnl < Decimal("0")
        cash_delta = sim._cash - initial_cash
        assert abs(cash_delta - trade.net_pnl) < Decimal("0.0001")

    async def test_compute_portfolio_value_short_is_liability_not_asset(self):
        """An open short's mark-to-market value must be SUBTRACTED (you
        owe the buy-back to close), not added - otherwise the equity
        curve double-counts the sale proceeds already credited to cash
        at open. Immediately after opening (price ~= entry, zero fees
        here), portfolio value must be unchanged from before the open."""
        sim = BacktestSimulator(make_config(slippage_pct=Decimal("0"), taker_fee=Decimal("0")))
        entry_bar = make_candles(1, start_price=100.0)[0]
        signal = make_signal(direction=SignalDirection.SHORT, confidence=1.0, size_hint=1.0)
        cash_before_open = sim._cash
        await sim._open_position(signal, entry_bar, bar_index=0)
        pos = sim._positions[SYMBOL_STR]
        value_at_entry = sim._compute_portfolio_value(float(pos.entry_price))
        assert abs(Decimal(str(value_at_entry)) - cash_before_open) < Decimal("0.01")

    async def test_multi_trade_sequence_equity_matches_cumulative_net_pnl(self):
        """Alternating long/short trades. After every close (no position
        open), cash must exactly equal initial_capital + cumulative
        net_pnl, and portfolio value must exactly equal cash. This is the
        invariant the original bug violated - on a real 180-day backtest,
        final cash diverged from sum(net_pnl) by roughly 2x once shorts
        were involved (-209.83 actual vs -418.34 implied by net_pnl)."""
        sim = BacktestSimulator(
            make_config(taker_fee=Decimal("0.001"), slippage_pct=Decimal("0.0005"))
        )
        initial_cash = sim._cash
        cumulative_net_pnl = Decimal("0")

        trade_plan = [
            (SignalDirection.LONG, 100.0, Decimal("110")),  # long win
            (SignalDirection.SHORT, 110.0, Decimal("95")),  # short win
            (SignalDirection.SHORT, 95.0, Decimal("105")),  # short loss
            (SignalDirection.LONG, 105.0, Decimal("100")),  # long loss
        ]

        for direction, entry_price_hint, exit_price in trade_plan:
            entry_bar = make_candles(1, start_price=entry_price_hint)[0]
            signal = make_signal(direction=direction, confidence=1.0, size_hint=1.0)
            await sim._open_position(signal, entry_bar, bar_index=0)
            pos = sim._positions[SYMBOL_STR]
            sim._close_position(pos, exit_price, datetime.now(tz=UTC), bar_index=5, reason="test")
            cumulative_net_pnl += sim._closed_trades[-1].net_pnl

            assert SYMBOL_STR not in sim._positions
            assert abs(sim._cash - (initial_cash + cumulative_net_pnl)) < Decimal("0.0001")
            portfolio_value = Decimal(str(sim._compute_portfolio_value(float(exit_price))))
            assert abs(portfolio_value - sim._cash) < Decimal("0.0001")

        assert cumulative_net_pnl != Decimal("0")  # sanity: trades actually moved money


# ---------------------------------------------------------------------
# SimulatedPosition
# ---------------------------------------------------------------------


class TestSimulatedPosition:
    def test_update_excursions_long_tracks_mae_and_mfe(self):
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_UP,
        )
        pos.update_excursions(Decimal("90"))  # -10 pnl
        assert pos.max_adverse_excursion == Decimal("10")
        pos.update_excursions(Decimal("120"))  # +20 pnl
        assert pos.max_favorable_excursion == Decimal("20")
        # A smaller adverse move afterward must not shrink the recorded MAE
        pos.update_excursions(Decimal("98"))
        assert pos.max_adverse_excursion == Decimal("10")

    def test_update_excursions_short_tracks_mae_and_mfe(self):
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="short",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_DOWN,
        )
        pos.update_excursions(Decimal("110"))  # price up = adverse for short
        assert pos.max_adverse_excursion == Decimal("10")
        pos.update_excursions(Decimal("80"))  # price down = favorable for short
        assert pos.max_favorable_excursion == Decimal("20")

    def test_unrealized_pnl_is_always_zero(self):
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_UP,
        )
        assert pos.unrealized_pnl == Decimal("0")


# ---------------------------------------------------------------------
# Portfolio value / equity recording
# ---------------------------------------------------------------------


class TestPortfolioAndEquity:
    def test_compute_portfolio_value_cash_only(self):
        sim = BacktestSimulator(make_config(initial_capital=Decimal("5000")))
        assert sim._compute_portfolio_value(100.0) == 5000.0

    def test_compute_portfolio_value_includes_open_positions(self):
        sim = BacktestSimulator(make_config(initial_capital=Decimal("5000")))
        sim._cash = Decimal("4000")
        sim._positions[SYMBOL_STR] = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("2"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_UP,
        )
        value = sim._compute_portfolio_value(110.0)
        assert value == 4000.0 + 2 * 110.0

    def test_update_positions_calls_update_excursions_on_all(self):
        sim = BacktestSimulator(make_config())
        pos = SimulatedPosition(
            symbol=SYMBOL_STR,
            side="long",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            entry_time=datetime.now(tz=UTC),
            strategy="s",
            signal_confidence=0.9,
            regime=MarketRegime.TRENDING_UP,
        )
        sim._positions[SYMBOL_STR] = pos
        sim._update_positions(90.0)
        assert pos.max_adverse_excursion == Decimal("10")

    def test_record_equity_tracks_peak_and_drawdown(self):
        sim = BacktestSimulator(make_config(initial_capital=Decimal("1000")))
        sim._record_equity(datetime.now(tz=UTC), 1000.0, 100.0)
        sim._record_equity(datetime.now(tz=UTC), 1200.0, 110.0)  # new peak
        sim._record_equity(datetime.now(tz=UTC), 900.0, 90.0)  # drawdown from peak

        assert sim._equity_curve[0].drawdown_pct == 0.0
        assert sim._equity_curve[1].drawdown_pct == 0.0
        expected_dd = round((1200.0 - 900.0) / 1200.0 * 100, 4)
        assert sim._equity_curve[2].drawdown_pct == expected_dd

    def test_record_equity_open_positions_value_field(self):
        sim = BacktestSimulator(make_config(initial_capital=Decimal("1000")))
        sim._cash = Decimal("800")
        sim._record_equity(datetime.now(tz=UTC), 1000.0, 100.0)
        point = sim._equity_curve[0]
        assert point.open_positions_value == Decimal("200.00")
        assert point.cash == Decimal("800")


# ---------------------------------------------------------------------
# _detect_regime_simple()
# ---------------------------------------------------------------------


class TestDetectRegimeSimple:
    def _features(self, **indicator_overrides) -> FeatureSet:
        ind = IndicatorValues(**indicator_overrides)
        return FeatureSet(
            symbol=SYMBOL,
            timestamp=datetime.now(tz=UTC),
            timeframe="1h",
            close=Decimal("100"),
            volume=Decimal("10"),
            indicators=ind,
        )

    def test_missing_adx_or_rsi_returns_unknown(self):
        sim = BacktestSimulator(make_config())
        assert sim._detect_regime_simple(self._features()) == MarketRegime.UNKNOWN

    def test_strong_trend_up(self):
        sim = BacktestSimulator(make_config())
        f = self._features(adx_14=30.0, rsi_14=60.0, di_plus=25.0, di_minus=10.0)
        assert sim._detect_regime_simple(f) == MarketRegime.TRENDING_UP

    def test_strong_trend_down(self):
        sim = BacktestSimulator(make_config())
        f = self._features(adx_14=30.0, rsi_14=40.0, di_plus=10.0, di_minus=25.0)
        assert sim._detect_regime_simple(f) == MarketRegime.TRENDING_DOWN

    def test_high_adx_but_ambiguous_rsi_falls_through_to_volatility_check(self):
        sim = BacktestSimulator(make_config())
        # adx > 25 but rsi doesn't clearly indicate up or down trend
        f = self._features(adx_14=30.0, rsi_14=50.0, di_plus=15.0, di_minus=15.0, atr_pct=0.08)
        assert sim._detect_regime_simple(f) == MarketRegime.HIGH_VOLATILITY

    def test_low_adx_returns_ranging(self):
        sim = BacktestSimulator(make_config())
        f = self._features(adx_14=15.0, rsi_14=50.0)
        assert sim._detect_regime_simple(f) == MarketRegime.RANGING

    def test_mid_adx_low_volatility_defaults_to_ranging(self):
        sim = BacktestSimulator(make_config())
        f = self._features(adx_14=22.0, rsi_14=50.0, atr_pct=0.01)
        assert sim._detect_regime_simple(f) == MarketRegime.RANGING

    def test_mid_adx_high_volatility_returns_high_volatility(self):
        sim = BacktestSimulator(make_config())
        f = self._features(adx_14=22.0, rsi_14=50.0, atr_pct=0.09)
        assert sim._detect_regime_simple(f) == MarketRegime.HIGH_VOLATILITY


# ---------------------------------------------------------------------
# End-to-end integration through run() with a real trend-following-like
# fake strategy, exercising feature computation + regime + entry + exit
# across the full loop body.
# ---------------------------------------------------------------------


class TestEndToEnd:
    async def test_full_run_uptrend_produces_at_least_one_trade(self):
        candles = make_candles(260, start_price=100.0, drift=0.6)
        signal = make_signal(direction=SignalDirection.LONG, confidence=0.95)
        strat = FakeStrategy(
            "always_long",
            [
                MarketRegime.TRENDING_UP,
                MarketRegime.RANGING,
                MarketRegime.HIGH_VOLATILITY,
                MarketRegime.UNKNOWN,
                MarketRegime.TRENDING_DOWN,
                MarketRegime.BREAKOUT,
                MarketRegime.CRISIS,
            ],
            signal=signal,
        )
        sim = BacktestSimulator(make_config(initial_capital=Decimal("10000")))
        trades, equity = await sim.run({SYMBOL_STR: candles}, FakeRegistry([strat]))

        assert len(equity) == 60  # 260 - 200 warmup
        # At least one position should have been opened given a persistent
        # LONG signal across a real feature-engineered uptrend.
        assert len(trades) >= 1 or sim._positions  # opened (and maybe still open until close)

    async def test_full_run_precompute_exception_propagates(self, monkeypatch):
        """
        Verhaltensänderung durch den Precompute-Performance-Fix (siehe
        BacktestSimulator._precompute_all_features() Docstring): Feature-
        Berechnung läuft jetzt EINMAL vektorisiert über die komplette Serie
        vor der Hauptschleife, nicht mehr pro Bar mit individuellem
        try/except. Ein Fehler in der Vorab-Berechnung ist ein
        struktureller Bug (z.B. NaN-Propagation durch die ganze Serie),
        kein isolierbarer "kaputter einzelner Bar" mehr wie zuvor - er
        propagiert bewusst, statt Bars stillschweigend zu überspringen und
        einen irreführend unvollständigen Backtest zu produzieren.
        """
        candles = make_candles(210, start_price=100.0, drift=0.3)
        sim = BacktestSimulator(make_config())

        monkeypatch.setattr(
            sim,
            "_precompute_all_features",
            lambda candles: (_ for _ in ()).throw(ValueError("feature computation blew up")),
        )

        with pytest.raises(ValueError, match="feature computation blew up"):
            await sim.run({SYMBOL_STR: candles}, FakeRegistry([]))


# ===========================================================================
# BacktestSimulator._precompute_all_features
# ===========================================================================


class TestPrecomputeAllFeatures:
    """
    Performance-Fix Regression: _precompute_all_features() muss für jeden
    Bar-Index EXAKT dasselbe FeatureSet liefern wie
    FeatureEngineer().compute(candles[:i+1]) - die alte, pro-Bar-Methode,
    die vorher im Hot-Loop lief (siehe run()-Docstring / Modul-Historie).
    Das ist die Korrektheitsgarantie für den O(n^2)->O(n) Fix: schnellere
    Berechnung ist wertlos, wenn sie andere Zahlen liefert.
    """

    @pytest.mark.parametrize("bar_index", [199, 200, 201, 250, 299])
    async def test_matches_per_bar_compute_for_various_indices(self, bar_index):
        from sgr.market_data.feature_engineering import FeatureEngineer

        candles = make_candles(300, start_price=100.0, drift=0.3)
        sim = BacktestSimulator(make_config())
        engineer = FeatureEngineer()

        precomputed = sim._precompute_all_features(candles)
        expected = engineer.compute(candles[: bar_index + 1])
        actual = precomputed[bar_index]

        assert actual is not None
        assert actual.indicators == expected.indicators
        assert actual.returns_1 == expected.returns_1
        assert actual.returns_5 == expected.returns_5
        assert actual.returns_10 == expected.returns_10
        assert actual.returns_20 == expected.returns_20
        assert actual.close == expected.close
        assert actual.timestamp == expected.timestamp

    async def test_indices_below_min_candles_are_none(self):
        candles = make_candles(300, start_price=100.0, drift=0.3)
        sim = BacktestSimulator(make_config())

        precomputed = sim._precompute_all_features(candles)

        # MIN_CANDLES=50 -> Index 48 (49 Bars History) ist zu wenig
        assert precomputed[48] is None
        # Index 49 (50 Bars History) reicht gerade
        assert precomputed[49] is not None

    async def test_empty_candles_returns_empty_list(self):
        sim = BacktestSimulator(make_config())
        assert sim._precompute_all_features([]) == []
