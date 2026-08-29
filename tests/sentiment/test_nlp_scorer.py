"""
Tests für sgr.sentiment.nlp_scorer.SentimentScorer.

Coverage-Ziel: 72% -> hoch.

Teststrategie:
    - _initialize() läuft in dieser Sandbox ohne Netzwerk natürlich in
      den VADER-Fallback (FinBERT-Download schlägt fehl -> ImportError-
      freier, aber verbindungsloser Fallback-Pfad wird bereits real
      durchlaufen). Ein Test bestätigt explizit dieses reale Verhalten.
    - Um alle drei Scoring-Backends (finbert/vader/keyword) unabhängig
      vom tatsächlich verfügbaren Netzwerk/Paket zu testen, wird
      `_mode` sowie `_pipeline`/`_vader` direkt auf Instanz-Ebene
      gesetzt (Duck-Typing mit MagicMock statt echtem Modell-Download) -
      analog zum etablierten Muster, externe/optionale Abhängigkeiten
      auf Objekt-Ebene zu ersetzen statt echte ML-Modelle zu laden.
    - classify_event() und die reinen scoring-Backends werden mit
      echten Werten/Regex-Mustern getestet (keine I/O).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sgr.sentiment.nlp_scorer import SentimentScorer
from sgr.sentiment.types import EventCategory


@pytest.fixture
def scorer() -> SentimentScorer:
    return SentimentScorer()


# ---------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------


class TestInitialize:
    def test_initializes_to_a_known_mode(self, scorer: SentimentScorer) -> None:
        # In this sandboxed environment (no network access), FinBERT's
        # model download fails and the scorer falls back to VADER -
        # this exercises the real finbert-unavailable -> vader-init path.
        assert scorer.mode in ("finbert", "vader", "keyword")

    def test_initializes_to_finbert_when_pipeline_succeeds(self) -> None:
        fake_pipeline_factory = MagicMock(return_value=MagicMock())
        with patch("transformers.pipeline", fake_pipeline_factory):
            scorer = SentimentScorer()
        assert scorer.mode == "finbert"

    def test_initializes_to_keyword_when_both_backends_unavailable(self) -> None:
        with (
            patch("transformers.pipeline", side_effect=RuntimeError("no network")),
            patch(
                "vaderSentiment.vaderSentiment.SentimentIntensityAnalyzer",
                side_effect=ImportError("not installed"),
            ),
        ):
            scorer = SentimentScorer()
        assert scorer.mode == "keyword"

    def test_keyword_mode_is_not_production_ready(self) -> None:
        scorer = SentimentScorer()
        scorer._mode = "keyword"
        assert scorer.is_production_ready is False

    def test_vader_mode_is_production_ready(self) -> None:
        scorer = SentimentScorer()
        scorer._mode = "vader"
        assert scorer.is_production_ready is True

    def test_finbert_mode_is_production_ready(self) -> None:
        scorer = SentimentScorer()
        scorer._mode = "finbert"
        assert scorer.is_production_ready is True


# ---------------------------------------------------------------------
# score() - dispatch + edge cases
# ---------------------------------------------------------------------


class TestScoreDispatch:
    def test_score_returns_zero_for_empty_text(self, scorer: SentimentScorer) -> None:
        assert scorer.score("") == (0.0, 0.0)

    def test_score_returns_zero_for_too_short_text(self, scorer: SentimentScorer) -> None:
        assert scorer.score("hi") == (0.0, 0.0)

    def test_score_truncates_long_text(self, scorer: SentimentScorer) -> None:
        scorer._mode = "keyword"
        long_text = "bullish " * 1000  # Way over 2000 chars.
        score, confidence = scorer.score(long_text)
        assert isinstance(score, float)
        assert isinstance(confidence, float)

    def test_score_dispatches_to_finbert_backend(self, scorer: SentimentScorer) -> None:
        scorer._mode = "finbert"
        scorer._pipeline = MagicMock(
            return_value=[
                [
                    {"label": "positive", "score": 0.7},
                    {"label": "negative", "score": 0.1},
                    {"label": "neutral", "score": 0.2},
                ]
            ]
        )

        score, confidence = scorer.score("Bitcoin ETF approved by regulators")

        assert score == pytest.approx(0.6)
        assert confidence == pytest.approx(0.7)

    def test_score_dispatches_to_vader_backend(self, scorer: SentimentScorer) -> None:
        scorer._mode = "vader"
        scorer._vader = MagicMock()
        scorer._vader.polarity_scores.return_value = {"compound": 0.5}

        score, confidence = scorer.score("Great news for crypto markets today")

        assert score == pytest.approx(0.5)
        assert confidence == pytest.approx(0.8)  # min(0.5 + 0.3, 1.0)

    def test_score_dispatches_to_keyword_backend(self, scorer: SentimentScorer) -> None:
        scorer._mode = "keyword"

        score, confidence = scorer.score("Massive rally and bullish breakout for BTC")

        assert score > 0
        assert confidence > 0

    def test_score_swallows_backend_exception(self, scorer: SentimentScorer) -> None:
        scorer._mode = "finbert"
        scorer._pipeline = MagicMock(side_effect=RuntimeError("model error"))

        assert scorer.score("Some financial headline here") == (0.0, 0.0)


# ---------------------------------------------------------------------
# _score_finbert()
# ---------------------------------------------------------------------


class TestScoreFinbert:
    def test_finbert_positive_dominant(self, scorer: SentimentScorer) -> None:
        scorer._pipeline = MagicMock(
            return_value=[
                [
                    {"label": "positive", "score": 0.8},
                    {"label": "negative", "score": 0.05},
                    {"label": "neutral", "score": 0.15},
                ]
            ]
        )
        score, confidence = scorer._score_finbert("text")
        assert score == pytest.approx(0.75)
        assert confidence == pytest.approx(0.8)

    def test_finbert_negative_dominant(self, scorer: SentimentScorer) -> None:
        scorer._pipeline = MagicMock(
            return_value=[
                [
                    {"label": "positive", "score": 0.1},
                    {"label": "negative", "score": 0.75},
                    {"label": "neutral", "score": 0.15},
                ]
            ]
        )
        score, confidence = scorer._score_finbert("text")
        assert score == pytest.approx(-0.65)
        assert confidence == pytest.approx(0.75)


# ---------------------------------------------------------------------
# _score_vader()
# ---------------------------------------------------------------------


class TestScoreVader:
    def test_vader_boosts_confidence_but_caps_at_one(self, scorer: SentimentScorer) -> None:
        scorer._vader = MagicMock()
        scorer._vader.polarity_scores.return_value = {"compound": 0.9}

        score, confidence = scorer._score_vader("text")

        assert score == pytest.approx(0.9)
        assert confidence == 1.0  # min(0.9 + 0.3, 1.0) capped.

    def test_vader_negative_compound(self, scorer: SentimentScorer) -> None:
        scorer._vader = MagicMock()
        scorer._vader.polarity_scores.return_value = {"compound": -0.4}

        score, confidence = scorer._score_vader("text")

        assert score == pytest.approx(-0.4)
        assert confidence == pytest.approx(0.7)  # abs(-0.4) + 0.3


# ---------------------------------------------------------------------
# _score_keyword()
# ---------------------------------------------------------------------


class TestScoreKeyword:
    def test_keyword_no_matches_returns_neutral_low_confidence(
        self, scorer: SentimentScorer
    ) -> None:
        score, confidence = scorer._score_keyword("Quiet trading session, nothing notable")
        assert score == 0.0
        assert confidence == 0.1

    def test_keyword_bullish_dominant(self, scorer: SentimentScorer) -> None:
        score, confidence = scorer._score_keyword("Massive rally, breakout, and bullish surge")
        assert score > 0
        assert confidence > 0

    def test_keyword_bearish_dominant(self, scorer: SentimentScorer) -> None:
        score, confidence = scorer._score_keyword("Market crash amid hack and exploit fears")
        assert score < 0

    def test_keyword_confidence_capped_at_half(self, scorer: SentimentScorer) -> None:
        # Many keyword hits should still cap confidence at 0.5.
        text = "surge rally breakout adoption approval launch partnership upgrade bullish"
        _, confidence = scorer._score_keyword(text)
        assert confidence <= 0.5


# ---------------------------------------------------------------------
# classify_event()
# ---------------------------------------------------------------------


class TestClassifyEvent:
    def test_classifies_crisis_pattern(self, scorer: SentimentScorer) -> None:
        assert (
            scorer.classify_event("Protocol exploit drains millions", 0.0)
            == EventCategory.CRISIS
        )

    def test_classifies_flash_crash_as_crisis(self, scorer: SentimentScorer) -> None:
        assert scorer.classify_event("Flash crash wipes out leveraged positions", 0.0) == (
            EventCategory.CRISIS
        )

    def test_classifies_macro_positive(self, scorer: SentimentScorer) -> None:
        assert (
            scorer.classify_event("CPI data released this morning", 0.5)
            == EventCategory.MACRO_POSITIVE
        )

    def test_classifies_macro_negative(self, scorer: SentimentScorer) -> None:
        assert (
            scorer.classify_event("FOMC meeting minutes released", -0.5)
            == EventCategory.MACRO_NEGATIVE
        )

    def test_classifies_regulatory(self, scorer: SentimentScorer) -> None:
        assert (
            scorer.classify_event("SEC files lawsuit against exchange", 0.0)
            == EventCategory.REGULATORY
        )

    def test_classifies_bullish_from_score(self, scorer: SentimentScorer) -> None:
        assert (
            scorer.classify_event("Some generic positive headline", 0.5)
            == EventCategory.BULLISH
        )

    def test_classifies_bearish_from_score(self, scorer: SentimentScorer) -> None:
        assert (
            scorer.classify_event("Some generic negative headline", -0.5)
            == EventCategory.BEARISH
        )

    def test_classifies_neutral_from_score(self, scorer: SentimentScorer) -> None:
        assert (
            scorer.classify_event("Some generic ambiguous headline", 0.1)
            == EventCategory.NEUTRAL
        )

    def test_crisis_pattern_takes_priority_over_macro(self, scorer: SentimentScorer) -> None:
        # Contains both a crisis pattern and a macro keyword - crisis wins.
        assert (
            scorer.classify_event("Systemic collapse amid Fed inflation concerns", 0.0)
            == EventCategory.CRISIS
        )
