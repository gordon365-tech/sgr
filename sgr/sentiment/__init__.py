"""SGR Sentiment Engine"""

from sgr.sentiment.engine import SentimentAggregator, SentimentEngine
from sgr.sentiment.macro_monitor import MacroEventMonitor
from sgr.sentiment.news_fetcher import NewsFetcher
from sgr.sentiment.nlp_scorer import SentimentScorer
from sgr.sentiment.types import (
    EventCategory,
    MacroEvent,
    MacroEventType,
    SentimentAggregate,
    SentimentSignal,
    SentimentSource,
)

__all__ = [
    "SentimentEngine",
    "SentimentAggregator",
    "SentimentScorer",
    "NewsFetcher",
    "MacroEventMonitor",
    "SentimentSignal",
    "SentimentAggregate",
    "MacroEvent",
    "SentimentSource",
    "EventCategory",
    "MacroEventType",
]
