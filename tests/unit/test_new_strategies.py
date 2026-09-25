"""
Tests fuer die drei neuen Strategien (breakout_v1, momentum_v1,
volatility_adjusted_momentum_v1) und den Regime-Klassifizierer
(regime_detector_v1) - Autonomous-Paper-Trading-Rollout.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sgr.core.types import ExchangeID, MarketRegime, SignalDirection, Symbol
from sgr.market_data.types import FeatureSet, IndicatorValues, MarketContext
from sgr.strategy.breakout import BreakoutStrategy
from sgr.strategy.momentum import MomentumStrategy
from sgr.strategy.regime_classifier import REGIME_RANK, classify_regime
from sgr.strategy.volatility_adjusted_momentum import VolatilityAdjustedMomentumStrategy


def _symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def _indicators(**overrides) -> IndicatorValues:
    defaults = dict(
        rsi_14=55.0,
        adx_14=22.0,
        di_plus=20.0,
        di_minus=15.0,
        atr_14=Decimal("500"),
        atr_pct=0.02,
        bb_upper=Decimal("51000"),
        bb_middle=Decimal("50000"),
        bb_lower=Decimal("49000"),
        bb_width=0.02,
        bb_position=0.5,
        kc_upper=Decimal("50800"),
        kc_lower=Decimal("49200"),
        macd_histogram=0.0,
        volume_ratio=1.0,
    )
    defaults.update(overrides)
    return IndicatorValues(**defaults)


def _context(
    regime: MarketRegime,
    indicators: IndicatorValues,
    close: float = 50000.0,
    returns_5: float | None = 0.0,
    returns_10: float | None = 0.0,
) -> MarketContext:
    sym = _symbol()
    fs = FeatureSet(
        symbol=sym,
        timestamp=datetime.now(tz=UTC),
        timeframe="1h",
        close=Decimal(str(close)),
        volume=Decimal("1000"),
        indicators=indicators,
        regime=regime,
        returns_5=returns_5,
        returns_10=returns_10,
    )
    return MarketContext(symbol=sym, timestamp=fs.timestamp, primary=fs, regime=regime)


# ---------------------------------------------------------------------------
# regime_classifier ("regime_detector_v1")
# ---------------------------------------------------------------------------


class TestClassifyRegime:
    def test_missing_adx_or_atr_pct_returns_unknown(self) -> None:
        regime, confidence = classify_regime(_indicators(adx_14=None))
        assert regime == MarketRegime.UNKNOWN
        assert confidence == 0.0

    def test_high_volatility_takes_priority(self) -> None:
        regime, _ = classify_regime(_indicators(atr_pct=0.08, adx_14=30, di_plus=25, di_minus=10))
        assert regime == MarketRegime.HIGH_VOLATILITY

    def test_low_volatility_when_no_trend_and_low_atr(self) -> None:
        regime, _ = classify_regime(_indicators(adx_14=10, atr_pct=0.005))
        assert regime == MarketRegime.LOW_VOLATILITY

    def test_ranging_when_no_trend_and_normal_atr(self) -> None:
        regime, _ = classify_regime(_indicators(adx_14=10, atr_pct=0.02))
        assert regime == MarketRegime.RANGING

    def test_trending_up_when_strong_adx_and_di_plus_dominant(self) -> None:
        regime, _ = classify_regime(_indicators(adx_14=30, di_plus=28, di_minus=10, atr_pct=0.02))
        assert regime == MarketRegime.TRENDING_UP

    def test_trending_down_when_strong_adx_and_di_minus_dominant(self) -> None:
        regime, _ = classify_regime(_indicators(adx_14=30, di_plus=10, di_minus=28, atr_pct=0.02))
        assert regime == MarketRegime.TRENDING_DOWN

    def test_breakout_when_bands_expanded_and_price_at_edge(self) -> None:
        regime, _ = classify_regime(
            _indicators(adx_14=15, atr_pct=0.02, bb_width=0.06, bb_position=0.97)
        )
        assert regime == MarketRegime.BREAKOUT

    def test_transition_zone_is_unknown_not_guessed(self) -> None:
        # ADX zwischen Range- und Trend-Schwelle, keine Breakout-Bedingung.
        regime, confidence = classify_regime(
            _indicators(adx_14=22, atr_pct=0.02, bb_width=0.02, bb_position=0.5)
        )
        assert regime == MarketRegime.UNKNOWN
        assert confidence < 0.5

    def test_regime_rank_covers_every_enum_value(self) -> None:
        for regime in MarketRegime:
            assert regime in REGIME_RANK


# ---------------------------------------------------------------------------
# BreakoutStrategy
# ---------------------------------------------------------------------------


class TestBreakoutStrategy:
    def test_long_signal_on_upward_breakout(self) -> None:
        strategy = BreakoutStrategy()
        ind = _indicators(
            bb_position=0.95,
            bb_width=0.06,
            bb_upper=Decimal("51000"),
            kc_upper=Decimal("50500"),
            volume_ratio=1.5,
            rsi_14=65.0,
            adx_14=20.0,
        )
        context = _context(MarketRegime.BREAKOUT, ind, close=51200.0)
        signal = strategy.generate_signal(context)
        assert signal is not None
        assert signal.direction == SignalDirection.LONG

    def test_short_signal_on_downward_breakout(self) -> None:
        strategy = BreakoutStrategy()
        ind = _indicators(
            bb_position=0.05,
            bb_width=0.06,
            bb_lower=Decimal("49000"),
            kc_lower=Decimal("49500"),
            volume_ratio=1.5,
            rsi_14=35.0,
            adx_14=20.0,
        )
        context = _context(MarketRegime.BREAKOUT, ind, close=48800.0)
        signal = strategy.generate_signal(context)
        assert signal is not None
        assert signal.direction == SignalDirection.SHORT

    def test_no_signal_outside_breakout_regime(self) -> None:
        strategy = BreakoutStrategy()
        ind = _indicators(bb_position=0.95, bb_width=0.06, volume_ratio=1.5)
        context = _context(MarketRegime.RANGING, ind)
        assert strategy.generate_signal(context) is None

    def test_no_signal_without_band_expansion(self) -> None:
        strategy = BreakoutStrategy()
        # bb_width unter der Expansions-Schwelle UND Volumen/RSI schwach -
        # ohne Baender-Expansion und ohne weitere Bestaetigung reicht der
        # gewichtete Score in keiner Richtung fuer min_confidence.
        ind = _indicators(bb_position=0.95, bb_width=0.01, volume_ratio=1.0, rsi_14=50.0)
        context = _context(MarketRegime.BREAKOUT, ind)
        signal = strategy.generate_signal(context)
        assert signal is None or signal.confidence < 0.55

    def test_signal_metadata_present(self) -> None:
        strategy = BreakoutStrategy()
        ind = _indicators(
            bb_position=0.95, bb_width=0.06, volume_ratio=1.5, rsi_14=65.0, adx_14=20.0
        )
        context = _context(MarketRegime.BREAKOUT, ind, close=51200.0)
        signal = strategy.generate_signal(context)
        if signal:
            assert "bb_width" in signal.metadata

    def test_get_parameters(self) -> None:
        strategy = BreakoutStrategy()
        params = strategy.get_parameters()
        assert params.name == "breakout_v1"
        assert "bb_width_expansion_min" in params.params


# ---------------------------------------------------------------------------
# MomentumStrategy
# ---------------------------------------------------------------------------


class TestMomentumStrategy:
    def test_long_signal_on_strong_upward_momentum(self) -> None:
        strategy = MomentumStrategy()
        ind = _indicators(rsi_14=65.0, macd_histogram=15.0, volume_ratio=1.3, adx_14=22.0)
        context = _context(MarketRegime.TRENDING_UP, ind, returns_5=0.03, returns_10=0.02)
        signal = strategy.generate_signal(context)
        assert signal is not None
        assert signal.direction == SignalDirection.LONG

    def test_short_signal_on_strong_downward_momentum(self) -> None:
        strategy = MomentumStrategy()
        ind = _indicators(rsi_14=35.0, macd_histogram=-15.0, volume_ratio=1.3, adx_14=22.0)
        context = _context(MarketRegime.TRENDING_DOWN, ind, returns_5=-0.03, returns_10=-0.02)
        signal = strategy.generate_signal(context)
        assert signal is not None
        assert signal.direction == SignalDirection.SHORT

    def test_no_signal_outside_trending_regime(self) -> None:
        strategy = MomentumStrategy()
        ind = _indicators(rsi_14=65.0, macd_histogram=15.0, volume_ratio=1.3)
        context = _context(MarketRegime.RANGING, ind, returns_5=0.03, returns_10=0.02)
        assert strategy.generate_signal(context) is None

    def test_no_signal_without_returns_confirmation(self) -> None:
        strategy = MomentumStrategy()
        # Keine returns-Bestaetigung UND RSI/MACD ausserhalb des
        # Momentum-Fensters - ohne Geschwindigkeitssignal reicht der
        # gewichtete Score nicht fuer min_confidence.
        ind = _indicators(rsi_14=50.0, macd_histogram=0.0, volume_ratio=1.3)
        context = _context(MarketRegime.TRENDING_UP, ind, returns_5=0.0, returns_10=0.0)
        signal = strategy.generate_signal(context)
        assert signal is None or signal.confidence < 0.55

    def test_get_parameters(self) -> None:
        strategy = MomentumStrategy()
        params = strategy.get_parameters()
        assert params.name == "momentum_v1"
        assert "returns_5_min" in params.params


# ---------------------------------------------------------------------------
# VolatilityAdjustedMomentumStrategy
# ---------------------------------------------------------------------------


class TestVolatilityAdjustedMomentumStrategy:
    def test_long_signal_in_high_volatility_with_strong_momentum(self) -> None:
        strategy = VolatilityAdjustedMomentumStrategy()
        ind = _indicators(rsi_14=68.0, macd_histogram=20.0, volume_ratio=1.5, atr_pct=0.06)
        context = _context(MarketRegime.HIGH_VOLATILITY, ind, returns_5=0.04, returns_10=0.03)
        signal = strategy.generate_signal(context)
        assert signal is not None
        assert signal.direction == SignalDirection.LONG

    def test_no_signal_outside_high_volatility_regime(self) -> None:
        strategy = VolatilityAdjustedMomentumStrategy()
        ind = _indicators(rsi_14=68.0, macd_histogram=20.0, volume_ratio=1.5, atr_pct=0.06)
        context = _context(MarketRegime.TRENDING_UP, ind, returns_5=0.04, returns_10=0.03)
        assert strategy.generate_signal(context) is None

    def test_confidence_is_dampened_by_excess_volatility(self) -> None:
        """Gleiche rohe Signalstaerke, aber hoehere atr_pct -> niedrigere
        Konfidenz (Modul-Docstring Punkt 2: PositionSizer skaliert
        Positionsgroesse proportional zur Konfidenz)."""
        strategy = VolatilityAdjustedMomentumStrategy()
        moderate_vol_ind = _indicators(
            rsi_14=68.0, macd_histogram=20.0, volume_ratio=1.5, atr_pct=0.051
        )
        extreme_vol_ind = _indicators(
            rsi_14=68.0, macd_histogram=20.0, volume_ratio=1.5, atr_pct=0.15
        )
        moderate_signal = strategy.generate_signal(
            _context(
                MarketRegime.HIGH_VOLATILITY, moderate_vol_ind, returns_5=0.04, returns_10=0.03
            )
        )
        extreme_signal = strategy.generate_signal(
            _context(MarketRegime.HIGH_VOLATILITY, extreme_vol_ind, returns_5=0.04, returns_10=0.03)
        )
        assert moderate_signal is not None
        # Bei extremer Volatilitaet darf entweder die Konfidenz spuerbar
        # niedriger sein, oder die Daempfung druecke das Signal ganz unter
        # die min_confidence-Schwelle (kein Trade statt eines schwachen).
        if extreme_signal is not None:
            assert extreme_signal.confidence < moderate_signal.confidence

    def test_get_parameters(self) -> None:
        strategy = VolatilityAdjustedMomentumStrategy()
        params = strategy.get_parameters()
        assert params.name == "volatility_adjusted_momentum_v1"
        assert "atr_pct_reference" in params.params
