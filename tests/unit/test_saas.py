"""
Tests für den SaaS Layer.
Auth, Performance Fees, Tenant Management.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sgr.saas.auth import AuthService
from sgr.saas.fees import DEFAULT_FEE_RATE, PerformanceFeeEngine
from sgr.saas.types import (
    FeeStatus,
    HighWaterMark,
    SubscriptionTier,
    TenantConfig,
)

# ===========================================================================
# Auth Service
# ===========================================================================


class TestAuthService:
    def test_hash_password_produces_hash(self) -> None:
        auth = AuthService()
        try:
            hashed = auth.hash_password("MySecurePassword123!")
            assert hashed != "MySecurePassword123!"
            assert len(hashed) > 20
        except RuntimeError:
            pytest.skip("passlib not installed")

    def test_verify_password_correct(self) -> None:
        auth = AuthService()
        try:
            hashed = auth.hash_password("MySecurePassword123!")
            assert auth.verify_password("MySecurePassword123!", hashed) is True
        except RuntimeError:
            pytest.skip("passlib not installed")

    def test_verify_password_wrong(self) -> None:
        auth = AuthService()
        try:
            hashed = auth.hash_password("CorrectPassword123!")
            assert auth.verify_password("WrongPassword123!", hashed) is False
        except RuntimeError:
            pytest.skip("passlib not installed")

    def test_password_strength_too_short(self) -> None:
        auth = AuthService()
        errors = auth.validate_password_strength("short")
        assert any("12 characters" in e for e in errors)

    def test_password_strength_no_uppercase(self) -> None:
        auth = AuthService()
        errors = auth.validate_password_strength("alllowercase123!")
        assert any("uppercase" in e for e in errors)

    def test_password_strength_no_digit(self) -> None:
        auth = AuthService()
        errors = auth.validate_password_strength("NoDigitsHereABC!")
        assert any("digit" in e for e in errors)

    def test_password_strength_no_special(self) -> None:
        auth = AuthService()
        errors = auth.validate_password_strength("NoSpecialChar123")
        assert any("special" in e for e in errors)

    def test_strong_password_no_errors(self) -> None:
        auth = AuthService()
        errors = auth.validate_password_strength("MyStr0ng!Pass#2024")
        assert errors == []

    def test_create_decode_access_token(self) -> None:
        from sgr.core.types import TradingMode

        auth = AuthService()
        try:
            token = auth.create_access_token(
                user_id="user-123",
                trading_mode=TradingMode.PAPER,
                is_admin=False,
            )
            payload = auth.verify_access_token(token)
            assert payload["sub"] == "user-123"
            assert payload["trading_mode"] == "paper"
            assert payload["type"] == "access"
        except RuntimeError:
            pytest.skip("python-jose not installed")

    def test_access_token_not_usable_as_refresh(self) -> None:
        from sgr.core.types import TradingMode

        auth = AuthService()
        try:
            token = auth.create_access_token("user-123", TradingMode.PAPER)
            with pytest.raises(ValueError, match="Not a refresh token"):
                auth.verify_refresh_token(token)
        except RuntimeError:
            pytest.skip("python-jose not installed")

    def test_refresh_token_decode(self) -> None:
        auth = AuthService()
        try:
            token = auth.create_refresh_token("user-456")
            user_id = auth.verify_refresh_token(token)
            assert user_id == "user-456"
        except RuntimeError:
            pytest.skip("python-jose not installed")

    def test_invalid_token_raises(self) -> None:
        auth = AuthService()
        try:
            with pytest.raises(ValueError):
                auth.decode_token("not.a.valid.token")
        except RuntimeError:
            pytest.skip("python-jose not installed")

    def test_totp_generate_and_verify(self) -> None:
        auth = AuthService()
        try:
            secret = auth.generate_totp_secret()
            assert len(secret) >= 16

            # Generiere aktuellen Code und verifiziere
            import pyotp

            totp = pyotp.TOTP(secret)
            current_code = totp.now()
            assert auth.verify_totp(secret, current_code) is True
        except (RuntimeError, ImportError):
            pytest.skip("pyotp not installed")

    def test_totp_wrong_code_fails(self) -> None:
        auth = AuthService()
        try:
            secret = auth.generate_totp_secret()
            assert auth.verify_totp(secret, "000000") is False
        except (RuntimeError, ImportError):
            pytest.skip("pyotp not installed")

    def test_totp_encrypt_decrypt_roundtrip(self) -> None:
        auth = AuthService()
        try:
            secret = auth.generate_totp_secret()
            encrypted = auth.encrypt_totp_secret(secret)
            decrypted = auth.decrypt_totp_secret(encrypted)
            assert decrypted == secret
            assert encrypted != secret
        except RuntimeError:
            pytest.skip("deps not installed")

    def test_totp_uri_format(self) -> None:
        auth = AuthService()
        try:
            secret = auth.generate_totp_secret()
            uri = auth.get_totp_uri(secret, "test@sgr.app")
            assert uri.startswith("otpauth://totp/")
            assert "ProjectSGR" in uri
        except (RuntimeError, ImportError):
            pytest.skip("pyotp not installed")


# ===========================================================================
# High-Water-Mark
# ===========================================================================


class TestHighWaterMark:
    def test_no_profit_above_hwm_at_start(self) -> None:
        hwm = HighWaterMark(user_id="u1", current_hwm=Decimal("10000"))
        assert hwm.calculate_new_high(Decimal("10000")) == Decimal("0")

    def test_profit_above_hwm_positive(self) -> None:
        hwm = HighWaterMark(user_id="u1", current_hwm=Decimal("10000"))
        profit = hwm.calculate_new_high(Decimal("12000"))
        assert profit == Decimal("2000")

    def test_loss_below_hwm_returns_zero(self) -> None:
        hwm = HighWaterMark(user_id="u1", current_hwm=Decimal("12000"))
        profit = hwm.calculate_new_high(Decimal("10000"))
        assert profit == Decimal("0")

    def test_update_hwm_on_new_high(self) -> None:
        hwm = HighWaterMark(user_id="u1", current_hwm=Decimal("10000"))
        hwm.update_hwm(Decimal("15000"))
        assert hwm.current_hwm == Decimal("15000")

    def test_hwm_not_updated_on_loss(self) -> None:
        hwm = HighWaterMark(user_id="u1", current_hwm=Decimal("15000"))
        hwm.update_hwm(Decimal("12000"))
        assert hwm.current_hwm == Decimal("15000")

    def test_hwm_partial_recovery(self) -> None:
        """Verlust + partieller Recovery → nur neues High über HWM zählt."""
        hwm = HighWaterMark(user_id="u1", current_hwm=Decimal("12000"))

        # Recovery: 11500 < 12000 HWM → kein Fee
        no_profit = hwm.calculate_new_high(Decimal("11500"))
        assert no_profit == Decimal("0")

        # Neues Hoch: 13000 > 12000 → Fee nur auf 1000
        profit = hwm.calculate_new_high(Decimal("13000"))
        assert profit == Decimal("1000")


# ===========================================================================
# Performance Fee Engine
# ===========================================================================


class TestPerformanceFeeEngine:
    def _make_hwm(self, value: float) -> HighWaterMark:
        return HighWaterMark(user_id="u1", current_hwm=Decimal(str(value)))

    def test_fee_on_new_high(self) -> None:
        engine = PerformanceFeeEngine()
        hwm = self._make_hwm(10000)
        now = datetime.now(tz=UTC)

        calc = engine.calculate_fee(
            user_id="u1",
            period_start=now,
            period_end=now,
            portfolio_value_start=Decimal("10000"),
            portfolio_value_end=Decimal("12000"),
            hwm=hwm,
            fee_rate=DEFAULT_FEE_RATE,
        )

        assert calc.profit_above_hwm == Decimal("2000")
        assert calc.fee_amount == Decimal("100.00")  # 2000 * 5%
        assert calc.fee_rate == Decimal("0.05")

    def test_no_fee_below_hwm(self) -> None:
        engine = PerformanceFeeEngine()
        hwm = self._make_hwm(12000)
        now = datetime.now(tz=UTC)

        calc = engine.calculate_fee(
            user_id="u1",
            period_start=now,
            period_end=now,
            portfolio_value_start=Decimal("12000"),
            portfolio_value_end=Decimal("10000"),
            hwm=hwm,
        )

        assert calc.fee_amount == Decimal("0")
        assert calc.profit_above_hwm == Decimal("0")

    def test_hwm_updated_after_fee(self) -> None:
        engine = PerformanceFeeEngine()
        hwm = self._make_hwm(10000)
        now = datetime.now(tz=UTC)

        engine.calculate_fee(
            user_id="u1",
            period_start=now,
            period_end=now,
            portfolio_value_start=Decimal("10000"),
            portfolio_value_end=Decimal("12000"),
            hwm=hwm,
        )

        assert hwm.current_hwm == Decimal("12000")

    def test_fee_rounding_to_cents(self) -> None:
        """Fee wird auf 2 Dezimalstellen gerundet."""
        engine = PerformanceFeeEngine()
        hwm = self._make_hwm(10000)
        now = datetime.now(tz=UTC)

        calc = engine.calculate_fee(
            user_id="u1",
            period_start=now,
            period_end=now,
            portfolio_value_start=Decimal("10000"),
            portfolio_value_end=Decimal("10001"),  # +1 USDT
            hwm=hwm,
        )

        # 1 * 5% = 0.05 USDT → zu klein (< 1.00 MIN_FEE) → 0
        assert calc.fee_amount == Decimal("0")

    def test_fee_minimum_threshold(self) -> None:
        """Micro-Fees unter 1 USDT werden nicht berechnet."""
        engine = PerformanceFeeEngine()
        hwm = self._make_hwm(10000)
        now = datetime.now(tz=UTC)

        calc = engine.calculate_fee(
            user_id="u1",
            period_start=now,
            period_end=now,
            portfolio_value_start=Decimal("10000"),
            portfolio_value_end=Decimal("10015"),  # +15 USDT → Fee = 0.75 < 1.00
            hwm=hwm,
        )

        assert calc.fee_amount == Decimal("0")

    def test_generate_invoice_structure(self) -> None:
        engine = PerformanceFeeEngine()
        hwm = self._make_hwm(10000)
        now = datetime.now(tz=UTC)

        calc = engine.calculate_fee(
            user_id="u1",
            period_start=now,
            period_end=now,
            portfolio_value_start=Decimal("10000"),
            portfolio_value_end=Decimal("15000"),
            hwm=hwm,
        )

        invoice = engine.generate_invoice(calc)
        assert invoice.user_id == "u1"
        assert invoice.performance_fee == calc.fee_amount
        assert invoice.status == FeeStatus.INVOICED
        assert len(invoice.line_items) > 0

    def test_consecutive_periods_hwm(self) -> None:
        """Mehrere Perioden: HWM korrekt über Perioden hinweg."""
        engine = PerformanceFeeEngine()
        hwm = self._make_hwm(10000)
        now = datetime.now(tz=UTC)

        # Periode 1: +20%
        calc1 = engine.calculate_fee("u1", now, now, Decimal("10000"), Decimal("12000"), hwm)
        assert calc1.fee_amount == Decimal("100.00")  # 2000 * 5%

        # Periode 2: -10% (kein neues Hoch)
        calc2 = engine.calculate_fee("u1", now, now, Decimal("12000"), Decimal("10800"), hwm)
        assert calc2.fee_amount == Decimal("0")

        # Periode 3: +20% über Periode-2-Level aber noch unter HWM
        calc3 = engine.calculate_fee("u1", now, now, Decimal("10800"), Decimal("11500"), hwm)
        assert calc3.fee_amount == Decimal("0")  # 11500 < 12000 HWM

        # Periode 4: Neues Hoch über 12000
        calc4 = engine.calculate_fee("u1", now, now, Decimal("11500"), Decimal("13000"), hwm)
        assert calc4.profit_above_hwm == Decimal("1000")  # 13000 - 12000
        assert calc4.fee_amount == Decimal("50.00")  # 1000 * 5%


# ===========================================================================
# Tenant Config
# ===========================================================================


class TestTenantConfig:
    def test_default_config_is_free(self) -> None:
        config = TenantConfig(user_id="u1")
        assert config.tier == SubscriptionTier.FREE
        assert config.performance_fee_rate == Decimal("0.05")
        assert config.is_live_trading_enabled is False

    def test_enterprise_config(self) -> None:
        config = TenantConfig(
            user_id="u1",
            tier=SubscriptionTier.ENTERPRISE,
            max_leverage=Decimal("5.0"),
            max_open_positions=50,
            is_live_trading_enabled=True,
        )
        assert config.tier == SubscriptionTier.ENTERPRISE
        assert config.max_leverage == Decimal("5.0")
        assert config.max_open_positions == 50
