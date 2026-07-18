"""
SGR Feature Store
=================
Redis-basierter Cache für berechnete Features.

Warum Redis?
- Sub-Millisekunde Lesezugriffe (kritisch für Live-Trading)
- TTL-basiertes Expiry (alte Features werden automatisch gelöscht)
- Pub/Sub für Feature-Update-Notifications (Strategy Engine wartet darauf)
- Kein DB-Druck für hot-path (DB nur für historische Features)

Key-Schema:
    features:latest:{exchange}:{symbol}:{timeframe}
        → Immer das neueste FeatureSet (wird überschrieben)
    features:{exchange}:{symbol}:{timeframe}:{ts}
        → Spezifischer Zeitstempel (für Backtesting)

TTL:
    1m Features:  15 Minuten  (3 Bar-Längen Puffer)
    5m Features:  1 Stunde
    1h Features:  6 Stunden
    4h Features:  24 Stunden
    1d Features:  7 Tage
"""

from __future__ import annotations

from datetime import datetime

import orjson
import redis.asyncio as aioredis

from sgr.core.config import get_config
from sgr.core.logging import get_logger
from sgr.market_data.types import FeatureSet

log = get_logger(__name__)

# TTL per Timeframe (Sekunden)
_TTL_BY_TIMEFRAME: dict[str, int] = {
    "1m": 15 * 60,
    "3m": 20 * 60,
    "5m": 60 * 60,
    "15m": 2 * 60 * 60,
    "30m": 3 * 60 * 60,
    "1h": 6 * 60 * 60,
    "2h": 12 * 60 * 60,
    "4h": 24 * 60 * 60,
    "6h": 36 * 60 * 60,
    "1d": 7 * 24 * 60 * 60,
}

_DEFAULT_TTL = 60 * 60  # 1 Stunde fallback


class FeatureStore:
    """
    Redis-backed Feature Store.

    Lifecycle: connect() → use → close()
    Oder als Singleton via get_feature_store().
    """

    def __init__(self) -> None:
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        config = get_config()
        self._redis = aioredis.from_url(
            config.redis.url,
            encoding="utf-8",
            decode_responses=False,
            max_connections=20,
        )
        await self._redis.ping()
        log.info("feature_store.connected")

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            log.info("feature_store.closed")

    def _require_connected(self) -> aioredis.Redis:
        if self._redis is None:
            raise RuntimeError("FeatureStore not connected. Call connect() first.")
        return self._redis

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def save(self, features: FeatureSet) -> None:
        """
        Speichert FeatureSet in Redis.
        Schreibt zwei Keys:
          1. Zeitstempel-spezifischer Key (für Backtesting-Replay)
          2. 'latest' Key (für Live-Trading)
        Publiziert Feature-Update Event für Strategy Engine.
        """
        redis = self._require_connected()

        ttl = _TTL_BY_TIMEFRAME.get(features.timeframe, _DEFAULT_TTL)
        payload = orjson.dumps(
            features.model_dump(mode="json"),
            option=orjson.OPT_NON_STR_KEYS,
        )

        pipe = redis.pipeline(transaction=False)

        # Timestamped key (für Replay)
        pipe.set(features.cache_key, payload, ex=ttl * 10)  # 10x TTL für History

        # Latest key (für Live-Trading, kürzeres TTL)
        pipe.set(features.latest_key, payload, ex=ttl)

        await pipe.execute()

        # Notify Strategy Engine via Pub/Sub
        channel = f"feature_update:{features.symbol.exchange.value}:{features.symbol.ccxt_symbol}:{features.timeframe}"
        await redis.publish(channel, features.latest_key.encode())

        log.debug(
            "feature_store.saved",
            symbol=str(features.symbol),
            timeframe=features.timeframe,
            timestamp=features.timestamp.isoformat(),
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_latest(
        self,
        symbol_key: str,  # z.B. "binance:BTC/USDT"
        timeframe: str,
    ) -> FeatureSet | None:
        """
        Lädt das neueste FeatureSet für ein Symbol/Timeframe.
        Gibt None zurück wenn kein Feature gecacht ist.
        """
        redis = self._require_connected()
        key = f"features:latest:{symbol_key}:{timeframe}"

        raw = await redis.get(key)
        if raw is None:
            return None

        try:
            data = orjson.loads(raw)
            return FeatureSet.model_validate(data)
        except Exception as e:
            log.error(
                "feature_store.deserialize_error",
                key=key,
                error=str(e),
            )
            return None

    async def get_at(
        self,
        symbol_key: str,
        timeframe: str,
        timestamp: datetime,
    ) -> FeatureSet | None:
        """
        Lädt FeatureSet für einen spezifischen Zeitstempel.
        Verwendet für Backtesting.
        """
        redis = self._require_connected()
        ts = int(timestamp.timestamp())
        key = f"features:{symbol_key}:{timeframe}:{ts}"

        raw = await redis.get(key)
        if raw is None:
            return None

        try:
            data = orjson.loads(raw)
            return FeatureSet.model_validate(data)
        except Exception as e:
            log.error("feature_store.deserialize_error", key=key, error=str(e))
            return None

    async def get_many_latest(
        self,
        symbol_keys: list[str],
        timeframe: str,
    ) -> dict[str, FeatureSet | None]:
        """
        Batch-Abruf mehrerer Symbole (Pipeline für Effizienz).
        """
        redis = self._require_connected()

        keys = [f"features:latest:{sk}:{timeframe}" for sk in symbol_keys]
        raw_values = await redis.mget(*keys)

        result: dict[str, FeatureSet | None] = {}
        for sk, raw in zip(symbol_keys, raw_values, strict=False):
            if raw is None:
                result[sk] = None
                continue
            try:
                data = orjson.loads(raw)
                result[sk] = FeatureSet.model_validate(data)
            except Exception:
                result[sk] = None

        return result

    async def invalidate(self, symbol_key: str, timeframe: str) -> None:
        """Löscht gecachtes Feature (z.B. nach Datenfehler)."""
        redis = self._require_connected()
        key = f"features:latest:{symbol_key}:{timeframe}"
        await redis.delete(key)
        log.info("feature_store.invalidated", key=key)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_store: FeatureStore | None = None


def get_feature_store() -> FeatureStore:
    global _store
    if _store is None:
        _store = FeatureStore()
    return _store
