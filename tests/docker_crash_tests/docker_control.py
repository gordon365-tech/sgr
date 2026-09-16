"""
Docker-Steuerung fuer die docker_crash-Testsuite
==================================================
Duenner Wrapper um das Docker SDK (Paket "docker") - jede Funktion hier
ruft eine ECHTE Docker-Engine-Operation auf (stop/start/kill/restart/
network disconnect), kein Mock.

WICHTIG (Docker-outside-of-Docker): dieser Testprozess laeuft selbst in
einem Container mit gemountetem /var/run/docker.sock. Der Docker-Daemon
dahinter ist der HOST-Daemon - Bind-Mount-Quellen in `containers.run()`
muessen deshalb HOST-Pfade sein, nicht Pfade innerhalb DIESES Containers.
Da das Repo hier 1:1 unter demselben Pfad gemountet ist wie auf dem Host
(siehe REPO_ROOT unten), ist das in dieser Umgebung unproblematisch.

Container-/Netzwerknamen sind an docker/docker-compose.prod.yml
angelehnt (container_name: / networks: sgr-net, externer Compose-
Projektname "docker" -> Netzwerkname "docker_sgr-net", siehe
`docker network ls`).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

CONTAINER_API = "sgr-api"
CONTAINER_WORKER_GORDON = "sgr-worker-gordon"
CONTAINER_WORKER_SUMO = "sgr-worker-sumo"
CONTAINER_POSTGRES = "sgr-postgres"
CONTAINER_REDIS = "sgr-redis"
NETWORK_NAME = "docker_sgr-net"
CRASHTEST_IMAGE = "sgr-worker:latest"

# WICHTIG: NICHT Path(__file__).resolve() verwenden. Dieser Testprozess
# laeuft selbst in einem Container, der das Repo unter /app mountet -
# __file__ loest deshalb zu einem Pfad IN DIESEM Container auf (z.B.
# /app/tests/...), waehrend containers.run(volumes=...) unten gegen den
# HOST-Daemon geht (siehe Docker-outside-of-Docker-Hinweis oben). Ein
# nicht auf dem Host existierender Bind-Mount-Quellpfad fuehrt NICHT zu
# einem Fehler, sondern dazu, dass Docker dort still ein leeres
# Verzeichnis anlegt ("can't find '__main__' module in
# '/crashtest_entrypoint.py'" beim Ausfuehren) - genau deshalb explizit
# ueber eine Env-Var konfigurierbar, mit dem in dieser Umgebung bekannten
# Host-Pfad als Default.
REPO_ROOT = Path(os.environ.get("HOST_REPO_ROOT", "/home/ubuntu/sgr"))
ENTRYPOINT_HOST_PATH = str(REPO_ROOT / "tests" / "docker_crash_tests" / "_crashtest_entrypoint.py")


class DockerUnavailable(RuntimeError):
    """Docker-Socket/-Daemon nicht erreichbar - Voraussetzung fuer den
    gesamten docker_crash-Marker (siehe pyproject.toml Markerbeschreibung
    und conftest.py docker_client-Fixture)."""


def get_docker_client() -> Any:
    from docker.errors import DockerException

    import docker

    try:
        client = docker.from_env()
        client.ping()
        return client
    except DockerException as e:
        raise DockerUnavailable(
            "Docker-Socket nicht erreichbar. Voraussetzung fuer diese "
            "Tests: /var/run/docker.sock muss in den Testcontainer "
            "gemountet sein (-v /var/run/docker.sock:/var/run/docker.sock) "
            "und das Docker-SDK-Paket 'docker' muss installiert sein "
            "(siehe pyproject.toml [project.optional-dependencies] dev)."
        ) from e


def wait_until(
    predicate: Callable[[], bool],
    timeout: float = 30.0,
    interval: float = 0.5,
    description: str = "condition",
) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as e:  # noqa: BLE001
            last_exc = e
        time.sleep(interval)
    detail = f" (letzter Fehler: {last_exc})" if last_exc else ""
    raise TimeoutError(f"Timeout nach {timeout}s beim Warten auf: {description}{detail}")


def wait_for_status(client: Any, container_name: str, status: str, timeout: float = 30.0) -> None:
    def check() -> bool:
        c = client.containers.get(container_name)
        c.reload()
        return bool(c.status == status)

    wait_until(check, timeout=timeout, description=f"Container {container_name} Status=={status}")


def wait_for_healthy(client: Any, container_name: str, timeout: float = 90.0) -> None:
    def check() -> bool:
        c = client.containers.get(container_name)
        c.reload()
        health = c.attrs.get("State", {}).get("Health")
        if not health:
            return bool(c.status == "running")
        return bool(health.get("Status") == "healthy")

    wait_until(check, timeout=timeout, description=f"Container {container_name} healthy")


def run_env_from_current_process(extra: dict[str, str]) -> dict[str, str]:
    """
    Baut das Environment fuer einen ephemeren Crashtest-Worker-Container
    aus den bereits im aktuellen Testprozess gesetzten DB_*/REDIS_*-
    Variablen (dieser Prozess wurde selbst mit --env-file .env.prod
    gestartet, siehe README/Testlauf-Kommando) plus den Test-spezifischen
    CRASHTEST_*/TENANT_ID-Werten. Vermeidet, .env.prod ein zweites Mal
    zu parsen.
    """
    passthrough_keys = [
        "DB_HOST",
        "DB_PORT",
        "DB_USER",
        "DB_PASSWORD",
        "DB_NAME",
        "REDIS_HOST",
        "REDIS_PORT",
        "REDIS_PASSWORD",
        "ENCRYPTION_MASTER_KEY",
        "SECRET_KEY",
    ]
    env = {k: os.environ[k] for k in passthrough_keys if k in os.environ}
    env["ENVIRONMENT"] = "production"
    env["TRADING_MODE"] = "paper"
    env["LOG_LEVEL"] = "info"
    env.update(extra)
    return env


def start_crashtest_worker(
    client: Any,
    *,
    name: str,
    tenant_id: str,
    order_id: str,
    signal_id: str,
    signal_key: str,
    symbol: str = "BTC/USDT",
    side: str = "buy",
    qty: str = "0.001",
    delay_seconds: float = 5.0,
    fault: str | None = None,
) -> Any:
    """
    Startet einen ECHTEN, eigenstaendigen Worker-Container (sgr-worker:
    latest Image - dasselbe Image wie Gordon/Sumo, nur eigener Name/
    Command) fuer genau eine Order-Ausfuehrung. Siehe
    _crashtest_entrypoint.py Modul-Docstring fuer den vollstaendigen
    Ablauf.
    """
    env = run_env_from_current_process(
        {
            "TENANT_ID": tenant_id,
            "CRASHTEST_ORDER_ID": order_id,
            "CRASHTEST_SIGNAL_ID": signal_id,
            "CRASHTEST_SYMBOL": symbol,
            "CRASHTEST_SIDE": side,
            "CRASHTEST_QTY": qty,
            "CRASHTEST_SIGNAL_KEY": signal_key,
            "CRASHTEST_DELAY_SECONDS": str(delay_seconds),
        }
    )
    if fault:
        env["CRASHTEST_FAULT"] = fault

    return client.containers.run(
        CRASHTEST_IMAGE,
        command=["python", "/crashtest_entrypoint.py"],
        environment=env,
        network=NETWORK_NAME,
        volumes={ENTRYPOINT_HOST_PATH: {"bind": "/crashtest_entrypoint.py", "mode": "ro"}},
        name=name,
        detach=True,
        remove=False,
    )


async def await_container_status(
    client: Any, name: str, status: str, timeout: float = 60.0, interval: float = 0.5
) -> None:
    """Async-freundliche Variante von wait_for_status() - pollt ueber
    asyncio.sleep() statt time.sleep(), damit sie in einer async
    Testfunktion neben anderen awaits verwendet werden kann."""
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout
    last_status = None
    while asyncio.get_event_loop().time() < deadline:
        c = client.containers.get(name)
        c.reload()
        last_status = c.status
        if last_status == status:
            return
        await asyncio.sleep(interval)
    raise TimeoutError(
        f"Timeout nach {timeout}s: Container {name} erreichte nicht Status "
        f"{status!r} (zuletzt: {last_status!r})"
    )


async def await_redis_key(
    client: Any, key: str, timeout: float = 20.0, interval: float = 0.2
) -> str:
    """Wartet auf das Erscheinen eines Redis-Keys (Sync-Marker zwischen
    Testtreiber und Crashtest-Worker-Container, siehe
    _crashtest_entrypoint.py Modul-Docstring)."""
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        val = await client.get(key)
        if val is not None:
            return str(val)
        await asyncio.sleep(interval)
    raise TimeoutError(f"Timeout nach {timeout}s: Redis-Key {key!r} erschien nicht")


def cleanup_container(client: Any, name: str) -> None:
    """Best-effort: entfernt einen (ephemeren Test-)Container, egal in
    welchem Zustand er sich gerade befindet."""
    try:
        c = client.containers.get(name)
        c.remove(force=True)
    except Exception:  # noqa: BLE001 - best-effort Cleanup, Container existiert evtl. schon nicht mehr
        pass
