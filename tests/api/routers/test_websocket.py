"""
Tests für sgr.api.routers.websocket.

Strategie: Die vier WebSocket-Handler laufen als `while True`-Loops mit
`asyncio.sleep(...)`. Statt über den vollen FastAPI-TestClient (der in
dieser fastapi/starlette-Kombination Request-DI in WebSocket-Routen nicht
zuverlässig auflöst), rufen wir die Handler-Coroutinen direkt auf und
simulieren WebSocket + Request per AsyncMock/Fake-Objekten. Das erlaubt
präzise Kontrolle über Loop-Iterationen (Disconnect nach N Iterationen)
und schnelle Tests (asyncio.sleep wird gepatcht).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocketDisconnect

from sgr.api.routers import websocket as ws_router
from sgr.core.types import TradingMode
from sgr.risk.kill_switch import _kill_switches

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """Minimaler WebSocket-Stand-in mit kontrollierbarer send/close-Historie."""

    def __init__(self, disconnect_after: int | None = None) -> None:
        self.accepted = False
        self.closed = False
        self.sent: list[str] = []
        self._send_count = 0
        self._disconnect_after = disconnect_after

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, data: str) -> None:
        self._send_count += 1
        if self._disconnect_after is not None and self._send_count > self._disconnect_after:
            raise WebSocketDisconnect()
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    @property
    def messages(self) -> list[dict]:
        return [json.loads(s) for s in self.sent]


class FakeRequest:
    def __init__(self, **state_kwargs) -> None:
        self.app = MagicMock()
        for k, v in state_kwargs.items():
            setattr(self.app.state, k, v)


@pytest.fixture(autouse=True)
def _fast_sleep():
    """asyncio.sleep im websocket-Modul beschleunigen, damit Tests nicht real warten."""
    with patch("sgr.api.routers.websocket.asyncio.sleep", new=AsyncMock(return_value=None)):
        yield


@pytest.fixture(autouse=True)
def _clean_kill_switches():
    _kill_switches.clear()
    yield
    _kill_switches.clear()


# ---------------------------------------------------------------------------
# _json_safe / _send_json
# ---------------------------------------------------------------------------


class TestJsonSafe:
    def test_decimal_converted_to_str(self):
        assert ws_router._json_safe(Decimal("1.5")) == "1.5"

    def test_datetime_converted_to_isoformat(self):
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        assert ws_router._json_safe(dt) == dt.isoformat()

    def test_dict_recursively_converted(self):
        result = ws_router._json_safe({"a": Decimal("2"), "b": {"c": Decimal("3")}})
        assert result == {"a": "2", "b": {"c": "3"}}

    def test_list_recursively_converted(self):
        result = ws_router._json_safe([Decimal("1"), Decimal("2")])
        assert result == ["1", "2"]

    def test_plain_value_passed_through(self):
        assert ws_router._json_safe("hello") == "hello"
        assert ws_router._json_safe(42) == 42


class TestSendJson:
    async def test_returns_true_on_success(self):
        fake = FakeWebSocket()
        result = await ws_router._send_json(fake, {"type": "x"})
        assert result is True
        assert len(fake.sent) == 1

    async def test_returns_false_on_exception(self):
        fake = FakeWebSocket(disconnect_after=0)
        result = await ws_router._send_json(fake, {"type": "x"})
        assert result is False


# ---------------------------------------------------------------------------
# /ws/portfolio
# ---------------------------------------------------------------------------


class TestWsPortfolio:
    """
    Liest ausschliesslich DB (get_repositories()) statt app.state.
    Analog zu GET /api/v1/portfolio/* (siehe sgr/api/routers/portfolio.py).
    """

    async def test_no_snapshot_sends_error_message_and_continues(self):
        repos = MagicMock()
        repos.portfolio_snapshots.get_latest = AsyncMock(return_value=None)

        fake_ws = FakeWebSocket(disconnect_after=1)
        request = FakeRequest()

        with (
            patch("sgr.api.routers.websocket.get_repositories", return_value=repos),
            patch("sgr.api.dependencies.get_trading_mode", return_value=TradingMode.PAPER),
        ):
            await ws_router.ws_portfolio(fake_ws, request, token="")

        assert fake_ws.accepted is True
        assert fake_ws.messages[0]["type"] == "portfolio_update"
        assert fake_ws.messages[0]["error"] == "No portfolio snapshot available yet"

    async def test_sends_portfolio_updates_until_disconnect(self):
        repos = MagicMock()
        repos.portfolio_snapshots.get_latest = AsyncMock(
            return_value={
                "portfolio_value": Decimal("1000"),
                "cash": Decimal("500"),
                "unrealized_pnl": Decimal("15"),
                "open_positions_count": 1,
            }
        )
        repos.positions.get_open_positions = AsyncMock(
            return_value=[
                {
                    "symbol": "BTC/USDT",
                    "side": "long",
                    "quantity": Decimal("1.5"),
                    "entry_price": Decimal("100"),
                    "current_price": Decimal("110"),
                    "unrealized_pnl": Decimal("15"),
                }
            ]
        )

        fake_ws = FakeWebSocket(disconnect_after=2)
        request = FakeRequest()

        with (
            patch("sgr.api.routers.websocket.get_repositories", return_value=repos),
            patch("sgr.api.dependencies.get_trading_mode", return_value=TradingMode.PAPER),
        ):
            await ws_router.ws_portfolio(fake_ws, request, token="tok")

        assert fake_ws.accepted is True
        assert len(fake_ws.messages) == 2
        msg = fake_ws.messages[0]
        assert msg["type"] == "portfolio_update"
        assert msg["data"]["portfolio_value"] == "1000"
        assert msg["data"]["positions"][0]["symbol"] == "BTC/USDT"
        assert msg["data"]["positions"][0]["side"] == "long"

    async def test_heartbeat_sent_every_15_updates(self):
        repos = MagicMock()
        repos.portfolio_snapshots.get_latest = AsyncMock(
            return_value={
                "portfolio_value": Decimal("1000"),
                "cash": Decimal("500"),
                "unrealized_pnl": Decimal("0"),
                "open_positions_count": 0,
            }
        )
        repos.positions.get_open_positions = AsyncMock(return_value=[])

        fake_ws = FakeWebSocket(disconnect_after=16)
        request = FakeRequest()

        with (
            patch("sgr.api.routers.websocket.get_repositories", return_value=repos),
            patch("sgr.api.dependencies.get_trading_mode", return_value=TradingMode.PAPER),
        ):
            await ws_router.ws_portfolio(fake_ws, request, token="")

        heartbeats = [m for m in fake_ws.messages if m.get("type") == "heartbeat"]
        assert len(heartbeats) == 1

    async def test_generic_exception_is_caught_and_logged(self):
        repos = MagicMock()
        repos.portfolio_snapshots.get_latest = AsyncMock(side_effect=RuntimeError("boom"))

        fake_ws = FakeWebSocket()
        request = FakeRequest()

        with (
            patch("sgr.api.routers.websocket.get_repositories", return_value=repos),
            patch("sgr.api.dependencies.get_trading_mode", return_value=TradingMode.PAPER),
        ):
            await ws_router.ws_portfolio(fake_ws, request, token="")
        assert fake_ws.accepted is True

    async def test_websocket_disconnect_raised_directly_is_caught(self):
        """WebSocketDisconnect raised from outside _send_json (e.g. DB read)
        must be caught by the dedicated except WebSocketDisconnect branch."""
        repos = MagicMock()
        repos.portfolio_snapshots.get_latest = AsyncMock(side_effect=WebSocketDisconnect())

        fake_ws = FakeWebSocket()
        request = FakeRequest()

        with (
            patch("sgr.api.routers.websocket.get_repositories", return_value=repos),
            patch("sgr.api.dependencies.get_trading_mode", return_value=TradingMode.PAPER),
        ):
            await ws_router.ws_portfolio(fake_ws, request, token="")
        assert fake_ws.accepted is True


# ---------------------------------------------------------------------------
# /ws/risk
# ---------------------------------------------------------------------------


class TestWsRisk:
    """
    Liest ausschliesslich Redis (read_risk_metrics_from_redis,
    read_kill_switch_state_from_redis) statt app.state. Analog zu
    GET /api/v1/risk/metrics (siehe sgr/api/routers/risk.py).

    Tenant-Scoping (Audit nach Commit 5): ws_risk dekodiert das Token
    selbst (_decode_token), um tenant_id zu bestimmen - alle Tests unten
    mocken das, um einen gueltigen Tenant-Kontext zu simulieren, ausser
    den beiden expliziten Auth-Failure-Tests.
    """

    _TOKEN_DATA = SimpleNamespace(user_id="tenant-abc", is_admin=False)

    async def test_invalid_token_sends_error_and_closes(self):
        """Ohne gueltigen Token gibt es keinen Tenant-Kontext - die
        Verbindung wird sauber geschlossen statt eine HTTPException aus
        _decode_token() unbehandelt zu propagieren."""
        fake_ws = FakeWebSocket()
        request = FakeRequest()

        await ws_router.ws_risk(fake_ws, request, token="not-a-real-jwt")

        assert fake_ws.accepted is True
        assert fake_ws.closed is True
        assert fake_ws.messages == [{"error": "Invalid or missing auth token"}]

    async def test_no_redis_connection_sends_error_and_closes(self):
        fake_ws = FakeWebSocket()
        request = FakeRequest()

        with (
            patch("sgr.api.dependencies._decode_token", return_value=self._TOKEN_DATA),
            patch(
                "sgr.api.routers.websocket.get_redis_client_or_none", return_value=None
            ),
        ):
            await ws_router.ws_risk(fake_ws, request, token="valid")

        assert fake_ws.closed is True
        assert fake_ws.messages == [{"error": "Redis connection not available"}]

    async def test_sends_risk_updates_until_disconnect(self):
        redis_client = AsyncMock()
        fake_ws = FakeWebSocket(disconnect_after=2)
        request = FakeRequest()

        metrics = {
            "portfolio_value": Decimal("1000"),
            "daily_pnl": Decimal("10"),
            "daily_pnl_pct": 0.01,
            "drawdown_from_peak": 0.05,
            "var_95": 0.02,
            "expected_shortfall": 0.03,
            "portfolio_heat": 0.4,
            "active_positions": 2,
        }
        ks_state = {"is_active": False, "reason": None}

        with (
            patch("sgr.api.dependencies._decode_token", return_value=self._TOKEN_DATA),
            patch(
                "sgr.api.routers.websocket.get_redis_client_or_none",
                return_value=redis_client,
            ),
            patch("sgr.api.dependencies.get_trading_mode", return_value=TradingMode.PAPER),
            patch(
                "sgr.risk.metrics_cache.read_risk_metrics_from_redis",
                new=AsyncMock(return_value=metrics),
            ) as mock_metrics,
            patch(
                "sgr.risk.kill_switch.read_kill_switch_state_from_redis",
                new=AsyncMock(return_value=ks_state),
            ) as mock_ks,
        ):
            await ws_router.ws_risk(fake_ws, request, token="valid")

        assert len(fake_ws.messages) == 2
        msg = fake_ws.messages[0]
        assert msg["type"] == "risk_update"
        assert msg["data"]["portfolio_value"] == "1000"
        assert msg["data"]["daily_pnl_pct"] == 1.0
        assert msg["data"]["drawdown_pct"] == 5.0
        assert msg["data"]["active_positions"] == 2
        assert msg["data"]["kill_switch_active"] is False
        assert msg["data"]["kill_switch_reason"] is None
        assert msg["data"]["stale"] is False
        # tenant_id aus dem Token muss an beide Redis-Reads durchgereicht
        # werden - der zentrale Punkt dieses Audits.
        mock_metrics.assert_awaited_with(
            redis_client, TradingMode.PAPER, tenant_id="tenant-abc"
        )
        mock_ks.assert_awaited_with(redis_client, TradingMode.PAPER, tenant_id="tenant-abc")

    async def test_stale_when_no_metrics_written_yet(self):
        redis_client = AsyncMock()
        fake_ws = FakeWebSocket(disconnect_after=1)
        request = FakeRequest()

        with (
            patch("sgr.api.dependencies._decode_token", return_value=self._TOKEN_DATA),
            patch(
                "sgr.api.routers.websocket.get_redis_client_or_none",
                return_value=redis_client,
            ),
            patch("sgr.api.dependencies.get_trading_mode", return_value=TradingMode.PAPER),
            patch(
                "sgr.risk.metrics_cache.read_risk_metrics_from_redis",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "sgr.risk.kill_switch.read_kill_switch_state_from_redis",
                new=AsyncMock(return_value=None),
            ),
        ):
            await ws_router.ws_risk(fake_ws, request, token="valid")

        msg = fake_ws.messages[0]
        assert msg["data"]["stale"] is True
        assert msg["data"]["kill_switch_active"] is False

    async def test_reflects_active_kill_switch(self):
        redis_client = AsyncMock()
        fake_ws = FakeWebSocket(disconnect_after=1)
        request = FakeRequest()

        metrics = {
            "portfolio_value": Decimal("1000"),
            "daily_pnl": Decimal("0"),
            "daily_pnl_pct": 0.0,
            "drawdown_from_peak": 0.0,
            "var_95": 0.0,
            "expected_shortfall": 0.0,
            "portfolio_heat": 0.0,
            "active_positions": 0,
        }
        ks_state = {"is_active": True, "reason": "max_drawdown_breached"}

        with (
            patch("sgr.api.dependencies._decode_token", return_value=self._TOKEN_DATA),
            patch(
                "sgr.api.routers.websocket.get_redis_client_or_none",
                return_value=redis_client,
            ),
            patch("sgr.api.dependencies.get_trading_mode", return_value=TradingMode.PAPER),
            patch(
                "sgr.risk.metrics_cache.read_risk_metrics_from_redis",
                new=AsyncMock(return_value=metrics),
            ),
            patch(
                "sgr.risk.kill_switch.read_kill_switch_state_from_redis",
                new=AsyncMock(return_value=ks_state),
            ),
        ):
            await ws_router.ws_risk(fake_ws, request, token="valid")

        msg = fake_ws.messages[0]
        assert msg["data"]["kill_switch_active"] is True
        assert msg["data"]["kill_switch_reason"] == "max_drawdown_breached"

    async def test_generic_exception_is_caught(self):
        redis_client = AsyncMock()
        fake_ws = FakeWebSocket()
        request = FakeRequest()

        with (
            patch("sgr.api.dependencies._decode_token", return_value=self._TOKEN_DATA),
            patch(
                "sgr.api.routers.websocket.get_redis_client_or_none",
                return_value=redis_client,
            ),
            patch("sgr.api.dependencies.get_trading_mode", return_value=TradingMode.PAPER),
            patch(
                "sgr.risk.metrics_cache.read_risk_metrics_from_redis",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            await ws_router.ws_risk(fake_ws, request, token="valid")
        assert fake_ws.accepted is True

    async def test_websocket_disconnect_raised_directly_is_caught(self):
        redis_client = AsyncMock()
        fake_ws = FakeWebSocket()
        request = FakeRequest()

        with (
            patch("sgr.api.dependencies._decode_token", return_value=self._TOKEN_DATA),
            patch(
                "sgr.api.routers.websocket.get_redis_client_or_none",
                return_value=redis_client,
            ),
            patch("sgr.api.dependencies.get_trading_mode", return_value=TradingMode.PAPER),
            patch(
                "sgr.risk.metrics_cache.read_risk_metrics_from_redis",
                new=AsyncMock(side_effect=WebSocketDisconnect()),
            ),
        ):
            await ws_router.ws_risk(fake_ws, request, token="valid")
        assert fake_ws.accepted is True


# ---------------------------------------------------------------------------
# /ws/market/{symbol}
# ---------------------------------------------------------------------------


class TestWsMarket:
    """
    Liest aus dem Redis-Ticker-Cache (identisches Read-Only-Muster wie
    ws_risk, siehe sgr/market_data/ticker_cache.py). Kein Live-Exchange-
    Call mehr aus dem API-Prozess.
    """

    async def test_no_redis_sends_error_and_closes(self):
        """Ohne Redis-Verbindung: sauberer Abbruch, identisches Muster
        wie ws_risk.test_no_redis_..."""
        fake_ws = FakeWebSocket()
        request = FakeRequest()

        with patch(
            "sgr.api.routers.websocket.get_redis_client_or_none",
            return_value=None,
        ):
            await ws_router.ws_market(fake_ws, "btc-usdt", request, token="")

        assert fake_ws.accepted is True
        assert fake_ws.closed is True
        assert len(fake_ws.messages) == 1
        assert "error" in fake_ws.messages[0]

    async def test_ticker_available_sends_normalized_tick(self):
        """Symbol wird normalisiert ('btc-usdt' -> 'BTC/USDT'), Cache-Wert
        wird unveraendert unter 'data' durchgereicht."""
        fake_ws = FakeWebSocket(disconnect_after=1)
        request = FakeRequest()
        redis_client = MagicMock()
        cached_ticker = {
            "symbol": "BTC/USDT",
            "bid": "50000",
            "ask": "50010",
            "last": "50005",
            "volume_24h": "1234.5",
            "change_24h_pct": 1.2,
            "timestamp": "2026-01-01T00:00:00+00:00",
        }

        with (
            patch(
                "sgr.api.routers.websocket.get_redis_client_or_none",
                return_value=redis_client,
            ),
            patch(
                "sgr.market_data.ticker_cache.read_ticker_from_redis",
                new=AsyncMock(return_value=cached_ticker),
            ),
        ):
            await ws_router.ws_market(fake_ws, "btc-usdt", request, token="")

        assert fake_ws.accepted is True
        assert len(fake_ws.messages) == 1
        msg = fake_ws.messages[0]
        assert msg["type"] == "market_tick"
        assert msg["symbol"] == "BTC/USDT"
        assert msg["stale"] is False
        assert msg["data"] == cached_ticker

    async def test_no_cached_ticker_marks_stale_without_closing(self):
        """Kein TTL-gueltiger Cache-Eintrag (noch nie geschrieben oder
        abgelaufen) darf die Verbindung NICHT beenden - nur stale=True
        signalisieren, siehe ws_market Docstring."""
        fake_ws = FakeWebSocket(disconnect_after=1)
        request = FakeRequest()
        redis_client = MagicMock()

        with (
            patch(
                "sgr.api.routers.websocket.get_redis_client_or_none",
                return_value=redis_client,
            ),
            patch(
                "sgr.market_data.ticker_cache.read_ticker_from_redis",
                new=AsyncMock(return_value=None),
            ),
        ):
            await ws_router.ws_market(fake_ws, "eth-usdt", request, token="")

        assert len(fake_ws.messages) == 1
        msg = fake_ws.messages[0]
        assert msg["stale"] is True
        assert msg["data"] is None
        # Kein Fehler, kein direktes close() durch die Fehlerbehandlung -
        # die Loop wurde nur durch den simulierten Disconnect beim
        # zweiten send beendet (siehe disconnect_after=0), nicht durch
        # aktives Schliessen wegen "stale".
        assert fake_ws.closed is False

    async def test_disconnect_ends_loop_cleanly(self):
        fake_ws = FakeWebSocket(disconnect_after=1)
        request = FakeRequest()
        redis_client = MagicMock()

        with (
            patch(
                "sgr.api.routers.websocket.get_redis_client_or_none",
                return_value=redis_client,
            ),
            patch(
                "sgr.market_data.ticker_cache.read_ticker_from_redis",
                new=AsyncMock(return_value=None),
            ),
        ):
            await ws_router.ws_market(fake_ws, "btc-usdt", request, token="")

        assert len(fake_ws.messages) == 1


# ---------------------------------------------------------------------------
# /ws/alerts
# ---------------------------------------------------------------------------


class TestWsAlerts:
    async def test_get_config_error_sends_error_and_closes(self):
        """Same fix as ws_market: get_config() failure must be caught
        gracefully instead of propagating unhandled (deferred finding)."""
        fake_ws = FakeWebSocket()
        request = FakeRequest()

        with patch(
            "sgr.core.config.get_config",
            side_effect=RuntimeError("invalid configuration"),
        ):
            await ws_router.ws_alerts(fake_ws, request, token="")

        assert fake_ws.closed is True
        assert fake_ws.messages == [{"error": "Server configuration error"}]

    async def test_relays_pubsub_messages_and_unsubscribes(self):
        fake_ws = FakeWebSocket(disconnect_after=1)

        messages = [
            {"type": "message", "channel": "sgr:alerts", "data": json.dumps({"level": "warn"})},
        ]

        async def fake_listen():
            for m in messages:
                yield m
            # Keep the generator "open" briefly; loop exits via WebSocketDisconnect
            # raised from _send_json's underlying send_text once disconnect_after hit.
            import asyncio as _asyncio

            await _asyncio.sleep(0)
            yield {
                "type": "message",
                "channel": "sgr:alerts",
                "data": json.dumps({"level": "info"}),
            }

        pubsub = AsyncMock()
        pubsub.subscribe = AsyncMock()
        pubsub.listen = MagicMock(return_value=fake_listen())
        pubsub.unsubscribe = AsyncMock()

        redis_client = AsyncMock()
        redis_client.pubsub = MagicMock(return_value=pubsub)
        redis_client.aclose = AsyncMock()

        request = FakeRequest()

        with (
            patch("sgr.core.config.get_config") as mock_cfg,
            patch("redis.asyncio.from_url", return_value=redis_client),
        ):
            mock_cfg.return_value.redis.url = "redis://localhost:6379"
            await ws_router.ws_alerts(fake_ws, request, token="")

        pubsub.subscribe.assert_awaited_once_with("sgr:alerts", "sgr:kill_switch")
        pubsub.unsubscribe.assert_awaited()
        redis_client.aclose.assert_awaited()
        assert any(m.get("type") == "alert" for m in fake_ws.messages)

    async def test_malformed_pubsub_message_is_ignored(self):
        fake_ws = FakeWebSocket(disconnect_after=0)

        async def fake_listen():
            yield {"type": "message", "channel": "sgr:alerts", "data": "not-json"}

        pubsub = AsyncMock()
        pubsub.subscribe = AsyncMock()
        pubsub.listen = MagicMock(return_value=fake_listen())
        pubsub.unsubscribe = AsyncMock()

        redis_client = AsyncMock()
        redis_client.pubsub = MagicMock(return_value=pubsub)
        redis_client.aclose = AsyncMock()

        request = FakeRequest()

        with (
            patch("sgr.core.config.get_config") as mock_cfg,
            patch("redis.asyncio.from_url", return_value=redis_client),
        ):
            mock_cfg.return_value.redis.url = "redis://localhost:6379"
            # Should not raise despite malformed JSON.
            await ws_router.ws_alerts(fake_ws, request, token="")

        assert fake_ws.accepted is True

    async def test_non_message_pubsub_events_skipped(self):
        fake_ws = FakeWebSocket(disconnect_after=1)

        async def fake_listen():
            yield {"type": "subscribe", "channel": "sgr:alerts", "data": 1}
            yield {"type": "message", "channel": "sgr:alerts", "data": json.dumps({"ok": True})}

        pubsub = AsyncMock()
        pubsub.subscribe = AsyncMock()
        pubsub.listen = MagicMock(return_value=fake_listen())
        pubsub.unsubscribe = AsyncMock()

        redis_client = AsyncMock()
        redis_client.pubsub = MagicMock(return_value=pubsub)
        redis_client.aclose = AsyncMock()

        request = FakeRequest()

        with (
            patch("sgr.core.config.get_config") as mock_cfg,
            patch("redis.asyncio.from_url", return_value=redis_client),
        ):
            mock_cfg.return_value.redis.url = "redis://localhost:6379"
            await ws_router.ws_alerts(fake_ws, request, token="")

        assert any(m.get("type") == "alert" for m in fake_ws.messages)

    async def test_connection_error_is_caught_and_cleanup_still_attempted(self):
        fake_ws = FakeWebSocket()
        request = FakeRequest()

        with (
            patch("sgr.core.config.get_config") as mock_cfg,
            patch("redis.asyncio.from_url", side_effect=RuntimeError("no redis")),
        ):
            mock_cfg.return_value.redis.url = "redis://localhost:6379"
            # Should not raise; pubsub/redis_client never got assigned, cleanup's
            # own except-pass must absorb the NameError/AttributeError.
            await ws_router.ws_alerts(fake_ws, request, token="")

        assert fake_ws.accepted is True

    async def test_websocket_disconnect_during_heartbeat_handled(self):
        """Heartbeat send fails immediately (disconnect); listen() finishes right after."""
        fake_ws = FakeWebSocket(disconnect_after=0)

        async def fake_listen():
            # Empty async generator: listen() finishes immediately, gather()
            # then only waits on the heartbeat task, which fails fast because
            # every send_text call raises (disconnect_after=0).
            return
            yield  # pragma: no cover - unreachable, keeps this an async generator

        pubsub = AsyncMock()
        pubsub.subscribe = AsyncMock()
        pubsub.listen = MagicMock(return_value=fake_listen())
        pubsub.unsubscribe = AsyncMock()

        redis_client = AsyncMock()
        redis_client.pubsub = MagicMock(return_value=pubsub)
        redis_client.aclose = AsyncMock()

        request = FakeRequest()

        with (
            patch("sgr.core.config.get_config") as mock_cfg,
            patch("redis.asyncio.from_url", return_value=redis_client),
        ):
            mock_cfg.return_value.redis.url = "redis://localhost:6379"
            await ws_router.ws_alerts(fake_ws, request, token="")

        # Cleanup still ran.
        redis_client.aclose.assert_awaited()


class TestWsAlertsDisconnect:
    async def test_websocket_disconnect_from_subscribe_is_caught(self):
        """WebSocketDisconnect raised directly from pubsub.subscribe() (outside
        the gather's return_exceptions=True boundary) hits the outer except."""
        fake_ws = FakeWebSocket()

        pubsub = AsyncMock()
        pubsub.subscribe = AsyncMock(side_effect=WebSocketDisconnect())
        pubsub.unsubscribe = AsyncMock()

        redis_client = AsyncMock()
        redis_client.pubsub = MagicMock(return_value=pubsub)
        redis_client.aclose = AsyncMock()

        request = FakeRequest()

        with (
            patch("sgr.core.config.get_config") as mock_cfg,
            patch("redis.asyncio.from_url", return_value=redis_client),
        ):
            mock_cfg.return_value.redis.url = "redis://localhost:6379"
            await ws_router.ws_alerts(fake_ws, request, token="")

        redis_client.aclose.assert_awaited()
