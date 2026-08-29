"""
Sentry Integration für Error Tracking
======================================
"""

from __future__ import annotations

import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

from sgr.core.config import get_config
from sgr.core.logging import get_logger

log = get_logger(__name__)


def setup_sentry() -> None:
    """Initializes Sentry for error tracking."""
    config = get_config()

    if not config.monitoring.sentry_dsn:
        log.info("monitoring.sentry_disabled")
        return

    sentry_sdk.init(
        dsn=config.monitoring.sentry_dsn,
        environment=config.environment.value,
        traces_sample_rate=0.1,  # Sample 10% of transactions
        profiles_sample_rate=0.1,  # Sample 10% of profiling data
        integrations=[
            FastApiIntegration(),
            SqlalchemyIntegration(),
            AsyncioIntegration(),
        ],
    )

    log.info("monitoring.sentry_enabled", environment=config.environment.value)
