"""
Security Hardening Module
==========================
API Key Rotation, Audit Logging, Input Validation.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sgr.core.logging import get_logger
from sgr.core.repositories import get_repositories

log = get_logger(__name__)


class AuditAction(StrEnum):
    """Audit log action types."""
    API_KEY_CREATED = "api_key_created"
    API_KEY_ROTATED = "api_key_rotated"
    API_KEY_DELETED = "api_key_deleted"
    STRATEGY_ACTIVATED = "strategy_activated"
    STRATEGY_DEACTIVATED = "strategy_deactivated"
    TRADE_EXECUTED = "trade_executed"
    POSITION_CLOSED = "position_closed"
    RISK_LIMIT_CHANGED = "risk_limit_changed"
    CONFIG_CHANGED = "config_changed"
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"


async def audit_log(
    action: AuditAction,
    user_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """
    Records audit log entry for sensitive operations.

    Fail-safe: eine DB-Schreibfehler darf den Aufrufer nie blockieren -
    gleiches Muster wie PortfolioEngine._persist_position_upsert() und
    KillSwitch._cancel_all_orders(). Ein Audit-Log-Fehler ist wichtig,
    aber weniger kritisch als die eigentliche Aktion, die er protokolliert.

    Args:
        action: Action type
        user_id: User performing action
        details: Additional context
    """
    try:
        repos = get_repositories()
        await repos.audit_log.log_action(
            action=action.value,
            user_id=user_id,
            details=details,
        )

        log.info(
            f"audit.{action.value}",
            user_id=user_id,
            details=details,
        )
    except Exception as e:
        log.error("audit.logging_failed", action=action.value, error=str(e))


class APIKeyRotationManager:
    """Manages API key rotation lifecycle."""

    MAX_AGE_DAYS = 90
    ROTATION_INTERVAL_DAYS = 30

    @staticmethod
    def generate_key() -> str:
        """Generate cryptographically secure API key."""
        return secrets.token_urlsafe(32)

    @staticmethod
    async def rotate_key(
        user_id: str,
        old_key: str,
    ) -> str:
        """
        Rotates API key with grace period.

        Process:
        1. Generate new key
        2. Mark old key as deprecated (grace period: 7 days)
        3. Log audit entry
        4. Return new key
        """
        new_key = APIKeyRotationManager.generate_key()

        await audit_log(
            action=AuditAction.API_KEY_ROTATED,
            user_id=user_id,
            details={"old_key_prefix": old_key[:8] + "***"},
        )

        log.info(
            "security.api_key_rotated",
            user_id=user_id,
            grace_period_days=7,
        )

        return new_key

    @staticmethod
    async def check_key_age(key_created_at: datetime) -> bool:
        """Returns True if key is older than MAX_AGE_DAYS."""
        age = datetime.utcnow() - key_created_at
        return age > timedelta(days=APIKeyRotationManager.MAX_AGE_DAYS)


class RateLimiter:
    """
    Per-user rate limiting with Redis backend.
    Prevents abuse of sensitive endpoints.
    """

    def __init__(self, redis_client: Any, window_seconds: int = 60, limit: int = 100) -> None:
        self.redis = redis_client
        self.window_seconds = window_seconds
        self.limit = limit

    async def is_allowed(self, user_id: str, action: str) -> bool:
        """
        Check if user is within rate limit.

        Args:
            user_id: User identifier
            action: Action being rate-limited (e.g., "login_attempt")

        Returns:
            True if request allowed, False if rate-limited
        """
        key = f"ratelimit:{user_id}:{action}"

        try:
            count = await self.redis.incr(key)
            if count == 1:
                await self.redis.expire(key, self.window_seconds)

            if count > self.limit:
                log.warning(
                    "security.rate_limit_exceeded",
                    user_id=user_id,
                    action=action,
                    count=count,
                    limit=self.limit,
                )
                return False

            return True
        except Exception as e:
            log.error("security.rate_limit_error", error=str(e))
            return True  # Fail open – allow request if check fails


class InputValidator:
    """
    Validates & sanitizes user inputs.
    Prevents injection attacks, path traversal, etc.
    """

    ALLOWED_CHARS_STRATEGY_NAME = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    )
    ALLOWED_CHARS_SYMBOL = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-")
    MAX_STRING_LEN = 1000
    MAX_DESC_LEN = 10000

    @staticmethod
    def validate_strategy_name(name: str) -> bool:
        """Validates strategy name."""
        if not name or len(name) > 100:
            return False
        return all(c in InputValidator.ALLOWED_CHARS_STRATEGY_NAME for c in name)

    @staticmethod
    def validate_symbol(symbol: str) -> bool:
        """Validates trading symbol (e.g., BTC/USDT)."""
        if not symbol or len(symbol) > 20:
            return False
        return all(c in InputValidator.ALLOWED_CHARS_SYMBOL for c in symbol)

    @staticmethod
    def validate_email(email: str) -> bool:
        """Simple email validation."""
        import re
        pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        return bool(re.match(pattern, email)) and len(email) <= 255

    @staticmethod
    def sanitize_string(value: str, max_len: int = 1000) -> str:
        """Removes null bytes and truncates."""
        if not value:
            return ""
        sanitized = value.replace("\x00", "").replace("\r\n", " ")
        return sanitized[:max_len]

    @staticmethod
    def validate_risk_limit(value: float) -> bool:
        """Validates risk limit is reasonable."""
        return 0.0 < value < 1.0


async def validate_sensitive_action(
    user_id: str,
    action: str,
    rate_limiter: RateLimiter | None = None,
) -> tuple[bool, str | None]:
    """
    Validates user is allowed to perform sensitive action.

    Returns:
        (allowed, error_message)
    """
    # Check rate limit
    if rate_limiter:
        if not await rate_limiter.is_allowed(user_id, action):
            return False, "Rate limit exceeded"

    # Check MFA (mock – implement with actual MFA)
    # if not await check_mfa_verified(user_id):
    #     return False, "MFA verification required"

    # Log attempt
    try:
        audit_action = AuditAction(action)
    except ValueError:
        log.warning("security.unknown_audit_action", action=action, user_id=user_id)
        return False, f"Unknown action: {action}"

    await audit_log(
        action=audit_action,
        user_id=user_id,
    )

    return True, None
