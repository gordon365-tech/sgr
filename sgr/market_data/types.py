"""
SGR Market Data Types
=====================
Ergänzende Domain-Types für die Market Data Engine.
Nicht in core/types.py, da diese nur vom Market Data Modul genutzt werden.

Konzept Feature Store:
    FeatureSet = alle berechneten Features für ein Symbol zum Zeitpunkt T.
    Wird im Redis Feature Store gecacht und vom Strategy Engine konsumiert.
    Immutable: einmal berechnet, nie mutiert (neuer Zeitstempel = neues FeatureSet).

Konzept MarketContext:
    Aggregierter Kontext über alle Features + Regime + Sentiment.
    Ist das, was eine Strategie als Input bekommt.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from sgr.core.types import MarketRegime, Symbol


class IndicatorValues(BaseModel):
    """
    Technische Indikatoren als Features (nicht isoliert tradeable).
    Alle Werte normalized wo sinnvoll (z.B. RSI: 0-100).
    None = nicht genug Daten für Berechnung (z.B. zu wenig History).
    """

    model_config = {"frozen": True}

    # Momentum
    rsi_14: float | None = None  # 0-100
    rsi_7: float | None = None  # 0-100 (short-term)
    macd_line: float | None = None  # MACD - Signal
    macd_signal: float | None = None
    macd_histogram: float | None = None

    # Trend
    adx_14: float | None = None  # 0-100, > 25 = trending
    di_plus: float | None = None
    di_minus: float | None = None

    # Volatility
    atr_14: Decimal | None = None  # Average True Range (price units)
    atr_pct: float | None = None  # ATR as % of price
    bb_upper: Decimal | None = None  # Bollinger Upper
    bb_middle: Decimal | None = None  # Bollinger Middle (SMA20)
    bb_lower: Decimal | None = None  # Bollinger Lower
    bb_width: float | None = None  # (Upper-Lower)/Middle, normalized
    bb_position: float | None = None  # Where price is in BB: 0=lower, 1=upper
    kc_upper: Decimal | None = None  # Keltner Channel Upper
    kc_lower: Decimal | None = None  # Keltner Channel Lower

    # Moving Averages
    ema_9: Decimal | None = None
    ema_21: Decimal | None = None
    ema_50: Decimal | None = None
    ema_200: Decimal | None = None
    sma_20: Decimal | None = None

    # Volume
    vwap: Decimal | None = None  # Volume Weighted Average Price
    volume_sma_20: Decimal | None = None  # 20-period volume SMA
    volume_ratio: float | None = None  # Current vol / SMA vol (>1 = above avg)
    obv: float | None = None  # On-Balance Volume (normalized delta)

    # Ichimoku
    tenkan_sen: Decimal | None = None
    kijun_sen: Decimal | None = None
    senkou_a: Decimal | None = None
    senkou_b: Decimal | None = None
    chikou_span: Decimal | None = None


class OrderBookFeatures(BaseModel):
    """
    Aus dem Orderbook abgeleitete Features.
    Orderbook-Imbalance ist ein starkes Signal für kurzfristige Richtung.
    """

    model_config = {"frozen": True}

    bid_ask_spread: Decimal
    bid_ask_spread_pct: float
    mid_price: Decimal

    # Imbalance: (bid_volume - ask_volume) / (bid_volume + ask_volume)
    # > 0 = mehr Bid-Druck (bullish), < 0 = mehr Ask-Druck (bearish)
    order_imbalance_5: float  # top 5 levels
    order_imbalance_10: float  # top 10 levels
    order_imbalance_20: float  # top 20 levels

    # Depth
    bid_depth_usdt: Decimal  # Total USDT value in top 20 bids
    ask_depth_usdt: Decimal  # Total USDT value in top 20 asks

    # Large orders (potential walls)
    largest_bid_level: Decimal
    largest_ask_level: Decimal


class FuturesFeatures(BaseModel):
    """
    Futures-spezifische Features.
    Funding Rate + Open Interest sind wichtige Sentiment-Indikatoren.
    """

    model_config = {"frozen": True}

    funding_rate: Decimal
    funding_rate_annualized: float  # rate * 3 * 365 * 100 (als %)
    is_contango: bool  # funding_rate > 0 = longs pay shorts

    open_interest: Decimal
    open_interest_usd: Decimal
    open_interest_change_1h: float | None = None  # % Änderung letzte Stunde
    open_interest_change_24h: float | None = None  # % Änderung letzte 24h


class FeatureSet(BaseModel):
    """
    Vollständiger Feature-Vektor für ein Symbol zu einem Zeitpunkt.
    Wird im Feature Store (Redis) gecacht.
    Strategy Engine konsumiert FeatureSet – nie rohe Candles direkt.

    Unveränderlich nach Erstellung: timestamp + symbol = eindeutiger Key.
    """

    model_config = {"frozen": True}

    symbol: Symbol
    timestamp: datetime
    timeframe: str

    # Aktueller Preis
    close: Decimal
    volume: Decimal

    # Feature Gruppen (optional – nicht jeder Exchange liefert alles)
    indicators: IndicatorValues = Field(default_factory=IndicatorValues)
    orderbook: OrderBookFeatures | None = None
    futures: FuturesFeatures | None = None

    # Preisbewegung (lookback)
    returns_1: float | None = None  # 1-Candle Return
    returns_5: float | None = None  # 5-Candle Return
    returns_10: float | None = None  # 10-Candle Return
    returns_20: float | None = None  # 20-Candle Return

    # Marktregime (von ML Regime Detector befüllt, initial UNKNOWN)
    regime: MarketRegime = MarketRegime.UNKNOWN

    @property
    def cache_key(self) -> str:
        """Redis cache key für dieses FeatureSet."""
        ts = int(self.timestamp.timestamp())
        return (
            f"features:{self.symbol.exchange.value}:{self.symbol.ccxt_symbol}:{self.timeframe}:{ts}"
        )

    @property
    def latest_key(self) -> str:
        """Redis key für das neueste FeatureSet (überschreibt bei Update)."""
        return (
            f"features:latest:{self.symbol.exchange.value}:"
            f"{self.symbol.ccxt_symbol}:{self.timeframe}"
        )


class MarketContext(BaseModel):
    """
    Aggregierter Marktkontext für Strategien.
    Kombiniert Features über mehrere Timeframes + Sentiment + Regime.
    Das ist der vollständige Input für eine Strategie.
    """

    model_config = {"frozen": True}

    symbol: Symbol
    timestamp: datetime

    # Primary timeframe features (z.B. 1h)
    primary: FeatureSet

    # Higher timeframe features (z.B. 4h, 1d) für Kontext
    htf_4h: FeatureSet | None = None
    htf_1d: FeatureSet | None = None

    # Regime (unified across timeframes)
    regime: MarketRegime = MarketRegime.UNKNOWN

    # Sentiment Score (-1.0 bis +1.0)
    sentiment_score: float | None = None
    sentiment_confidence: float | None = None

    # Convenience properties
    @property
    def current_price(self) -> Decimal:
        return self.primary.close

    @property
    def atr(self) -> Decimal | None:
        return self.primary.indicators.atr_14

    @property
    def is_trending(self) -> bool:
        adx = self.primary.indicators.adx_14
        return adx is not None and adx > 25

    @property
    def is_high_volatility(self) -> bool:
        return self.regime in (MarketRegime.HIGH_VOLATILITY, MarketRegime.CRISIS)


class DataGap(BaseModel):
    """Fehlende Daten in einer OHLCV-Serie."""

    symbol: Symbol
    timeframe: str
    gap_start: datetime
    gap_end: datetime
    missing_candles: int
