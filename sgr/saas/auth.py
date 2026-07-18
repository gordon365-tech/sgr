"""
SGR Auth Service
================
Authentifizierung und Autorisierung für SaaS-User.

Features:
    - JWT Access + Refresh Tokens
    - bcrypt Passwort-Hashing
    - TOTP 2FA (für Live-Trading obligatorisch)
    - Token Rotation (Refresh erzwingt neue Tokens)
    - Brute-Force Schutz (Rate Limiting via Redis)

Security Entscheidungen:
    - Access Token: 60 Min TTL (kurz wegen Live-Trading-Risiko)
    - Refresh Token: 30 Tage TTL, einmalig nutzbar (Rotation)
    - 2FA: verpflichtend für Live-Trading-Aktivierung
    - Password: min 12 Zeichen, bcrypt mit 12 Rounds
    - Never log tokens, passwords, or TOTP codes
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sgr.core.config import get_config
from sgr.core.logging import audit_log, get_logger
from sgr.core.types import TradingMode

log = get_logger(__name__)


class AuthService:
    """
    Auth Service: Token-Erstellung, Validierung, 2FA.
    Stateless – kein interner State.
    """

    def __init__(self) -> None:
        self._config = get_config()

    # ------------------------------------------------------------------
    # Password
    # ------------------------------------------------------------------

    def hash_password(self, password: str) -> str:
        """bcrypt Hash mit 12 Rounds."""
        try:
            from passlib.context import CryptContext

            ctx = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)
            return ctx.hash(password)
        except ImportError:
            raise RuntimeError("passlib not installed: pip install passlib[bcrypt]")

    def verify_password(self, plain: str, hashed: str) -> bool:
        """Vergleicht Plaintext mit bcrypt-Hash."""
        try:
            from passlib.context import CryptContext

            ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
            return ctx.verify(plain, hashed)
        except ImportError:
            return False
        except Exception:
            return False

    def validate_password_strength(self, password: str) -> list[str]:
        """
        Prüft Passwort-Stärke. Gibt Liste von Fehlern zurück.
        Leere Liste = Passwort akzeptiert.
        """
        errors = []
        if len(password) < 12:
            errors.append("Password must be at least 12 characters")
        if not any(c.isupper() for c in password):
            errors.append("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in password):
            errors.append("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in password):
            errors.append("Password must contain at least one digit")
        if not any(c in "!@#$%^&*()-_=+[]{}|;:,.<>?" for c in password):
            errors.append("Password must contain at least one special character")
        return errors

    # ------------------------------------------------------------------
    # JWT Tokens
    # ------------------------------------------------------------------

    def create_access_token(
        self,
        user_id: str,
        trading_mode: TradingMode,
        is_admin: bool = False,
        extra_claims: dict[str, Any] | None = None,
    ) -> str:
        """Erstellt JWT Access Token (60 Min TTL)."""
        try:
            from jose import jwt
        except ImportError:
            raise RuntimeError("python-jose not installed")

        now = datetime.now(tz=UTC)
        claims = {
            "sub": user_id,
            "trading_mode": trading_mode.value,
            "is_admin": is_admin,
            "iat": now,
            "exp": now + timedelta(minutes=self._config.api.access_token_expire_minutes),
            "type": "access",
        }
        if extra_claims:
            claims.update(extra_claims)

        return jwt.encode(
            claims,
            self._config.api.secret_key.get_secret_value(),
            algorithm=self._config.api.algorithm,
        )

    def create_refresh_token(self, user_id: str) -> str:
        """Erstellt JWT Refresh Token (30 Tage TTL)."""
        try:
            from jose import jwt
        except ImportError:
            raise RuntimeError("python-jose not installed")

        now = datetime.now(tz=UTC)
        claims = {
            "sub": user_id,
            "iat": now,
            "exp": now + timedelta(days=self._config.api.refresh_token_expire_days),
            "type": "refresh",
        }

        return jwt.encode(
            claims,
            self._config.api.secret_key.get_secret_value(),
            algorithm=self._config.api.algorithm,
        )

    def decode_token(self, token: str) -> dict[str, Any]:
        """
        Dekodiert und validiert JWT Token.
        Wirft Exception bei ungültigem/abgelaufenem Token.
        """
        try:
            from jose import JWTError, jwt
        except ImportError:
            raise RuntimeError("python-jose not installed")

        try:
            payload = jwt.decode(
                token,
                self._config.api.secret_key.get_secret_value(),
                algorithms=[self._config.api.algorithm],
            )
            return payload
        except JWTError as e:
            raise ValueError(f"Invalid token: {e}") from e

    def verify_access_token(self, token: str) -> dict[str, Any]:
        """Dekodiert Access Token und prüft Type."""
        payload = self.decode_token(token)
        if payload.get("type") != "access":
            raise ValueError("Not an access token")
        return payload

    def verify_refresh_token(self, token: str) -> str:
        """Dekodiert Refresh Token. Returns user_id."""
        payload = self.decode_token(token)
        if payload.get("type") != "refresh":
            raise ValueError("Not a refresh token")
        return str(payload["sub"])

    # ------------------------------------------------------------------
    # TOTP / 2FA
    # ------------------------------------------------------------------

    def generate_totp_secret(self) -> str:
        """Generiert neues TOTP-Secret für User-Onboarding."""
        try:
            import pyotp

            return pyotp.random_base32()
        except ImportError:
            raise RuntimeError("pyotp not installed: pip install pyotp")

    def get_totp_uri(
        self,
        secret: str,
        user_email: str,
        issuer: str = "ProjectSGR",
    ) -> str:
        """
        TOTP URI für QR-Code-Generierung.
        Format: otpauth://totp/...
        Wird dem User beim 2FA-Setup gezeigt.
        """
        try:
            import pyotp

            totp = pyotp.TOTP(secret)
            return totp.provisioning_uri(name=user_email, issuer_name=issuer)
        except ImportError:
            raise RuntimeError("pyotp not installed")

    def verify_totp(self, secret: str, code: str) -> bool:
        """
        Validiert TOTP-Code.
        Window=1: akzeptiert auch Code ±30s (Clock Drift).
        """
        try:
            import pyotp

            totp = pyotp.TOTP(secret)
            return totp.verify(code, valid_window=1)
        except ImportError:
            return False
        except Exception:
            return False

    def encrypt_totp_secret(self, secret: str) -> str:
        """Verschlüsselt TOTP-Secret für DB-Speicherung."""
        from sgr.core.encryption import get_cipher

        return get_cipher().encrypt(secret)

    def decrypt_totp_secret(self, encrypted: str) -> str:
        """Entschlüsselt TOTP-Secret aus DB."""
        from sgr.core.encryption import get_cipher

        return get_cipher().decrypt(encrypted)

    # ------------------------------------------------------------------
    # Registration / Login
    # ------------------------------------------------------------------

    async def register_user(
        self,
        email: str,
        password: str,
        trading_mode: TradingMode = TradingMode.PAPER,
    ) -> dict[str, Any]:
        """
        Registriert neuen User.
        Returns: {user_id, access_token, refresh_token}
        """
        from sgr.core.repositories import get_repositories

        # Passwort-Validierung
        errors = self.validate_password_strength(password)
        if errors:
            raise ValueError(f"Weak password: {'; '.join(errors)}")

        repos = get_repositories()

        # Email bereits vorhanden?
        existing = await repos.users.get_by_email(email)
        if existing:
            raise ValueError("Email already registered")

        # User erstellen
        hashed = self.hash_password(password)
        user_id = await repos.users.create(
            email=email,
            hashed_password=hashed,
            trading_mode=trading_mode,
        )

        # Tokens
        access_token = self.create_access_token(user_id, trading_mode)
        refresh_token = self.create_refresh_token(user_id)

        audit_log.log_auth_event(
            event="register",
            user_id=user_id,
            ip_address="unknown",
            success=True,
        )

        log.info("auth.user_registered", user_id=user_id, trading_mode=trading_mode.value)

        return {
            "user_id": user_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "trading_mode": trading_mode.value,
        }

    async def login(
        self,
        email: str,
        password: str,
        totp_code: str | None = None,
        ip_address: str = "unknown",
    ) -> dict[str, Any]:
        """
        Login: Email + Passwort [+ TOTP falls aktiviert].
        Returns: {access_token, refresh_token, ...}
        """
        from sgr.core.repositories import get_repositories

        repos = get_repositories()
        user = await repos.users.get_by_email(email)

        if not user or not self.verify_password(password, user["hashed_password"]):
            audit_log.log_auth_event(
                event="login_failed",
                user_id=email,
                ip_address=ip_address,
                success=False,
            )
            raise ValueError("Invalid credentials")

        if not user["is_active"]:
            raise ValueError("Account deactivated")

        # 2FA Check
        if user["is_2fa_enabled"]:
            if not totp_code:
                raise ValueError("2FA code required")
            encrypted_secret = user.get("totp_secret", "")
            if not encrypted_secret:
                raise ValueError("2FA not properly configured")
            secret = self.decrypt_totp_secret(encrypted_secret)
            if not self.verify_totp(secret, totp_code):
                audit_log.log_auth_event(
                    event="2fa_failed",
                    user_id=user["id"],
                    ip_address=ip_address,
                    success=False,
                )
                raise ValueError("Invalid 2FA code")

        trading_mode = TradingMode(user["trading_mode"])
        access_token = self.create_access_token(user["id"], trading_mode)
        refresh_token = self.create_refresh_token(user["id"])

        await repos.users.update_last_login(user["id"])

        audit_log.log_auth_event(
            event="login",
            user_id=user["id"],
            ip_address=ip_address,
            success=True,
        )

        return {
            "user_id": user["id"],
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "trading_mode": trading_mode.value,
        }

    async def setup_2fa(self, user_id: str, email: str) -> dict[str, str]:
        """
        Initiiert 2FA-Setup.
        Returns: {secret, totp_uri, qr_code_url}
        User muss dann verify_2fa aufrufen um zu bestätigen.
        """
        from sgr.core.repositories import get_repositories

        secret = self.generate_totp_secret()
        totp_uri = self.get_totp_uri(secret, email)
        encrypted = self.encrypt_totp_secret(secret)

        get_repositories()
        from sqlalchemy import update

        from sgr.core.database import UserModel, get_session

        async with get_session() as session:
            from sqlalchemy import update

            stmt = update(UserModel).where(UserModel.id == user_id).values(totp_secret=encrypted)
            await session.execute(stmt)

        return {
            "totp_uri": totp_uri,
            "secret": secret,  # Nur für initiales Setup – NICHT in DB klar speichern
        }

    async def enable_2fa(self, user_id: str, totp_code: str) -> bool:
        """
        Aktiviert 2FA nach Bestätigung des Codes.
        User muss zuerst setup_2fa aufgerufen haben.
        """
        from sqlalchemy import select, update

        from sgr.core.database import UserModel, get_session

        async with get_session() as session:
            result = await session.execute(select(UserModel).where(UserModel.id == user_id))
            user = result.scalar_one_or_none()
            if not user or not user.totp_secret:
                return False

            secret = self.decrypt_totp_secret(user.totp_secret)
            if not self.verify_totp(secret, totp_code):
                return False

            await session.execute(
                update(UserModel).where(UserModel.id == user_id).values(is_2fa_enabled=True)
            )

        log.info("auth.2fa_enabled", user_id=user_id)
        audit_log.log_auth_event("2fa_enabled", user_id, "system", True)
        return True
