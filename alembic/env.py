"""
Alembic configuration for async SQLAlchemy + TimescaleDB.

Features:
- URL from environment variables
- Async SQLAlchemy engine
- Autogenerate from ORM models
- TimescaleDB compatible
- Transaction safe migrations

Usage:
    alembic upgrade head
    alembic downgrade -1
    alembic revision --autogenerate -m "description"
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context


# Alembic Config object
config = context.config


# Logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


# Import ORM metadata. The model imports below are required as side effects
# (they register each model's table with Base.metadata) even though they are
# not referenced directly here — needed for `alembic revision --autogenerate`.
from sgr.core.database import Base
from sgr.core.database import (  # noqa: F401
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
    Database URL from environment.

    Priority:
    1. DATABASE_URL
    2. DB_* variables
    3. alembic.ini fallback
    """

    url = os.environ.get("DATABASE_URL")

    if url:
        return url.replace(
            "postgresql+asyncpg://",
            "postgresql://",
        )

    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ.get("DB_NAME", "sgr")
    user = os.environ.get("DB_USER", "sgr")
    password = os.environ.get(
        "DB_PASSWORD",
        "changeme",
    )

    return (
        f"postgresql://{user}:{password}"
        f"@{host}:{port}/{name}"
    )


def run_migrations_offline() -> None:
    """
    Generate SQL without database connection.
    """

    url = get_url()

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={
            "paramstyle": "named"
        },
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """
    Execute migrations on sync connection
    provided by async engine.
    """

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    Run migrations using asyncpg.
    """

    url = get_url()

    connectable = create_async_engine(
        url.replace(
            "postgresql://",
            "postgresql+asyncpg://",
        ),
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(
            do_run_migrations
        )

    await connectable.dispose()


def run_migrations_online() -> None:
    """
    Online migration entrypoint.
    """

    asyncio.run(
        run_async_migrations()
    )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
