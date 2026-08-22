"""
OpenTelemetry Setup für SGR
===========================
Distributed Tracing + Metrics Integration.
"""

from __future__ import annotations

from opentelemetry import metrics, trace
from opentelemetry.exporter.jaeger import JaegerExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.asyncio import AsyncioInstrumentor
from prometheus_client import REGISTRY

from sgr.core.config import get_config
from sgr.core.logging import get_logger

log = get_logger(__name__)


def setup_tracing() -> None:
    """Konfiguriert OpenTelemetry Tracing mit Jaeger."""
    config = get_config()

    if not config.monitoring.enable_tracing:
        log.info("observability.tracing_disabled")
        return

    jaeger_host = config.monitoring.jaeger_host or "localhost"
    jaeger_port = config.monitoring.jaeger_port or 6831

    jaeger_exporter = JaegerExporter(
        agent_host_name=jaeger_host,
        agent_port=jaeger_port,
    )

    trace_provider = TracerProvider()
    trace_provider.add_span_processor(BatchSpanProcessor(jaeger_exporter))
    trace.set_tracer_provider(trace_provider)

    log.info(
        "observability.tracing_enabled",
        jaeger_host=jaeger_host,
        jaeger_port=jaeger_port,
    )


def setup_metrics() -> None:
    """Konfiguriert OpenTelemetry Metrics mit Prometheus."""
    config = get_config()

    prometheus_reader = PrometheusMetricReader(registry=REGISTRY)

    meter_provider = MeterProvider(metric_readers=[prometheus_reader])
    metrics.set_meter_provider(meter_provider)

    log.info("observability.metrics_enabled", prometheus_registry="REGISTRY")


def setup_auto_instrumentation() -> None:
    """Auto-instrumentation für FastAPI, SQL, Redis, etc."""
    try:
        FastAPIInstrumentor.instrument_app(get_fastapi_app())
        SQLAlchemyInstrumentor().instrument()
        RedisInstrumentor().instrument()
        RequestsInstrumentor().instrument()
        AsyncioInstrumentor().instrument()

        log.info("observability.auto_instrumentation_complete")
    except Exception as e:
        log.warning("observability.auto_instrumentation_error", error=str(e))


def get_fastapi_app():
    """Helper to get FastAPI app for instrumentation."""
    from sgr.api.main import app
    return app


def setup_observability() -> None:
    """Master setup function."""
    log.info("observability.setup_started")
    setup_tracing()
    setup_metrics()
    setup_auto_instrumentation()
    log.info("observability.setup_complete")
