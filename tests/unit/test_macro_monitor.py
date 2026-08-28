"""
Tests für sgr.sentiment.macro_monitor – MacroEventMonitor.
Coverage-Ziel: 37% -> 100%.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from sgr.sentiment.macro_monitor import MacroEventMonitor
from sgr.sentiment.types import EventCategory, MacroEvent, MacroEventType

# ===========================================================================
# Helpers
# ===========================================================================


def make_event(
    *,
    event_type: MacroEventType = MacroEventType.CPI_RELEASE,
    timestamp: datetime | None = None,
    surprise_magnitude: float = 0.05,
    impact: EventCategory = EventCategory.MACRO_POSITIVE,
) -> MacroEvent:
    return MacroEvent(
        event_type=event_type,
        timestamp=timestamp or datetime.now(tz=UTC),
        expected_value=3.0,
        actual_value=3.1,
        surprise_magnitude=surprise_magnitude,
        is_positive_surprise=surprise_magnitude > 0,
        impact_assessment=impact,
        description="test event",
    )


class FakeResponse:
    def __init__(self, status: int, json_data: dict) -> None:
        self.status = status
        self._json_data = json_data

    async def json(self) -> dict:
        return self._json_data

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeSession:
    """Mimics aiohttp.ClientSession as an async context manager."""

    def __init__(self, responses: list[FakeResponse] | Exception) -> None:
        self._responses = responses
        self._call_index = 0

    def get(self, url: str, params: dict) -> FakeResponse:
        if isinstance(self._responses, Exception):
            raise self._responses
        response = self._responses[self._call_index % len(self._responses)]
        self._call_index += 1
        return response

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


# ===========================================================================
# set_fred_api_key
# ===========================================================================


class TestSetFredApiKey:
    def test_sets_key(self) -> None:
        monitor = MacroEventMonitor()
        assert monitor._fred_api_key is None

        monitor.set_fred_api_key("secret-key")

        assert monitor._fred_api_key == "secret-key"


# ===========================================================================
# fetch_recent_events
# ===========================================================================


class TestFetchRecentEvents:
    @pytest.mark.asyncio
    async def test_returns_fresh_events_from_fred(self) -> None:
        monitor = MacroEventMonitor()
        fresh_event = make_event(timestamp=datetime.now(tz=UTC))

        with patch.object(monitor, "_fetch_fred_data", AsyncMock(return_value=[fresh_event])):
            result = await monitor.fetch_recent_events(max_age_days=7)

        assert result == [fresh_event]
        assert monitor._recent_events == [fresh_event]

    @pytest.mark.asyncio
    async def test_filters_out_old_events(self) -> None:
        monitor = MacroEventMonitor()
        old_event = make_event(timestamp=datetime.now(tz=UTC) - timedelta(days=30))
        fresh_event = make_event(timestamp=datetime.now(tz=UTC))

        with patch.object(
            monitor, "_fetch_fred_data", AsyncMock(return_value=[old_event, fresh_event])
        ):
            result = await monitor.fetch_recent_events(max_age_days=7)

        assert result == [fresh_event]

    @pytest.mark.asyncio
    async def test_fred_failure_falls_back_to_cached_events(self) -> None:
        monitor = MacroEventMonitor()
        cached_event = make_event(timestamp=datetime.now(tz=UTC))
        monitor._recent_events = [cached_event]

        with patch.object(
            monitor, "_fetch_fred_data", AsyncMock(side_effect=RuntimeError("network down"))
        ):
            result = await monitor.fetch_recent_events(max_age_days=7)

        assert result == [cached_event]

    @pytest.mark.asyncio
    async def test_fred_failure_and_no_cache_returns_empty(self) -> None:
        monitor = MacroEventMonitor()

        with patch.object(
            monitor, "_fetch_fred_data", AsyncMock(side_effect=RuntimeError("network down"))
        ):
            result = await monitor.fetch_recent_events(max_age_days=7)

        assert result == []

    @pytest.mark.asyncio
    async def test_no_events_from_fred_uses_cache_then_filters(self) -> None:
        monitor = MacroEventMonitor()
        old_cached = make_event(timestamp=datetime.now(tz=UTC) - timedelta(days=100))
        monitor._recent_events = [old_cached]

        with patch.object(monitor, "_fetch_fred_data", AsyncMock(return_value=[])):
            result = await monitor.fetch_recent_events(max_age_days=7)

        assert result == []
        assert monitor._recent_events == []


# ===========================================================================
# _fetch_fred_data
# ===========================================================================


class TestFetchFredData:
    @pytest.mark.asyncio
    async def test_successful_fetch_classifies_all_series(self) -> None:
        monitor = MacroEventMonitor()

        response = FakeResponse(
            200,
            {
                "observations": [
                    {"value": "3.5", "date": "2024-01-01"},
                    {"value": "3.0", "date": "2023-12-01"},
                ]
            },
        )
        fake_session = FakeSession([response])

        with patch("aiohttp.ClientSession", return_value=fake_session):
            events = await monitor._fetch_fred_data()

        # 4 series configured (cpi, fed_funds, unemployment, gdp)
        assert len(events) == 4
        assert all(isinstance(e, MacroEvent) for e in events)

    @pytest.mark.asyncio
    async def test_uses_api_key_when_set(self) -> None:
        monitor = MacroEventMonitor()
        monitor.set_fred_api_key("my-key")

        captured_params = []

        class RecordingSession(FakeSession):
            def get(self, url: str, params: dict) -> FakeResponse:
                captured_params.append(dict(params))
                return super().get(url, params)

        response = FakeResponse(
            200,
            {
                "observations": [
                    {"value": "3.5", "date": "2024-01-01"},
                    {"value": "3.0", "date": "2023-12-01"},
                ]
            },
        )
        fake_session = RecordingSession([response])

        with patch("aiohttp.ClientSession", return_value=fake_session):
            await monitor._fetch_fred_data()

        assert all(p.get("api_key") == "my-key" for p in captured_params)

    @pytest.mark.asyncio
    async def test_non_200_status_skips_series(self) -> None:
        monitor = MacroEventMonitor()
        response = FakeResponse(500, {})
        fake_session = FakeSession([response])

        with patch("aiohttp.ClientSession", return_value=fake_session):
            events = await monitor._fetch_fred_data()

        assert events == []

    @pytest.mark.asyncio
    async def test_too_few_observations_skips_series(self) -> None:
        monitor = MacroEventMonitor()
        response = FakeResponse(200, {"observations": [{"value": "3.5", "date": "2024-01-01"}]})
        fake_session = FakeSession([response])

        with patch("aiohttp.ClientSession", return_value=fake_session):
            events = await monitor._fetch_fred_data()

        assert events == []

    @pytest.mark.asyncio
    async def test_dot_values_are_treated_as_missing(self) -> None:
        monitor = MacroEventMonitor()
        response = FakeResponse(
            200,
            {
                "observations": [
                    {"value": ".", "date": "2024-01-01"},
                    {"value": "3.0", "date": "2023-12-01"},
                ]
            },
        )
        fake_session = FakeSession([response])

        with patch("aiohttp.ClientSession", return_value=fake_session):
            events = await monitor._fetch_fred_data()

        assert events == []

    @pytest.mark.asyncio
    async def test_previous_dot_value_also_skips(self) -> None:
        monitor = MacroEventMonitor()
        response = FakeResponse(
            200,
            {
                "observations": [
                    {"value": "3.5", "date": "2024-01-01"},
                    {"value": ".", "date": "2023-12-01"},
                ]
            },
        )
        fake_session = FakeSession([response])

        with patch("aiohttp.ClientSession", return_value=fake_session):
            events = await monitor._fetch_fred_data()

        assert events == []

    @pytest.mark.asyncio
    async def test_exception_during_series_fetch_is_caught_and_continues(self) -> None:
        monitor = MacroEventMonitor()

        class RaisingSession(FakeSession):
            def get(self, url: str, params: dict) -> FakeResponse:
                raise ValueError("boom")

        fake_session = RaisingSession([])

        with patch("aiohttp.ClientSession", return_value=fake_session):
            events = await monitor._fetch_fred_data()

        assert events == []

    @pytest.mark.asyncio
    async def test_significance_filter_yields_neutral_event(self) -> None:
        monitor = MacroEventMonitor()
        # actual very close to previous -> normalized_surprise below 0.01 threshold
        response = FakeResponse(
            200,
            {
                "observations": [
                    {"value": "100.0001", "date": "2024-01-01"},
                    {"value": "100.0", "date": "2023-12-01"},
                ]
            },
        )
        fake_session = FakeSession([response])

        with patch("aiohttp.ClientSession", return_value=fake_session):
            events = await monitor._fetch_fred_data()

        assert len(events) == 4
        assert all(e.impact_assessment == EventCategory.NEUTRAL for e in events)


# ===========================================================================
# _classify_macro_event
# ===========================================================================


class TestClassifyMacroEvent:
    def test_cpi_surprise_high_is_bearish(self) -> None:
        monitor = MacroEventMonitor()

        event = monitor._classify_macro_event(
            metric_name="cpi",
            actual=4.0,
            expected=3.0,
            timestamp=datetime.now(tz=UTC),
        )

        assert event is not None
        assert event.event_type == MacroEventType.CPI_RELEASE
        assert event.impact_assessment == EventCategory.MACRO_NEGATIVE
        assert event.is_positive_surprise is False

    def test_cpi_surprise_low_is_bullish(self) -> None:
        monitor = MacroEventMonitor()

        event = monitor._classify_macro_event(
            metric_name="cpi",
            actual=2.0,
            expected=3.0,
            timestamp=datetime.now(tz=UTC),
        )

        assert event is not None
        assert event.impact_assessment == EventCategory.MACRO_POSITIVE
        assert event.is_positive_surprise is True

    def test_fed_funds_maps_to_fomc_meeting(self) -> None:
        monitor = MacroEventMonitor()

        event = monitor._classify_macro_event(
            metric_name="fed_funds",
            actual=5.5,
            expected=5.0,
            timestamp=datetime.now(tz=UTC),
        )

        assert event is not None
        assert event.event_type == MacroEventType.FOMC_MEETING
        assert event.impact_assessment == EventCategory.MACRO_NEGATIVE

    def test_unemployment_maps_to_employment(self) -> None:
        monitor = MacroEventMonitor()

        event = monitor._classify_macro_event(
            metric_name="unemployment",
            actual=3.0,
            expected=4.0,
            timestamp=datetime.now(tz=UTC),
        )

        assert event is not None
        assert event.event_type == MacroEventType.EMPLOYMENT
        # Lower unemployment (surprise negative) => normalized_surprise<0 =>
        # is_positive_for_crypto=True per the (cpi/fed_funds/unemployment) branch
        assert event.impact_assessment == EventCategory.MACRO_POSITIVE

    def test_gdp_strong_surprise_is_positive(self) -> None:
        monitor = MacroEventMonitor()

        event = monitor._classify_macro_event(
            metric_name="gdp",
            actual=3.0,
            expected=2.0,
            timestamp=datetime.now(tz=UTC),
        )

        assert event is not None
        assert event.event_type == MacroEventType.GDP
        assert event.impact_assessment == EventCategory.MACRO_POSITIVE

    def test_gdp_weak_surprise_is_negative(self) -> None:
        monitor = MacroEventMonitor()

        event = monitor._classify_macro_event(
            metric_name="gdp",
            actual=1.0,
            expected=2.0,
            timestamp=datetime.now(tz=UTC),
        )

        assert event is not None
        assert event.impact_assessment == EventCategory.MACRO_NEGATIVE

    def test_unknown_metric_maps_to_other(self) -> None:
        monitor = MacroEventMonitor()

        event = monitor._classify_macro_event(
            metric_name="mystery_metric",
            actual=10.0,
            expected=5.0,
            timestamp=datetime.now(tz=UTC),
        )

        assert event is not None
        assert event.event_type == MacroEventType.OTHER
        # Falls through to GDP-style branch: surprise > 0 => positive
        assert event.impact_assessment == EventCategory.MACRO_POSITIVE

    def test_insignificant_surprise_is_neutral(self) -> None:
        monitor = MacroEventMonitor()

        event = monitor._classify_macro_event(
            metric_name="cpi",
            actual=3.0001,
            expected=3.0,
            timestamp=datetime.now(tz=UTC),
        )

        assert event is not None
        assert event.impact_assessment == EventCategory.NEUTRAL

    def test_description_contains_metric_name_and_values(self) -> None:
        monitor = MacroEventMonitor()

        event = monitor._classify_macro_event(
            metric_name="cpi",
            actual=4.0,
            expected=3.0,
            timestamp=datetime.now(tz=UTC),
        )

        assert event is not None
        assert "CPI" in event.description
        assert "4.00" in event.description
        assert "3.00" in event.description

    def test_zero_expected_value_uses_floor(self) -> None:
        monitor = MacroEventMonitor()

        # expected=0 -> division uses max(abs(expected), 0.001) floor
        event = monitor._classify_macro_event(
            metric_name="gdp",
            actual=1.0,
            expected=0.0,
            timestamp=datetime.now(tz=UTC),
        )

        assert event is not None
        assert event.surprise_magnitude > 0


# ===========================================================================
# get_macro_bias
# ===========================================================================


class TestGetMacroBias:
    def test_empty_events_returns_zero(self) -> None:
        monitor = MacroEventMonitor()

        assert monitor.get_macro_bias([]) == 0.0

    def test_single_positive_event_with_magnitude(self) -> None:
        monitor = MacroEventMonitor()
        event = make_event(
            timestamp=datetime.now(tz=UTC),
            surprise_magnitude=0.5,
            impact=EventCategory.MACRO_POSITIVE,
        )

        bias = monitor.get_macro_bias([event])

        assert bias > 0
        assert bias <= 1.0

    def test_single_negative_event_with_magnitude(self) -> None:
        monitor = MacroEventMonitor()
        event = make_event(
            timestamp=datetime.now(tz=UTC),
            surprise_magnitude=-0.5,
            impact=EventCategory.MACRO_NEGATIVE,
        )

        bias = monitor.get_macro_bias([event])

        assert bias < 0
        assert bias >= -1.0

    def test_positive_event_with_non_positive_magnitude_uses_default(self) -> None:
        monitor = MacroEventMonitor()
        # surprise_magnitude <= 0 but impact is POSITIVE -> default 0.2 fallback
        event = make_event(
            timestamp=datetime.now(tz=UTC),
            surprise_magnitude=-0.1,
            impact=EventCategory.MACRO_POSITIVE,
        )

        bias = monitor.get_macro_bias([event])

        assert bias > 0

    def test_negative_event_with_non_negative_magnitude_uses_default(self) -> None:
        monitor = MacroEventMonitor()
        # surprise_magnitude >= 0 but impact is NEGATIVE -> default -0.2 fallback
        event = make_event(
            timestamp=datetime.now(tz=UTC),
            surprise_magnitude=0.1,
            impact=EventCategory.MACRO_NEGATIVE,
        )

        bias = monitor.get_macro_bias([event])

        assert bias < 0

    def test_neutral_event_contributes_zero(self) -> None:
        monitor = MacroEventMonitor()
        event = make_event(
            timestamp=datetime.now(tz=UTC),
            surprise_magnitude=0.0,
            impact=EventCategory.NEUTRAL,
        )

        bias = monitor.get_macro_bias([event])

        assert bias == 0.0

    def test_older_events_decay_and_contribute_less(self) -> None:
        monitor = MacroEventMonitor()
        recent = make_event(
            timestamp=datetime.now(tz=UTC),
            surprise_magnitude=0.5,
            impact=EventCategory.MACRO_POSITIVE,
        )
        old = make_event(
            timestamp=datetime.now(tz=UTC) - timedelta(days=30),
            surprise_magnitude=0.5,
            impact=EventCategory.MACRO_POSITIVE,
        )

        bias_recent_only = monitor.get_macro_bias([recent])
        bias_with_old = monitor.get_macro_bias([recent, old])

        # Adding a heavily decayed old event should barely change the bias
        assert abs(bias_with_old - bias_recent_only) < 0.05

    def test_bias_clamped_to_range(self) -> None:
        monitor = MacroEventMonitor()
        extreme_event = make_event(
            timestamp=datetime.now(tz=UTC),
            surprise_magnitude=50.0,
            impact=EventCategory.MACRO_POSITIVE,
        )

        bias = monitor.get_macro_bias([extreme_event])

        assert bias == 1.0

    def test_bias_clamped_negative_range(self) -> None:
        monitor = MacroEventMonitor()
        extreme_event = make_event(
            timestamp=datetime.now(tz=UTC),
            surprise_magnitude=-50.0,
            impact=EventCategory.MACRO_NEGATIVE,
        )

        bias = monitor.get_macro_bias([extreme_event])

        assert bias == -1.0

    def test_extreme_age_underflow_returns_zero(self) -> None:
        monitor = MacroEventMonitor()
        # Age so large that 2**(-age_days/3.0) underflows to exactly 0.0,
        # making weight_total == 0.0 even though events is non-empty.
        ancient_event = make_event(
            timestamp=datetime.now(tz=UTC) - timedelta(days=10_000),
            surprise_magnitude=0.5,
            impact=EventCategory.MACRO_POSITIVE,
        )

        bias = monitor.get_macro_bias([ancient_event])

        assert bias == 0.0

    def test_mixed_events_average_out(self) -> None:
        monitor = MacroEventMonitor()
        positive = make_event(
            timestamp=datetime.now(tz=UTC),
            surprise_magnitude=0.3,
            impact=EventCategory.MACRO_POSITIVE,
        )
        negative = make_event(
            timestamp=datetime.now(tz=UTC),
            surprise_magnitude=-0.3,
            impact=EventCategory.MACRO_NEGATIVE,
        )

        bias = monitor.get_macro_bias([positive, negative])

        assert abs(bias) < 0.01
