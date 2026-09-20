"""Tests für sgr.market_data.grid_suitability.compute_grid_suitability()."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sgr.core.types import ExchangeID, GridDirection, MarketRegime, Symbol
from sgr.market_data.grid_suitability import compute_grid_suitability
from sgr.market_data.types import FeatureSet, FuturesFeatures, IndicatorValues, MarketContext


def _context(
    adx: float | None = 15.0,
    atr_pct: float | None = 0.01,
    bb_width: float | None = 0.05,
    di_plus: float | None = 20.0,
    di_minus: float | None = 20.0,
    volume: Decimal = Decimal("1000000"),
    close: Decimal = Decimal("100"),
    funding_annualized_pct: float | None = None,
) -> MarketContext:
    symbol = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX)
    indicators = IndicatorValues(
        adx_14=adx, atr_pct=atr_pct, bb_width=bb_width, di_plus=di_plus, di_minus=di_minus
    )
    futures = None
    if funding_annualized_pct is not None:
        futures = FuturesFeatures(
            funding_rate=Decimal("0.0001"),
            funding_rate_annualized=funding_annualized_pct,
            is_contango=funding_annualized_pct > 0,
            open_interest=Decimal("1000"),
            open_interest_usd=Decimal("1000000"),
        )
    features = FeatureSet(
        symbol=symbol,
        timestamp=datetime.now(tz=UTC),
        timeframe="1h",
        close=close,
        volume=volume,
        indicators=indicators,
        futures=futures,
    )
    return MarketContext(
        symbol=symbol, timestamp=features.timestamp, primary=features, regime=MarketRegime.RANGING
    )


class TestRangeMarketIsSuitable:
    def test_low_adx_tight_bands_high_liquidity_is_suitable(self) -> None:
        # atr_pct nahe der "Sweet Spot"-Mitte zwischen MIN/MAX-Volatilitaet
        # (siehe _MIN_VOLATILITY_ATR_PCT/_MAX_VOLATILITY_ATR_PCT) - weder
        # zu ruhig (kein Grid-Capture-Potential) noch zu wild.
        ctx = _context(adx=12.0, atr_pct=0.04, bb_width=0.03, volume=Decimal("5000000"))
        score = compute_grid_suitability(ctx)

        assert score.is_suitable is True
        assert score.range_quality > 0.5
        assert score.suggested_direction in (GridDirection.LONG, GridDirection.SHORT)


class TestTrendingMarketIsUnsuitable:
    def test_strong_trend_reduces_range_quality_and_suitability(self) -> None:
        ctx = _context(adx=45.0, atr_pct=0.02, bb_width=0.03, di_plus=40.0, di_minus=5.0)
        score = compute_grid_suitability(ctx)

        assert score.range_quality < 0.5
        assert score.is_suitable is False
        assert score.suggested_direction == GridDirection.NEUTRAL


class TestExtremeVolatilityIsUnsuitable:
    def test_extreme_atr_zeroes_volatility_quality(self) -> None:
        ctx = _context(adx=12.0, atr_pct=0.15, bb_width=0.02)
        score = compute_grid_suitability(ctx)

        assert score.volatility_quality == 0.0
        assert score.is_suitable is False


class TestIlliquidMarketIsUnsuitable:
    def test_low_volume_reduces_liquidity_score(self) -> None:
        ctx = _context(
            adx=12.0, atr_pct=0.015, bb_width=0.02, volume=Decimal("1"), close=Decimal("1")
        )
        score = compute_grid_suitability(ctx, min_liquidity_volume=100_000)

        assert score.liquidity_score < 0.1
        assert score.is_suitable is False


class TestFundingCostLimitsGridSuitability:
    def test_extreme_funding_rate_zeroes_funding_score(self) -> None:
        ctx = _context(adx=12.0, atr_pct=0.015, bb_width=0.02, funding_annualized_pct=80.0)
        score = compute_grid_suitability(ctx, max_funding_annualized_pct=50.0)

        assert score.funding_cost_score == 0.0
        assert score.is_suitable is False

    def test_normal_funding_rate_does_not_block(self) -> None:
        ctx = _context(adx=12.0, atr_pct=0.015, bb_width=0.02, funding_annualized_pct=5.0)
        score = compute_grid_suitability(ctx, max_funding_annualized_pct=50.0)

        assert score.funding_cost_score > 0.8

    def test_no_futures_context_means_no_funding_penalty(self) -> None:
        ctx = _context(adx=12.0, atr_pct=0.015, bb_width=0.02, funding_annualized_pct=None)
        score = compute_grid_suitability(ctx)

        assert score.funding_cost_score == 1.0


class TestMissingIndicatorsAreConservative:
    def test_missing_adx_yields_zero_range_quality(self) -> None:
        ctx = _context(adx=None)
        score = compute_grid_suitability(ctx)

        assert score.range_quality == 0.0
        assert any("ADX" in r for r in score.reasons)

    def test_missing_atr_yields_zero_volatility_quality(self) -> None:
        ctx = _context(atr_pct=None)
        score = compute_grid_suitability(ctx)

        assert score.volatility_quality == 0.0


class TestSuggestedDirectionBias:
    def test_bullish_di_bias_suggests_long(self) -> None:
        ctx = _context(adx=22.0, atr_pct=0.015, bb_width=0.03, di_plus=25.0, di_minus=10.0)
        score = compute_grid_suitability(ctx)

        if score.is_suitable:
            assert score.suggested_direction == GridDirection.LONG

    def test_bearish_di_bias_suggests_short(self) -> None:
        ctx = _context(adx=22.0, atr_pct=0.015, bb_width=0.03, di_plus=10.0, di_minus=25.0)
        score = compute_grid_suitability(ctx)

        if score.is_suitable:
            assert score.suggested_direction == GridDirection.SHORT
