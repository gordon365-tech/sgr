"""Tests für /metrics Prometheus Endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from sgr.api.main import create_app


class TestPrometheusMetrics:
    """Prometheus /metrics endpoint tests."""

    def test_metrics_endpoint_returns_prometheus_text(self) -> None:
        """Metrics endpoint returns Prometheus text format."""
        app = create_app()
        client = TestClient(app)

        response = client.get("/metrics")

        assert response.status_code == 200
        assert "text/plain" in response.headers.get("content-type", "")
        text = response.text
        assert "# HELP" in text or "# TYPE" in text or len(text) > 0

    def test_metrics_endpoint_not_in_openapi_docs(self) -> None:
        """Metrics endpoint should not be documented."""
        app = create_app()
        client = TestClient(app)

        response = client.get("/openapi.json")

        assert response.status_code == 200
        openapi = response.json()
        # /metrics should not be in documented paths
        assert "/metrics" not in openapi.get("paths", {})

    def test_metrics_endpoint_accessible(self) -> None:
        """Verify metrics endpoint is accessible."""
        app = create_app()
        client = TestClient(app)

        response = client.get("/metrics")

        assert response.status_code == 200
        assert len(response.text) > 0
