"""
SGR Risk Metrics Cache
=======================
Redis-backed Cache für die zuletzt vom RiskEngine berechneten RiskMetrics.

Hintergrund (seit sgr-api/sgr-worker-Trennung):
    RiskMetrics (VaR, Drawdown, Portfolio Heat, ...) werden ausschließlich
    vom RiskEngine im sgr-worker-Prozess berechnet (bei jedem evaluate()-
    Aufruf, siehe sgr/risk/engine.py). Die API besitzt keinen eigenen
    RiskEngine mehr und braucht rein lesenden Zugriff auf den zuletzt
    bekannten Stand, um /api/v1/risk/metrics zu bedienen.

Design-Entscheidung: Redis-Key statt neuer DB-Tabelle
    RiskMetrics sind ein AKTUELLER Zustand ("wie sieht das Risiko gerade
    aus"), keine Zeitreihe, die historisch ausgewertet werden soll (dafür
    gibt es bereits PortfolioSnapshotRepository für Portfolio-Werte über
    Zeit). Ein einzelner Redis-Key mit TTL ist daher das passende Muster -
    exakt analog zu sgr/risk/kill_switch.py (SET + fail-safe Read).

    Unterschied zum Kill Switch: RiskMetrics werden periodisch NEU
    berechnet, nicht wie der Kill-Switch-Status bewusst gesetzt und bis
    zum expliziten Reset gültig. Ein TTL verhindert, dass ein Konsument
    einen Stunden alten Snapshot fälschlich für "aktuell" hält, falls der
    Worker abgestürzt ist und keine neuen Metriken mehr schreibt.

Fail-Safe-Prinzip (wie kill_switch.py):
    - Kein injizierter Redis-Client → Schreiben/Lesen ist ein no-op bzw.
      liefert None. Bestehende RiskEngine-Nutzung ohne Redis bleibt exakt
      unverändert (keine neue Pflicht-Abhängigkeit).
    - Ein Redis-Fehler beim Schreiben darf evaluate() niemals unterbrechen
      oder verlangsamen (best-effort, geloggt, nie geworfen).
    - Ein Redis-Fehler oder fehlender Wert beim Lesen liefert None zurück.
      Der Aufrufer (z.B. der Risk-Router) muss None als "Status unbekannt"
      behandeln, NICHT als "Risiko ist null/harmlos" - dieselbe Fail-Safe-
      Semantik wie bei read_kill_switch_state_from_redis().
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sgr.core.logging import get_logger
from sgr.core.types import RiskMetrics, TradingMode

if TYPE_CHECKING:
    from redis.asyncio import Redis

log = get_logger(__name__)

_REDIS_KEY_PREFIX = "sgr:risk:metrics"
_METRICS_TTL_SECONDS = 120  # Grosszuegig ueber dem erwarteten evaluate()-Intervall


def _redis_key(trading_mode: TradingMode) -> str:
    return f"{_REDIS_KEY_PREFIX}:{trading_mode.value}"


async def publish_risk_metrics(
    redis_client: Redis | None,
    trading_mode: TradingMode,
    metrics: RiskMetrics,
) -> None:
    """
    Schreibt die zuletzt berechneten RiskMetrics nach Redis (mit TTL).

    Additiv und fail-safe: wird von RiskEngine.evaluate() nach jeder
    _compute_metrics()-Berechnung aufgerufen. Ohne redis_client (None)
    ein no-op. Ein Fehler beim Schreiben wird geloggt, aber niemals
    nach oben geworfen - darf die eigentliche Risk-Bewertung nicht
    beeinträchtigen.
    """
    if redis_client is None:
        return
    try:
        payload = json.dumps(metrics.model_dump(mode="json"))
        await redis_client.set(
            _redis_key(trading_mode), payload, ex=_METRICS_TTL_SECONDS
        )
    except Exception as e:
        log.error("risk_metrics_cache.redis_publish_failed", error=str(e))


async def read_risk_metrics_from_redis(
    redis_client: Redis,
    trading_mode: TradingMode,
) -> dict[str, Any] | None:
    """
    Rein lesender Zugriff auf die zuletzt vom Worker berechneten
    RiskMetrics - für Prozesse (z.B. sgr-api), die keinen eigenen
    RiskEngine mehr besitzen.

    Gibt None zurück, wenn:
        - noch nie Metriken geschrieben wurden (z.B. frisches Deployment),
        - der TTL abgelaufen ist (Worker berechnet seit >120s nichts Neues -
          z.B. weil er abgestürzt ist oder keine Signale verarbeitet),
        - ein Redis-Fehler auftrat.

    In allen drei Fällen ist "Status unbekannt" die korrekte Interpretation
    für den Aufrufer, nicht "kein Risiko vorhanden".
    """
    try:
        raw = await redis_client.get(_redis_key(trading_mode))
        if raw is None:
            return None
        result: dict[str, Any] = json.loads(raw)
        return result
    except Exception as e:
        log.error("risk_metrics_cache.redis_read_failed", error=str(e))
        return None
