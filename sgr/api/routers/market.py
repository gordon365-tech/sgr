"""SGR Market Data Router"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from sgr.api.dependencies import TokenData, get_exchange_pool, get_feature_store, require_auth
from sgr.market_data.feature_store import FeatureStore

router = APIRouter()


@router.get("/ticker/{symbol}")
async def get_ticker(
    symbol: str,
    pool: Annotated[Any, Depends(get_exchange_pool)],
    user: Annotated[TokenData, Depends(require_auth)] = None,  # type: ignore
) -> dict:
    """Aktueller Ticker für ein Symbol."""
    from sgr.core.config import get_config
    from sgr.core.types import ExchangeID

    config = get_config()
    try:
        adapter = pool.get(ExchangeID.PIONEX, config.trading_mode)
        ticker = await adapter.get_ticker(symbol.upper().replace("-", "/"))
        return {
            "symbol": ticker.symbol,
            "bid": str(ticker.bid),
            "ask": str(ticker.ask),
            "last": str(ticker.last),
            "volume_24h": str(ticker.volume_24h),
            "change_24h_pct": ticker.change_24h_pct,
            "timestamp": ticker.timestamp.isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/features/{symbol}")
async def get_features(
    symbol: str,
    timeframe: str = Query(default="1h", pattern="^(1m|5m|15m|1h|4h|1d)$"),
    store: Annotated[FeatureStore, Depends(get_feature_store)] = None,  # type: ignore
    user: Annotated[TokenData, Depends(require_auth)] = None,  # type: ignore
) -> dict:
    """Aktuelle berechnete Features für ein Symbol."""
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
