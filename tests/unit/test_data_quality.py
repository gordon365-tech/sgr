"""Tests fuer sgr.backtesting.data_quality.assess_data_quality()."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sgr.backtesting.data_quality import (
    MIN_CANDLES,
    DataQualityStatus,
    assess_data_quality,
)
from sgr.core.types import Candle, ExchangeID, Symbol


def _symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def _candles(n: int, *, start: datetime | None = None, interval_hours: int = 1) -> list[Candle]:
    start = start or datetime(2026, 1, 1, tzinfo=UTC)
    sym = _symbol()
    out = []
    price = Decimal("50000")
    for i in range(n):
        out.append(
            Candle(
                symbol=sym,
                timestamp=start + timedelta(hours=i * interval_hours),
                timeframe="1h",
                open=price,
                high=price + Decimal("10"),
                low=price - Decimal("10"),
                close=price,
                volume=Decimal("100"),
            )
        )
    return out


class TestInsufficientData:
    def test_too_few_candles_is_insufficient(self) -> None:
        result = assess_data_quality(_candles(50), "1h")
        assert result.status == DataQualityStatus.INSUFFICIENT_DATA
        assert not result.passed

    def test_exactly_min_candles_is_not_insufficient_on_count_alone(self) -> None:
        result = assess_data_quality(_candles(MIN_CANDLES), "1h")
        assert result.status != DataQualityStatus.INSUFFICIENT_DATA

    def test_large_gap_ratio_is_insufficient(self) -> None:
        first_half = _candles(MIN_CANDLES // 2)
        # Zweite Haelfte startet weit spaeter -> riesige Luecke.
        second_half = _candles(
            MIN_CANDLES // 2,
            start=first_half[-1].timestamp + timedelta(days=365),
        )
        candles = first_half + second_half
        result = assess_data_quality(candles, "1h")
        assert result.status == DataQualityStatus.INSUFFICIENT_DATA
        assert result.missing_bars > 0


class TestInvalidData:
    def test_non_positive_price_is_invalid(self) -> None:
        candles = _candles(MIN_CANDLES)
        bad = candles[10].model_copy(update={"open": Decimal("0")})
        candles[10] = bad
        result = assess_data_quality(candles, "1h")
        assert result.status == DataQualityStatus.INVALID_DATA

    def test_negative_volume_is_invalid(self) -> None:
        candles = _candles(MIN_CANDLES)
        candles[5] = candles[5].model_copy(update={"volume": Decimal("-1")})
        result = assess_data_quality(candles, "1h")
        assert result.status == DataQualityStatus.INVALID_DATA

    def test_duplicate_timestamps_are_invalid(self) -> None:
        candles = _candles(MIN_CANDLES)
        candles[3] = candles[3].model_copy(update={"timestamp": candles[2].timestamp})
        result = assess_data_quality(candles, "1h")
        assert result.status == DataQualityStatus.INVALID_DATA


class TestOk:
    def test_clean_continuous_series_passes(self) -> None:
        result = assess_data_quality(_candles(MIN_CANDLES + 100), "1h")
        assert result.status == DataQualityStatus.OK
        assert result.passed
        assert result.n_candles == MIN_CANDLES + 100
        assert result.gap_count == 0

    def test_insufficient_data_never_reports_as_invalid(self) -> None:
        """Ein Symbol mit zu wenig Historie darf NIEMALS als
        'schlechte Strategie'-Signal (INVALID_DATA) gewertet werden -
        siehe Task-Vorgabe Phase 3."""
        result = assess_data_quality(_candles(10), "1h")
        assert result.status == DataQualityStatus.INSUFFICIENT_DATA
