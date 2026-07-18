"""
Tests für Sentiment Engine und ML-Sentiment-Integration.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sgr.sentiment.engine import SentimentAggregator
from sgr.sentiment.macro_monitor import MacroEventMonitor
from sgr.sentiment.nlp_scorer import SentimentScorer
from sgr.sentiment.types import (
    EventCategory,
    MacroEvent,
    MacroEventType,
    SentimentAggregate,
    SentimentSignal,
    SentimentSource,
)

# ---------------------------------------------------------------------------
# NLP Scorer Tests
# ---------------------------------------------------------------------------


class TestSentimentScorer:
    def test_scorer_initializes(self) -> None:
        scorer = SentimentScorer()
        assert scorer.mode in ("finbert", "vader", "keyword")

    def test_score_returns_tuple(self) -> None:
        scorer = SentimentScorer()
        score, confidence = scorer.score("Bitcoin surges to new all-time high")
        assert isinstance(score, float)
        assert isinstance(confidence, float)

    def test_score_in_bounds(self) -> None:
        scorer = SentimentScorer()
        texts = [
            "Bitcoin crashes 30% amid market panic",
            "Bitcoin adoption reaches record highs",
            "Market remains stable",
            "",
        ]
        for text in texts:
            score, confidence = scorer.score(text)
            assert -1.0 <= score <= 1.0, f"Score {score} out of bounds for: {text}"
            assert 0.0 <= confidence <= 1.0

    def test_empty_text_returns_neutral(self) -> None:
        scorer = SentimentScorer()
        score, confidence = scorer.score("")
        assert score == 0.0
        assert confidence == 0.0

    def test_bullish_text_positive_score(self) -> None:
        scorer = SentimentScorer()
        score, _ = scorer.score("Bitcoin ETF approved by SEC, institutions rush to buy")
        # Keyword-basiert: sollte positiv sein
        assert score >= 0.0

    def test_bearish_text_negative_score(self) -> None:
        scorer = SentimentScorer()
        score, _ = scorer.score("Exchange hacked, billions in crypto stolen")
        assert score <= 0.0

    def test_classify_crisis_event(self) -> None:
        scorer = SentimentScorer()
        score, _ = scorer.score("Exchange exploit drains $300M")
        category = scorer.classify_event("Exchange exploit drains $300M", score)
        assert category == EventCategory.CRISIS

    def test_classify_macro_event(self) -> None:
        scorer = SentimentScorer()
        score, _ = scorer.score("CPI inflation rises above expectations")
        category = scorer.classify_event("CPI inflation rises above expectations", score)
        assert category in (EventCategory.MACRO_POSITIVE, EventCategory.MACRO_NEGATIVE)

    def test_classify_regulatory(self) -> None:
        scorer = SentimentScorer()
        text = "SEC files lawsuit against major crypto exchange"
        score, _ = scorer.score(text)
        category = scorer.classify_event(text, score)
        assert category == EventCategory.REGULATORY

    def test_long_text_truncated(self) -> None:
        """Sehr langer Text soll nicht crashen."""
        scorer = SentimentScorer()
        long_text = "Bitcoin " * 1000  # Sehr langer Text
        score, confidence = scorer.score(long_text)
        assert -1.0 <= score <= 1.0

    def test_keyword_fallback_scoring(self) -> None:
        """Keyword-Fallback funktioniert korrekt."""
        scorer = SentimentScorer()
        score, confidence = scorer._score_keyword("Bitcoin surge rally growth")
        assert score > 0
        score2, confidence2 = scorer._score_keyword("Bitcoin crash hack exploit")
        assert score2 < 0


# ---------------------------------------------------------------------------
# Sentiment Aggregator Tests
# ---------------------------------------------------------------------------


class TestSentimentAggregator:
    def _make_signal(
        self,
        score: float = 0.5,
        confidence: float = 0.8,
        entity: str = "BTC",
        source: SentimentSource = SentimentSource.NEWS,
        age_seconds: int = 0,
    ) -> SentimentSignal:
        from datetime import timedelta

        ts = datetime.now(tz=UTC)
        if age_seconds > 0:
            ts = ts - timedelta(seconds=age_seconds)
        return SentimentSignal(
            source=source,
            timestamp=ts,
            raw_score=score,
            confidence=confidence,
            event_category=EventCategory.BULLISH if score > 0 else EventCategory.BEARISH,
            entity=entity,
            headline="",
        )

    def test_empty_aggregator_returns_neutral(self) -> None:
        agg = SentimentAggregator()
        result = agg.aggregate("BTC")
        assert result.score_1h == 0.0
        assert result.signals_1h == 0

    def test_add_signal_included_in_aggregate(self) -> None:
        agg = SentimentAggregator()
        agg.add_signal(self._make_signal(score=0.8, confidence=0.9))
        result = agg.aggregate("BTC")
        assert result.score_1h > 0
        assert result.signals_1h > 0

    def test_low_confidence_signal_excluded(self) -> None:
        agg = SentimentAggregator()
        agg.add_signal(self._make_signal(score=0.9, confidence=0.1))  # < 0.30 threshold
        result = agg.aggregate("BTC")
        assert result.signals_1h == 0  # Soll ausgeschlossen sein

    def test_multiple_signals_averaged(self) -> None:
        agg = SentimentAggregator()
        agg.add_signal(self._make_signal(score=1.0, confidence=0.8))
        agg.add_signal(self._make_signal(score=-1.0, confidence=0.8))
        result = agg.aggregate("BTC")
        assert abs(result.score_1h) < 0.1  # Sollte nahe 0 sein

    def test_primary_score_weighted(self) -> None:
        agg = SentimentAggregator()
        agg.add_signal(self._make_signal(score=0.8, confidence=0.9, age_seconds=100))
        result = agg.aggregate("BTC")
        assert result.primary_score != 0.0

    def test_entity_filter(self) -> None:
        agg = SentimentAggregator()
        agg.add_signal(self._make_signal(score=0.8, entity="ETH"))  # ETH, nicht BTC
        result_btc = agg.aggregate("BTC")
        result_eth = agg.aggregate("ETH")
        # BTC sollte ETH-Signal nicht sehen (außer "market")
        assert result_eth.signals_1h >= result_btc.signals_1h

    def test_sentiment_label_extreme(self) -> None:
        agg = SentimentAggregator()
        agg.add_signal(self._make_signal(score=0.95, confidence=0.9))
        result = agg.aggregate("BTC")
        assert result.sentiment_label in ("strongly_bullish", "bullish")

    def test_sentiment_label_bearish(self) -> None:
        agg = SentimentAggregator()
        agg.add_signal(self._make_signal(score=-0.8, confidence=0.9))
        result = agg.aggregate("BTC")
        assert result.sentiment_label in ("strongly_bearish", "bearish")

    def test_is_extreme_property(self) -> None:
        agg = SentimentAggregator()
        agg.add_signal(self._make_signal(score=0.95, confidence=0.9))
        result = agg.aggregate("BTC")
        assert result.is_extreme is True

    def test_macro_bias_included(self) -> None:
        agg = SentimentAggregator()
        result = agg.aggregate("BTC", macro_bias=0.5)
        assert result.macro_bias == 0.5


# ---------------------------------------------------------------------------
# Macro Monitor Tests
# ---------------------------------------------------------------------------


class TestMacroMonitor:
    def test_get_macro_bias_empty(self) -> None:
        monitor = MacroEventMonitor()
        bias = monitor.get_macro_bias([])
        assert bias == 0.0

    def test_positive_surprise_positive_bias(self) -> None:
        """Dovish Überraschung → positive Bias für Crypto."""
        monitor = MacroEventMonitor()
        event = MacroEvent(
            event_type=MacroEventType.CPI_RELEASE,
            timestamp=datetime.now(tz=UTC),
            expected_value=3.5,
            actual_value=3.0,  # Niedriger als erwartet → gut für Crypto
            surprise_magnitude=-0.14,
            is_positive_surprise=True,
            impact_assessment=EventCategory.MACRO_POSITIVE,
            description="CPI: actual=3.0, expected=3.5",
        )
        bias = monitor.get_macro_bias([event])
        assert bias > 0

    def test_negative_surprise_negative_bias(self) -> None:
        monitor = MacroEventMonitor()
        event = MacroEvent(
            event_type=MacroEventType.CPI_RELEASE,
            timestamp=datetime.now(tz=UTC),
            expected_value=3.0,
            actual_value=3.8,  # Höher als erwartet → hawkish → bearish
            surprise_magnitude=0.27,
            is_positive_surprise=False,
            impact_assessment=EventCategory.MACRO_NEGATIVE,
            description="CPI: actual=3.8, expected=3.0",
        )
        bias = monitor.get_macro_bias([event])
        assert bias < 0

    def test_bias_bounded(self) -> None:
        monitor = MacroEventMonitor()
        events = [
            MacroEvent(
                event_type=MacroEventType.CPI_RELEASE,
                timestamp=datetime.now(tz=UTC),
                expected_value=1.0,
                actual_value=0.0,
                surprise_magnitude=-1.0,
                is_positive_surprise=True,
                impact_assessment=EventCategory.MACRO_POSITIVE,
                description="test",
            )
            for _ in range(10)
        ]
        bias = monitor.get_macro_bias(events)
        assert -1.0 <= bias <= 1.0

    def test_old_events_discounted(self) -> None:
        """Alte Events haben weniger Gewicht."""
        from datetime import timedelta

        monitor = MacroEventMonitor()
        recent = MacroEvent(
            event_type=MacroEventType.CPI_RELEASE,
            timestamp=datetime.now(tz=UTC),
            expected_value=3.0,
            actual_value=2.5,
            surprise_magnitude=-0.16,
            is_positive_surprise=True,
            impact_assessment=EventCategory.MACRO_POSITIVE,
            description="recent",
        )
        old = MacroEvent(
            event_type=MacroEventType.CPI_RELEASE,
            timestamp=datetime.now(tz=UTC) - timedelta(days=10),
            expected_value=3.0,
            actual_value=2.5,
            surprise_magnitude=-0.16,
            is_positive_surprise=True,
            impact_assessment=EventCategory.MACRO_POSITIVE,
            description="old",
        )
        bias_recent_only = monitor.get_macro_bias([recent])
        bias_with_old = monitor.get_macro_bias([recent, old])
        # Mit altem Event: immer noch positiv aber ähnlich (old hat wenig Gewicht)
        assert bias_recent_only > 0
        assert bias_with_old > 0


# ---------------------------------------------------------------------------
# SentimentAggregate Properties
# ---------------------------------------------------------------------------


class TestSentimentAggregateProperties:
    def _make_aggregate(self, s1h: float, s4h: float, s24h: float) -> SentimentAggregate:
        return SentimentAggregate(
            symbol="BTC",
            timestamp=datetime.now(tz=UTC),
            score_1h=s1h,
            score_4h=s4h,
            score_24h=s24h,
            confidence_1h=0.8,
            confidence_4h=0.7,
            confidence_24h=0.6,
            signals_1h=5,
            signals_4h=20,
            signals_24h=80,
        )

    def test_primary_score_weights(self) -> None:
        """Primary Score: 50% 1h + 30% 4h + 20% 24h."""
        agg = self._make_aggregate(1.0, 0.0, 0.0)
        assert agg.primary_score == pytest.approx(0.50, rel=0.01)

    def test_is_extreme_true(self) -> None:
        agg = self._make_aggregate(0.9, 0.8, 0.7)
        assert agg.is_extreme is True

    def test_is_extreme_false_for_neutral(self) -> None:
        agg = self._make_aggregate(0.1, -0.1, 0.0)
        assert agg.is_extreme is False

    def test_sentiment_labels(self) -> None:
        cases = [
            (0.8, "strongly_bullish"),
            (0.35, "bullish"),
            (0.05, "neutral"),
            (-0.35, "bearish"),
            (-0.8, "strongly_bearish"),
        ]
        for score, expected_label in cases:
            agg = self._make_aggregate(score, score, score)
            assert agg.sentiment_label == expected_label, (
                f"Score {score}: expected {expected_label}, got {agg.sentiment_label}"
            )
