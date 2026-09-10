"""
SGR Ticker Cache
================
Redis-backed Cache für rohe Ticker-Snapshots (bid/ask/last/volume_24h),
geschrieben vom sgr-worker, gelesen von sgr-api (GET /market/ticker/{symbol}
und WS /ws/market/{symbol}).

Hintergrund (seit sgr-api/sgr-worker-Trennung, Commit 4)
---------------------------------------------------------
Vor der Trennung riefen sgr/api/routers/market.py und
sgr/api/routers/websocket.py (ws_market) einen Live-ExchangeAdapter
direkt aus dem API-Prozess auf. Das ist seit Commit 4 nicht mehr erlaubt
(die API besitzt keinen eigenen ExchangePool mehr). Der bestehende
FeatureStore (sgr/market_data/feature_store.py) liefert nur BERECHNETE
Features (OHLCV-Aggregation + Indikatoren), keine rohen Ticker-Felder
wie bid/ask/volume_24h/change_24h_pct - ein FeatureSet.close als Ersatz
auszugeben wäre ein stiller Contract-Bruch. Beide Endpunkte lieferten
seitdem bewusst 501/"not yet available" (siehe deren Modul-Docstrings) -
dieses Modul schließt die Lücke.

Design-Entscheidung: Redis-Key statt neuer DB-Tabelle
    Wie RiskMetrics (siehe sgr/risk/metrics_cache.py, identisches
    Muster) ist ein Ticker ein AKTUELLER Zustand ("wie steht der Preis
    gerade"), keine Zeitreihe, die historisch ausgewertet werden soll
    (dafür existieren bereits Candles/TimescaleDB). Ein einzelner
    Redis-Key mit TTL pro Symbol ist daher passend.

Schreibpunkt: SymbolFeed.update() in sgr/market_data/engine.py
    Jeder Poll-Zyklus holt ohnehin frische Marktdaten für das Symbol
    (siehe engine.py Docstring zum Poll-Loop). Der Ticker-Write ist ein
    zusätzlicher, best-effort get_ticker()-Call direkt nach erfolgreichem
    Candle-Update - kein separater Poll-Loop, keine zusätzliche
    Exchange-Rate-Limit-Belastung durch eine zweite unabhängige Schleife.
    Ein fehlgeschlagener Ticker-Fetch darf den Candle/Feature-Flow
    niemals beeinträchtigen (fail-safe, wie überall in diesem Modul).

Fail-Safe-Prinzip (wie kill_switch.py / metrics_cache.py):
    - Kein injizierter Redis-Client → Schreiben/Lesen ist ein no-op bzw.
      liefert None.
    - Ein Redis- oder Exchange-Fehler beim Schreiben darf den Market-
      Data-Poll-Loop niemals unterbrechen (best-effort, geloggt, nie
      geworfen).
    - Ein Redis-Fehler oder fehlender Wert beim Lesen liefert None
      zurück. Der Aufrufer (Router/WS-Handler) muss None als "noch kein
      Ticker verfügbar" behandeln, NICHT als Fehler im eigenen Ablauf.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sgr.core.logging import get_logger

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from sgr.exchanges.base import TickerData

log = get_logger(__name__)

_REDIS_KEY_PREFIX = "sgr:market:ticker"
_TICKER_TTL_SECONDS = 60  # Grosszuegig ueber dem kuerzesten Poll-Intervall (1m: 55s)


def _redis_key(symbol: str) -> str:
    # Symbol enthaelt "/" (z.B. "BTC/USDT") - kein Problem fuer Redis-Keys,
    # aber zur Konsistenz mit dem Rest des Key-Schemas (siehe
    # feature_store.py: "features:latest:{exchange}:{symbol}:{timeframe}")
    # bleibt das Symbol hier unveraendert, nicht normalisiert.
    return f"{_REDIS_KEY_PREFIX}:{symbol}"


async def publish_ticker(
    redis_client: Redis | None,
    ticker: TickerData,
) -> None:
    """
    Schreibt den zuletzt geholten Ticker-Snapshot nach Redis (mit TTL).

    Additiv und fail-safe: wird von SymbolFeed.update() nach jedem
    erfolgreichen Candle-Update best-effort aufgerufen. Ohne
    redis_client (None) ein no-op. Ein Fehler beim Schreiben wird
    geloggt, aber niemals nach oben geworfen - darf den Market-Data-
    Poll-Loop nicht beeinträchtigen.
    """
    if redis_client is None:
        return
    try:
        payload = json.dumps(
            {
                "symbol": ticker.symbol,
                "bid": str(ticker.bid),
                "ask": str(ticker.ask),
                "last": str(ticker.last),
                "volume_24h": str(ticker.volume_24h),
                "change_24h_pct": ticker.change_24h_pct,
                "timestamp": ticker.timestamp.isoformat(),
            }
        )
        await redis_client.set(_redis_key(ticker.symbol), payload, ex=_TICKER_TTL_SECONDS)
    except Exception as e:
        log.error("ticker_cache.redis_publish_failed", symbol=ticker.symbol, error=str(e))


async def read_ticker_from_redis(
    redis_client: Redis,
    symbol: str,
) -> dict[str, Any] | None:
    """
    Rein lesender Zugriff auf den zuletzt vom Worker geschriebenen
    Ticker - für Prozesse (z.B. sgr-api), die keinen eigenen
    ExchangePool mehr besitzen.

    Gibt None zurück, wenn:
        - noch nie ein Ticker für dieses Symbol geschrieben wurde
          (z.B. Symbol nicht subscribed, oder frisches Deployment),
        - der TTL abgelaufen ist (Worker holt seit >60s keinen neuen
          Ticker mehr - z.B. abgestürzt oder Symbol deabonniert),
        - ein Redis-Fehler auftrat.

    In allen drei Fällen ist "noch kein aktueller Ticker verfügbar" die
    korrekte Interpretation für den Aufrufer, nicht ein impliziter Preis
    von 0 oder ähnliches.

    bid/ask/last/volume_24h kommen als Strings zurück (Decimal-Praezision
    ueber JSON hinweg erhalten, identisches Muster wie
    read_risk_metrics_from_redis) - der Aufrufer konvertiert bei Bedarf
    mit Decimal(...).
    """
    try:
        raw = await redis_client.get(_redis_key(symbol))
        if raw is None:
            return None
        result: dict[str, Any] = json.loads(raw)
        return result
    except Exception as e:
        log.error("ticker_cache.redis_read_failed", symbol=symbol, error=str(e))
        return None
