"""
SGR Sentiment Engine – Domain Types
=====================================
Alle Types für Sentiment-Analyse und Makro-Event-Klassifikation.

Architektur (kein Keyword-Matching – echte NLP):
    News/Social → Entity Extraction → Event Classification → Sentiment Score

Event-Kategorien (nicht Keyword-basiert):
    BULLISH:    Fed Pivot, ETF Approval, Institutional Adoption
    BEARISH:    Exchange Hack, Regulatory Ban, Liquidity Crisis
    NEUTRAL:    Routine Earnings, Data Release ohne Surprise
    CRISIS:     Flash Crash, Exchange Collapse, Protocol Exploit
    MACRO:      CPI, FOMC, Employment Data

Sentiment-Score:
    -1.0 (stark bearish) bis +1.0 (stark bullish)
    Aggregiert über mehrere Quellen und Zeitfenster (1h, 4h, 24h).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class SentimentSource(StrEnum):
    NEWS = "news"
    TWITTER = "twitter"
    REDDIT = "reddit"
    TELEGRAM = "telegram"
    MACRO = "macro"
    ON_CHAIN = "on_chain"


class EventCategory(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    CRISIS = "crisis"
    MACRO_POSITIVE = "macro_positive"
    MACRO_NEGATIVE = "macro_negative"
    REGULATORY = "regulatory"
    TECHNICAL = "technical"


class MacroEventType(StrEnum):
    CPI_RELEASE = "cpi"
    FOMC_MEETING = "fomc"
    EMPLOYMENT = "employment"
    GDP = "gdp"
    FED_SPEECH = "fed_speech"
    ETF_DECISION = "etf_decision"
    REGULATORY_ACTION = "regulatory_action"
    EXCHANGE_EVENT = "exchange_event"
    PROTOCOL_EVENT = "protocol_event"
    OTHER = "other"


@dataclass(frozen=True)
class SentimentSignal:
    """
    Ein einzelnes Sentiment-Signal aus einer Quelle.
    """

    source: SentimentSource
    timestamp: datetime
    raw_score: float  # -1.0 bis +1.0 (Rohwert aus NLP-Modell)
    confidence: float  # 0.0 bis 1.0
    event_category: EventCategory
    entity: str  # z.B. "BTC", "ETH", "Bitcoin", "Binance"
    headline: str  # Original-Headline (nie im Log landen!)
    url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MacroEvent:
    """
    Makroökonomisches Ereignis mit Markterwartung vs. Actual.
    CPI: erwartet 3.2%, actual 3.5% → hawkish surprise → bearish Crypto
    """

    event_type: MacroEventType
    timestamp: datetime
    expected_value: float | None
    actual_value: float | None
    surprise_magnitude: float  # (actual - expected) normalisiert
    is_positive_surprise: bool  # Für Crypto: Fed Pause = positiv
    impact_assessment: EventCategory
    description: str


@dataclass
class SentimentAggregate:
    """
    Aggregierter Sentiment-Score über mehrere Quellen und Zeitfenster.
    Das ist der Output den Strategy Engine und ML Engine konsumieren.
    """

    symbol: str  # "BTC", "ETH" oder "market" für gesamt
    timestamp: datetime

    # Aggregierte Scores per Zeitfenster
    score_1h: float  # -1.0 bis +1.0
    score_4h: float
    score_24h: float

    # Konfidenz
    confidence_1h: float  # 0.0 bis 1.0
    confidence_4h: float
    confidence_24h: float

    # Signal-Counts per Zeitfenster (Gewichtung)
    signals_1h: int
    signals_4h: int
    signals_24h: int

    # Source-Breakdown
    source_scores: dict[str, float] = field(default_factory=dict)  # source → score

    # Aktuelles Makro-Umfeld
    active_macro_events: list[MacroEvent] = field(default_factory=list)
    macro_bias: float = 0.0  # Makro-Einfluss auf Score

    @property
    def primary_score(self) -> float:
        """Gewichteter Haupt-Score (24h mit Decay-Gewichtung)."""
        w1h, w4h, w24h = 0.50, 0.30, 0.20
        return self.score_1h * w1h + self.score_4h * w4h + self.score_24h * w24h

    @property
    def primary_confidence(self) -> float:
        return max(self.confidence_1h, self.confidence_4h)

    @property
    def is_extreme(self) -> bool:
        """Score > 0.7 oder < -0.7 = extremes Sentiment."""
        return abs(self.primary_score) > 0.70

    @property
    def sentiment_label(self) -> str:
        score = self.primary_score
        if score > 0.5:
            return "strongly_bullish"
        elif score > 0.2:
            return "bullish"
        elif score > -0.2:
            return "neutral"
        elif score > -0.5:
            return "bearish"
        else:
            return "strongly_bearish"
