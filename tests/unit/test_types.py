"""
Tests for core domain types.
All types must be validated, immutable where specified, and serializable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from sgr.core.types import (
    AssetClass,
    Candle,
    ExchangeID,
    MarketRegime,
    OrderBook,
    OrderBookLevel,
    Position,
    PositionSide,
    Price,
    Signal,
    SignalDirection,
    Symbol,
    TradingMode,
)

# ---------------------------------------------------------------------------
# Symbol
# ---------------------------------------------------------------------------


class TestSymbol:
    def test_uppercase_normalization(self) -> None:
        s = Symbol(base="btc", quote="usdt", exchange=ExchangeID.BINANCE)
        assert s.base == "BTC"
        assert s.quote == "USDT"

    def test_ccxt_symbol(self) -> None:
        s = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)
        assert s.ccxt_symbol == "BTC/USDT"

    def test_str_representation(self) -> None:
        s = Symbol(base="ETH", quote="USDT", exchange=ExchangeID.PIONEX)
        assert str(s) == "ETH/USDT:pionex"

    def test_immutability(self) -> None:
        s = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)
        with pytest.raises((TypeError, ValidationError)):
            s.base = "ETH"  # type: ignore

    def test_default_asset_class(self) -> None:
        s = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)
        assert s.asset_class == AssetClass.SPOT


# ---------------------------------------------------------------------------
# Price
# ---------------------------------------------------------------------------


class TestPrice:
    def test_multiplication(self) -> None:
        p = Price(value=Decimal("100.00"))
        result = p * Decimal("1.5")
        assert result.value == Decimal("150.00")

    def test_addition(self) -> None:
        p1 = Price(value=Decimal("100.00"))
        p2 = Price(value=Decimal("50.00"))
        result = p1 + p2
        assert result.value == Decimal("150.00")

    def test_comparison(self) -> None:
        p1 = Price(value=Decimal("100"))
        p2 = Price(value=Decimal("200"))
        assert p1 < p2
        assert p2 > p1

    def test_currency_mismatch_raises(self) -> None:
        p1 = Price(value=Decimal("100"), currency="USDT")
        p2 = Price(value=Decimal("100"), currency="BTC")
        with pytest.raises(ValueError):
            _ = p1 + p2


# ---------------------------------------------------------------------------
# Candle
# ---------------------------------------------------------------------------


class TestCandle:
    def _make_symbol(self) -> Symbol:
        return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)

    def test_valid_candle(self) -> None:
        candle = Candle(
            symbol=self._make_symbol(),
            timestamp=datetime.now(tz=UTC),
            timeframe="1h",
            open=Decimal("50000"),
            high=Decimal("51000"),
            low=Decimal("49000"),
            close=Decimal("50500"),
            volume=Decimal("1000"),
        )
        assert candle.close == Decimal("50500")

    def test_high_lt_low_raises(self) -> None:
        with pytest.raises(ValidationError):
            Candle(
                symbol=self._make_symbol(),
                timestamp=datetime.now(tz=UTC),
                timeframe="1h",
                open=Decimal("50000"),
                high=Decimal("48000"),  # high < low → invalid
                low=Decimal("49000"),
                close=Decimal("50000"),
                volume=Decimal("1000"),
            )

    def test_immutability(self) -> None:
        candle = Candle(
            symbol=self._make_symbol(),
            timestamp=datetime.now(tz=UTC),
            timeframe="1h",
            open=Decimal("50000"),
            high=Decimal("51000"),
            low=Decimal("49000"),
            close=Decimal("50500"),
            volume=Decimal("1000"),
        )
        with pytest.raises((TypeError, ValidationError)):
            candle.close = Decimal("99999")  # type: ignore


# ---------------------------------------------------------------------------
# OrderBook
# ---------------------------------------------------------------------------


class TestOrderBook:
    def _make_symbol(self) -> Symbol:
        return Symbol(base="ETH", quote="USDT", exchange=ExchangeID.BINANCE)

    def test_spread_calculation(self) -> None:
        ob = OrderBook(
            symbol=self._make_symbol(),
            timestamp=datetime.now(tz=UTC),
            bids=[OrderBookLevel(price=Decimal("2000"), size=Decimal("1.0"))],
            asks=[OrderBookLevel(price=Decimal("2001"), size=Decimal("1.0"))],
        )
        assert ob.spread == Decimal("1")
        assert ob.mid_price == Decimal("2000.5")

    def test_best_bid_ask(self) -> None:
        ob = OrderBook(
            symbol=self._make_symbol(),
            timestamp=datetime.now(tz=UTC),
            bids=[
                OrderBookLevel(price=Decimal("2000"), size=Decimal("1.0")),
                OrderBookLevel(price=Decimal("1999"), size=Decimal("2.0")),
            ],
            asks=[
                OrderBookLevel(price=Decimal("2001"), size=Decimal("1.0")),
                OrderBookLevel(price=Decimal("2002"), size=Decimal("3.0")),
            ],
        )
        assert ob.best_bid == Decimal("2000")
        assert ob.best_ask == Decimal("2001")

    def test_empty_orderbook(self) -> None:
        ob = OrderBook(
            symbol=self._make_symbol(),
            timestamp=datetime.now(tz=UTC),
            bids=[],
            asks=[],
        )
        assert ob.best_bid == Decimal(0)
        assert ob.best_ask == Decimal(0)


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------


class TestSignal:
    def test_valid_signal(self) -> None:
        signal = Signal(
            timestamp=datetime.now(tz=UTC),
            strategy_name="trend_following_v1",
            symbol=Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE),
            direction=SignalDirection.LONG,
            confidence=0.82,
            regime=MarketRegime.TRENDING_UP,
        )
        assert signal.confidence == 0.82
        assert signal.id is not None

    def test_confidence_bounds(self) -> None:
        base = dict(
            timestamp=datetime.now(tz=UTC),
            strategy_name="test",
            symbol=Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE),
            direction=SignalDirection.LONG,
            regime=MarketRegime.RANGING,
        )
        with pytest.raises(ValidationError):
            Signal(**base, confidence=1.1)  # over 1.0
        with pytest.raises(ValidationError):
            Signal(**base, confidence=-0.1)  # negative


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------


class TestPosition:
    def test_notional_value(self) -> None:
        pos = Position(
            symbol=Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE),
            side=PositionSide.LONG,
            quantity=Decimal("0.5"),
            entry_price=Decimal("50000"),
            current_price=Decimal("52000"),
            opened_at=datetime.now(tz=UTC),
            strategy_name="trend_v1",
            trading_mode=TradingMode.PAPER,
        )
        assert pos.notional_value == Decimal("26000")

    def test_pnl_pct(self) -> None:
        pos = Position(
            symbol=Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE),
            side=PositionSide.LONG,
            quantity=Decimal("1.0"),
            entry_price=Decimal("50000"),
            current_price=Decimal("55000"),
            opened_at=datetime.now(tz=UTC),
            strategy_name="trend_v1",
            trading_mode=TradingMode.PAPER,
        )
        assert pos.pnl_pct == pytest.approx(0.10, abs=0.001)  # +10%
