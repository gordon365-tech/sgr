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
import logging
import signal
import sys
from typing import Any

from sgr.api.main import lifespan, AppState
from sgr.core.logging import setup_logging
from sgr.core.config import get_config
from sgr.core.types import Environment, TradingMode


logger = logging.getLogger(__name__)


class TradingWorker:
    """Hauptklasse für Trading Worker."""

    def __init__(self) -> None:
        self.config = get_config()
        self.app_state = AppState()
        self._shutdown_event = asyncio.Event()
        self._running = False

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

        # Lifespan Context: initialisiert alle Services
        # (identisch wie in der API, nutzt dieselbe shared Infrastructure)
        async with lifespan(None):  # type: ignore[arg-type]
            logger.info("worker.ready")

            # Worker hauptschleife
            try:
                await self._shutdown_event.wait()
            except asyncio.CancelledError:
                logger.info("worker.cancelled")
                raise

        logger.info("worker.stopped")

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
