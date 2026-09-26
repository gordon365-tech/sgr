"""
Tests für /health/live, /health/ready, /health/trading (3-Tier Model).

Migrationsstand (sgr-api Read-Only-Zielarchitektur, Commit 3):
    Alle Checks laufen jetzt über echte Konnektivitäts-Prüfungen
    (gemockte DB-Session, gemockter Redis-Client über
    app.state.feature_store.redis_client) und Redis-gelesene Zustände
    (Kill Switch über read_kill_switch_state_from_redis), NICHT mehr
    über die Anwesenheit von In-Memory-Engines in app.state
    (exchange_pool, risk_engine, execution_engine, market_data_engine).

Testziele:
1. /health/live = pure liveness (200 always)
2. /health/ready = 200 if DB+Redis ready, else 503
3. /health/trading = 200 (trading_enabled) nur wenn alle vier
   Worker-Health-Signale (aus dem Redis-Heartbeat, siehe
   sgr/monitoring/worker_health.py) "true" sind - UNABHAENGIG vom Kill
   Switch (SYSTEM HEALTH != ORDER ADMISSION BLOCKED, siehe
   sgr/api/routers/health.py Modul-Docstring, Fix 2026-09-25). Ohne
   Heartbeat bleiben die Signale "unknown" und trading_enabled bleibt
   fail-safe False. order_admission ist zusaetzlich kill-switch-abhaengig.
4. Backward compatibility: /health endpoint works
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from sgr.api.main import create_app


def _mock_feature_store(redis_client: MagicMock | None) -> MagicMock:
    """FeatureStore-Mock mit redis_client-Property wie im echten Objekt."""
    store = MagicMock()
    store.redis_client = redis_client
    return store


def _mock_redis_ok() -> AsyncMock:
    redis = AsyncMock()
    redis.ping = AsyncMock(return_value=True)
    return redis


class TestHealthLive:
    """GET /health/live - Pure process liveness."""

    def test_health_live_returns_200_always(self) -> None:
        """Liveness probe never depends on external systems."""
        app = create_app()
        client = TestClient(app)

        response = client.get("/health/live")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "alive"
        assert "timestamp" in body

    def test_health_live_no_dependencies(self) -> None:
        """No external dependencies can be checked in liveness."""
        app = create_app()
        app.state.exchange_pool = None
        app.state.feature_store = None

        client = TestClient(app)
        response = client.get("/health/live")

        assert response.status_code == 200


class TestHealthReady:
    """GET /health/ready - Readiness for traffic."""

    def test_health_ready_returns_200_when_dependencies_ready(self) -> None:
        """Ready probe returns 200 when DB and Redis are connected."""
        app = create_app()
        app.state.feature_store = _mock_feature_store(_mock_redis_ok())

        with patch(
            "sgr.api.routers.health._check_database",
            AsyncMock(return_value=(True, "connected")),
        ):
            client = TestClient(app)
            response = client.get("/health/ready")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "healthy"
        assert body["db_connected"] is True
        assert body["redis_connected"] is True
        assert body["components_initialized"] is True

    def test_health_ready_returns_503_when_db_missing(self) -> None:
        """Ready probe returns 503 when database is not connected."""
        app = create_app()
        app.state.feature_store = _mock_feature_store(_mock_redis_ok())

        with patch(
            "sgr.api.routers.health._check_database",
            AsyncMock(return_value=(False, "error: connection refused")),
        ):
            client = TestClient(app)
            response = client.get("/health/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unhealthy"
        assert body["db_connected"] is False
        assert body["redis_connected"] is True
        assert body["components_initialized"] is False

    def test_health_ready_returns_503_when_redis_missing(self) -> None:
        """Ready probe returns 503 when Redis is not connected."""
        app = create_app()
        app.state.feature_store = _mock_feature_store(None)

        with patch(
            "sgr.api.routers.health._check_database",
            AsyncMock(return_value=(True, "connected")),
        ):
            client = TestClient(app)
            response = client.get("/health/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unhealthy"
        assert body["db_connected"] is True
        assert body["redis_connected"] is False
        assert body["components_initialized"] is False

    def test_health_ready_returns_503_when_both_missing(self) -> None:
        """Ready probe returns 503 when both DB and Redis are down."""
        app = create_app()
        app.state.feature_store = None

        with patch(
            "sgr.api.routers.health._check_database",
            AsyncMock(return_value=(False, "error: connection refused")),
        ):
            client = TestClient(app)
            response = client.get("/health/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unhealthy"
        assert body["components_initialized"] is False


_HEALTHY_WORKER_HEARTBEAT = {
    "risk_engine_available": True,
    "preflight_available": True,
    "exchange_connected": True,
    "market_data_active": True,
    "updated_at": "2026-09-25T00:00:00+00:00",
}


class TestHealthTrading:
    """
    GET /health/trading - Trading pipeline operational.

    SYSTEM HEALTH (trading_enabled) wird aus dem Worker-Health-Heartbeat
    (Redis, siehe sgr/monitoring/worker_health.py) abgeleitet und ist
    bewusst UNABHAENGIG vom Kill Switch. ORDER ADMISSION (order_admission)
    ist zusaetzlich kill-switch-abhaengig - siehe Modul-Docstring von
    sgr/api/routers/health.py (Fix 2026-09-25).
    """

    def test_health_trading_reports_unknown_without_worker_heartbeat(self) -> None:
        """Kein (noch kein/abgelaufener) Worker-Heartbeat -> alle vier
        Signale bleiben fail-safe 'unknown', trading_enabled bleibt False."""
        app = create_app()
        redis_client = _mock_redis_ok()
        app.state.feature_store = _mock_feature_store(redis_client)

        with (
            patch(
                "sgr.api.routers.health.read_kill_switch_state_from_redis",
                AsyncMock(return_value={"is_active": False, "reason": None}),
            ),
            patch(
                "sgr.api.routers.health.read_worker_health_from_redis",
                AsyncMock(return_value=None),
            ),
        ):
            client = TestClient(app)
            response = client.get("/health/trading")

        assert response.status_code == 503
        body = response.json()
        assert body["trading_enabled"] is False
        assert body["exchange_connected"] == "unknown"
        assert body["risk_engine_available"] == "unknown"
        assert body["preflight_available"] == "unknown"
        assert body["market_data_active"] == "unknown"
        assert body["kill_switch_active"] is False
        assert body["order_admission"] is False
        assert body["order_admission_reason"] == "system_unhealthy"

    def test_health_trading_enabled_true_when_healthy_and_kill_switch_inactive(self) -> None:
        """Alle vier Worker-Signale 'true' und Kill Switch inaktiv ->
        trading_enabled=True (200), order_admission=True."""
        app = create_app()
        redis_client = _mock_redis_ok()
        app.state.feature_store = _mock_feature_store(redis_client)

        with (
            patch(
                "sgr.api.routers.health.read_kill_switch_state_from_redis",
                AsyncMock(return_value={"is_active": False, "reason": None}),
            ),
            patch(
                "sgr.api.routers.health.read_worker_health_from_redis",
                AsyncMock(return_value=dict(_HEALTHY_WORKER_HEARTBEAT)),
            ),
        ):
            client = TestClient(app)
            response = client.get("/health/trading")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "healthy"
        assert body["trading_enabled"] is True
        assert body["exchange_connected"] == "true"
        assert body["risk_engine_available"] == "true"
        assert body["preflight_available"] == "true"
        assert body["market_data_active"] == "true"
        assert body["order_admission"] is True
        assert body["order_admission_reason"] is None

    def test_health_trading_system_healthy_but_order_admission_blocked_by_kill_switch(
        self,
    ) -> None:
        """SYSTEM HEALTH != ORDER ADMISSION BLOCKED: ein aktiver Kill
        Switch bei sonst gesundem System haelt trading_enabled=True/200,
        blockiert aber order_admission mit reason='kill_switch'."""
        app = create_app()
        redis_client = _mock_redis_ok()
        app.state.feature_store = _mock_feature_store(redis_client)

        with (
            patch(
                "sgr.api.routers.health.read_kill_switch_state_from_redis",
                AsyncMock(return_value={"is_active": True, "reason": "manual"}),
            ),
            patch(
                "sgr.api.routers.health.read_worker_health_from_redis",
                AsyncMock(return_value=dict(_HEALTHY_WORKER_HEARTBEAT)),
            ),
        ):
            client = TestClient(app)
            response = client.get("/health/trading")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "healthy"
        assert body["trading_enabled"] is True
        assert body["kill_switch_active"] is True
        assert body["order_admission"] is False
        assert body["order_admission_reason"] == "kill_switch"

    def test_health_trading_returns_503_when_kill_switch_active_and_system_unhealthy(
        self,
    ) -> None:
        """Kill Switch aktiv UND kein Worker-Heartbeat -> weiterhin 503,
        Grund ist aber 'system_unhealthy' (nicht der Kill Switch), da
        trading_enabled bereits an den fehlenden Signalen scheitert."""
        app = create_app()
        redis_client = _mock_redis_ok()
        app.state.feature_store = _mock_feature_store(redis_client)

        with (
            patch(
                "sgr.api.routers.health.read_kill_switch_state_from_redis",
                AsyncMock(return_value={"is_active": True, "reason": "manual"}),
            ),
            patch(
                "sgr.api.routers.health.read_worker_health_from_redis",
                AsyncMock(return_value=None),
            ),
        ):
            client = TestClient(app)
            response = client.get("/health/trading")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["trading_enabled"] is False
        assert body["kill_switch_active"] is True
        assert body["order_admission"] is False
        assert body["order_admission_reason"] == "system_unhealthy"

    def test_health_trading_partial_worker_signals_keep_trading_disabled(self) -> None:
        """Nur EIN Signal false (z.B. exchange_connected) reicht, um
        trading_enabled auf False zu halten - kein Teil-Gesundheitsbonus."""
        app = create_app()
        redis_client = _mock_redis_ok()
        app.state.feature_store = _mock_feature_store(redis_client)
        heartbeat = dict(_HEALTHY_WORKER_HEARTBEAT)
        heartbeat["exchange_connected"] = False

        with (
            patch(
                "sgr.api.routers.health.read_kill_switch_state_from_redis",
                AsyncMock(return_value={"is_active": False, "reason": None}),
            ),
            patch(
                "sgr.api.routers.health.read_worker_health_from_redis",
                AsyncMock(return_value=heartbeat),
            ),
        ):
            client = TestClient(app)
            response = client.get("/health/trading")

        assert response.status_code == 503
        body = response.json()
        assert body["trading_enabled"] is False
        assert body["exchange_connected"] == "false"
        assert body["risk_engine_available"] == "true"
        assert body["order_admission_reason"] == "system_unhealthy"

    def test_health_trading_kill_switch_unknown_when_redis_unavailable(self) -> None:
        """Kein Redis -> Kill-Switch-Status ist 'unknown', nicht 'inaktiv'
        (Fail-Safe-Prinzip: unbekannt darf niemals als sicher gelten)."""
        app = create_app()
        app.state.feature_store = _mock_feature_store(None)

        client = TestClient(app)
        response = client.get("/health/trading")

        assert response.status_code == 503
        body = response.json()
        assert body["trading_enabled"] is False
        assert body["kill_switch_active"] is None
        assert body["details"]["kill_switch_active"] == "unknown"

    def test_health_trading_kill_switch_unknown_when_never_written(self) -> None:
        """Redis erreichbar, aber noch nie ein State geschrieben (frisches
        Deployment) -> ebenfalls 'unknown', kein falsches 'inaktiv'."""
        app = create_app()
        redis_client = _mock_redis_ok()
        app.state.feature_store = _mock_feature_store(redis_client)

        with patch(
            "sgr.api.routers.health.read_kill_switch_state_from_redis",
            AsyncMock(return_value=None),
        ):
            client = TestClient(app)
            response = client.get("/health/trading")

        assert response.status_code == 503
        body = response.json()
        assert body["kill_switch_active"] is None
        assert body["trading_enabled"] is False

    def test_health_trading_tenant_id_query_param_scopes_both_reads(self) -> None:
        """sgr-api ist ein gemeinsamer Prozess fuer mehrere Worker (Gordon/
        Sumo, jeweils eigene tenant_id) - ?tenant_id=<uuid> muss sowohl
        den Kill-Switch- als auch den Worker-Health-Read auf genau diesen
        Tenant scopen, nicht auf config.tenant_id (None fuer sgr-api)."""
        app = create_app()
        redis_client = _mock_redis_ok()
        app.state.feature_store = _mock_feature_store(redis_client)

        with (
            patch(
                "sgr.api.routers.health.read_kill_switch_state_from_redis",
                AsyncMock(return_value={"is_active": False, "reason": None}),
            ) as mock_ks,
            patch(
                "sgr.api.routers.health.read_worker_health_from_redis",
                AsyncMock(return_value=dict(_HEALTHY_WORKER_HEARTBEAT)),
            ) as mock_wh,
        ):
            client = TestClient(app)
            response = client.get(
                "/health/trading?tenant_id=a47d994d-35cc-4619-83bb-86fd0cb48447"
            )

        assert response.status_code == 200
        assert response.json()["trading_enabled"] is True
        assert response.json()["details"]["tenant_id"] == "a47d994d-35cc-4619-83bb-86fd0cb48447"
        mock_ks.assert_awaited_once()
        assert mock_ks.await_args.kwargs["tenant_id"] == "a47d994d-35cc-4619-83bb-86fd0cb48447"
        mock_wh.assert_awaited_once()
        assert mock_wh.await_args.kwargs["tenant_id"] == "a47d994d-35cc-4619-83bb-86fd0cb48447"

    def test_health_trading_without_tenant_id_falls_back_to_default_key(self) -> None:
        """Ohne ?tenant_id bleibt das Verhalten identisch zu vorher (kein
        Breaking Change): tenant_id=None wird durchgereicht."""
        app = create_app()
        redis_client = _mock_redis_ok()
        app.state.feature_store = _mock_feature_store(redis_client)

        with (
            patch(
                "sgr.api.routers.health.read_kill_switch_state_from_redis",
                AsyncMock(return_value={"is_active": False, "reason": None}),
            ) as mock_ks,
            patch(
                "sgr.api.routers.health.read_worker_health_from_redis",
                AsyncMock(return_value=None),
            ) as mock_wh,
        ):
            client = TestClient(app)
            response = client.get("/health/trading")

        assert response.json()["details"]["tenant_id"] == "default"
        assert mock_ks.await_args.kwargs["tenant_id"] is None
        assert mock_wh.await_args.kwargs["tenant_id"] is None


class TestHealthBackwardCompatibility:
    """GET /health - Backward compatible alias."""

    def test_health_endpoint_accessible(self) -> None:
        """Default /health endpoint still works, now backed by real
        DB/Redis connectivity checks instead of app.state engine presence."""
        app = create_app()
        app.state.feature_store = _mock_feature_store(_mock_redis_ok())

        with patch(
            "sgr.api.routers.health._check_database",
            AsyncMock(return_value=(True, "connected")),
        ):
            client = TestClient(app)
            response = client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert "components" in body
        assert body["components"]["database"] == "ok"
        assert body["components"]["redis"] == "ok"

    def test_health_endpoint_degraded_when_db_down(self) -> None:
        app = create_app()
        app.state.feature_store = _mock_feature_store(_mock_redis_ok())

        with patch(
            "sgr.api.routers.health._check_database",
            AsyncMock(return_value=(False, "error: down")),
        ):
            client = TestClient(app)
            response = client.get("/health")

        body = response.json()
        assert body["status"] == "degraded"
        assert body["components"]["database"] == "degraded"

    def test_ping_endpoint_works(self) -> None:
        """Simple ping endpoint for connectivity checks."""
        app = create_app()
        client = TestClient(app)

        response = client.get("/ping")

        assert response.status_code == 200
        body = response.json()
        assert "pong" in body
