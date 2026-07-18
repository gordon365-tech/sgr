"""
SGR NLP Sentiment Scorer
=========================
Bewertet Text-Sentiment via NLP-Modell.

Primär: FinBERT (Finance-spezifisches BERT)
    - Fine-tuned auf Financial News
    - Output: positive / negative / neutral mit Konfidenz
    - Besser als allgemeines Sentiment für Finance-Texte

Fallback 1: VADER (Rule-based)
    - Kein ML-Modell nötig
    - Schnell, interpretierbar
    - Schlechter bei Finance-spezifischem Kontext

Fallback 2: Keyword-basiert (letzter Ausweg)
    - Nur wenn nichts anderes verfügbar
    - NIEMALS für Live-Trading-Entscheidungen allein nutzen

Wichtig: Event Classification ≠ Keyword-Matching
    "Bitcoin ETF abgelehnt" → bearish (nicht "abgelehnt" = bearish)
    Context matters: "Fed Pause" kann bullish oder bearish sein
    je nach Erwartung. FinBERT versteht Kontext besser als Keywords.

Score-Mapping:
    FinBERT "positive" → +confidence
    FinBERT "negative" → -confidence
    FinBERT "neutral"  → 0
"""

from __future__ import annotations

import re
from typing import Any

from sgr.core.logging import get_logger
from sgr.sentiment.types import EventCategory

log = get_logger(__name__)

# Positive/Negative Keyword-Listen (nur für Fallback)
_BULLISH_KEYWORDS = frozenset(
    {
        "surge",
        "rally",
        "breakout",
        "adoption",
        "approval",
        "launch",
        "partnership",
        "upgrade",
        "bullish",
        "all-time high",
        "ath",
        "institutional",
        "etf approved",
        "buy",
        "growth",
        "positive",
    }
)
_BEARISH_KEYWORDS = frozenset(
    {
        "crash",
        "hack",
        "exploit",
        "ban",
        "collapse",
        "liquidation",
        "bear",
        "decline",
        "drop",
        "sell",
        "negative",
        "scam",
        "fraud",
        "regulatory",
        "investigation",
        "lawsuit",
        "vulnerability",
    }
)


class SentimentScorer:
    """
    NLP-basierter Sentiment Scorer.
    Verwendet FinBERT wenn verfügbar, sonst VADER, sonst Keyword-Fallback.
    """

    def __init__(self) -> None:
        self._model: Any = None
        self._tokenizer: Any = None
        self._vader: Any = None
        self._mode = "unavailable"
        self._initialize()

    def _initialize(self) -> None:
        """Initialisiert bestes verfügbares Modell."""
        # Versuch 1: FinBERT
        try:
            from transformers import pipeline

            self._pipeline = pipeline(
                "text-classification",
                model="ProsusAI/finbert",
                return_all_scores=True,
                truncation=True,
                max_length=512,
            )
            self._mode = "finbert"
            log.info("sentiment_scorer.initialized", mode="finbert")
            return
        except Exception as e:
            log.info("sentiment_scorer.finbert_unavailable", error=str(e))

        # Versuch 2: VADER
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

            self._vader = SentimentIntensityAnalyzer()
            self._mode = "vader"
            log.info("sentiment_scorer.initialized", mode="vader")
            return
        except ImportError:
            log.info("sentiment_scorer.vader_unavailable")

        # Fallback: Keyword-Matching
        self._mode = "keyword"
        log.warning(
            "sentiment_scorer.initialized",
            mode="keyword",
            note="Keyword-based fallback – not suitable for production",
        )

    def score(self, text: str) -> tuple[float, float]:
        """
        Bewertet Text und gibt (score, confidence) zurück.
        score: -1.0 (bearish) bis +1.0 (bullish)
        confidence: 0.0 bis 1.0

        Lange Texte werden auf 512 Tokens truncated.
        """
        if not text or len(text.strip()) < 5:
            return 0.0, 0.0

        # Sicherheits-Truncation (verhindert Out-of-Memory)
        text = text[:2000]

        try:
            if self._mode == "finbert":
                return self._score_finbert(text)
            elif self._mode == "vader":
                return self._score_vader(text)
            else:
                return self._score_keyword(text)
        except Exception as e:
            log.error("sentiment_scorer.score_error", error=str(e))
            return 0.0, 0.0

    def classify_event(self, text: str, score: float) -> EventCategory:
        """
        Klassifiziert Text als Event-Kategorie.
        Nutzt Score + strukturelle Hinweise im Text.
        Kein reines Keyword-Matching: Score liefert den Kontext.
        """
        text_lower = text.lower()

        # Crisis-Indikatoren: spezifische Muster
        crisis_patterns = [
            r"exploit\w*",
            r"hack\w*",
            r"rug.?pull",
            r"collapse",
            r"flash.?crash",
            r"emergency",
            r"systemic",
        ]
        if any(re.search(p, text_lower) for p in crisis_patterns):
            return EventCategory.CRISIS

        # Makro-Ereignisse
        macro_patterns = [
            r"cpi",
            r"inflation",
            r"fed\b",
            r"fomc",
            r"interest rate",
            r"gdp",
            r"employment",
            r"payroll",
        ]
        if any(re.search(p, text_lower) for p in macro_patterns):
            return EventCategory.MACRO_POSITIVE if score > 0 else EventCategory.MACRO_NEGATIVE

        # Regulatorisch
        regulatory_patterns = [r"sec\b", r"cftc", r"regulation", r"ban\w*", r"lawsuit", r"comply"]
        if any(re.search(p, text_lower) for p in regulatory_patterns):
            return EventCategory.REGULATORY

        # Score-basiert für allgemeine News
        if score > 0.3:
            return EventCategory.BULLISH
        elif score < -0.3:
            return EventCategory.BEARISH
        else:
            return EventCategory.NEUTRAL

    # ------------------------------------------------------------------
    # Scoring Backends
    # ------------------------------------------------------------------

    def _score_finbert(self, text: str) -> tuple[float, float]:
        """FinBERT scoring via Transformers pipeline."""
        results = self._pipeline(text)[0]
        scores_dict = {r["label"].lower(): r["score"] for r in results}

        positive = scores_dict.get("positive", 0.0)
        negative = scores_dict.get("negative", 0.0)
        neutral = scores_dict.get("neutral", 0.0)

        # Combined score: positive - negative, gewichtet nach Konfidenz
        net_score = positive - negative
        confidence = max(positive, negative, neutral)

        return float(net_score), float(confidence)

    def _score_vader(self, text: str) -> tuple[float, float]:
        """VADER compound score → SGR score."""
        scores = self._vader.polarity_scores(text)
        compound = float(scores["compound"])  # -1.0 bis +1.0
        confidence = abs(compound)
        return compound, min(confidence + 0.3, 1.0)  # VADER ist generisch: Confidence boost

    def _score_keyword(self, text: str) -> tuple[float, float]:
        """
        Einfaches Keyword-Matching als letzter Fallback.
        NICHT für echte Trading-Entscheidungen geeignet.
        """
        text_lower = text.lower()
        bullish_hits = sum(1 for kw in _BULLISH_KEYWORDS if kw in text_lower)
        bearish_hits = sum(1 for kw in _BEARISH_KEYWORDS if kw in text_lower)

        total = bullish_hits + bearish_hits
        if total == 0:
            return 0.0, 0.1  # Neutral, niedrige Konfidenz

        score = (bullish_hits - bearish_hits) / total
        confidence = min(total * 0.15, 0.5)  # Max 50% Konfidenz für Keyword-Matching

        return float(score), float(confidence)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def is_production_ready(self) -> bool:
        """Nur FinBERT oder VADER sind für Live-Trading geeignet."""
        return self._mode in ("finbert", "vader")
