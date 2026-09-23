"""
SGR Grid Rate Limiter
========================
SGR-seitige Rate-Limit-Budgetierung fuer Futures-Grid-API-Calls (Phase J,
2026-09-23 - Architekturbericht "Futures-Grid-Strategie fuer SGR",
Abschnitt C.8: "keine SGR-seitige Rate-Limit-Budgetierung - nur ccxt-
generisches Throttling"). Ein Grid multipliziert die Anzahl der Order-
Submissions gegenueber einer einzelnen direktionalen Position (mehrere
gleichzeitige Level, jedes mit eigenem Fill/Gegenorder-Zyklus) - ccxt's
generisches enableRateLimit deckt nur die generische Request-Rate ab,
nicht Binance's tatsaechliches, striktes ORDER-Rate-Limit (getrennt und
strenger als das allgemeine Request-Limit).

Architekturprinzip (explizite Anweisung: "Keine zweite widersprüchliche
Rate-Limit-Architektur bauen. Bestehende Redis-/Ban-Koordination
wiederverwenden, sofern passend"):
    Nutzt DENSELBEN Redis-Client-Typ (redis.asyncio), den FeatureStore
    und KillSwitch bereits fuer Cross-Prozess-Koordination verwenden -
    KEIN neuer Infrastruktur-Typ. Sliding-Window-Zaehlung per
    INCR+EXPIRE (atomar innerhalb einer einzelnen Redis-Operation,
    kein Lua-Skript noetig fuer diese Grobkoernigkeit) statt eines
    echten Token-Buckets - bewusst einfach, ausreichend fuer "Bursts
    begrenzen", nicht fuer Nanosekunden-praezises Rate-Shaping.

    Ergaenzt (nicht ersetzt) die bestehende ccxt-Retry-/Backoff-Logik
    (CCXTBaseAdapter._retryable_exchange_call, IP-Ban-Erkennung in
    _map_error): dieser Limiter greift VOR jedem Submit/Poll-Call,
    verhindert dadurch ueberhaupt erst, dass ein Rate-Limit-Fehler oder
    ein Ban beim Exchange-Adapter ausgeloest wird - kein Ersatz fuer
    dessen reaktive Fehlerbehandlung, sondern eine proaktive
    Vorstufe. Kompatibel: wenn trotzdem ein RateLimitError/Ban auftritt,
    greift die bestehende Adapter-Logik unveraendert weiter.

Fail-Safe-Prinzip bei Redis-Ausfall: KEIN Hard-Fail (ein Grid darf nicht
komplett blockiert werden, nur weil Redis kurzzeitig nicht erreichbar
ist) - ein Redis-Fehler wird als "Budget nicht pruefbar" behandelt und
erlaubt den Call (fail-open fuer VERFUEGBARKEIT, nicht fuer Sicherheit -
die eigentliche Sicherheitsschicht bleibt die bestehende Risk-Engine/
Kill-Switch-Kette, die von diesem Limiter unberuehrt bleibt).
"""

from __future__ import annotations

from typing import Any

from sgr.core.logging import get_logger

log = get_logger(__name__)

_REDIS_KEY_PREFIX = "sgr:grid_rate_limit"


class GridRateLimiter:
    """
    Usage:
        limiter = GridRateLimiter(redis_client, max_calls_per_window=20, window_seconds=10)
        if not await limiter.acquire("binance", tenant_id, "order_submit"):
            # Budget erschoepft - NICHT senden, kein Retry-Sturm, best-effort
            # spaeter erneut versuchen (naechster Preis-Tick/Scheduler-Zyklus).
            return
    """

    def __init__(
        self,
        redis_client: Any,
        max_calls_per_window: int = 20,
        window_seconds: int = 10,
    ) -> None:
        self._redis = redis_client
        self._max_calls = max_calls_per_window
        self._window_seconds = window_seconds

    def _key(self, exchange: str, tenant_id: str | None, category: str) -> str:
        # Pro (Tenant, Exchange, Kategorie) getrennt budgetiert -
        # verhindert, dass Gordon und Sumo (getrennte Prozesse, aber
        # potenziell dieselbe Exchange) sich gegenseitig das Budget
        # wegnehmen, UND dass Order-Submits und Recovery-Polls
        # (unterschiedliche Kategorien) sich nicht gegenseitig blockieren.
        tenant = tenant_id or "default"
        return f"{_REDIS_KEY_PREFIX}:{tenant}:{exchange}:{category}"

    async def acquire(self, exchange: str, tenant_id: str | None, category: str) -> bool:
        """
        True = Call ist innerhalb des Budgets, darf ausgefuehrt werden.
        False = Budget fuer dieses Zeitfenster erschoepft - Aufrufer MUSS
        den Call auslassen (kein Retry-Sturm), nicht blockierend warten.
        """
        if self._redis is None:
            return True  # kein Redis injiziert - Limiter deaktiviert, siehe Docstring

        key = self._key(exchange, tenant_id, category)
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, self._window_seconds)
        except Exception as e:
            # Fail-open bei Redis-Fehler (siehe Modul-Docstring) - ein
            # Grid darf nicht komplett stillstehen, nur weil die
            # Budget-Pruefung selbst fehlschlaegt.
            log.warning("grid_rate_limiter.redis_error_fail_open", error=str(e))
            return True

        allowed = count <= self._max_calls
        if not allowed:
            log.warning(
                "grid_rate_limiter.budget_exhausted",
                exchange=exchange,
                tenant_id=tenant_id,
                category=category,
                count=count,
                max_calls=self._max_calls,
            )
            try:
                from sgr.monitoring.metrics import record_futures_grid_rate_limit_event

                record_futures_grid_rate_limit_event(
                    exchange=exchange, tenant_id=tenant_id or "default", outcome="rejected"
                )
            except Exception as metric_err:
                log.debug("grid_rate_limiter.metric_failed", error=str(metric_err))
        return allowed
