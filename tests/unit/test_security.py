"""
Tests für sgr.core.security.

Kontext: Das Modul war zuvor komplett unimportierbar (ImportError beim
Import von async_db_session, das nie in sgr.core.database existierte -
0% Coverage war also keine Test-Lücke, sondern ein Blocker wie
sgr/monitoring/__init__.py vor dessen Fix). audit_log() enthielt zudem
rohes SQL-String-Statement ohne text()-Wrapper gegen eine nicht
existierende Tabelle ("Pseudo-code" laut eigenem Kommentar).

Fix: audit_log() nutzt jetzt AuditLogRepository (analog zu
RiskEventRepository) statt rohem SQL - konsistent mit dem Rest der
Repository-Schicht, kein separater DB-Zugriffspfad.

Getestet:
    1. audit_log: schreibt über AuditLogRepository, fail-safe bei DB-Fehler
    2. APIKeyRotationManager: Key-Generierung, Rotation, Age-Check
    3. RateLimiter: allow/deny, fail-open bei Redis-Fehler
    4. InputValidator: alle Validierungsfunktionen (gültig + ungültig)
    5. validate_sensitive_action: Rate-Limit-Pfad, unbekannte Action
       (Regressionstest für den ungefixten ValueError-Fall),
       erfolgreicher Pfad
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from sgr.core.security import (
    APIKeyRotationManager,
    AuditAction,
    InputValidator,
    RateLimiter,
    audit_log,
    validate_sensitive_action,
)

if TYPE_CHECKING:
    import pytest_mock


# ---------------------------------------------------------------------------
# audit_log
# ---------------------------------------------------------------------------


class TestAuditLog:
    async def test_audit_log_writes_via_repository(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        mock_repos = MagicMock()
        mock_repos.audit_log.log_action = AsyncMock()
        mocker.patch("sgr.core.security.get_repositories", return_value=mock_repos)

        await audit_log(AuditAction.LOGIN, user_id="user-1", details={"ip": "1.2.3.4"})

        mock_repos.audit_log.log_action.assert_called_once_with(
            action="login",
            user_id="user-1",
            details={"ip": "1.2.3.4"},
        )

    async def test_audit_log_defaults_user_id_to_none_passthrough(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        mock_repos = MagicMock()
        mock_repos.audit_log.log_action = AsyncMock()
        mocker.patch("sgr.core.security.get_repositories", return_value=mock_repos)

        await audit_log(AuditAction.CONFIG_CHANGED)

        mock_repos.audit_log.log_action.assert_called_once_with(
            action="config_changed",
            user_id=None,
            details=None,
        )

    async def test_audit_log_failure_is_swallowed_not_raised(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        """
        Fail-Safe-Grundsatz: ein Audit-Log-Fehler darf den Aufrufer nie
        blockieren (analog PortfolioEngine._persist_position_upsert()).
        """
        mock_repos = MagicMock()
        mock_repos.audit_log.log_action = AsyncMock(side_effect=RuntimeError("db down"))
        mocker.patch("sgr.core.security.get_repositories", return_value=mock_repos)

        await audit_log(AuditAction.LOGIN_FAILED, user_id="user-2")
        # Keine Exception -> Test besteht allein durch Nicht-Crashen.


# ---------------------------------------------------------------------------
# APIKeyRotationManager
# ---------------------------------------------------------------------------


class TestAPIKeyRotationManager:
    def test_generate_key_returns_url_safe_string(self) -> None:
        key = APIKeyRotationManager.generate_key()
        assert isinstance(key, str)
        assert len(key) > 20

    def test_generate_key_is_unique_per_call(self) -> None:
        keys = {APIKeyRotationManager.generate_key() for _ in range(10)}
        assert len(keys) == 10

    async def test_rotate_key_returns_new_key_and_logs_audit(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        mock_audit = mocker.patch(
            "sgr.core.security.audit_log", new_callable=AsyncMock
        )

        new_key = await APIKeyRotationManager.rotate_key("user-1", "old-key-12345678")

        assert isinstance(new_key, str)
        assert new_key != "old-key-12345678"
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args.kwargs
        assert call_kwargs["action"] == AuditAction.API_KEY_ROTATED
        assert call_kwargs["user_id"] == "user-1"
        # Alter Key darf nie im Klartext geloggt werden
        assert "old-key-12345678" not in str(call_kwargs["details"])
        assert call_kwargs["details"]["old_key_prefix"] == "old-key-***"

    async def test_check_key_age_true_when_older_than_max(self) -> None:
        old_date = datetime.utcnow() - timedelta(days=91)
        assert await APIKeyRotationManager.check_key_age(old_date) is True

    async def test_check_key_age_false_when_within_max(self) -> None:
        recent_date = datetime.utcnow() - timedelta(days=10)
        assert await APIKeyRotationManager.check_key_age(recent_date) is False


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    async def test_allows_request_within_limit(self) -> None:
        redis = AsyncMock()
        redis.incr.return_value = 1
        limiter = RateLimiter(redis, window_seconds=60, limit=100)

        allowed = await limiter.is_allowed("user-1", "login_attempt")

        assert allowed is True
        redis.expire.assert_called_once_with("ratelimit:user-1:login_attempt", 60)

    async def test_does_not_reset_expiry_on_subsequent_calls(self) -> None:
        redis = AsyncMock()
        redis.incr.return_value = 5  # nicht der erste Call
        limiter = RateLimiter(redis, window_seconds=60, limit=100)

        await limiter.is_allowed("user-1", "login_attempt")

        redis.expire.assert_not_called()

    async def test_denies_request_over_limit(self) -> None:
        redis = AsyncMock()
        redis.incr.return_value = 101
        limiter = RateLimiter(redis, window_seconds=60, limit=100)

        allowed = await limiter.is_allowed("user-1", "login_attempt")

        assert allowed is False

    async def test_fails_open_on_redis_error(self) -> None:
        """
        Bewusste Design-Entscheidung (siehe Docstring): ein Redis-Fehler
        darf Requests nicht blockieren - fail open, nicht fail closed.
        """
        redis = AsyncMock()
        redis.incr.side_effect = RuntimeError("redis unreachable")
        limiter = RateLimiter(redis)

        allowed = await limiter.is_allowed("user-1", "login_attempt")

        assert allowed is True


# ---------------------------------------------------------------------------
# InputValidator
# ---------------------------------------------------------------------------


class TestInputValidatorStrategyName:
    def test_valid_strategy_name(self) -> None:
        assert InputValidator.validate_strategy_name("trend_following_v1") is True

    def test_empty_strategy_name_invalid(self) -> None:
        assert InputValidator.validate_strategy_name("") is False

    def test_too_long_strategy_name_invalid(self) -> None:
        assert InputValidator.validate_strategy_name("a" * 101) is False

    def test_special_chars_invalid(self) -> None:
        assert InputValidator.validate_strategy_name("drop table; --") is False


class TestInputValidatorSymbol:
    def test_valid_symbol(self) -> None:
        assert InputValidator.validate_symbol("BTC/USDT") is True

    def test_empty_symbol_invalid(self) -> None:
        assert InputValidator.validate_symbol("") is False

    def test_too_long_symbol_invalid(self) -> None:
        assert InputValidator.validate_symbol("A" * 21) is False

    def test_lowercase_symbol_invalid(self) -> None:
        """Symbole sind laut ALLOWED_CHARS_SYMBOL nur Grossbuchstaben."""
        assert InputValidator.validate_symbol("btc/usdt") is False


class TestInputValidatorEmail:
    def test_valid_email(self) -> None:
        assert InputValidator.validate_email("gordon@example.com") is True

    def test_invalid_email_no_at(self) -> None:
        assert InputValidator.validate_email("not-an-email") is False

    def test_invalid_email_no_domain(self) -> None:
        assert InputValidator.validate_email("user@") is False

    def test_too_long_email_invalid(self) -> None:
        long_email = "a" * 250 + "@example.com"
        assert InputValidator.validate_email(long_email) is False


class TestInputValidatorSanitizeString:
    def test_removes_null_bytes(self) -> None:
        result = InputValidator.sanitize_string("hello\x00world")
        assert "\x00" not in result

    def test_replaces_crlf(self) -> None:
        result = InputValidator.sanitize_string("line1\r\nline2")
        assert "\r\n" not in result

    def test_truncates_to_max_len(self) -> None:
        result = InputValidator.sanitize_string("a" * 2000, max_len=100)
        assert len(result) == 100

    def test_empty_string_returns_empty(self) -> None:
        assert InputValidator.sanitize_string("") == ""


class TestInputValidatorRiskLimit:
    def test_valid_risk_limit(self) -> None:
        assert InputValidator.validate_risk_limit(0.02) is True

    def test_zero_invalid(self) -> None:
        assert InputValidator.validate_risk_limit(0.0) is False

    def test_one_invalid(self) -> None:
        assert InputValidator.validate_risk_limit(1.0) is False

    def test_negative_invalid(self) -> None:
        assert InputValidator.validate_risk_limit(-0.5) is False


# ---------------------------------------------------------------------------
# validate_sensitive_action
# ---------------------------------------------------------------------------


class TestValidateSensitiveAction:
    async def test_denied_when_rate_limited(self) -> None:
        limiter = AsyncMock()
        limiter.is_allowed.return_value = False

        allowed, error = await validate_sensitive_action(
            "user-1", "login", rate_limiter=limiter
        )

        assert allowed is False
        assert error == "Rate limit exceeded"

    async def test_unknown_action_returns_error_not_raises(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        """
        Regressionstest: AuditAction(action) mit ungueltigem String wirft
        sonst eine unbehandelte ValueError. Muss stattdessen ein
        kontrolliertes (False, error) liefern.
        """
        mocker.patch("sgr.core.security.audit_log", new_callable=AsyncMock)

        allowed, error = await validate_sensitive_action("user-1", "not_a_real_action")

        assert allowed is False
        assert error is not None
        assert "not_a_real_action" in error

    async def test_allowed_when_no_rate_limiter_and_valid_action(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        mock_audit = mocker.patch("sgr.core.security.audit_log", new_callable=AsyncMock)

        allowed, error = await validate_sensitive_action("user-1", "login")

        assert allowed is True
        assert error is None
        mock_audit.assert_called_once()

    async def test_allowed_when_within_rate_limit(
        self, mocker: pytest_mock.MockerFixture
    ) -> None:
        limiter = AsyncMock()
        limiter.is_allowed.return_value = True
        mocker.patch("sgr.core.security.audit_log", new_callable=AsyncMock)

        allowed, error = await validate_sensitive_action(
            "user-1", "login", rate_limiter=limiter
        )

        assert allowed is True
        assert error is None
