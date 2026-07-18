"""
SGR Market Data Module
"""

from sgr.market_data.engine import MarketDataEngine
from sgr.market_data.feature_engineering import FeatureEngineer, calc_orderbook_features
from sgr.market_data.feature_store import FeatureStore, get_feature_store
from sgr.market_data.gap_detector import GapDetector
from sgr.market_data.types import (
    DataGap,
    FeatureSet,
    FuturesFeatures,
    IndicatorValues,
    MarketContext,
    OrderBookFeatures,
)

__all__ = [
    "MarketDataEngine",
    "FeatureEngineer",
    "FeatureStore",
    "get_feature_store",
    "GapDetector",
    "calc_orderbook_features",
    "FeatureSet",
    "MarketContext",
    "IndicatorValues",
    "OrderBookFeatures",
    "FuturesFeatures",
    "DataGap",
]
