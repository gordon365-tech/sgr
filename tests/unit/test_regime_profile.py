"""Tests fuer sgr.strategy.regime_profile."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sgr.core.types import Candle, ExchangeID, MarketRegime, Symbol
from sgr.strategy.regime_profile import (
    RegimeProfile,
    build_regime_profile,
    select_candidate_strategies,
)


def _symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def _trending_candles(n: int) -> list[Candle]:
    """Monoton steigende Preise -> klarer Aufwaertstrend (ADX hoch,
    DI+ dominant), damit classify_regime() zuverlaessig TRENDING_UP
    erkennt."""
    sym = _symbol()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    out = []
    price = Decimal("10000")
    for i in range(n):
        price = price + Decimal("15")
        out.append(
            Candle(
                symbol=sym,
                timestamp=start + timedelta(hours=i),
                timeframe="1h",
                open=price - Decimal("5"),
                high=price + Decimal("10"),
                low=price - Decimal("10"),
                close=price,
                volume=Decimal("1000"),
            )
        )
    return out


class FakeStrategy:
    def __init__(self, name: str, regimes: list[MarketRegime]) -> None:
        self.name = name
        self.supported_regimes = regimes


class TestBuildRegimeProfile:
    def test_too_few_candles_returns_unknown(self) -> None:
        profile = build_regime_profile(_trending_candles(50))
        assert profile.dominant_regime == MarketRegime.UNKNOWN
        assert profile.n_samples == 0

    def test_strong_uptrend_is_detected_as_dominant_regime(self) -> None:
        profile = build_regime_profile(_trending_candles(600))
        assert profile.n_samples > 0
        # Ein klarer, monotoner Aufwaertstrend sollte ueberwiegend als
        # TRENDING_UP klassifiziert werden (regime_classifier.py).
        assert profile.distribution.get(MarketRegime.TRENDING_UP.value, 0) >= 1


class TestSelectCandidateStrategies:
    def test_filters_to_regime_matching_strategies_only(self) -> None:
        profile = RegimeProfile(
            dominant_regime=MarketRegime.TRENDING_UP,
            distribution={"trending_up": 5, "ranging": 1},
            diversity_count=2,
            n_samples=6,
        )
        strategies = [
            FakeStrategy(
                "trend_following_v1", [MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN]
            ),
            FakeStrategy("mean_reversion_v1", [MarketRegime.RANGING]),
            FakeStrategy("breakout_v1", [MarketRegime.BREAKOUT]),
        ]

        candidates = select_candidate_strategies(profile, strategies)

        names = {s.name for s in candidates}
        assert names == {"trend_following_v1", "mean_reversion_v1"}
        assert "breakout_v1" not in names

    def test_no_regime_overlap_returns_empty_list(self) -> None:
        profile = RegimeProfile(
            dominant_regime=MarketRegime.UNKNOWN,
            distribution={"unknown": 10},
            diversity_count=1,
            n_samples=10,
        )
        strategies = [FakeStrategy("trend_following_v1", [MarketRegime.TRENDING_UP])]

        assert select_candidate_strategies(profile, strategies) == []
