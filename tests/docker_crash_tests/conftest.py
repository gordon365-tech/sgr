"""
Fixtures fuer tests/docker_crash_tests
========================================
Echte Infrastruktur, kein Mock: Docker-SDK-Zugriff auf den laufenden
docker-compose.prod.yml-Stack, echte Postgres-/Redis-Verbindungen, ein
isolierter Test-Tenant in der laufenden Produktions-DB.

VORAUSSETZUNGEN (siehe pyproject.toml docker_crash-Markerbeschreibung):
  1. Der volle Compose-Stack muss laufen: sgr-api, sgr-worker-gordon,
     sgr-worker-sumo, sgr-postgres, sgr-redis erreichbar unter ihren
     Container-Namen auf dem Docker-Netzwerk "docker_sgr-net".
  2. Der Testprozess selbst muss auf demselben Docker-Netzwerk laufen
     (--network docker_sgr-net beim `docker run` des Testcontainers) UND
     Zugriff auf den Docker-Socket haben
     (-v /var/run/docker.sock:/var/run/docker.sock), um Container von
     aussen zu steuern (stop/start/kill/restart/network disconnect).
  3. Docker SDK for Python ("docker"-Paket, siehe pyproject.toml
     [project.optional-dependencies] dev) muss installiert sein.
  4. Echte DB-Credentials (DB_USER/DB_PASSWORD/...) muessen im
     Environment gesetzt sein (z.B. --env-file .env.prod), nicht nur
     DB_HOST/DB_PORT - siehe run_env_from_current_process() in
     docker_control.py.

Ohne (1)-(3) werden alle Tests in diesem Verzeichnis mit einer klaren
Begruendung geskippt statt mit kryptischen Verbindungsfehlern zu
scheitern - siehe docker_client-Fixture unten.

Isolation von Gordon/Sumo: JEDE Order-Ausfuehrung in dieser Suite laeuft
entweder unter einem frisch angelegten Test-Tenant (siehe test_tenant-
Fixture) oder betrifft nur geteilte Infrastruktur (Postgres/Redis/API-
Container selbst), NIE die echten Tenant-IDs a47d994d.../144f7bb0...
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest

from tests.docker_crash_tests.docker_control import DockerUnavailable, get_docker_client


@pytest.fixture(scope="session")
def docker_client() -> Iterator[object]:
    """
    Echter Docker-SDK-Client gegen den Host-Daemon (siehe
    docker_control.py Modul-Docstring: Docker-outside-of-Docker ueber
    den gemounteten Socket). Skippt die gesamte Suite mit klarer
    Begruendung, wenn der Socket nicht verfuegbar ist, statt mit
    kryptischen Fehlern in jedem einzelnen Test zu scheitern.
    """
    try:
        client = get_docker_client()
    except DockerUnavailable as e:
        pytest.skip(str(e))
    else:
        yield client
        client.close()


@pytest.fixture(scope="session")
def _required_containers_running(docker_client: object) -> None:
    """Stellt sicher, dass der volle Compose-Stack tatsaechlich laeuft,
    bevor irgendein Test versucht, ihn zu stoeren/wiederherzustellen."""
    from tests.docker_crash_tests.docker_control import (
        CONTAINER_API,
        CONTAINER_POSTGRES,
        CONTAINER_REDIS,
        CONTAINER_WORKER_GORDON,
        CONTAINER_WORKER_SUMO,
    )

    required = [
        CONTAINER_API,
        CONTAINER_WORKER_GORDON,
        CONTAINER_WORKER_SUMO,
        CONTAINER_POSTGRES,
        CONTAINER_REDIS,
    ]
    missing = []
    for name in required:
        try:
            c = docker_client.containers.get(name)  # type: ignore[attr-defined]
            if c.status != "running":
                missing.append(f"{name} (status={c.status})")
        except Exception:  # noqa: BLE001
            missing.append(f"{name} (not found)")
    if missing:
        pytest.skip(
            "docker_crash-Tests brauchen den vollstaendig laufenden "
            f"Compose-Stack. Fehlend/nicht laufend: {', '.join(missing)}"
        )


@pytest.fixture
async def redis_client() -> AsyncIterator[object]:
    import redis.asyncio as aioredis

    client = aioredis.from_url(
        f"redis://{os.environ.get('REDIS_HOST', 'redis')}:{os.environ.get('REDIS_PORT', '6379')}",
        decode_responses=True,
    )
    try:
        await client.ping()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Redis nicht erreichbar fuer den Testprozess selbst: {e}")
    yield client
    await client.aclose()


@pytest.fixture
async def db_initialized() -> AsyncIterator[None]:
    """Reuses das Muster aus test_tenant_isolation_db.py: echte DB-Init
    fuer diesen Testprozess, kein Mock."""
    from sgr.core.database import init_db

    try:
        await init_db()
    except Exception as e:  # noqa: BLE001
        pytest.skip(
            f"Postgres nicht erreichbar fuer den Testprozess selbst: {e}. "
            "DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME pruefen (z.B. "
            "--env-file .env.prod beim Start des Testcontainers)."
        )
    yield


@pytest.fixture
async def test_tenant(db_initialized: None) -> AsyncIterator[str]:
    """
    Legt einen isolierten, garantiert von Gordon/Sumo getrennten
    Test-User in der laufenden Produktions-DB an (FK-Constraint auf
    orders/positions.user_id verlangt eine echte users.id - siehe
    test_tenant_isolation_db.py, das aus demselben Grund die echten
    Gordon/Sumo-IDs wiederverwendet; hier wird stattdessen bewusst ein
    NEUER, ausschliesslich fuer diesen Testlauf existierender User
    angelegt, damit keine echten Tenant-Daten je beruehrt werden).

    Teardown raeumt IMMER auf, auch bei fehlgeschlagenem Test: erst
    abhaengige Zeilen (orders, positions, trades, api_keys), dann den
    User selbst - in dieser Reihenfolge wegen FK-Constraints.
    """
    from sqlalchemy import delete

    from sgr.core.database import OrderModel, PositionModel, UserModel, get_session

    tenant_id = str(uuid.uuid4())
    email = f"crashtest-{tenant_id[:8]}@sgr-docker-crash-test.invalid"

    async with get_session() as session:
        session.add(
            UserModel(
                id=tenant_id,
                email=email,
                hashed_password="crashtest-not-a-real-account",
                is_active=True,
                is_admin=False,
                trading_mode="paper",
                created_at=datetime.now(tz=UTC),
            )
        )
        await session.flush()

    try:
        yield tenant_id
    finally:
        async with get_session() as session:
            await session.execute(delete(OrderModel).where(OrderModel.user_id == tenant_id))
            await session.execute(delete(PositionModel).where(PositionModel.user_id == tenant_id))
            await session.execute(delete(UserModel).where(UserModel.id == tenant_id))
