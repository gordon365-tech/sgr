"""
SGR WebSocket Router
====================
Echtzeit-Streams für das Dashboard.

Streams:
    /ws/portfolio    → Portfolio-Updates (PnL, Positionen) alle 2s
    /ws/risk         → Risk-Metriken alle 5s
    /ws/market/{sym} → Preis-Ticks für ein Symbol (TEMPORÄR AUSSER BETRIEB,
                        siehe ws_market Docstring - identisches Muster wie
                        GET /api/v1/market/ticker/{symbol})
    /ws/alerts       → System-Alerts und Kill-Switch-Events

Read-Only Architektur (sgr-api Zielarchitektur, Commit 4)
-----------------------------------------------------------
Seit der sgr-api/sgr-worker-Trennung besitzt die API keinen eigenen
Trading Lifecycle mehr (siehe sgr/api/dependencies.py Modul-Docstring).
ws_portfolio liest DB (PortfolioSnapshotRepository, PositionRepository,
analog sgr/api/routers/portfolio.py), ws_risk liest Redis
(read_risk_metrics_from_redis/read_kill_switch_state_from_redis, analog
sgr/api/routers/risk.py). Kein Zugriff mehr auf app.state.portfolio_engine/
risk_engine/exchange_pool als In-Memory-Engines.

Design:
    - Heartbeat alle 30s (Verbindung aktiv halten)
    - JSON-Messages (kein Binary)
    - Client-Disconnect sauber handeln
    - Kein State auf WebSocket-Ebene (stateless)

Auth:
    Token als Query-Parameter: /ws/portfolio?token=<JWT>
    (WebSocket-Standard: Header nicht zuverlässig in allen Clients)
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect

from sgr.api.dependencies import get_redis_client_or_none
from sgr.core.logging import get_logger
from sgr.core.repositories import get_repositories

router = APIRouter()
log = get_logger(__name__)


def _json_safe(obj: Any) -> Any:
    """Konvertiert nicht-serialisierbare Typen für JSON."""
    from decimal import Decimal

    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(i) for i in obj]
    return obj


async def _send_json(ws: WebSocket, data: dict) -> bool:
    """Sendet JSON-Message. Returns False bei Disconnect."""
    try:
        await ws.send_text(json.dumps(_json_safe(data)))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Portfolio Stream
# ---------------------------------------------------------------------------


@router.websocket("/portfolio")
async def ws_portfolio(
    websocket: WebSocket,
    request: Request,
    token: str = Query(default=""),
) -> None:
    """
    Portfolio-Updates alle 2 Sekunden.
    Sendet: portfolio_value, cash, positions, unrealized_pnl

    Liest ausschliesslich aus der DB (PortfolioSnapshotRepository,
    PositionRepository) - kein Zugriff mehr auf eine In-Memory-
    PortfolioEngine im API-Prozess, analog zu GET /api/v1/portfolio/*
    (siehe sgr/api/routers/portfolio.py). Der zurueckgegebene Stand ist
    der zuletzt vom Worker persistierte Snapshot, kein Live-Wert -
    identisch zur bestehenden REST-Semantik.
    """
    await websocket.accept()

    from sgr.api.dependencies import get_trading_mode

    trading_mode = get_trading_mode()
    repos = get_repositories()

    log.info("ws.portfolio.connected")

    try:
        heartbeat_counter = 0
        while True:
            snapshot = await repos.portfolio_snapshots.get_latest(trading_mode)

            if snapshot is None:
                msg: dict[str, Any] = {
                    "type": "portfolio_update",
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                    "error": "No portfolio snapshot available yet",
                }
            else:
                positions_raw = await repos.positions.get_open_positions(trading_mode)
                positions = [
                    {
                        "symbol": p["symbol"],
                        "side": p["side"],
                        "qty": str(p["quantity"]),
                        "entry_price": str(p["entry_price"]),
                        "current_price": str(p["current_price"]),
                        "unrealized_pnl": str(p["unrealized_pnl"]),
                    }
                    for p in positions_raw
                ]

                msg = {
                    "type": "portfolio_update",
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                    "data": {
                        "portfolio_value": str(snapshot["portfolio_value"]),
                        "cash": str(snapshot["cash"]),
                        "unrealized_pnl": str(snapshot["unrealized_pnl"]),
                        "open_positions": snapshot["open_positions_count"],
                        "positions": positions,
                    },
                }

            if not await _send_json(websocket, msg):
                break

            # Heartbeat alle 15 Updates (30s)
            heartbeat_counter += 1
            if heartbeat_counter % 15 == 0:
                await _send_json(
                    websocket, {"type": "heartbeat", "ts": datetime.now(tz=UTC).isoformat()}
                )

            await asyncio.sleep(2.0)

    except WebSocketDisconnect:
        log.info("ws.portfolio.disconnected")
    except Exception as e:
        log.error("ws.portfolio.error", error=str(e))


# ---------------------------------------------------------------------------
# Risk Stream
# ---------------------------------------------------------------------------


@router.websocket("/risk")
async def ws_risk(
    websocket: WebSocket,
    request: Request,
    token: str = Query(default=""),
) -> None:
    """
    Risk-Metriken alle 5 Sekunden.

    Liest ausschliesslich aus Redis (read_risk_metrics_from_redis,
    read_kill_switch_state_from_redis, vom Worker geschrieben) - kein
    Zugriff mehr auf eine In-Memory-RiskEngine/PortfolioEngine im
    API-Prozess, analog zu GET /api/v1/risk/metrics (siehe
    sgr/api/routers/risk.py). "stale=True" bedeutet: der Worker hat seit
    ueber 120s (TTL) keine neuen Metriken geschrieben, oder noch nie -
    identisch zur bestehenden REST-Semantik.
    """
    await websocket.accept()

    from sgr.api.dependencies import get_trading_mode
    from sgr.risk.kill_switch import read_kill_switch_state_from_redis
    from sgr.risk.metrics_cache import read_risk_metrics_from_redis

    redis_client = get_redis_client_or_none(request)
    if redis_client is None:
        await websocket.send_text(json.dumps({"error": "Redis connection not available"}))
        await websocket.close()
        return

    trading_mode = get_trading_mode()

    log.info("ws.risk.connected")

    try:
        while True:
            metrics = await read_risk_metrics_from_redis(redis_client, trading_mode)
            ks_state = await read_kill_switch_state_from_redis(redis_client, trading_mode)
            kill_switch_active = bool(ks_state["is_active"]) if ks_state is not None else None

            if metrics is None:
                data = {
                    "portfolio_value": "0",
                    "daily_pnl_pct": 0.0,
                    "drawdown_pct": 0.0,
                    "var_95_pct": 0.0,
                    "portfolio_heat_pct": 0.0,
                    "active_positions": 0,
                    "kill_switch_active": bool(kill_switch_active),
                    "kill_switch_reason": ks_state.get("reason") if ks_state else None,
                    "stale": True,
                }
            else:
                data = {
                    "portfolio_value": str(metrics["portfolio_value"]),
                    "daily_pnl_pct": round(metrics["daily_pnl_pct"] * 100, 2),
                    "drawdown_pct": round(metrics["drawdown_from_peak"] * 100, 2),
                    "var_95_pct": round(metrics["var_95"] * 100, 4),
                    "portfolio_heat_pct": round(metrics["portfolio_heat"] * 100, 2),
                    "active_positions": metrics["active_positions"],
                    "kill_switch_active": bool(kill_switch_active),
                    "kill_switch_reason": ks_state.get("reason") if ks_state else None,
                    "stale": False,
                }

            msg = {
                "type": "risk_update",
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "data": data,
            }

            if not await _send_json(websocket, msg):
                break

            await asyncio.sleep(5.0)

    except WebSocketDisconnect:
        log.info("ws.risk.disconnected")
    except Exception as e:
        log.error("ws.risk.error", error=str(e))


# ---------------------------------------------------------------------------
# Market Tick Stream
# ---------------------------------------------------------------------------


@router.websocket("/market/{symbol}")
async def ws_market(
    websocket: WebSocket,
    symbol: str,
    request: Request,
    token: str = Query(default=""),
) -> None:
    """
    Live-Preis-Ticks für ein Symbol.

    TEMPORÄR AUSSER BETRIEB - identisches, bewusstes Muster wie
    GET /api/v1/market/ticker/{symbol} (siehe sgr/api/routers/market.py
    Modul-Docstring): der bisherige Code rief einen Live-Exchange-Adapter
    direkt aus dem API-Prozess auf. Seit der sgr-api/sgr-worker-Trennung
    (Commit 4) darf die API keine Live-Exchange-Calls mehr machen, und es
    existiert noch kein Redis-Cache fuer rohe Ticker-Daten (nur
    FeatureStore fuer berechnete Features, keine Rohdaten wie bid/ask/
    volume_24h). Ein Ersatz ueber FeatureSet.close waere ein stiller
    Contract-Bruch. Der Ticker-Cache im Worker bleibt bewusst ein eigener,
    fokussierter Folge-Commit (siehe Gap-Analyse zu Commit 3/4) - dieser
    Stream sendet stattdessen eine explizite "not yet available"-Meldung
    und schliesst die Verbindung, statt stillschweigend zu haengen oder
    falsche Daten zu liefern.
    """
    await websocket.accept()
    log.info("ws.market.not_yet_available", symbol=symbol)
    await _send_json(
        websocket,
        {
            "type": "error",
            "code": 501,
            "message": (
                "Market tick stream not yet migrated to the read-only API "
                "architecture. Live exchange calls from the API process are "
                "no longer permitted; a Redis-backed ticker cache written by "
                "sgr-worker is planned as a follow-up."
            ),
        },
    )
    await websocket.close()


# ---------------------------------------------------------------------------
# Alerts Stream (Kill Switch + System Events)
# ---------------------------------------------------------------------------


@router.websocket("/alerts")
async def ws_alerts(
    websocket: WebSocket,
    request: Request,
    token: str = Query(default=""),
) -> None:
    """
    System-Alerts in Echtzeit.
    Subscribed auf Redis Pub/Sub für sofortige Delivery.
    """
    await websocket.accept()
    log.info("ws.alerts.connected")

    from sgr.core.config import get_config

    try:
        config = get_config()
    except Exception as e:
        # Gleiches Muster wie ws_market: get_config() lief zuvor außerhalb
        # jedes try/except (deferred finding, analog behoben).
        log.error("ws.alerts.config_error", error=str(e))
        await websocket.send_text(json.dumps({"error": "Server configuration error"}))
        await websocket.close()
        return

    try:
        # Redis Pub/Sub für Alert-Channel
        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(config.redis.url, decode_responses=True)
        pubsub = redis_client.pubsub()
        await pubsub.subscribe("sgr:alerts", "sgr:kill_switch")

        async def listen() -> None:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    try:
                        data = json.loads(message["data"])
                        if not await _send_json(
                            websocket,
                            {
                                "type": "alert",
                                "channel": message["channel"],
                                "data": data,
                            },
                        ):
                            break
                    except Exception:
                        pass

        # Heartbeat parallel zum Listener
        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(30)
                if not await _send_json(
                    websocket,
                    {
                        "type": "heartbeat",
                        "ts": datetime.now(tz=UTC).isoformat(),
                    },
                ):
                    break

        await asyncio.gather(
            listen(),
            heartbeat(),
            return_exceptions=True,
        )

    except WebSocketDisconnect:
        log.info("ws.alerts.disconnected")
    except Exception as e:
        log.error("ws.alerts.error", error=str(e))
    finally:
        try:
            await pubsub.unsubscribe()
            await redis_client.aclose()
        except Exception:
            pass
