"""
SGR Tenant Manager
==================
Verwaltet per-User Engine-Instanzen für Multi-Tenant-Betrieb.

Kern-Prinzip: Vollständige Isolation zwischen Tenants.
    - Jeder User hat eigene Portfolio-Engine-Instanz
    - Jeder User hat eigenen Kill Switch
    - Jeder User hat eigene API-Key-Verschlüsselung
    - PostgreSQL Row-Level Security für DB-Isolation

Warum eigene Engine-Instanzen statt shared State?
    - Kein Cross-Tenant-Leak möglich (State-Isolation)
    - Kill Switch triggert nur für betroffenen User
    - Unabhängige Risk-Limits (Enterprise konfigurierbar)
    - Einfacheres Debugging (klare Tenant-Zuordnung)

Memory:
    Engines werden lazy erstellt und gecacht.
    Inaktive Tenants (>30 Min kein Request) werden aus Cache entfernt.
    Maximale gleichzeitige Tenants: 1000 (konfigurierbar).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sgr.core.encryption import get_cipher
from sgr.core.logging import get_logger
from sgr.core.types import ExchangeID, TradingMode
from sgr.portfolio.engine import PortfolioEngine
from sgr.risk.engine import RiskEngine
from sgr.risk.kill_switch import KillSwitch
from sgr.saas.types import TenantConfig

log = get_logger(__name__)

# Max gleichzeitige Tenant-Sessions (Memory-Schutz)
_MAX_TENANT_CACHE = 1000
# Inaktiv-Timeout: Engine aus Cache entfernen
_TENANT_IDLE_TIMEOUT_MINUTES = 30


class TenantSession:
    """
    Vollständige Engine-Session für einen Tenant.
    Enthält alle nötigen Instanzen für Trading.
    """

    def __init__(
        self,
        user_id: str,
        trading_mode: TradingMode,
        config: TenantConfig,
    ) -> None:
        self.user_id = user_id
        self.trading_mode = trading_mode
        self.config = config
        self.last_activity = datetime.now(tz=UTC)

        # Per-Tenant Engine-Instanzen
        self.portfolio_engine = PortfolioEngine(
            trading_mode=trading_mode,
            initial_cash=Decimal("10000"),  # Wird aus DB geladen
        )
        self.risk_engine = RiskEngine(trading_mode=trading_mode)
        self.kill_switch = KillSwitch(trading_mode=trading_mode)

        # Exchange Adapters (lazy erstellt)
        self._adapters: dict[str, Any] = {}

    def touch(self) -> None:
        """Aktualisiert Last-Activity-Timestamp."""
        self.last_activity = datetime.now(tz=UTC)

    @property
    def is_idle(self) -> bool:
        idle_threshold = datetime.now(tz=UTC) - timedelta(minutes=_TENANT_IDLE_TIMEOUT_MINUTES)
        return self.last_activity < idle_threshold

    async def initialize(self) -> None:
        """Initialisiert Risk Engine für diesen Tenant."""
        await self.risk_engine.initialize()


class TenantManager:
    """
    Verwaltet alle aktiven Tenant-Sessions.
    Singleton – eine Instanz pro API-Server.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, TenantSession] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Startet Background-Cleanup für idle Sessions."""
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="tenant_manager:cleanup",
        )
        log.info("tenant_manager.started")

    async def stop(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        log.info("tenant_manager.stopped")

    # ------------------------------------------------------------------
    # Session Management
    # ------------------------------------------------------------------

    async def get_or_create_session(
        self,
        user_id: str,
        trading_mode: TradingMode,
        tenant_config: TenantConfig | None = None,
    ) -> TenantSession:
        """
        Gibt bestehende Session zurück oder erstellt neue.
        Thread-safe via asyncio.Lock.
        """
        async with self._lock:
            if user_id in self._sessions:
                session = self._sessions[user_id]
                session.touch()
                return session

            # Cache-Limit prüfen
            if len(self._sessions) >= _MAX_TENANT_CACHE:
                await self._evict_oldest()

            # Neue Session erstellen
            config = tenant_config or TenantConfig(user_id=user_id)
            session = TenantSession(user_id, trading_mode, config)
            await session.initialize()

            self._sessions[user_id] = session

            log.info(
                "tenant_manager.session_created",
                user_id=user_id,
                trading_mode=trading_mode.value,
            )

            return session

    def get_session(self, user_id: str) -> TenantSession | None:
        """Gibt Session zurück wenn vorhanden, sonst None."""
        session = self._sessions.get(user_id)
        if session:
            session.touch()
        return session

    async def destroy_session(self, user_id: str) -> None:
        """Entfernt Tenant-Session (Logout / Account-Deletion)."""
        async with self._lock:
            if user_id in self._sessions:
                del self._sessions[user_id]
                log.info("tenant_manager.session_destroyed", user_id=user_id)

    # ------------------------------------------------------------------
    # API Key Management (per Tenant)
    # ------------------------------------------------------------------

    async def get_exchange_adapter(
        self,
        user_id: str,
        exchange_id: ExchangeID,
        trading_mode: TradingMode,
    ) -> Any:
        """
        Gibt Exchange Adapter für einen User zurück.
        Lädt verschlüsselte API Keys aus DB und entschlüsselt sie.
        """
        from sqlalchemy import and_, select

        from sgr.core.database import APIKeyModel, get_session as db_session

        session = self._sessions.get(user_id)
        if not session:
            raise ValueError(f"No session for user {user_id}")

        # Cache Check
        adapter_key = f"{exchange_id.value}:{trading_mode.value}"
        if adapter_key in session._adapters:
            return session._adapters[adapter_key]

        # API Keys aus DB laden
        async with db_session() as db:
            result = await db.execute(
                select(APIKeyModel).where(
                    and_(
                        APIKeyModel.user_id == user_id,
                        APIKeyModel.exchange == exchange_id.value,
                        APIKeyModel.trading_mode == trading_mode.value,
                        APIKeyModel.is_active,
                    )
                )
            )
            key_record = result.scalar_one_or_none()

        if not key_record:
            raise ValueError(
                f"No API keys configured for {exchange_id.value} ({trading_mode.value})"
            )

        # Entschlüsseln
        cipher = get_cipher()
        api_key = cipher.decrypt(
            key_record.encrypted_api_key,
            associated_data=user_id.encode(),
        )
        secret = cipher.decrypt(
            key_record.encrypted_secret,
            associated_data=user_id.encode(),
        )

        # Adapter erstellen
        from sgr.exchanges.factory import ExchangeFactory

        adapter = ExchangeFactory.create_with_credentials(
            exchange_id=exchange_id,
            trading_mode=trading_mode,
            api_key=api_key,
            secret=secret,
        )
        await adapter.connect()

        session._adapters[adapter_key] = adapter

        log.info(
            "tenant_manager.adapter_created",
            user_id=user_id,
            exchange=exchange_id.value,
            mode=trading_mode.value,
        )

        return adapter

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

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @property
    def active_sessions(self) -> int:
        return len(self._sessions)

    def get_stats(self) -> dict[str, Any]:
        return {
            "active_sessions": self.active_sessions,
            "max_sessions": _MAX_TENANT_CACHE,
            "idle_timeout_minutes": _TENANT_IDLE_TIMEOUT_MINUTES,
        }

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _cleanup_loop(self) -> None:
        """Entfernt idle Sessions alle 5 Minuten."""
        while True:
            await asyncio.sleep(300)
            try:
                await self._cleanup_idle()
            except Exception as e:
                log.error("tenant_manager.cleanup_error", error=str(e))

    async def _cleanup_idle(self) -> None:
        async with self._lock:
            idle_users = [uid for uid, s in self._sessions.items() if s.is_idle]
            for uid in idle_users:
                del self._sessions[uid]
            if idle_users:
                log.info("tenant_manager.idle_cleaned", count=len(idle_users))

    async def _evict_oldest(self) -> None:
        """Entfernt älteste Session wenn Cache voll."""
        if not self._sessions:
            return
        oldest_uid = min(
            self._sessions,
            key=lambda uid: self._sessions[uid].last_activity,
        )
        del self._sessions[oldest_uid]
        log.warning("tenant_manager.session_evicted", user_id=oldest_uid)


# Singleton
_manager: TenantManager | None = None


def get_tenant_manager() -> TenantManager:
    global _manager
    if _manager is None:
        _manager = TenantManager()
    return _manager
