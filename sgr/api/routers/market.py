"""
SGR Market Data Router
========================
/features/{symbol}: bereits Redis-nativ (FeatureStore), keine Änderung
nötig für die sgr-api Read-Only-Zielarchitektur.

/ticker/{symbol}: liest aus dem Redis-Ticker-Cache (sgr/market_data/
ticker_cache.py), geschrieben von sgr-worker (SymbolFeed._update_ticker_cache,
siehe sgr/market_data/engine.py). Vor diesem Cache rief der Endpunkt einen
Live-Exchange-Adapter direkt aus dem API-Prozess auf - seit der
sgr-api/sgr-worker-Trennung (Commit 4) nicht mehr erlaubt (die API besitzt
keinen eigenen ExchangePool mehr). Liefert 404, wenn noch kein Ticker
für dieses Symbol im Cache liegt (z.B. Symbol nicht subscribed, oder der
Worker hat seit über 60s keinen neuen Ticker geschrieben - TTL-Ablauf,
siehe read_ticker_from_redis Docstring) - NICHT 501, da die Funktionalität
jetzt existiert, nur eben (noch) keine Daten für dieses spezifische Symbol
vorliegen.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from redis.asyncio import Redis

from sgr.api.dependencies import (
    TokenData,
    get_feature_store_connection,
    get_redis_client,
    require_auth,
)
from sgr.market_data.feature_store import FeatureStore
from sgr.market_data.ticker_cache import read_ticker_from_redis

router = APIRouter()


@router.get("/ticker/{symbol}")
async def get_ticker(
    symbol: str,
    user: Annotated[TokenData, Depends(require_auth)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
) -> dict:
    """
    Aktueller Ticker für ein Symbol, aus dem Redis-Cache (siehe
    Modul-Docstring). symbol wird wie bei /features/{symbol} normalisiert
    (z.B. "btc-usdt" -> "BTC/USDT"), da SymbolFeed intern mit dem
    ccxt-Symbolformat schreibt (siehe engine.py: get_ticker(self.symbol)).
    """
    normalized_symbol = symbol.upper().replace("-", "/")
    ticker = await read_ticker_from_redis(redis_client, normalized_symbol)
    if ticker is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "No ticker available for this symbol yet. Either the symbol "
                "is not subscribed by sgr-worker, or the cached value has "
                "expired (worker may be down)."
            ),
        )
    return ticker


@router.get("/features/{symbol}")
async def get_features(
    symbol: str,
    user: Annotated[TokenData, Depends(require_auth)],
    store: Annotated[FeatureStore, Depends(get_feature_store_connection)],
    timeframe: str = Query(default="1h", pattern="^(1m|5m|15m|1h|4h|1d)$"),
) -> dict:
    """Aktuelle berechnete Features für ein Symbol (bereits Redis-nativ)."""
    symbol_key = f"binance:{symbol.upper().replace('-', '/')}"
    features = await store.get_latest(symbol_key, timeframe)
    if features is None:
        raise HTTPException(status_code=404, detail="No features available for this symbol")

    ind = features.indicators
    return {
        "symbol": str(features.symbol),
        "timestamp": features.timestamp.isoformat(),
        "timeframe": timeframe,
        "close": str(features.close),
        "regime": features.regime.value,
        "indicators": {
            "rsi_14": ind.rsi_14,
            "rsi_7": ind.rsi_7,
            "macd_histogram": ind.macd_histogram,
            "adx_14": ind.adx_14,
            "atr_pct": ind.atr_pct,
            "bb_position": ind.bb_position,
            "bb_width": ind.bb_width,
            "volume_ratio": ind.volume_ratio,
        },
        "returns": {
            "1bar": features.returns_1,
            "5bar": features.returns_5,
            "10bar": features.returns_10,
            "20bar": features.returns_20,
        },
    }
