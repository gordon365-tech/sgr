"""
SGR WebSocket Router
====================
Echtzeit-Streams für das Dashboard.

Streams:
    /ws/portfolio    → Portfolio-Updates (PnL, Positionen) alle 2s
    /ws/risk         → Risk-Metriken alle 5s
    /ws/market/{sym} → Preis-Ticks für ein Symbol (via Redis Pub/Sub)
    /ws/alerts       → System-Alerts und Kill-Switch-Events

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

from sgr.core.logging import get_logger

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
    """
    await websocket.accept()
    portfolio = getattr(request.app.state, "portfolio_engine", None)

    if portfolio is None:
        await websocket.send_text(json.dumps({"error": "Portfolio engine not available"}))
        await websocket.close()
        return

    log.info("ws.portfolio.connected")

    try:
        heartbeat_counter = 0
        while True:
            summary = portfolio.summary()
            positions = [
                {
                    "symbol": p.symbol.ccxt_symbol,
                    "side": p.side.value,
                    "qty": str(p.quantity),
                    "entry_price": str(p.entry_price),
                    "current_price": str(p.current_price),
                    "unrealized_pnl": str(p.unrealized_pnl),
                    "pnl_pct": round(p.pnl_pct * 100, 2),
                }
                for p in portfolio.positions
            ]

            msg = {
                "type": "portfolio_update",
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "data": {**summary, "positions": positions},
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
    """
    await websocket.accept()
    risk = getattr(request.app.state, "risk_engine", None)
    portfolio = getattr(request.app.state, "portfolio_engine", None)

    if risk is None or portfolio is None:
        await websocket.send_text(json.dumps({"error": "Engines not available"}))
        await websocket.close()
        return

    from sgr.risk.kill_switch import get_kill_switch

    ks = get_kill_switch(risk._trading_mode)

    log.info("ws.risk.connected")

    try:
        while True:
            metrics = risk._compute_metrics(
                portfolio_value=portfolio.portfolio_value,
                positions=portfolio.positions,
            )

            msg = {
                "type": "risk_update",
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "data": {
                    "portfolio_value": str(metrics.portfolio_value),
                    "daily_pnl_pct": round(metrics.daily_pnl_pct * 100, 2),
                    "drawdown_pct": round(metrics.drawdown_from_peak * 100, 2),
                    "var_95_pct": round(metrics.var_95 * 100, 4),
                    "portfolio_heat_pct": round(metrics.portfolio_heat * 100, 2),
                    "active_positions": metrics.active_positions,
                    "kill_switch_active": ks.is_active,
                    "kill_switch_reason": ks.state.reason,
                },
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
    Pollt Exchange alle Sekunde.
    """
    await websocket.accept()
    pool = getattr(request.app.state, "exchange_pool", None)

    if pool is None:
        await websocket.send_text(json.dumps({"error": "Exchange pool not available"}))
        await websocket.close()
        return

    from sgr.core.config import get_config
    from sgr.core.types import ExchangeID

    try:
        config = get_config()
    except Exception as e:
        # get_config() kann bei fehlerhafter/fehlender Konfiguration werfen
        # (z.B. pydantic-settings ValidationError). Vorher lief dieser Call
        # außerhalb jedes try/except - ein Fehler hier hätte die Exception
        # unbehandelt propagiert, statt dem bereits akzeptierten Client eine
        # saubere Fehlermeldung + Close zu liefern (siehe deferred finding:
        # api/routers/websocket.py ws_market Zeile 217).
        log.error("ws.market.config_error", symbol=symbol, error=str(e))
        await websocket.send_text(json.dumps({"error": "Server configuration error"}))
        await websocket.close()
        return

    ccxt_symbol = symbol.upper().replace("-", "/")

    log.info("ws.market.connected", symbol=ccxt_symbol)

    try:
        while True:
            try:
                adapter = pool.get(ExchangeID.PIONEX, config.trading_mode)
                ticker = await adapter.get_ticker(ccxt_symbol)

                msg = {
                    "type": "tick",
                    "symbol": ccxt_symbol,
                    "timestamp": ticker.timestamp.isoformat(),
                    "data": {
                        "bid": str(ticker.bid),
                        "ask": str(ticker.ask),
                        "last": str(ticker.last),
                        "spread_pct": round(
                            float((ticker.ask - ticker.bid) / ticker.last * 100), 4
                        ),
                        "volume_24h": str(ticker.volume_24h),
                        "change_24h_pct": round(ticker.change_24h_pct, 2),
                    },
                }
            except Exception as e:
                msg = {"type": "error", "message": str(e)}

            if not await _send_json(websocket, msg):
                break

            await asyncio.sleep(1.0)

    except WebSocketDisconnect:
        log.info("ws.market.disconnected", symbol=ccxt_symbol)
    except Exception as e:
        log.error("ws.market.error", symbol=ccxt_symbol, error=str(e))


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
