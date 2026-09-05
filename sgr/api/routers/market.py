"""
SGR Market Data Router
========================
/features/{symbol}: bereits Redis-nativ (FeatureStore), keine Änderung
nötig für die sgr-api Read-Only-Zielarchitektur.

/ticker/{symbol}: TEMPORÄR AUSSER BETRIEB (501). Der bisherige Code rief
einen Live-Exchange-Adapter direkt aus dem API-Prozess auf (echter
Netzwerk-Call zu Pionex/Binance) - das ist kein bloßes app.state-
Lesbarkeitsproblem, sondern ein Architekturbruch: die API darf laut
Zielbild keine Live-Exchange-Calls mehr machen. Es existiert noch kein
Redis-Cache für rohe Ticker-Daten (nur FeatureStore für BERECHNETE
Features, keine Rohdaten wie bid/ask/volume_24h). Ein FeatureSet.close
als Ersatz auszugeben wäre ein stiller Contract-Bruch (bid/ask/volume
könnten nicht befüllt werden). Der Ticker-Cache im Worker ist daher ein
eigener, fokussierter Folge-Commit - siehe Gap-Analyse zu Commit 3.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from sgr.api.dependencies import TokenData, get_feature_store_connection, require_auth
from sgr.market_data.feature_store import FeatureStore

router = APIRouter()


@router.get("/ticker/{symbol}")
async def get_ticker(
    symbol: str,
    user: Annotated[TokenData, Depends(require_auth)],
) -> dict:
    """
    Aktueller Ticker für ein Symbol.

    Noch nicht auf die sgr-api Read-Only-Zielarchitektur migriert (siehe
    Modul-Docstring) - liefert bewusst 501 statt live einen Exchange-
    Adapter aus der API heraus aufzurufen oder erfundene/unvollständige
    Daten zurückzugeben.
    """
    raise HTTPException(
        status_code=501,
        detail=(
            "Ticker endpoint not yet migrated to the read-only API "
            "architecture. Live exchange calls from the API process are "
            "no longer permitted; a Redis-backed ticker cache written by "
            "sgr-worker is planned as a follow-up."
        ),
    )


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
