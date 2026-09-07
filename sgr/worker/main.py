"""
SGR Trading Worker
==================
Separater Container für asynchrone Trading-Verarbeitung.

Dieser Worker ist unabhängig von der REST API und verarbeitet:
- CandleEvents vom Event Bus
- Trading-Zyklen automatisch oder on-demand
- Recovery nach Crash
- Lifecycle-Management mit korrekter Signalbehandlung

Der Worker ist stateless bezgl. der Datenebene (DB ist Single Source of Truth),
kann aber lokale In-Memory State wie Peak Value oder Cooldown haltiger (wie
RiskEngine/ExecutionEngine auch heute).

WICHTIG:
- Worker und API sollten NICHT in demselben Container laufen
- Ein Neustart des API sollte den Worker NICHT beeinflussen
- Ein Neustart des Worker sollte KEINE Orders duplizieren (Idempotency Keys!)
- SIGTERM sollte graceful shutdown einleiten
- SIGKILL sollte verarbeitet werden (via tini)
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path
from typing import Any

from sgr.api.main import AppState, lifespan
from sgr.core.config import get_config
from sgr.core.logging import get_logger, setup_logging
from sgr.core.types import Environment

logger = get_logger(__name__)

# Datei-basierter Liveness-Marker fuer den Docker-HEALTHCHECK.
#
# Der Worker exponiert bewusst keinen HTTP-Port (siehe docker-compose.prod.yml:
# "NO ports exposed - internal only"). Ein vom sgr-api-Image geerbter
# HTTP-Healthcheck gegen localhost:8000 schlaegt deshalb immer fehl, obwohl
# der Worker-Prozess funktional laeuft (docker inspect zeigte:
# "curl: (7) Failed to connect to localhost port 8000").
#
# Statt HTTP wird hier ein Heartbeat-File verwendet: sobald der Worker
# betriebsbereit ist (lifespan()-Init abgeschlossen), wird die Datei
# angelegt und danach periodisch aktualisiert. Der HEALTHCHECK in
# Dockerfile.worker prueft Existenz + Alter dieser Datei.
HEARTBEAT_PATH = Path("/tmp/worker_heartbeat")
HEARTBEAT_INTERVAL_SECONDS = 15


class _WorkerAppState:
    """
    Kompatibilitaetsadapter: minimaler App-Stand-in fuer lifespan(), das
    intern app.state.xyz schreibt/liest. Der Worker hat kein echtes
    FastAPI-App-Objekt, braucht aber denselben Attribut-Speicherort wie
    die API, weil lifespan() strukturell weiterhin an app.state gebunden
    ist (siehe sgr/api/main.py).

    WICHTIG - bewusst NICHT als erledigt zu verstehen: Commit 4 loest nur
    die Verdoppelung des Trading Lifecycle zwischen sgr-api und
    sgr-worker (siehe role-Parameter in lifespan()), NICHT die
    zugrundeliegende Kopplung von lifespan() an app.state als
    Speicherort. Diese vollstaendige State-Entkopplung (lifespan() gibt
    einen eigenen State-Container zurueck statt in app.state zu
    schreiben, wovon dann auch sgr/api/dependencies.py und alle Router
    betroffen waeren) ist weiterhin eine offene, nicht angegangene
    Architekturfrage - siehe Entscheidung zu Commit 4 (Option A vs. B).
    Dieser Adapter ist der bewusst gewaehlte Zwischenzustand, kein
    Uebergangs-Hack, der "eigentlich schon erledigt" waere.
    """

    def __init__(self, state: AppState) -> None:
        self.state = state


class TradingWorker:
    """Hauptklasse für Trading Worker."""

    def __init__(self) -> None:
        self.config = get_config()
        self.app_state = AppState()
        self._shutdown_event = asyncio.Event()
        self._running = False
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Startet den Worker."""
        # Logging Setup
        setup_logging(
            log_level=self.config.monitoring.log_level,
            json_output=self.config.environment == Environment.PRODUCTION,
            trading_mode=self.config.trading_mode,
        )

        logger.info(
            "worker.starting",
            version=self.config.version,
            environment=self.config.environment.value,
            trading_mode=self.config.trading_mode.value,
        )

        # Signalhandler registrieren
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._signal_handler)

        self._running = True

        # Lifespan Context: initialisiert den vollstaendigen Trading
        # Lifecycle (role="worker", siehe sgr/api/main.py lifespan()
        # Docstring). Der Worker ist seit Commit 4 alleiniger Owner
        # dieser Komponenten - sgr-api laeuft mit role="api" und startet
        # sie nicht mehr (siehe _api_lifespan() in sgr/api/main.py).
        async with lifespan(_WorkerAppState(self.app_state), role="worker"):  # type: ignore[arg-type]
            logger.info("worker.ready")
            self._write_heartbeat()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            # Worker hauptschleife
            try:
                await self._shutdown_event.wait()
            except asyncio.CancelledError:
                logger.info("worker.cancelled")
                raise
            finally:
                if self._heartbeat_task is not None:
                    self._heartbeat_task.cancel()
                    try:
                        await self._heartbeat_task
                    except asyncio.CancelledError:
                        pass
                self._remove_heartbeat()

        logger.info("worker.stopped")

    def _write_heartbeat(self) -> None:
        """Schreibt/aktualisiert den Heartbeat-Marker fuer den Healthcheck."""
        try:
            HEARTBEAT_PATH.write_text(str(asyncio.get_event_loop().time()))
        except OSError as e:
            # Fail-safe: ein Heartbeat-Schreibfehler darf den Trading-
            # Lifecycle nicht blockieren, nur den Healthcheck betreffen.
            logger.warning("worker.heartbeat_write_failed", error=str(e))

    def _remove_heartbeat(self) -> None:
        """Entfernt den Heartbeat-Marker beim Shutdown, damit der
        Healthcheck einen gestoppten Worker korrekt als unhealthy zeigt."""
        try:
            HEARTBEAT_PATH.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("worker.heartbeat_cleanup_failed", error=str(e))

    async def _heartbeat_loop(self) -> None:
        """Aktualisiert den Heartbeat-Marker periodisch, solange der
        Worker laeuft. Ein hängender Event-Loop (z.B. Deadlock in der
        Trading-Hauptschleife) fuehrt dazu, dass der Marker veraltet und
        der Healthcheck den Container als unhealthy erkennt."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                self._write_heartbeat()
        except asyncio.CancelledError:
            raise

    def _signal_handler(self, signum: int, frame: Any) -> None:
        """SIGTERM/SIGINT Handler - triggert graceful shutdown."""
        sig_name = signal.Signals(signum).name
        logger.info("worker.signal_received", signal=sig_name)

        if not self._running:
            logger.warning("worker.already_shutting_down")
            return

        self._running = False
        self._shutdown_event.set()

    async def shutdown(self) -> None:
        """Triggert Shutdown."""
        self._running = False
        self._shutdown_event.set()


async def run_trading_worker() -> None:
    """Entry Point für den Trading Worker."""
    worker = TradingWorker()
    try:
        await worker.start()
    except KeyboardInterrupt:
        logger.info("worker.interrupted")
        sys.exit(0)
    except Exception as e:
        logger.error("worker.fatal_error", error=str(e), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_trading_worker())
