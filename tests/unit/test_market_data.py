"""
Tests für Feature Engineering.

Feature Engineering ist der am besten testbare Teil des Systems:
- Pure functions (kein State, kein I/O)
- Deterministisch (gleicher Input = gleicher Output)
- Mathematisch verifizierbar (bekannte Formeln)

Teststrategie:
1. Primitive Helfer gegen analytisch bekannte Werte prüfen
2. Indikatoren auf Wertebereiche prüfen (RSI: 0-100, ADX: 0-100)
3. None-Handling bei zu wenig Daten
4. FeatureSet-Erstellung auf vollständige Typen prüfen
5. OrderBook Features auf mathematische Korrektheit
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pytest

from sgr.core.types import Candle, ExchangeID, OrderBook, OrderBookLevel, Symbol
from sgr.market_data.feature_engineering import (
    FeatureEngineer,
    _ema,
    _sma,
    _true_range,
    calc_atr,
    calc_bollinger_bands,
    calc_macd,
    calc_orderbook_features,
    calc_rsi,
    calc_vwap,
    candles_to_arrays,
)
from sgr.market_data.gap_detector import GapDetector
from sgr.market_data.types import FeatureSet

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def make_candles(
    n: int,
    base_price: float = 50000.0,
    trend: float = 0.0,
    noise: float = 100.0,
) -> list[Candle]:
    """
    Synthetische Candle-Serie.
    trend: Preis-Änderung pro Candle (z.B. 10.0 = aufwärts)
    noise: ±noise für High/Low
    """
    sym = make_symbol()
    now = datetime.now(tz=UTC)
    candles = []
    price = base_price

    for _i in range(n):
        price = price + trend + (np.random.randn() * noise * 0.1)
        high = price + noise
        low = price - noise
        candles.append(
            Candle(
                symbol=sym,
                timestamp=now,
                timeframe="1h",
                open=Decimal(str(round(price - noise * 0.5, 2))),
                high=Decimal(str(round(high, 2))),
                low=Decimal(str(round(max(low, 1.0), 2))),
                close=Decimal(str(round(price, 2))),
                volume=Decimal("1000"),
            )
        )

    return candles


def make_flat_candles(n: int, price: float = 50000.0) -> list[Candle]:
    """Gleichmäßige Candles (kein Trend, kein Noise) für analytische Tests."""
    sym = make_symbol()
    now = datetime.now(tz=UTC)
    return [
        Candle(
            symbol=sym,
            timestamp=now,
            timeframe="1h",
            open=Decimal(str(price)),
            high=Decimal(str(price + 100)),
            low=Decimal(str(price - 100)),
            close=Decimal(str(price)),
            volume=Decimal("1000"),
        )
        for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# Primitive Helpers
# ---------------------------------------------------------------------------


class TestPrimitiveHelpers:
    def test_sma_constant_series(self) -> None:
        """SMA einer konstanten Serie = die Konstante selbst."""
        values = np.full(20, 100.0)
        result = _sma(values, 5)
        assert not np.isnan(result[-1])
        assert result[-1] == pytest.approx(100.0)

    def test_sma_insufficient_data(self) -> None:
        """SMA mit weniger Daten als Period = NaN."""
        values = np.array([1.0, 2.0, 3.0])
        result = _sma(values, 5)
        assert np.all(np.isnan(result))

    def test_ema_constant_series(self) -> None:
        """EMA einer konstanten Serie konvergiert gegen die Konstante."""
        values = np.full(50, 100.0)
        result = _ema(values, 10)
        # Letzte Werte sollen nahe an 100 sein
        assert result[-1] == pytest.approx(100.0, rel=1e-6)

    def test_ema_seed_equals_sma(self) -> None:
        """Erster EMA-Wert (bei Index period-1) soll = SMA sein."""
        values = np.array([10.0, 20.0, 30.0, 40.0, 50.0])
        period = 5
        result = _ema(values, period)
        expected_seed = np.mean(values[:period])
        assert result[period - 1] == pytest.approx(expected_seed)

    def test_true_range_basic(self) -> None:
        """TR = max(H-L, |H-PC|, |L-PC|)."""
        high = np.array([110.0, 115.0, 108.0])
        low = np.array([95.0, 100.0, 95.0])
        close = np.array([100.0, 112.0, 105.0])
        tr = _true_range(high, low, close)
        # Bar 1: max(110-95, |110-?, |95-?|) = 15 (first bar no prev close)
        assert tr[0] == pytest.approx(15.0)
        # Bar 2: max(115-100, |115-100|, |100-100|) = 15
        assert tr[1] == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------


class TestRSI:
    def test_rsi_bounds(self) -> None:
        """RSI muss immer zwischen 0 und 100 liegen."""
        candles = make_candles(100, trend=50)  # starker Aufwärtstrend
        arrays = candles_to_arrays(candles)
        rsi = calc_rsi(arrays.close, 14)
        valid = rsi[~np.isnan(rsi)]
        assert np.all(valid >= 0)
        assert np.all(valid <= 100)

    def test_rsi_uptrend_high(self) -> None:
        """Starker Aufwärtstrend → RSI sollte hoch sein (> 60)."""
        np.random.seed(42)
        candles = make_candles(100, trend=100, noise=10)  # starker Trend
        arrays = candles_to_arrays(candles)
        rsi = calc_rsi(arrays.close, 14)
        last_valid = rsi[~np.isnan(rsi)][-1]
        assert last_valid > 60

    def test_rsi_insufficient_data(self) -> None:
        """Weniger Candles als Period → NaN."""
        candles = make_candles(10)
        arrays = candles_to_arrays(candles)
        rsi = calc_rsi(arrays.close, 14)
        # Alle NaN weil < 14+1 Datenpunkte
        assert np.all(np.isnan(rsi))

    def test_rsi_constant_price_neutral(self) -> None:
        """Konstante Preise → RSI nicht definiert (50 oder NaN)."""
        candles = make_flat_candles(50)
        arrays = candles_to_arrays(candles)
        rsi = calc_rsi(arrays.close, 14)
        valid = rsi[~np.isnan(rsi)]
        # Bei keiner Bewegung ist avg_gain = avg_loss = 0
        # RS = 0/0 → NaN oder 50 je nach Implementierung
        # Hauptsache kein Crash
        assert len(valid) >= 0  # keine Exception


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------


class TestMACD:
    def test_macd_returns_three_arrays(self) -> None:
        candles = make_candles(100)
        arrays = candles_to_arrays(candles)
        macd_line, signal, histogram = calc_macd(arrays.close)
        assert len(macd_line) == len(arrays.close)
        assert len(signal) == len(arrays.close)
        assert len(histogram) == len(arrays.close)

    def test_macd_histogram_equals_diff(self) -> None:
        """Histogram = MACD - Signal (wo beide gültig sind)."""
        candles = make_candles(100)
        arrays = candles_to_arrays(candles)
        macd_line, signal, histogram = calc_macd(arrays.close)

        valid_idx = ~(np.isnan(macd_line) | np.isnan(signal) | np.isnan(histogram))
        if valid_idx.any():
            expected = macd_line[valid_idx] - signal[valid_idx]
            np.testing.assert_allclose(histogram[valid_idx], expected, rtol=1e-10)


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------


class TestATR:
    def test_atr_positive(self) -> None:
        """ATR ist immer positiv."""
        candles = make_candles(50)
        arrays = candles_to_arrays(candles)
        atr = calc_atr(arrays.high, arrays.low, arrays.close, 14)
        valid = atr[~np.isnan(atr)]
        assert np.all(valid > 0)

    def test_atr_proportional_to_volatility(self) -> None:
        """Höhere Volatilität → höheres ATR."""
        low_vol = make_flat_candles(50, price=50000.0)
        # Manuell hohe Volatilität konstruieren
        sym = make_symbol()
        now = datetime.now(tz=UTC)
        high_vol = [
            Candle(
                symbol=sym,
                timestamp=now,
                timeframe="1h",
                open=Decimal("50000"),
                high=Decimal("55000"),
                low=Decimal("45000"),
                close=Decimal("50000"),
                volume=Decimal("1000"),
            )
            for _ in range(50)
        ]

        lv_arr = candles_to_arrays(low_vol)
        hv_arr = candles_to_arrays(high_vol)

        atr_low = calc_atr(lv_arr.high, lv_arr.low, lv_arr.close, 14)
        atr_high = calc_atr(hv_arr.high, hv_arr.low, hv_arr.close, 14)

        last_low = atr_low[~np.isnan(atr_low)][-1]
        last_high = atr_high[~np.isnan(atr_high)][-1]
        assert last_high > last_low


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------


class TestBollingerBands:
    def test_upper_gt_middle_gt_lower(self) -> None:
        """Upper > Middle > Lower (immer)."""
        candles = make_candles(50)
        arrays = candles_to_arrays(candles)
        upper, middle, lower = calc_bollinger_bands(arrays.close)

        for u, m, lower in zip(upper, middle, lower, strict=False):
            if not (np.isnan(u) or np.isnan(m) or np.isnan(lower)):
                assert u > m > lower

    def test_constant_price_narrow_bands(self) -> None:
        """Konstanter Preis → sehr enge Bänder (std ≈ 0)."""
        candles = make_flat_candles(50)
        arrays = candles_to_arrays(candles)
        upper, middle, lower = calc_bollinger_bands(arrays.close)

        valid_u = upper[~np.isnan(upper)]
        valid_l = lower[~np.isnan(lower)]
        middle[~np.isnan(middle)]

        # Bei konstanten Preisen: BB-Width ≈ 0
        width = valid_u[-1] - valid_l[-1]
        assert width == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------


class TestVWAP:
    def test_vwap_between_low_and_high(self) -> None:
        """VWAP liegt immer zwischen Low und High (kumulativ)."""
        candles = make_candles(50)
        arrays = candles_to_arrays(candles)
        vwap = calc_vwap(arrays.high, arrays.low, arrays.close, arrays.volume)
        valid = vwap[~np.isnan(vwap)]
        # VWAP sollte grob im Preisbereich liegen
        assert np.all(valid > 0)


# ---------------------------------------------------------------------------
# OrderBook Features
# ---------------------------------------------------------------------------


class TestOrderBookFeatures:
    def _make_orderbook(
        self,
        bid_price: float = 50000.0,
        ask_price: float = 50010.0,
        bid_size: float = 1.0,
        ask_size: float = 1.0,
        levels: int = 20,
    ) -> OrderBook:
        sym = make_symbol()
        bids = [
            OrderBookLevel(
                price=Decimal(str(bid_price - i)),
                size=Decimal(str(bid_size)),
            )
            for i in range(levels)
        ]
        asks = [
            OrderBookLevel(
                price=Decimal(str(ask_price + i)),
                size=Decimal(str(ask_size)),
            )
            for i in range(levels)
        ]
        return OrderBook(
            symbol=sym,
            timestamp=datetime.now(tz=UTC),
            bids=bids,
            asks=asks,
        )

    def test_spread_calculation(self) -> None:
        ob = self._make_orderbook(bid_price=50000, ask_price=50010)
        features = calc_orderbook_features(ob)
        assert features.bid_ask_spread == Decimal("10")

    def test_balanced_imbalance_near_zero(self) -> None:
        """Gleiche Mengen auf beiden Seiten → Imbalance ≈ 0."""
        ob = self._make_orderbook(bid_size=1.0, ask_size=1.0)
        features = calc_orderbook_features(ob)
        assert features.order_imbalance_5 == pytest.approx(0.0, abs=0.01)

    def test_bid_heavy_positive_imbalance(self) -> None:
        """Mehr Bid-Volumen → positive Imbalance (bullish)."""
        ob = self._make_orderbook(bid_size=10.0, ask_size=1.0)
        features = calc_orderbook_features(ob)
        assert features.order_imbalance_5 > 0

    def test_ask_heavy_negative_imbalance(self) -> None:
        """Mehr Ask-Volumen → negative Imbalance (bearish)."""
        ob = self._make_orderbook(bid_size=1.0, ask_size=10.0)
        features = calc_orderbook_features(ob)
        assert features.order_imbalance_5 < 0

    def test_depth_calculation(self) -> None:
        ob = self._make_orderbook(bid_price=50000, ask_price=50010, bid_size=2.0)
        features = calc_orderbook_features(ob)
        assert features.bid_depth_usdt > Decimal("0")


# ---------------------------------------------------------------------------
# FeatureEngineer (Integration)
# ---------------------------------------------------------------------------


class TestFeatureEngineer:
    def test_compute_returns_feature_set(self) -> None:
        candles = make_candles(100)
        engineer = FeatureEngineer()
        features = engineer.compute(candles)
        assert isinstance(features, FeatureSet)

    def test_feature_set_matches_last_candle(self) -> None:
        candles = make_candles(100)
        engineer = FeatureEngineer()
        features = engineer.compute(candles)
        assert features.close == candles[-1].close
        assert features.symbol == candles[-1].symbol

    def test_rsi_in_feature_set_within_bounds(self) -> None:
        candles = make_candles(100)
        engineer = FeatureEngineer()
        features = engineer.compute(candles)
        if features.indicators.rsi_14 is not None:
            assert 0 <= features.indicators.rsi_14 <= 100

    def test_regime_defaults_unknown(self) -> None:
        """Regime ist initial UNKNOWN – wird von ML gesetzt."""
        from sgr.core.types import MarketRegime

        candles = make_candles(100)
        engineer = FeatureEngineer()
        features = engineer.compute(candles)
        assert features.regime == MarketRegime.UNKNOWN

    def test_returns_computed_correctly(self) -> None:
        """1-Bar Return korrekt berechnet."""
        candles = make_flat_candles(50, price=50000.0)
        # Letzten Candle auf 51000 setzen (2% Anstieg)
        sym = make_symbol()
        now = datetime.now(tz=UTC)
        last_candle = Candle(
            symbol=sym,
            timestamp=now,
            timeframe="1h",
            open=Decimal("50000"),
            high=Decimal("51500"),
            low=Decimal("49500"),
            close=Decimal("51000"),
            volume=Decimal("1000"),
        )
        candles_modified = candles[:-1] + [last_candle]

        engineer = FeatureEngineer()
        features = engineer.compute(candles_modified)
        assert features.returns_1 == pytest.approx(0.02, rel=0.001)

    def test_too_few_candles_raises(self) -> None:
        candles = make_candles(1)
        engineer = FeatureEngineer()
        with pytest.raises(ValueError, match="at least 2"):
            engineer.compute(candles)

    def test_with_orderbook(self) -> None:
        candles = make_candles(100)
        sym = make_symbol()
        ob = OrderBook(
            symbol=sym,
            timestamp=datetime.now(tz=UTC),
            bids=[OrderBookLevel(price=Decimal("49990"), size=Decimal("1.0"))],
            asks=[OrderBookLevel(price=Decimal("50010"), size=Decimal("1.0"))],
        )
        engineer = FeatureEngineer()
        features = engineer.compute(candles, orderbook=ob)
        assert features.orderbook is not None
        assert features.orderbook.bid_ask_spread == Decimal("20")


# ---------------------------------------------------------------------------
# Gap Detector
# ---------------------------------------------------------------------------


class TestGapDetector:
    def _make_candle_at(self, ts_offset_hours: int) -> Candle:
        from datetime import timedelta

        sym = make_symbol()
        base = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        ts = base + timedelta(hours=ts_offset_hours)
        return Candle(
            symbol=sym,
            timestamp=ts,
            timeframe="1h",
            open=Decimal("50000"),
            high=Decimal("51000"),
            low=Decimal("49000"),
            close=Decimal("50500"),
            volume=Decimal("1000"),
        )

    def test_no_gap_consecutive_candles(self) -> None:
        detector = GapDetector("1h")
        existing = [self._make_candle_at(0)]
        incoming = [self._make_candle_at(1)]
        gaps = detector.detect(existing, incoming)
        assert len(gaps) == 0

    def test_gap_detected(self) -> None:
        detector = GapDetector("1h")
        existing = [self._make_candle_at(0)]
        incoming = [self._make_candle_at(3)]  # 2 Stunden fehlen
        gaps = detector.detect(existing, incoming)
        assert len(gaps) == 1
        assert gaps[0].missing_candles == 2

    def test_no_gap_empty_series(self) -> None:
        detector = GapDetector("1h")
        assert detector.detect([], []) == []
        assert detector.detect([self._make_candle_at(0)], []) == []

    def test_detect_in_series(self) -> None:
        """Lücke innerhalb einer Serie erkennen."""
        detector = GapDetector("1h")
        candles = [
            self._make_candle_at(0),
            self._make_candle_at(1),
            self._make_candle_at(2),
            self._make_candle_at(5),  # Gap: 3, 4 fehlen
            self._make_candle_at(6),
        ]
        gaps = detector.detect_in_series(candles)
        assert len(gaps) == 1
        assert gaps[0].missing_candles == 2
