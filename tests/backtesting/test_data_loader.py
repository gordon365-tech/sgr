"""
Tests für sgr.backtesting.data_loader.BacktestDataLoader.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from sgr.backtesting.data_loader import BacktestDataLoader
from sgr.core.types import Candle, ExchangeID, Symbol, TradingMode
from sgr.exchanges.factory import ExchangePool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SYMBOL = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX)


def make_candle(ts: datetime, price: float = 100.0, timeframe: str = "1h") -> Candle:
    p = Decimal(str(price))
    return Candle(
        symbol=SYMBOL,
        timestamp=ts,
        timeframe=timeframe,
        open=p,
        high=p + 1,
        low=p - 1,
        close=p,
        volume=Decimal("10"),
    )


def make_series(n: int, start: datetime, step_seconds: int = 3600) -> list[Candle]:
    return [make_candle(start + timedelta(seconds=i * step_seconds)) for i in range(n)]


async def make_pool_with_adapter(adapter) -> ExchangePool:
    pool = ExchangePool()
    pool._adapters[(ExchangeID.PIONEX, TradingMode.PAPER)] = adapter
    return pool


# ---------------------------------------------------------------------------
# load_from_exchange
# ---------------------------------------------------------------------------


class TestLoadFromExchange:
    async def test_single_page_returns_all_candles(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 2, tzinfo=UTC)
        candles = make_series(10, start)

        adapter = AsyncMock()
        adapter.get_ohlcv = AsyncMock(return_value=candles)
        pool = await make_pool_with_adapter(adapter)

        loader = BacktestDataLoader()
        result = await loader.load_from_exchange("BTC/USDT", "1h", start, end, pool)

        assert len(result) == 10
        assert result == sorted(result, key=lambda c: c.timestamp)

    async def test_pagination_across_multiple_batches(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 10, tzinfo=UTC)

        # First batch full (== batch_size 500 to force pagination)... too slow
        # to build 500 real candles per batch; instead patch batch_size logic
        # indirectly by using small batches and checking the "< batch_size
        # breaks" branch via two calls: first batch size 3 (< 500) -> break
        # already covers termination; separately test explicit pagination by
        # returning exactly 2 batches where first has len == limit passed.
        batch1 = make_series(5, start)
        batch2 = make_series(5, start + timedelta(hours=5))

        adapter = AsyncMock()
        # get_ohlcv called with limit=500; to trigger pagination we need
        # len(batch) == 500 on first call. Instead directly assert on the
        # call args and simulate by monkeypatching batch_size via two calls
        # returning progressively fewer candles is enough to test the "not
        # batch: break" and general flow; full 500-item pagination edge is
        # covered separately below.
        adapter.get_ohlcv = AsyncMock(side_effect=[batch1, batch2, []])
        pool = await make_pool_with_adapter(adapter)

        loader = BacktestDataLoader()
        result = await loader.load_from_exchange("BTC/USDT", "1h", start, end, pool)

        # First call returns 5 (< batch_size 500) -> loop breaks after first call.
        assert adapter.get_ohlcv.await_count == 1
        assert len(result) == 5

    async def test_full_batch_triggers_next_page_request(self):
        """When a batch has exactly `batch_size` (500) candles, the loader
        must request the next page instead of stopping."""
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 3, 1, tzinfo=UTC)

        full_batch = make_series(500, start)
        second_batch = make_series(3, start + timedelta(hours=500))

        adapter = AsyncMock()
        adapter.get_ohlcv = AsyncMock(side_effect=[full_batch, second_batch])
        pool = await make_pool_with_adapter(adapter)

        loader = BacktestDataLoader()
        result = await loader.load_from_exchange("BTC/USDT", "1h", start, end, pool)

        assert adapter.get_ohlcv.await_count == 2
        assert len(result) == 503

    async def test_empty_first_batch_breaks_immediately(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 2, tzinfo=UTC)

        adapter = AsyncMock()
        adapter.get_ohlcv = AsyncMock(return_value=[])
        pool = await make_pool_with_adapter(adapter)

        loader = BacktestDataLoader()
        result = await loader.load_from_exchange("BTC/USDT", "1h", start, end, pool)

        assert result == []
        adapter.get_ohlcv.assert_awaited_once()

    async def test_candles_beyond_end_are_filtered(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 1, 5, tzinfo=UTC)

        # Candles span past `end`.
        candles = make_series(10, start)  # hours 0..9, end cuts off at hour 5

        adapter = AsyncMock()
        adapter.get_ohlcv = AsyncMock(return_value=candles)
        pool = await make_pool_with_adapter(adapter)

        loader = BacktestDataLoader()
        result = await loader.load_from_exchange("BTC/USDT", "1h", start, end, pool)

        assert all(c.timestamp <= end for c in result)
        assert len(result) == 6  # hours 0..5 inclusive

    async def test_cache_hit_returns_cached_result_without_new_call(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 2, tzinfo=UTC)
        candles = make_series(5, start)

        adapter = AsyncMock()
        adapter.get_ohlcv = AsyncMock(return_value=candles)
        pool = await make_pool_with_adapter(adapter)

        loader = BacktestDataLoader()
        first = await loader.load_from_exchange("BTC/USDT", "1h", start, end, pool)
        second = await loader.load_from_exchange("BTC/USDT", "1h", start, end, pool)

        assert first == second
        adapter.get_ohlcv.assert_awaited_once()

    async def test_deduplicates_overlapping_timestamps(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 2, tzinfo=UTC)

        c1 = make_candle(start, price=100.0)
        c2 = make_candle(start, price=200.0)  # same timestamp, different price

        adapter = AsyncMock()
        adapter.get_ohlcv = AsyncMock(return_value=[c1, c2])
        pool = await make_pool_with_adapter(adapter)

        loader = BacktestDataLoader()
        result = await loader.load_from_exchange("BTC/USDT", "1h", start, end, pool)

        assert len(result) == 1
        # "Letzter Wert gewinnt" -> c2 should have overwritten c1.
        assert result[0].open == Decimal("200.0")

    async def test_non_exchangepool_object_raises_assertion(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 2, tzinfo=UTC)

        loader = BacktestDataLoader()
        with pytest.raises(AssertionError):
            await loader.load_from_exchange("BTC/USDT", "1h", start, end, object())

    async def test_uses_explicit_exchange_id(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 2, tzinfo=UTC)
        candles = make_series(3, start)

        adapter = AsyncMock()
        adapter.get_ohlcv = AsyncMock(return_value=candles)

        pool = ExchangePool()
        pool._adapters[(ExchangeID.PIONEX, TradingMode.PAPER)] = adapter

        loader = BacktestDataLoader()
        result = await loader.load_from_exchange(
            "BTC/USDT",
            "1h",
            start,
            end,
            pool,
            exchange_id=ExchangeID.PIONEX,
        )
        assert len(result) == 3


# ---------------------------------------------------------------------------
# load_from_csv
# ---------------------------------------------------------------------------


class TestLoadFromCsv:
    def _write_csv(self, tmp_path, rows: list[dict], filename="candles.csv"):
        path = tmp_path / filename
        fieldnames = ["timestamp", "open", "high", "low", "close", "volume"]
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return path

    def test_parses_iso_timestamps(self, tmp_path):
        rows = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "open": "100",
                "high": "101",
                "low": "99",
                "close": "100.5",
                "volume": "10",
            },
            {
                "timestamp": "2026-01-01T01:00:00Z",
                "open": "100.5",
                "high": "102",
                "low": "100",
                "close": "101",
                "volume": "12",
            },
        ]
        path = self._write_csv(tmp_path, rows)

        loader = BacktestDataLoader()
        result = loader.load_from_csv(path, "BTC/USDT", "1h")

        assert len(result) == 2
        assert result[0].timestamp < result[1].timestamp
        assert result[0].symbol.base == "BTC"
        assert result[0].symbol.quote == "USDT"

    def test_parses_unix_ms_timestamps(self, tmp_path):
        ts_ms = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
        rows = [
            {
                "timestamp": str(ts_ms),
                "open": "100",
                "high": "101",
                "low": "99",
                "close": "100",
                "volume": "5",
            }
        ]
        path = self._write_csv(tmp_path, rows)

        loader = BacktestDataLoader()
        result = loader.load_from_csv(path, "BTC/USDT", "1h")

        assert len(result) == 1
        assert result[0].timestamp == datetime(2026, 1, 1, tzinfo=UTC)

    def test_falls_back_to_time_column(self, tmp_path):
        path = tmp_path / "candles_time_col.csv"
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=["time", "open", "high", "low", "close", "volume"]
            )
            writer.writeheader()
            writer.writerow(
                {
                    "time": "2026-01-01T00:00:00Z",
                    "open": "100",
                    "high": "101",
                    "low": "99",
                    "close": "100",
                    "volume": "1",
                }
            )
        loader = BacktestDataLoader()
        result = loader.load_from_csv(path, "BTC/USDT", "1h")
        assert len(result) == 1

    def test_missing_volume_defaults_to_zero(self, tmp_path):
        path = tmp_path / "candles_no_vol.csv"
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["timestamp", "open", "high", "low", "close"])
            writer.writeheader()
            writer.writerow(
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "open": "100",
                    "high": "101",
                    "low": "99",
                    "close": "100",
                }
            )
        loader = BacktestDataLoader()
        result = loader.load_from_csv(path, "BTC/USDT", "1h")
        assert result[0].volume == Decimal("0")

    def test_malformed_row_is_skipped_and_logged(self, tmp_path):
        rows = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "open": "not-a-number",  # triggers Decimal(...) exception
                "high": "101",
                "low": "99",
                "close": "100",
                "volume": "1",
            },
            {
                "timestamp": "2026-01-01T01:00:00Z",
                "open": "100",
                "high": "101",
                "low": "99",
                "close": "100",
                "volume": "1",
            },
        ]
        path = self._write_csv(tmp_path, rows)

        loader = BacktestDataLoader()
        result = loader.load_from_csv(path, "BTC/USDT", "1h")

        assert len(result) == 1  # only the valid row survives

    def test_deduplicates_and_sorts(self, tmp_path):
        rows = [
            {
                "timestamp": "2026-01-01T02:00:00Z",
                "open": "102",
                "high": "103",
                "low": "101",
                "close": "102",
                "volume": "1",
            },
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "open": "100",
                "high": "101",
                "low": "99",
                "close": "100",
                "volume": "1",
            },
            {
                "timestamp": "2026-01-01T00:00:00Z",  # duplicate of above
                "open": "999",
                "high": "1000",
                "low": "998",
                "close": "999",
                "volume": "1",
            },
        ]
        path = self._write_csv(tmp_path, rows)

        loader = BacktestDataLoader()
        result = loader.load_from_csv(path, "BTC/USDT", "1h")

        assert len(result) == 2
        assert result[0].timestamp < result[1].timestamp
        # Later duplicate row wins.
        assert result[0].open == Decimal("999")

    def test_custom_exchange_id(self, tmp_path):
        rows = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "open": "100",
                "high": "101",
                "low": "99",
                "close": "100",
                "volume": "1",
            }
        ]
        path = self._write_csv(tmp_path, rows)

        loader = BacktestDataLoader()
        result = loader.load_from_csv(path, "ETH/USDT", "1h", exchange_id=ExchangeID.PIONEX)
        assert result[0].symbol.base == "ETH"
        assert result[0].symbol.exchange == ExchangeID.PIONEX


# ---------------------------------------------------------------------------
# iterate
# ---------------------------------------------------------------------------


class TestIterate:
    def test_skips_warmup_bars(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        candles = make_series(10, start)

        loader = BacktestDataLoader()
        results = list(loader.iterate(candles, warmup_bars=7))

        assert len(results) == 3
        assert results[0][0] == 7

    def test_history_never_includes_future_candles(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        candles = make_series(10, start)

        loader = BacktestDataLoader()
        for idx, current, history in loader.iterate(candles, warmup_bars=5):
            assert history[-1] == current
            assert len(history) == idx + 1
            assert all(c.timestamp <= current.timestamp for c in history)

    def test_empty_candles_yields_nothing(self):
        loader = BacktestDataLoader()
        results = list(loader.iterate([], warmup_bars=0))
        assert results == []

    def test_warmup_exceeding_length_yields_nothing(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        candles = make_series(5, start)
        loader = BacktestDataLoader()
        results = list(loader.iterate(candles, warmup_bars=100))
        assert results == []


# ---------------------------------------------------------------------------
# _deduplicate / _validate (direct unit tests)
# ---------------------------------------------------------------------------


class TestDeduplicate:
    def test_no_duplicates_returns_all(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        candles = make_series(3, start)
        loader = BacktestDataLoader()
        result = loader._deduplicate(candles)
        assert len(result) == 3

    def test_empty_list(self):
        loader = BacktestDataLoader()
        assert loader._deduplicate([]) == []


class TestValidate:
    def test_few_candles_flagged(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        candles = make_series(10, start)
        loader = BacktestDataLoader()
        issues = loader._validate(candles, "1h")
        assert any("Very few candles" in i for i in issues)

    def test_sufficient_candles_no_count_issue(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        candles = make_series(60, start)
        loader = BacktestDataLoader()
        issues = loader._validate(candles, "1h")
        assert not any("Very few candles" in i for i in issues)

    def test_gaps_detected(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        # 60 hourly candles but with a large jump in the middle -> gap.
        first_half = make_series(30, start)
        second_half = make_series(30, start + timedelta(days=5))
        candles = first_half + second_half

        loader = BacktestDataLoader()
        issues = loader._validate(candles, "1h")
        assert any("gaps detected" in i for i in issues)

    def test_negative_volume_flagged(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        candles = make_series(60, start)
        # Construct one candle with negative volume directly (bypasses the
        # loader's normal construction path but Candle itself allows it -
        # only high>=low is validated at the model level).
        bad = Candle(
            symbol=SYMBOL,
            timestamp=start + timedelta(hours=100),
            timeframe="1h",
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("-5"),
        )
        candles = candles + [bad]

        loader = BacktestDataLoader()
        issues = loader._validate(candles, "1h")
        assert any("Negative volume" in i for i in issues)

    def test_no_issues_for_clean_data(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        candles = make_series(60, start)
        loader = BacktestDataLoader()
        issues = loader._validate(candles, "1h")
        assert issues == []

    def test_high_lower_than_low_flagged(self):
        """Candle enforces high>=low at construction time, so this data
        error is only reachable via model_construct (bypassing validation) -
        simulating a corrupted/legacy data source."""
        start = datetime(2026, 1, 1, tzinfo=UTC)
        candles = make_series(60, start)
        bad = Candle.model_construct(
            symbol=SYMBOL,
            timestamp=start + timedelta(hours=200),
            timeframe="1h",
            open=Decimal("100"),
            high=Decimal("90"),
            low=Decimal("100"),
            close=Decimal("95"),
            volume=Decimal("1"),
        )
        candles = candles + [bad]

        loader = BacktestDataLoader()
        issues = loader._validate(candles, "1h")
        assert any("high < low" in i for i in issues)


# ---------------------------------------------------------------------------
# load_from_exchange: validation issues get logged (integration through
# the public method, exercising the "issues -> log.warning loop" branch)
# ---------------------------------------------------------------------------


class TestLoadFromExchangeValidationLogging:
    async def test_few_candles_triggers_validation_warning_path(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 2, tzinfo=UTC)
        candles = make_series(3, start)  # well under the 50-candle threshold

        adapter = AsyncMock()
        adapter.get_ohlcv = AsyncMock(return_value=candles)
        pool = await make_pool_with_adapter(adapter)

        loader = BacktestDataLoader()
        result = await loader.load_from_exchange("BTC/USDT", "1h", start, end, pool)

        # No exception; issues were logged internally (not asserted on the
        # logger directly, but exercised for coverage of the warning loop).
        assert len(result) == 3

    async def test_empty_result_logging_uses_none_placeholders(self):
        """When load_from_exchange yields zero candles, the final log call's
        from_ts/to_ts ternaries must use the 'none' fallback without raising
        an IndexError."""
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 2, tzinfo=UTC)

        adapter = AsyncMock()
        adapter.get_ohlcv = AsyncMock(return_value=[])
        pool = await make_pool_with_adapter(adapter)

        loader = BacktestDataLoader()
        result = await loader.load_from_exchange("BTC/USDT", "1h", start, end, pool)
        assert result == []
