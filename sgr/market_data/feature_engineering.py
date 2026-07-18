"""
SGR Feature Engineering
=======================
Berechnung aller technischen Features aus rohen OHLCV-Daten.

Design-Prinzipien:
- Indikatoren sind Features, keine Signale. Keine Kauf-/Verkaufs-Logik hier.
- Alle Berechnungen sind deterministisch und testbar (gleicher Input = gleicher Output)
- Kein externer State: FeatureEngineer bekommt Candles rein, gibt FeatureSet zurück
- None statt Exception wenn zu wenig Daten vorhanden (< min_periods)
- Numpy/Pandas intern – Domain Types nach außen

Technische Entscheidungen:
- Kein TA-Lib: C-Dependency, schwer zu installieren, kein async
- Kein pandas_ta / ta: zu viele Abhängigkeiten, Overhead
- Eigene Implementierungen: minimaler Code, maximal transparent und testbar
- numpy für Performance auf großen historischen Datensätzen

Hinweis zu Indikatoren:
    Indikatoren in Isolation sind schwache Prädiktoren.
    Ihr Wert liegt im Kontext (Regime, Kombination, Zeitreihe).
    Der ML-Layer entscheidet was relevant ist – Feature Engineering
    ist nur die Transformation von Preis → strukturierter Vektor.
"""

from __future__ import annotations

from decimal import Decimal
from typing import NamedTuple

import numpy as np

from sgr.core.logging import get_logger
from sgr.core.types import Candle, MarketRegime, OrderBook
from sgr.market_data.types import (
    FeatureSet,
    IndicatorValues,
    OrderBookFeatures,
)

log = get_logger(__name__)


class OHLCV(NamedTuple):
    """Numpy arrays für schnelle Vektoroperationen."""

    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    timestamps: list  # datetime list (nicht numpy für timezone handling)


def candles_to_arrays(candles: list[Candle]) -> OHLCV:
    """Konvertiert Candle-Liste zu numpy arrays. Input muss sortiert sein (asc)."""
    return OHLCV(
        open=np.array([float(c.open) for c in candles], dtype=np.float64),
        high=np.array([float(c.high) for c in candles], dtype=np.float64),
        low=np.array([float(c.low) for c in candles], dtype=np.float64),
        close=np.array([float(c.close) for c in candles], dtype=np.float64),
        volume=np.array([float(c.volume) for c in candles], dtype=np.float64),
        timestamps=[c.timestamp for c in candles],
    )


# ---------------------------------------------------------------------------
# Primitive Helpers (alle pure functions, keine Side Effects)
# ---------------------------------------------------------------------------


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    """
    Exponential Moving Average.
    Erste EMA-Werte = SMA (Wilder's method Alternative).
    """
    if len(values) < period:
        return np.full(len(values), np.nan)

    result = np.full(len(values), np.nan)
    alpha = 2.0 / (period + 1)

    # Seed: SMA der ersten `period` Werte
    result[period - 1] = np.mean(values[:period])

    for i in range(period, len(values)):
        result[i] = values[i] * alpha + result[i - 1] * (1 - alpha)

    return result


def _sma(values: np.ndarray, period: int) -> np.ndarray:
    """Simple Moving Average via rolling window."""
    if len(values) < period:
        return np.full(len(values), np.nan)

    result = np.full(len(values), np.nan)
    for i in range(period - 1, len(values)):
        result[i] = np.mean(values[i - period + 1 : i + 1])
    return result


def _rma(values: np.ndarray, period: int) -> np.ndarray:
    """
    Wilder's Smoothed Moving Average (RMA).
    Verwendet für RSI, ATR, ADX.
    """
    if len(values) < period:
        return np.full(len(values), np.nan)

    result = np.full(len(values), np.nan)
    alpha = 1.0 / period

    # Seed: SMA der ersten `period` Werte
    result[period - 1] = np.mean(values[:period])

    for i in range(period, len(values)):
        result[i] = values[i] * alpha + result[i - 1] * (1 - alpha)

    return result


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """True Range = max(H-L, |H-PC|, |L-PC|)"""
    hl = high - low
    hc = np.abs(high[1:] - close[:-1])
    lc = np.abs(low[1:] - close[:-1])

    tr = np.full(len(high), np.nan)
    tr[0] = high[0] - low[0]  # First bar: no previous close
    tr[1:] = np.maximum(hl[1:], np.maximum(hc, lc))
    return tr


# ---------------------------------------------------------------------------
# Indicator Calculations
# ---------------------------------------------------------------------------


def calc_rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """
    RSI (Relative Strength Index) via Wilder's RMA.
    Returns array of same length, NaN where insufficient data.
    """
    if len(close) < period + 1:
        return np.full(len(close), np.nan)

    delta = np.diff(close)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)

    avg_gain = _rma(gains, period)
    avg_loss = _rma(losses, period)

    # Avoid division by zero
    rs = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)

    rsi = np.full(len(close), np.nan)
    rsi[1:] = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def calc_macd(
    close: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    MACD = EMA(fast) - EMA(slow)
    Signal = EMA(MACD, signal_period)
    Histogram = MACD - Signal
    Returns (macd_line, signal_line, histogram)
    """
    ema_fast = _ema(close, fast)
    ema_slow = _ema(close, slow)
    macd_line = ema_fast - ema_slow

    # Signal only where MACD is valid
    valid_macd = np.where(np.isnan(macd_line), np.nan, macd_line)
    signal_line = _ema(valid_macd[~np.isnan(valid_macd)], signal)

    # Align signal back to full array
    signal_full = np.full(len(close), np.nan)
    valid_idx = np.where(~np.isnan(macd_line))[0]
    if len(valid_idx) >= signal:
        signal_full[valid_idx[signal - 1 :]] = signal_line[signal - 1 :]

    histogram = macd_line - signal_full
    return macd_line, signal_full, histogram


def calc_atr(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14,
) -> np.ndarray:
    """Average True Range via Wilder's RMA."""
    tr = _true_range(high, low, close)
    return _rma(tr, period)


def calc_adx(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    ADX + DI+/DI- (Wilder's Directional Movement System).
    Returns (adx, di_plus, di_minus)
    ADX > 25: trending, < 20: ranging
    """
    tr = _true_range(high, low, close)

    # Directional Movement
    up_move = np.diff(high)
    down_move = -np.diff(low)

    dm_plus = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    dm_minus = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # Smooth with Wilder's RMA
    atr = _rma(tr, period)
    smooth_plus = _rma(dm_plus, period)
    smooth_minus = _rma(dm_minus, period)

    # DI+ and DI-
    di_plus = np.full(len(high), np.nan)
    di_minus = np.full(len(high), np.nan)

    valid = atr[1:] > 0
    di_plus[1:] = np.where(valid, 100 * smooth_plus / atr[1:], np.nan)
    di_minus[1:] = np.where(valid, 100 * smooth_minus / atr[1:], np.nan)

    # DX and ADX
    dx = np.full(len(high), np.nan)
    di_sum = di_plus[1:] + di_minus[1:]
    di_diff = np.abs(di_plus[1:] - di_minus[1:])
    dx[1:] = np.where(di_sum > 0, 100 * di_diff / di_sum, np.nan)

    adx = _rma(dx[~np.isnan(dx)], period)
    adx_full = np.full(len(high), np.nan)
    valid_idx = np.where(~np.isnan(dx))[0]
    if len(valid_idx) >= period:
        adx_full[valid_idx[period - 1 :]] = adx[period - 1 :]

    return adx_full, di_plus, di_minus


def calc_bollinger_bands(
    close: np.ndarray,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Bollinger Bands: upper, middle (SMA), lower.
    Returns (upper, middle, lower)
    """
    middle = _sma(close, period)
    std = np.full(len(close), np.nan)

    for i in range(period - 1, len(close)):
        std[i] = np.std(close[i - period + 1 : i + 1], ddof=0)

    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower


def calc_keltner_channels(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    ema_period: int = 20,
    atr_period: int = 10,
    multiplier: float = 2.0,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Keltner Channels: upper, lower.
    Squeeze: wenn BB innerhalb von KC → Volatilitäts-Kompression.
    """
    ema = _ema(close, ema_period)
    atr = calc_atr(high, low, close, atr_period)
    upper = ema + multiplier * atr
    lower = ema - multiplier * atr
    return upper, lower


def calc_vwap(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
) -> np.ndarray:
    """
    VWAP = cumsum(typical_price * volume) / cumsum(volume).
    Wird typischerweise täglich zurückgesetzt – hier rolling über alle Bars.
    """
    typical_price = (high + low + close) / 3
    cumulative_tpv = np.cumsum(typical_price * volume)
    cumulative_vol = np.cumsum(volume)
    return np.where(cumulative_vol > 0, cumulative_tpv / cumulative_vol, np.nan)


def calc_obv(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    """On-Balance Volume (direction-weighted cumulative volume)."""
    obv = np.zeros(len(close))
    for i in range(1, len(close)):
        if close[i] > close[i - 1]:
            obv[i] = obv[i - 1] + volume[i]
        elif close[i] < close[i - 1]:
            obv[i] = obv[i - 1] - volume[i]
        else:
            obv[i] = obv[i - 1]
    return obv


# ---------------------------------------------------------------------------
# Orderbook Features
# ---------------------------------------------------------------------------


def calc_orderbook_features(ob: OrderBook) -> OrderBookFeatures:
    """Berechnet Orderbook-Features aus einem OrderBook Snapshot."""

    def _depth_levels(levels: list, n: int) -> tuple[float, float]:
        """Summe von Volumen und USDT-Wert der top-n Levels."""
        total_qty = sum(float(level.size) for level in levels[:n])
        total_usdt = sum(float(level.price) * float(level.size) for level in levels[:n])
        return total_qty, total_usdt

    bid_qty_5, _ = _depth_levels(ob.bids, 5)
    ask_qty_5, _ = _depth_levels(ob.asks, 5)
    bid_qty_10, _ = _depth_levels(ob.bids, 10)
    ask_qty_10, _ = _depth_levels(ob.asks, 10)
    bid_qty_20, bid_usdt_20 = _depth_levels(ob.bids, 20)
    ask_qty_20, ask_usdt_20 = _depth_levels(ob.asks, 20)

    def _imbalance(bid_qty: float, ask_qty: float) -> float:
        total = bid_qty + ask_qty
        return (bid_qty - ask_qty) / total if total > 0 else 0.0

    mid = ob.mid_price
    spread_pct = float(ob.spread / mid) if mid > 0 else 0.0

    largest_bid = max((float(level.size) for level in ob.bids[:20]), default=0.0)
    largest_ask = max((float(level.size) for level in ob.asks[:20]), default=0.0)

    return OrderBookFeatures(
        bid_ask_spread=ob.spread,
        bid_ask_spread_pct=spread_pct,
        mid_price=mid,
        order_imbalance_5=_imbalance(bid_qty_5, ask_qty_5),
        order_imbalance_10=_imbalance(bid_qty_10, ask_qty_10),
        order_imbalance_20=_imbalance(bid_qty_20, ask_qty_20),
        bid_depth_usdt=Decimal(str(bid_usdt_20)),
        ask_depth_usdt=Decimal(str(ask_usdt_20)),
        largest_bid_level=Decimal(str(largest_bid)),
        largest_ask_level=Decimal(str(largest_ask)),
    )


# ---------------------------------------------------------------------------
# Main Feature Engineer
# ---------------------------------------------------------------------------


class FeatureEngineer:
    """
    Berechnet FeatureSet aus Candle-History + optionalem Orderbook.

    Stateless: kein interner State – gleicher Input → gleicher Output.
    Kann für mehrere Symbole parallel verwendet werden.

    min_candles: Mindestanzahl Candles für reliable Indikator-Berechnung.
    Empfehlung: >= 200 für alle Indikatoren (EMA200, ADX braucht Zeit).
    """

    MIN_CANDLES = 50  # Absolute Mindestanzahl
    FULL_CANDLES = 200  # Für alle Indikatoren

    def compute(
        self,
        candles: list[Candle],
        orderbook: OrderBook | None = None,
    ) -> FeatureSet:
        """
        Hauptmethode: berechnet vollständiges FeatureSet.

        Args:
            candles: Sortiert nach Zeit (älteste zuerst). Mindestens MIN_CANDLES.
            orderbook: Optional – aktuelle Orderbook-Snapshot.

        Returns:
            FeatureSet für den letzten Candle (aktueller Zeitpunkt).
        """
        if len(candles) < self.MIN_CANDLES:
            log.warning(
                "feature_engineer.insufficient_candles",
                count=len(candles),
                minimum=self.MIN_CANDLES,
                symbol=str(candles[-1].symbol) if candles else "unknown",
            )

        if len(candles) < 2:
            raise ValueError(f"Need at least 2 candles, got {len(candles)}")

        arrays = candles_to_arrays(candles)
        last_candle = candles[-1]
        n = len(candles)

        # --- Indicators ---
        indicators = self._compute_indicators(arrays, n)

        # --- Orderbook ---
        ob_features = calc_orderbook_features(orderbook) if orderbook else None

        # --- Returns ---
        close = arrays.close
        returns_1 = float((close[-1] - close[-2]) / close[-2]) if n >= 2 else None
        returns_5 = float((close[-1] - close[-6]) / close[-6]) if n >= 6 else None
        returns_10 = float((close[-1] - close[-11]) / close[-11]) if n >= 11 else None
        returns_20 = float((close[-1] - close[-21]) / close[-21]) if n >= 21 else None

        return FeatureSet(
            symbol=last_candle.symbol,
            timestamp=last_candle.timestamp,
            timeframe=last_candle.timeframe,
            close=last_candle.close,
            volume=last_candle.volume,
            indicators=indicators,
            orderbook=ob_features,
            returns_1=returns_1,
            returns_5=returns_5,
            returns_10=returns_10,
            returns_20=returns_20,
            regime=MarketRegime.UNKNOWN,  # Wird vom Regime Detector gesetzt
        )

    def _compute_indicators(self, arrays: OHLCV, n: int) -> IndicatorValues:
        """Berechnet alle Indikatoren. None wo zu wenig Daten."""
        c = arrays.close
        h = arrays.high
        lo = arrays.low
        v = arrays.volume

        def _last(arr: np.ndarray) -> float | None:
            val = arr[-1]
            return None if np.isnan(val) else float(val)

        def _last_dec(arr: np.ndarray) -> Decimal | None:
            val = arr[-1]
            return None if np.isnan(val) else Decimal(str(round(val, 8)))

        # RSI
        rsi_14 = calc_rsi(c, 14)
        rsi_7 = calc_rsi(c, 7)

        # MACD
        macd_line, macd_signal, macd_hist = calc_macd(c)

        # ATR
        atr_arr = calc_atr(h, lo, c, 14)
        atr_val = _last_dec(atr_arr)
        atr_pct = float(atr_val / Decimal(str(c[-1]))) if atr_val and c[-1] > 0 else None

        # ADX
        adx_arr, dip_arr, dim_arr = calc_adx(h, lo, c, 14)

        # Bollinger Bands
        bb_upper_arr, bb_mid_arr, bb_lower_arr = calc_bollinger_bands(c, 20, 2.0)
        bb_u = _last(bb_upper_arr)
        bb_m = _last(bb_mid_arr)
        bb_l = _last(bb_lower_arr)

        bb_width: float | None = None
        bb_position: float | None = None
        if bb_u and bb_m and bb_l and bb_m > 0:
            bb_width = (bb_u - bb_l) / bb_m
            if bb_u != bb_l:
                bb_position = (c[-1] - bb_l) / (bb_u - bb_l)

        # Keltner Channels
        kc_u_arr, kc_l_arr = calc_keltner_channels(h, lo, c)

        # EMAs
        ema_9_arr = _ema(c, 9)
        ema_21_arr = _ema(c, 21)
        ema_50_arr = _ema(c, 50) if n >= 50 else np.full(n, np.nan)
        ema_200_arr = _ema(c, 200) if n >= 200 else np.full(n, np.nan)
        sma_20_arr = _sma(c, 20)

        # Volume
        vwap_arr = calc_vwap(h, lo, c, v)
        vol_sma_arr = _sma(v, 20)
        vol_ratio: float | None = None
        if not np.isnan(vol_sma_arr[-1]) and vol_sma_arr[-1] > 0:
            vol_ratio = float(v[-1] / vol_sma_arr[-1])

        # OBV (normalized delta vs SMA)
        obv_arr = calc_obv(c, v)
        obv_sma = _sma(obv_arr, 20)
        obv_val: float | None = None
        if not np.isnan(obv_sma[-1]) and obv_sma[-1] != 0:
            obv_val = float((obv_arr[-1] - obv_sma[-1]) / abs(obv_sma[-1]))

        return IndicatorValues(
            rsi_14=_last(rsi_14),
            rsi_7=_last(rsi_7),
            macd_line=_last(macd_line),
            macd_signal=_last(macd_signal),
            macd_histogram=_last(macd_hist),
            adx_14=_last(adx_arr),
            di_plus=_last(dip_arr),
            di_minus=_last(dim_arr),
            atr_14=atr_val,
            atr_pct=atr_pct,
            bb_upper=_last_dec(bb_upper_arr),
            bb_middle=_last_dec(bb_mid_arr),
            bb_lower=_last_dec(bb_lower_arr),
            bb_width=bb_width,
            bb_position=bb_position,
            kc_upper=_last_dec(kc_u_arr),
            kc_lower=_last_dec(kc_l_arr),
            ema_9=_last_dec(ema_9_arr),
            ema_21=_last_dec(ema_21_arr),
            ema_50=_last_dec(ema_50_arr),
            ema_200=_last_dec(ema_200_arr),
            sma_20=_last_dec(sma_20_arr),
            vwap=_last_dec(vwap_arr),
            volume_sma_20=_last_dec(vol_sma_arr),
            volume_ratio=vol_ratio,
            obv=obv_val,
        )
