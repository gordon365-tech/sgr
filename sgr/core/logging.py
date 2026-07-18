"""
SGR Structured Logging
======================
Centralized logging setup using structlog.

Design decisions:
- JSON output in production (machine-parseable)
- Human-readable console output in development
- Automatic context injection (trading_mode, environment)
- Secret scrubbing (no API keys, passwords in logs)
- Trade logs are immutable audit records (separate stream)

Usage:
    from sgr.core.logging import get_logger
    log = get_logger(__name__)
    log.info("order.submitted", order_id=str(order.id), symbol=str(symbol))
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger

from sgr.core.types import TradingMode

# ---------------------------------------------------------------------------
# Secret Scrubbing
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|apikey|secret|password|token|authorization)\s*[:=]\s*\S+"),
    re.compile(r"[A-Za-z0-9]{40,}"),  # Long random strings (potential keys)
]

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apiKey",
        "secret",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "private_key",
    }
)


def _scrub_secrets(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """
    Processor that removes/redacts sensitive values from log records.
    Applied before serialization – secrets never hit the log sink.
    """
    for key in list(event_dict.keys()):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = "***REDACTED***"
        elif isinstance(event_dict[key], str):
            for pattern in _SECRET_PATTERNS:
                if pattern.search(str(event_dict[key])):
                    # Redact only the value, not the entire field
                    event_dict[key] = "***REDACTED***"
                    break
    return event_dict


# ---------------------------------------------------------------------------
# Trading Mode Context Injection
# ---------------------------------------------------------------------------


def _add_trading_context(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Injects trading_mode so every log line is tagged."""
    # Context is bound via structlog.contextvars
    return event_dict


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def setup_logging(
    log_level: str = "INFO",
    json_output: bool = False,
    trading_mode: TradingMode = TradingMode.PAPER,
) -> None:
    """
    Initialize structured logging. Call once at application startup.

    Args:
        log_level: Standard Python log level string.
        json_output: True in production (JSON), False in dev (colored console).
        trading_mode: Injected into every log record.
    """
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        _scrub_secrets,
        _add_trading_context,
    ]

    if json_output:
        # Production: JSON for log aggregation (Datadog, ELK, etc.)
        processors: list[Any] = shared_processors + [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Development: colored, human-readable
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(log_level.upper())
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Also configure stdlib logging to route through structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.getLevelName(log_level.upper()),
    )

    # Bind global context
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(trading_mode=trading_mode.value)


def get_logger(name: str) -> structlog.BoundLogger:
    """
    Get a named logger. Use module __name__ as convention.

    Example:
        log = get_logger(__name__)
        log.info("engine.started", version="0.1.0")
    """
    return structlog.get_logger(name)


# ---------------------------------------------------------------------------
# Audit Logger (immutable trade records)
# ---------------------------------------------------------------------------


class AuditLogger:
    """
    Separate logger for immutable audit records.
    Trade logs, kill switch events, auth events.
    These must NEVER be deleted and should be shipped to
    an append-only sink (S3, CloudWatch, etc.).

    In production: route audit.* to separate immutable storage.
    """

    def __init__(self) -> None:
        self._log = get_logger("audit")

    def log_trade(
        self,
        event: str,
        order_id: str,
        symbol: str,
        side: str,
        quantity: str,
        price: str | None,
        trading_mode: TradingMode,
        strategy: str,
        **kwargs: Any,
    ) -> None:
        self._log.info(
            f"trade.{event}",
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            trading_mode=trading_mode.value,
            strategy=strategy,
            audit=True,
            **kwargs,
        )

    def log_kill_switch(
        self,
        reason: str,
        trading_mode: TradingMode,
        triggered_by: str,
    ) -> None:
        self._log.critical(
            "kill_switch.triggered",
            reason=reason,
            trading_mode=trading_mode.value,
            triggered_by=triggered_by,
            audit=True,
        )

    def log_auth_event(
        self,
        event: str,
        user_id: str,
        ip_address: str,
        success: bool,
    ) -> None:
        self._log.warning(
            f"auth.{event}",
            user_id=user_id,
            ip_address=ip_address,
            success=success,
            audit=True,
        )


# Singleton audit logger
audit_log = AuditLogger()
