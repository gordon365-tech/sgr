"""
OpenTelemetry Setup für SGR
===========================
Distributed Tracing + Metrics Integration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opentelemetry import metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.asyncio import AsyncioInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from prometheus_client import REGISTRY

from sgr.core.config import get_config
from sgr.core.logging import get_logger

if TYPE_CHECKING:
    from fastapi import FastAPI

log = get_logger(__name__)


def setup_tracing() -> None:
    """
    Distributed Tracing (deaktiviert).

    `opentelemetry-exporter-jaeger` ist ab Version 1.21.0 auf PyPI ein
    leeres Stub-Wheel (Jaeger-Support wurde offiziell aus OpenTelemetry
    zugunsten von OTLP entfernt). Ein Fix erfordert einen Wechsel auf
    `opentelemetry-exporter-otlp-proto-grpc`, was eine kleine
    Architekturentscheidung ist (siehe deferred findings) und bewusst
    zurückgestellt wurde, bis Tracing tatsächlich benötigt wird.

    Diese Funktion ist daher ein dokumentierter No-Op, damit
    `setup_observability()` nicht am Metrics-Setup vorbei crasht.
    """
    config = get_config()

    if not config.monitoring.enable_tracing:
        log.info("observability.tracing_disabled")
        return

    log.warning(
        "observability.tracing_not_implemented",
        reason="jaeger_exporter_package_is_empty_stub_since_1.21.0",
        deferred_finding="see project memory: switch to otlp-proto-grpc when tracing is needed",
    )


def setup_metrics() -> None:
    """Konfiguriert OpenTelemetry Metrics mit Prometheus."""
    prometheus_reader = PrometheusMetricReader(registry=REGISTRY)

    meter_provider = MeterProvider(metric_readers=[prometheus_reader])
    metrics.set_meter_provider(meter_provider)

    log.info("observability.metrics_enabled", prometheus_registry="REGISTRY")


def setup_auto_instrumentation(app: FastAPI) -> None:
    """Auto-instrumentation für FastAPI, SQL, Redis, etc.

    Erwartet die bereits erstellte FastAPI-App-Instanz direkt als Parameter
    statt sie lazy über `from sgr.api.main import app` zu importieren. Das
    vermeidet eine unnötige Modul-Rückimport-Indirektion, da der Aufrufer
    (der Lifespan-Kontextmanager in api/main.py) die Instanz ohnehin bereits
    als eigenen Parameter besitzt.
    """
    try:
        FastAPIInstrumentor.instrument_app(app)
        SQLAlchemyInstrumentor().instrument()
        RedisInstrumentor().instrument()
        RequestsInstrumentor().instrument()
        AsyncioInstrumentor().instrument()

        log.info("observability.auto_instrumentation_complete")
    except Exception as e:
        log.warning("observability.auto_instrumentation_error", error=str(e))


def setup_observability(app: FastAPI) -> None:
    """Master setup function.

    Muss mit der aktiven FastAPI-App-Instanz aufgerufen werden (siehe
    setup_auto_instrumentation). Idempotent bzgl. Tracing (No-Op solange
    Finding zurückgestellt ist), aber setup_metrics()/
    setup_auto_instrumentation() dürfen nicht mehrfach pro Prozess laufen
    (OpenTelemetry Instrumentoren sind nicht re-entrant) - Aufrufer ist
    dafür verantwortlich, dies nur einmal beim App-Start aufzurufen.
    """
    log.info("observability.setup_started")
    setup_tracing()
    setup_metrics()
    setup_auto_instrumentation(app)
    log.info("observability.setup_complete")
