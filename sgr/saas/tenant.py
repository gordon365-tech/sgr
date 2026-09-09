"""
SGR Tenant Manager
==================
Verwaltet die Persistenz verschlüsselter Exchange-API-Keys pro Tenant.

STATUS (Audit nach Commit 5, Option 1 - vollstaendig entfernt statt nur
markiert): Dieses Modul enthielt urspruenglich zusaetzlich ein
Pro-Request-In-Memory-Engine-Modell (TenantSession, get_or_create_session,
get_exchange_adapter) fuer den API-Prozess. Das widersprach der seit
Commit 3/4 etablierten Zielarchitektur (API ist strikt read-only, haelt
keine Trading-Engines mehr - siehe sgr/api/dependencies.py Modul-
Docstring) und wurde nirgends aufgerufen. Nach der Entscheidung fuer
Commit 5 (Option A: Tenant-Isolation durch OS-Prozesstrennung, ein
sgr-worker-Container pro Tenant, Credentials via
sgr.core.tenant_credentials.load_tenant_credentials() im
lifespan()-Startup-Pfad) war dieses Modell endgueltig ueberfluessig und
wurde entfernt, statt als toter Code mit vollem Testaufwand liegen zu
bleiben.

Verbleibender Zweck: store_api_key() bleibt der einzige DB-Persistenz-
Layer fuer Tenant-Credentials, aktiv genutzt vom apikey_router (siehe
sgr.saas.routers) - unabhaengig davon, ob ein Worker sie spaeter beim
Start liest.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sgr.core.encryption import get_cipher
from sgr.core.logging import get_logger
from sgr.core.types import TradingMode

log = get_logger(__name__)


class TenantManager:
    """Persistiert verschlüsselte Exchange-API-Keys pro Tenant."""

    async def store_api_key(
        self,
        user_id: str,
        exchange_id: str,
        trading_mode: TradingMode,
        api_key: str,
        secret: str,
        label: str = "default",
    ) -> str:
        """
        Verschlüsselt und speichert API Key für einen User.
        Returns: api_key_id
        """
        import uuid

        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from sgr.core.database import APIKeyModel, get_session as db_session

        cipher = get_cipher()
        # User-ID als Associated Data: Key ist an User gebunden
        encrypted_key = cipher.encrypt(api_key, associated_data=user_id.encode())
        encrypted_secret = cipher.encrypt(secret, associated_data=user_id.encode())

        key_id = str(uuid.uuid4())
        now = datetime.now(tz=UTC)

        async with db_session() as db:
            stmt = pg_insert(APIKeyModel).values(
                id=key_id,
                user_id=user_id,
                exchange=exchange_id,
                trading_mode=trading_mode.value,
                label=label,
                encrypted_api_key=encrypted_key,
                encrypted_secret=encrypted_secret,
                is_active=True,
                created_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_api_key_user_exchange",
                set_={
                    "encrypted_api_key": encrypted_key,
                    "encrypted_secret": encrypted_secret,
                    "label": label,
                    "is_active": True,
                },
            )
            await db.execute(stmt)

        log.info(
            "tenant_manager.api_key_stored",
            user_id=user_id,
            exchange=exchange_id,
            mode=trading_mode.value,
        )

        return key_id


# Singleton
_manager: TenantManager | None = None


def get_tenant_manager() -> TenantManager:
    global _manager
    if _manager is None:
        _manager = TenantManager()
    return _manager
