"""
SGR Application Lifespan
========================
Orchestrates startup and shutdown of all system components.

Startup order matters:
1. Config validation (fail fast)
2. Logging (needed by everything)
3. Database (needed by all engines)
4. Redis / Event Bus (needed by all engines)
5. Engines (in dependency order)
6. API (last – only accept traffic when ready)

Shutdown is reverse order (graceful).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sgr.core.config import get_config
from sgr.core.database import close_db, init_db
from sgr.core.event_bus import get_event_bus
from sgr.core.logging import get_logger, setup_logging
from sgr.core.types import Environment

log = get_logger(__name__)


@asynccontextmanager
async def lifespan() -> AsyncIterator[None]:
    """
    System lifespan context manager.
    Use in FastAPI:
        app = FastAPI(lifespan=lifespan)
    """
    config = get_config()

    # 1. Logging
    setup_logging(
        log_level=config.monitoring.log_level,
        json_output=config.environment == Environment.PRODUCTION,
        trading_mode=config.trading_mode,
    )

    log.info(
        "sgr.starting",
        version=config.version,
        environment=config.environment.value,
        trading_mode=config.trading_mode.value,
    )

    # 2. Validate production constraints
    if config.is_live and config.is_production:
        log.warning(
            "sgr.live_trading_active",
            message="LIVE TRADING MODE – real funds at risk",
        )

    # 3. Database
    await init_db()

    # 4. Event Bus
    bus = get_event_bus()
    await bus.connect()

    log.info("sgr.ready", trading_mode=config.trading_mode.value)

    try:
        yield
    finally:
        log.info("sgr.shutting_down")

        # Reverse shutdown
        await bus.close()
        await close_db()

        log.info("sgr.stopped")
