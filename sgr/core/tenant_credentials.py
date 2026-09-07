"""
SGR Tenant Credentials
=======================
Laedt Exchange-Credentials fuer einen Multi-Tenant-Worker aus der DB
(APIKeyModel) statt aus config.credentials (.env).

Kontext (Commit 5, Option A - siehe Entscheidung im Migrationsplan):
Jeder Tenant (z.B. Gordon, Sumo) laeuft als eigener sgr-worker-Container
mit eigener TENANT_ID env var (siehe SGRConfig.tenant_id). Isolation
entsteht durch OS-Prozesstrennung (jeder Worker ist ein eigener
Prozess mit eigener RiskEngine/PortfolioEngine/KillSwitch-Instanz),
nicht durch In-Memory-Multiplexing im API-Prozess.

Wiederverwendet dieselbe Entschluesselungslogik wie
sgr.saas.tenant.TenantManager.get_exchange_adapter() (dort fuer den
inzwischen nicht mehr zum Worker-Modell passenden Pro-Request-Ansatz
in der API gedacht) - dieses Modul ist der worker-seitige Nachfolger
fuer den lifespan()-Startup-Pfad, siehe sgr/api/main.py.

Der bestehende single-tenant .env-Pfad (config.credentials,
config.tenant_id is None) bleibt unveraendert unberuehrt.
"""

from __future__ import annotations

from sgr.core.encryption import get_cipher
from sgr.core.logging import get_logger
from sgr.core.types import ExchangeID, TradingMode

log = get_logger(__name__)


async def load_tenant_credentials(
    tenant_id: str,
    exchange_id: ExchangeID,
    trading_mode: TradingMode,
) -> dict[str, str]:
    """
    Laedt und entschluesselt Exchange-Credentials fuer einen Tenant aus
    der DB (APIKeyModel, ueber sgr.saas.tenant.TenantManager.store_api_key()
    dort abgelegt).

    Raises:
        ValueError: entweder tenant_id ist keine gueltige UUID (TENANT_ID
            env var muss die tatsaechliche users.id-UUID sein, nicht ein
            sprechender Name wie "gordon" - siehe
            docker-compose.prod.yml Kommentar bei TENANT_ID), oder es
            existiert kein aktiver API-Key-Eintrag fuer diese
            (tenant_id, exchange_id, trading_mode)-Kombination. Der
            Aufrufer (lifespan()) behandelt beide Faelle gleich: Exchange
            Pool bleibt fuer diesen Prozess leer, Server startet
            trotzdem (kein fail-fast hier - ein Tenant ohne konfigurierte
            Keys soll den Worker nicht komplett verhindern, siehe
            bestehendes Verhalten in lifespan() Schritt 5).
    """
    from uuid import UUID

    from sqlalchemy import and_, select

    from sgr.core.database import APIKeyModel, get_session as db_session

    try:
        UUID(tenant_id)
    except ValueError as e:
        # APIKeyModel.user_id ist PG_UUID(as_uuid=False) - ein
        # nicht-UUID-Wert wuerde sonst erst als kryptischer
        # asyncpg.exceptions.DataError aus der DB-Query auftauchen
        # (siehe Deployment-Vorfall: TENANT_ID=sumo statt echter UUID).
        raise ValueError(
            f"tenant_id {tenant_id!r} is not a valid UUID. TENANT_ID must "
            f"be the actual users.id UUID (SELECT id FROM users WHERE "
            f"email = ...), not a human-readable name."
        ) from e

    async with db_session() as db:
        result = await db.execute(
            select(APIKeyModel).where(
                and_(
                    APIKeyModel.user_id == tenant_id,
                    APIKeyModel.exchange == exchange_id.value,
                    APIKeyModel.trading_mode == trading_mode.value,
                    APIKeyModel.is_active,
                )
            )
        )
        key_record = result.scalar_one_or_none()

    if key_record is None:
        raise ValueError(
            f"No API keys configured for tenant {tenant_id!r}, "
            f"{exchange_id.value} ({trading_mode.value}). "
            f"Store keys via POST /api-keys (sgr.saas.routers.store_api_key)."
        )

    cipher = get_cipher()
    api_key = cipher.decrypt(
        key_record.encrypted_api_key,
        associated_data=tenant_id.encode(),
    )
    secret = cipher.decrypt(
        key_record.encrypted_secret,
        associated_data=tenant_id.encode(),
    )

    log.info(
        "tenant_credentials.loaded",
        tenant_id=tenant_id,
        exchange=exchange_id.value,
        trading_mode=trading_mode.value,
    )

    return {"apiKey": api_key, "secret": secret}
