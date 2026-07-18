"""
SGR Macro Events Monitor
=========================
Überwacht und bewertet makroökonomische Ereignisse.

Datenquellen:
    FRED API:           CPI, Fed Funds Rate, GDP, Employment
    Economic Calendar:  Upcoming events (Investopedia, Forex Factory)
    Manuelle Config:    FOMC-Termine, wichtige Dates

Impact-Logik (kontextuell, nicht Keyword-basiert):
    CPI höher als erwartet → hawkish Fed-Erwartung → bearish Crypto
    CPI niedriger als erwartet → dovish Fed-Erwartung → bullish Crypto
    FOMC Pause → bullish
    FOMC Hike → bearish
    Strong Employment → hawkish (höhere Inflation möglich) → bearish

Wichtig: Surprise > Richtung
    Fed hikes +25bp (erwartet) → neutral
    Fed hikes +50bp (surprise) → stark bearish
    Fed pausiert (nicht erwartet) → stark bullish
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sgr.core.logging import get_logger
from sgr.sentiment.types import EventCategory, MacroEvent, MacroEventType

log = get_logger(__name__)

# FRED API Base URL
_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# FRED Series IDs
_FRED_SERIES = {
    "cpi": "CPIAUCSL",  # CPI All Urban Consumers
    "fed_funds": "FEDFUNDS",  # Federal Funds Rate
    "unemployment": "UNRATE",  # Unemployment Rate
    "gdp": "GDP",  # Real GDP
}


class MacroEventMonitor:
    """
    Überwacht Makro-Ereignisse und bewertet deren Crypto-Impact.

    Kein FRED API Key benötigt für öffentliche Serien.
    """

    def __init__(self) -> None:
        self._recent_events: list[MacroEvent] = []
        self._fred_api_key: str | None = None

    def set_fred_api_key(self, key: str) -> None:
        """Setzt FRED API Key (optional, für höhere Rate Limits)."""
        self._fred_api_key = key

    async def fetch_recent_events(
        self,
        max_age_days: int = 7,
    ) -> list[MacroEvent]:
        """
        Holt aktuelle Makro-Events.
        Gibt gecachte Events zurück wenn API nicht verfügbar.
        """
        events: list[MacroEvent] = []

        # FRED Daten abrufen (CPI, Fed Funds)
        try:
            fred_events = await self._fetch_fred_data()
            events.extend(fred_events)
        except Exception as e:
            log.warning("macro_monitor.fred_failed", error=str(e))

        # Gecachte Events als Fallback
        if not events:
            events = self._recent_events

        # Filter: nur aktuelle Events
        cutoff = datetime.now(tz=UTC) - timedelta(days=max_age_days)
        fresh = [e for e in events if e.timestamp >= cutoff]

        self._recent_events = fresh
        return fresh

    async def _fetch_fred_data(self) -> list[MacroEvent]:
        """
        Holt aktuelle Daten von FRED API.
        Bewertet Surprise vs. Erwartung (gleitender Durchschnitt als Proxy).
        """
        import aiohttp

        events = []
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for metric_name, series_id in _FRED_SERIES.items():
                try:
                    params: dict[str, Any] = {
                        "series_id": series_id,
                        "limit": 3,
                        "sort_order": "desc",
                        "file_type": "json",
                    }
                    if self._fred_api_key:
                        params["api_key"] = self._fred_api_key

                    async with session.get(_FRED_BASE, params=params) as resp:
                        if resp.status != 200:
                            continue

                        data = await resp.json()
                        observations = data.get("observations", [])

                        if len(observations) < 2:
                            continue

                        latest = observations[0]
                        previous = observations[1]

                        actual = float(latest["value"]) if latest["value"] != "." else None
                        prev_val = float(previous["value"]) if previous["value"] != "." else None

                        if actual is None or prev_val is None:
                            continue

                        # Surprise: Differenz zu Vorperiode als einfache Erwartungs-Proxy
                        change = actual - prev_val
                        change / max(abs(prev_val), 0.001)

                        event = self._classify_macro_event(
                            metric_name=metric_name,
                            actual=actual,
                            expected=prev_val,
                            timestamp=datetime.fromisoformat(latest["date"]).replace(tzinfo=UTC),
                        )
                        if event:
                            events.append(event)

                except Exception as e:
                    log.debug("macro_monitor.series_error", series=series_id, error=str(e))
                    continue

        return events

    def _classify_macro_event(
        self,
        metric_name: str,
        actual: float,
        expected: float,
        timestamp: datetime,
    ) -> MacroEvent | None:
        """Klassifiziert Makro-Event und bewertet Crypto-Impact."""
        surprise = actual - expected
        normalized_surprise = surprise / max(abs(expected), 0.001)

        # Mapping: Metric → Event Type
        type_map = {
            "cpi": MacroEventType.CPI_RELEASE,
            "fed_funds": MacroEventType.FOMC_MEETING,
            "unemployment": MacroEventType.EMPLOYMENT,
            "gdp": MacroEventType.GDP,
        }
        event_type = type_map.get(metric_name, MacroEventType.OTHER)

        # Impact Assessment für Crypto
        # Höhere Inflation/Zinsen = hawkish = bearish Crypto
        # Niedrigere Inflation/Zinsen = dovish = bullish Crypto
        if metric_name in ("cpi", "fed_funds", "unemployment"):
            # Überraschend hoch → hawkish → bearish
            is_positive_for_crypto = normalized_surprise < 0
        else:  # GDP
            # Überraschend stark → riskier assets möglicherweise gut
            is_positive_for_crypto = normalized_surprise > 0

        impact = (
            EventCategory.MACRO_POSITIVE if is_positive_for_crypto else EventCategory.MACRO_NEGATIVE
        )

        # Signifikanz-Filter: nur meaningful surprises
        if abs(normalized_surprise) < 0.01:
            impact = EventCategory.NEUTRAL

        return MacroEvent(
            event_type=event_type,
            timestamp=timestamp,
            expected_value=expected,
            actual_value=actual,
            surprise_magnitude=float(normalized_surprise),
            is_positive_surprise=is_positive_for_crypto,
            impact_assessment=impact,
            description=f"{metric_name.upper()}: actual={actual:.2f}, expected={expected:.2f}",
        )

    def get_macro_bias(self, events: list[MacroEvent]) -> float:
        """
        Berechnet aggregierten Makro-Bias für Crypto (-1.0 bis +1.0).
        Ältere Events haben weniger Gewicht (Decay).
        """
        if not events:
            return 0.0

        now = datetime.now(tz=UTC)
        weighted_sum = 0.0
        weight_total = 0.0

        for event in events:
            age_days = (now - event.timestamp).days
            # Exponentieller Decay: Halbwertszeit = 3 Tage
            weight = 2 ** (-age_days / 3.0)

            # Impact → numerischer Wert
            if event.impact_assessment == EventCategory.MACRO_POSITIVE:
                impact_val = event.surprise_magnitude if event.surprise_magnitude > 0 else 0.2
            elif event.impact_assessment == EventCategory.MACRO_NEGATIVE:
                impact_val = (
                    -abs(event.surprise_magnitude) if event.surprise_magnitude < 0 else -0.2
                )
            else:
                impact_val = 0.0

            weighted_sum += impact_val * weight
            weight_total += weight

        if weight_total == 0:
            return 0.0

        bias = weighted_sum / weight_total
        return float(max(-1.0, min(1.0, bias)))
