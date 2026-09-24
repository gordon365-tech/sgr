"""
SGR Worker Health Cache
========================
Redis-backed periodischer Health-Heartbeat vom sgr-worker-Prozess, lesbar
vom sgr-api-Prozess - schliesst die im Modul-Docstring von
sgr/api/routers/health.py dokumentierte Luecke: exchange_connected,
preflight_available und risk_engine_available waren dort seit jeher hart
auf "unknown" codiert, weil der Worker diese Zustaende nirgends
publizierte.

Design-Entscheidung: eigenes Modul statt Erweiterung von
sgr/risk/metrics_cache.py
    RiskMetrics (dort) werden NUR bei jedem RiskEngine.evaluate()-Aufruf
    geschrieben - waehrend eines aktiven Kill Switch (der evaluate() ganz
    am Anfang, VOR jeder Metrik-Berechnung, abbricht - siehe
    sgr/risk/engine.py Schritt 1) oder einfach in einer signalarmen Phase
    koennte dieser Wert daher laenger als die TTL nicht aktualisiert
    werden, OBWOHL der Worker-Prozess selbst voellig gesund ist. Ein
    "Risk Engine nicht verfuegbar"-Fehlalarm allein wegen fehlender
    Trading-Aktivitaet waere genau der Fehler, den dieser Fix beheben
    soll (siehe Health-Router-Docstring "SYSTEM HEALTH != ORDER
    ADMISSION BLOCKED"). Dieser Heartbeat wird stattdessen periodisch
    vom bestehenden MonitoringEngine-Loop geschrieben (aktuell alle 10s,
    siehe sgr/monitoring/engine.py), komplett unabhaengig von Trading-
    Aktivitaet oder Kill-Switch-Zustand.

Fail-Safe-Prinzip (wie kill_switch.py/metrics_cache.py):
    - Kein injizierter Redis-Client -> Schreiben/Lesen ist ein no-op bzw.
      liefert None.
    - Ein Redis-Fehler beim Schreiben darf den Monitoring-Loop nie
      unterbrechen (best-effort, geloggt, nie geworfen).
    - TTL knapp ueber dem Collect-Intervall: ein abgestuerzter/
      haengender Worker faellt automatisch auf "unknown" zurueck, statt
      seinen letzten (potenziell laengst veralteten) "gesund"-Zustand
      unbegrenzt weiterzumelden.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sgr.core.logging import get_logger
from sgr.core.types import TradingMode

if TYPE_CHECKING:
    from redis.asyncio import Redis

log = get_logger(__name__)

_REDIS_KEY_PREFIX = "sgr:worker_health"
# Grosszuegig ueber dem Standard-Collect-Intervall (10s, siehe
# MonitoringEngine.__init__ interval_seconds Default) - ein einzelner
# verpasster Zyklus (z.B. durch einen kurzen GC-Pause/Redis-Hickup) soll
# nicht sofort als "Worker down" gelten, aber ein wirklich abgestuerzter
# Prozess muss innerhalb weniger Zyklen als "unknown" sichtbar werden.
_HEALTH_TTL_SECONDS = 45


def _redis_key(trading_mode: TradingMode, tenant_id: str | None) -> str:
    # Identisches Tenant-Scoping-Muster wie kill_switch.py/metrics_cache.py.
    if tenant_id is None:
        return f"{_REDIS_KEY_PREFIX}:{trading_mode.value}"
    return f"{_REDIS_KEY_PREFIX}:{tenant_id}:{trading_mode.value}"


async def publish_worker_health(
    redis_client: Redis | None,
    trading_mode: TradingMode,
    *,
    risk_engine_available: bool,
    preflight_available: bool,
    exchange_connected: bool | None,
    market_data_active: bool,
    tenant_id: str | None = None,
) -> None:
    """
    Schreibt einen Health-Heartbeat nach Redis (mit TTL). Additiv und
    fail-safe: aufgerufen von MonitoringEngine._collect() bei jedem
    periodischen Zyklus, unabhaengig von Trading-/Signal-Aktivitaet.

    exchange_connected=None bedeutet "kein Exchange-Pool zum Pruefen
    injiziert" (z.B. Backtesting-Kontext) - wird beim Lesen als
    "unknown" interpretiert, NICHT als False (kein erfundener
    Fehlerzustand).
    """
    if redis_client is None:
        return
    try:
        payload = json.dumps(
            {
                "risk_engine_available": risk_engine_available,
                "preflight_available": preflight_available,
                "exchange_connected": exchange_connected,
                "market_data_active": market_data_active,
                "updated_at": datetime.now(tz=UTC).isoformat(),
            }
        )
        await redis_client.set(
            _redis_key(trading_mode, tenant_id), payload, ex=_HEALTH_TTL_SECONDS
        )
    except Exception as e:
        log.error("worker_health.redis_publish_failed", error=str(e))


async def read_worker_health_from_redis(
    redis_client: Redis,
    trading_mode: TradingMode,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """
    Rein lesender Zugriff fuer sgr-api. Gibt None zurueck, wenn noch nie
    geschrieben wurde, die TTL abgelaufen ist (Worker vermutlich down/
    haengend), oder bei einem Redis-Fehler - in allen Faellen ist
    "Status unbekannt" die korrekte Interpretation, nicht "ungesund"
    UND nicht "gesund" (siehe Health-Router: unknown -> konservativ
    False fuer trading_enabled).
    """
    try:
        raw = await redis_client.get(_redis_key(trading_mode, tenant_id))
        if raw is None:
            return None
        result: dict[str, Any] = json.loads(raw)
        return result
    except Exception as e:
        log.error("worker_health.redis_read_failed", error=str(e))
        return None
