"""
Alembic Migration Environment
==============================
Konfiguriert Alembic für async SQLAlchemy + TimescaleDB.

Wichtige Punkte:
    - URL aus Umgebungsvariablen (nie aus alembic.ini direkt)
    - Async Engine für PostgreSQL
    - Autogenerate: erkennt Schema-Änderungen aus ORM-Models
    - TimescaleDB: Hypertable-Erstellung in separater Migration
    - Rollback-sicher: jede Migration in einer Transaktion

Usage:
    alembic upgrade head    # Alle Migrationen anwenden
    alembic downgrade -1    # Eine Migration zurück
    alembic revision --autogenerate -m "add_ml_predictions"
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alembic import context

# Alembic Config-Objekt
config = context.config

# Logging aus alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import aller ORM-Models für Autogenerate
from sgr.core.database import Base
from sgr.core.database import (  # noqa: F401 – sicherstellen dass alle Models importiert sind
    APIKeyModel,
    CandleModel,
    OrderModel,
    PositionModel,
    RiskEventModel,
    StrategyModel,
    TradeModel,
    UserModel,
)

target_metadata = Base.metadata


def get_url() -> str:
    """
    DB-URL aus Umgebungsvariablen.
    Priorität: DATABASE_URL > einzelne DB_* Variablen > alembic.ini
    """
    # Alembic braucht sync URL (nicht asyncpg)
    url = os.environ.get("DATABASE_URL")
    if url:
        # asyncpg → psycopg2 für Alembic
        return url.replace("postgresql+asyncpg://", "postgresql://")

    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ.get("DB_NAME", "sgr")
    user = os.environ.get("DB_USER", "sgr")
    password = os.environ.get("DB_PASSWORD", "changeme")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def run_migrations_offline() -> None:
    """
    Offline-Modus: SQL-Skript generieren ohne DB-Verbindung.
    Nützlich für Code-Review und Audit.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # PostgreSQL-spezifisch: Schema-Vergleich
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Async-Migrationen für asyncpg."""
    url = get_url()
    connectable = create_async_engine(
        url.replace("postgresql://", "postgresql+asyncpg://"),
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Online-Modus: direkte DB-Verbindung."""
    # Sync-Fallback für Alembic-Kompatibilität
    url = get_url()
    connectable = engine_from_config(
        {"sqlalchemy.url": url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
