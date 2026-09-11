"""
SGR Worker Metrics Bridge
==========================
Macht die im sgr-worker-Prozess erzeugten Prometheus-Metriken
(sgr/monitoring/trading_metrics.py, sgr/monitoring/metrics.py via
MonitoringEngine) fuer Prometheus scrapebar, OHNE dass Prometheus
den Worker-Container direkt ansprechen muss.

Hintergrund
-----------
sgr-worker exponiert bewusst KEINEN eigenen HTTP-Port ("NO ports
exposed - internal only", siehe sgr/worker/main.py Docstring zu
_WorkerAppState und Dockerfile.worker/docker-compose.prod.yml). Ein
eigener HTTP-Server im Worker allein fuer /metrics wuerde diesem
bewussten Architekturprinzip widersprechen (ein zusaetzlicher offener
Port pro Tenant-Worker, mehr Angriffsflaeche, mehr Healthcheck-
Komplexitaet fuer einen einzelnen Read-Pfad).

Stattdessen: Push-Modell, identisches Grundmuster wie
sgr/market_data/ticker_cache.py und sgr/risk/metrics_cache.py.
    1. Jeder Worker schreibt periodisch (siehe WorkerMetricsPublisher)
       seinen kompletten aktuellen prometheus_client REGISTRY-Snapshot
       (generate_latest()) unter einem tenant-gescopten Redis-Key.
    2. sgr-api's /metrics-Handler liest zusaetzlich zu seiner eigenen,
       lokalen REGISTRY alle bekannten Worker-Snapshots aus Redis und
       haengt sie an den Textkoerper an (Prometheus-Textformat ist
       zeilenbasiert und zusammenfuegbar - siehe append_worker_metrics).

Worker-Registrierung
---------------------
Da nicht statisch bekannt ist, welche Tenants/Worker zu einem
gegebenen Zeitpunkt laufen (Tenants koennen hinzugefuegt werden, ohne
sgr-api neu zu deployen), fuehrt Redis zusaetzlich ein Set aller
aktuell bekannten Worker-Keys (SADD beim ersten Publish, SREM beim
Shutdown). /metrics iteriert dieses Set statt einer fest verdrahteten
Tenant-Liste.

Fail-Safe-Prinzip (wie ticker_cache.py/metrics_cache.py):
    - Kein injizierter Redis-Client -> Publish ist ein no-op.
    - Ein Redis-Fehler beim Schreiben wird geloggt, nie geworfen -
      darf den Trading-Lifecycle im Worker niemals beeintraechtigen.
    - Ein Redis-Fehler oder fehlender Wert beim Lesen (API-Seite)
      liefert einfach keine zusaetzlichen Zeilen - der /metrics-
      Endpoint bleibt nutzbar (mit den eigenen, wenigen API-Metriken),
      auch wenn kein Worker-Snapshot verfuegbar ist.
    - TTL auf jedem Snapshot-Key: ein abgestuerzter/gestoppter Worker,
      der SREM beim Shutdown nicht mehr ausfuehren konnte (z.B. SIGKILL),
      verschwindet nach Ablauf trotzdem aus /metrics statt einen ewig
      veralteten Snapshot zu zeigen.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sgr.core.logging import get_logger

if TYPE_CHECKING:
    from redis.asyncio import Redis

log = get_logger(__name__)

_REDIS_SNAPSHOT_PREFIX = "sgr:worker_metrics:snapshot"
_REDIS_WORKER_SET_KEY = "sgr:worker_metrics:known_workers"
_SNAPSHOT_TTL_SECONDS = 60  # Grosszuegig ueber dem Publish-Intervall (15s)
_PUBLISH_INTERVAL_SECONDS = 15.0  # Deckt sich mit Prometheus scrape_interval


def _worker_key(tenant_id: str, trading_mode: str) -> str:
    return f"{_REDIS_SNAPSHOT_PREFIX}:{tenant_id}:{trading_mode}"


class WorkerMetricsPublisher:
    """
    Hintergrund-Task im sgr-worker-Prozess: schreibt periodisch den
    lokalen Prometheus-REGISTRY-Snapshot nach Redis.

    Additiv und fail-safe: ohne redis_client (None) startet der Task
    gar nicht erst (no-op) - bestehende Deployments ohne Redis-
    Injection sind unveraendert lauffaehig, nur ohne Worker-Metriken
    in /metrics.
    """

    def __init__(
        self,
        redis_client: Redis | None,
        tenant_id: str,
        trading_mode: str,
    ) -> None:
        self._redis = redis_client
        self._key = _worker_key(tenant_id, trading_mode)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._redis is None:
            log.info("worker_metrics_publisher.skipped_no_redis")
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._publish_loop(), name="worker_metrics_publisher")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Best-effort Deregistrierung - ein fehlgeschlagener SREM hier
        # ist nicht fatal, siehe TTL-Fallback im Modul-Docstring.
        if self._redis is not None:
            try:
                await self._redis.srem(_REDIS_WORKER_SET_KEY, self._key)
                await self._redis.delete(self._key)
            except Exception as e:
                log.warning("worker_metrics_publisher.deregister_failed", error=str(e))

    async def _publish_loop(self) -> None:
        try:
            while True:
                await self._publish_once()
                await asyncio.sleep(_PUBLISH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise

    async def _publish_once(self) -> None:
        assert self._redis is not None
        try:
            from prometheus_client import REGISTRY, generate_latest

            snapshot = generate_latest(REGISTRY)
            await self._redis.set(self._key, snapshot, ex=_SNAPSHOT_TTL_SECONDS)
            await self._redis.sadd(_REDIS_WORKER_SET_KEY, self._key)
        except Exception as e:
            # Fail-safe: ein Fehler beim Metriken-Publish darf den
            # Trading-Lifecycle niemals beeintraechtigen - nur loggen.
            log.error("worker_metrics_publisher.publish_failed", error=str(e))


async def collect_worker_metrics(redis_client: Redis) -> bytes:
    """
    Liest alle bekannten Worker-Snapshots aus Redis und fuegt sie zu
    einem einzelnen Prometheus-Textformat-Koerper zusammen - fuer den
    Aufruf durch sgr-api's /metrics-Handler.

    Fail-safe: liefert bei jedem Redis-Fehler einfach b"" zurueck (der
    Aufrufer haengt das an seine eigenen Metriken an - ein leerer
    Zusatz aendert am gueltigen Prometheus-Textformat nichts). Ein
    einzelner nicht mehr lesbarer/abgelaufener Worker-Key blockiert
    nicht die anderen - wird einzeln uebersprungen statt die gesamte
    Sammlung abzubrechen.
    """
    try:
        worker_keys = await redis_client.smembers(_REDIS_WORKER_SET_KEY)
    except Exception as e:
        log.error("worker_metrics_collect.list_workers_failed", error=str(e))
        return b""

    if not worker_keys:
        return b""

    snapshots: list[bytes] = []
    for raw_key in worker_keys:
        key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
        try:
            snapshot = await redis_client.get(key)
            if snapshot is not None:
                snapshots.append(
                    snapshot if isinstance(snapshot, bytes) else snapshot.encode()
                )
            else:
                # TTL abgelaufen (Worker vermutlich gestorben, SREM nie
                # ausgefuehrt) - Set-Eintrag best-effort aufraeumen,
                # aber diesen Snapshot einfach uebersprungen behandeln.
                try:
                    await redis_client.srem(_REDIS_WORKER_SET_KEY, key)
                except Exception:
                    pass
        except Exception as e:
            log.warning("worker_metrics_collect.snapshot_read_failed", key=key, error=str(e))
            continue

    return b"\n".join(snapshots)
