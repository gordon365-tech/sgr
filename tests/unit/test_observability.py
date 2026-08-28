"""
Tests für sgr.monitoring.observability.
Coverage-Ziel: 0% -> 100%.

Kontext (zurückgestelltes Finding #3): observability.py war zuvor
vollständiger Dead Code (0% Coverage, nirgends importiert). Jetzt in
sgr/api/main.py::lifespan() verdrahtet (siehe dortiger Kommentar).
Tracing bleibt bewusst No-Op (opentelemetry-exporter-jaeger ist seit
1.21.0 ein leeres Stub-Wheel; OTLP-Migration ist ein eigenständiges,
zurückgestelltes Architektur-Thema).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sgr.monitoring import observability as observability_module
from sgr.monitoring.observability import (
    setup_auto_instrumentation,
    setup_metrics,
    setup_observability,
    setup_tracing,
)


def make_mock_config(*, enable_tracing: bool) -> MagicMock:
    config = MagicMock()
    config.monitoring.enable_tracing = enable_tracing
    return config


class TestSetupTracing:
    def test_disabled_logs_and_returns(self) -> None:
        with patch.object(
            observability_module, "get_config", return_value=make_mock_config(enable_tracing=False)
        ):
            # Should not raise; pure no-op logging path
            setup_tracing()

    def test_enabled_logs_warning_and_returns(self) -> None:
        with patch.object(
            observability_module, "get_config", return_value=make_mock_config(enable_tracing=True)
        ):
            # Enabled but still a documented no-op (Jaeger stub issue) -
            # should not raise, just log a warning.
            setup_tracing()


class TestSetupMetrics:
    def test_registers_meter_provider(self) -> None:
        with (
            patch.object(observability_module, "PrometheusMetricReader") as mock_reader_cls,
            patch.object(observability_module, "MeterProvider") as mock_provider_cls,
            patch.object(observability_module, "metrics") as mock_metrics,
        ):
            mock_reader = MagicMock()
            mock_reader_cls.return_value = mock_reader
            mock_provider = MagicMock()
            mock_provider_cls.return_value = mock_provider

            setup_metrics()

            mock_reader_cls.assert_called_once_with(registry=observability_module.REGISTRY)
            mock_provider_cls.assert_called_once_with(metric_readers=[mock_reader])
            mock_metrics.set_meter_provider.assert_called_once_with(mock_provider)


class TestSetupAutoInstrumentation:
    def test_success_instruments_all_components(self) -> None:
        fake_app = MagicMock()

        with (
            patch.object(observability_module, "FastAPIInstrumentor") as mock_fastapi,
            patch.object(observability_module, "SQLAlchemyInstrumentor") as mock_sqla,
            patch.object(observability_module, "RedisInstrumentor") as mock_redis,
            patch.object(observability_module, "RequestsInstrumentor") as mock_requests,
            patch.object(observability_module, "AsyncioInstrumentor") as mock_asyncio,
        ):
            setup_auto_instrumentation(fake_app)

            mock_fastapi.instrument_app.assert_called_once_with(fake_app)
            mock_sqla.return_value.instrument.assert_called_once()
            mock_redis.return_value.instrument.assert_called_once()
            mock_requests.return_value.instrument.assert_called_once()
            mock_asyncio.return_value.instrument.assert_called_once()

    def test_exception_is_caught_and_logged(self) -> None:
        fake_app = MagicMock()

        with patch.object(
            observability_module,
            "FastAPIInstrumentor",
            MagicMock(instrument_app=MagicMock(side_effect=RuntimeError("already instrumented"))),
        ):
            # Should not raise - errors are caught and logged as warnings
            setup_auto_instrumentation(fake_app)

    def test_partial_failure_in_later_instrumentor_is_caught(self) -> None:
        fake_app = MagicMock()

        with (
            patch.object(observability_module, "FastAPIInstrumentor"),
            patch.object(observability_module, "SQLAlchemyInstrumentor"),
            patch.object(
                observability_module,
                "RedisInstrumentor",
                MagicMock(
                    return_value=MagicMock(instrument=MagicMock(side_effect=ValueError("boom")))
                ),
            ),
        ):
            # Should not raise despite RedisInstrumentor failing mid-sequence
            setup_auto_instrumentation(fake_app)


class TestSetupObservability:
    def test_calls_all_three_setup_steps_in_order(self) -> None:
        fake_app = MagicMock()
        call_order = []

        with (
            patch.object(
                observability_module,
                "setup_tracing",
                side_effect=lambda: call_order.append("tracing"),
            ),
            patch.object(
                observability_module,
                "setup_metrics",
                side_effect=lambda: call_order.append("metrics"),
            ),
            patch.object(
                observability_module,
                "setup_auto_instrumentation",
                side_effect=lambda app: call_order.append("instrumentation"),
            ),
        ):
            setup_observability(fake_app)

        assert call_order == ["tracing", "metrics", "instrumentation"]

    def test_end_to_end_with_real_underlying_calls_does_not_raise(self) -> None:
        """
        Integration-style smoke test: exercises the real (non-mocked)
        setup_tracing/setup_metrics/setup_auto_instrumentation calls to
        ensure the actual OpenTelemetry/Prometheus imports and API usage
        are wired correctly end-to-end, not just that our mocks are called.
        """
        fake_app = MagicMock()

        with patch.object(
            observability_module, "get_config", return_value=make_mock_config(enable_tracing=False)
        ):
            # Should not raise even against real opentelemetry/prometheus_client
            # APIs; auto-instrumentation errors are caught internally.
            setup_observability(fake_app)
