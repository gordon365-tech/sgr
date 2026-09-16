#!/usr/bin/env python3
"""
Docker-Crash-Test Entrypoint
=============================
Laeuft INNERHALB eines ephemeren "sgr-crashtest-worker"-Containers (siehe
tests/docker_crash_tests/docker_control.py), NIE in sgr-worker-gordon/
-sumo selbst - so lassen sich Container-Kill/-Restart-Szenarien real
gegen einen echten, eigenstaendigen Worker-Prozess fahren, ohne jemals
eine echte Gordon/Sumo-Position oder deren Kill-Switch-Zustand zu
beruehren.

Fuehrt GENAU EINE echte Order ueber den echten Produktionscode-Pfad aus:
    ExecutionEngine.execute() -> SafeOrderExecutor.execute_safely()
    -> BinanceAdapter.place_order() -> _simulate_order() (PAPER, kein
       echter Netzwerk-Call zu einer Exchange fuer die Order selbst -
       nur get_ticker() ist ein echter, oeffentlicher Marktdaten-Call)
    -> echtes OrderRepository/PositionRepository gegen die echte,
       laufende Postgres-Instanz.

Der Docker-Treiber (test_crash_scenarios.py) muss den exakten Moment
"Order-Verarbeitung laeuft gerade" von aussen erkennen koennen, um den
Container deterministisch genau dann zu killen/neuzustarten - dafuer
setzt dieses Skript einen Redis-Marker (CRASHTEST_SIGNAL_KEY), BEVOR es
in die eigentliche (optional kuenstlich verzoegerte) Order-Verarbeitung
eintritt. Das ist Test-Timing-Steuerung, KEIN Mock der Exchange-Logik
selbst - place_order()/_simulate_order() laufen unveraendert echt.

Environment-Variablen (alle required, ausser CRASHTEST_* mit Default):
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME  - echte Postgres-Instanz
    REDIS_HOST, REDIS_PORT                            - echte Redis-Instanz
    TENANT_ID                                         - isolierter Test-Tenant
    CRASHTEST_ORDER_ID, CRASHTEST_SIGNAL_ID           - UUIDs
    CRASHTEST_SYMBOL   (z.B. "BTC/USDT")
    CRASHTEST_SIDE     ("buy" | "sell")
    CRASHTEST_QTY      (Decimal-String)
    CRASHTEST_SIGNAL_KEY                              - Redis-Key fuer Sync
    CRASHTEST_DELAY_SECONDS   (default "3")           - Verzoegerung VOR
        dem eigentlichen Exchange-Call, damit der Treiber Zeit hat, den
        Container zu killen, nachdem CRASHTEST_SIGNAL_KEY gesetzt wurde.
    CRASHTEST_FAULT   (optional: "timeout")           - injiziert einen
        ExchangeConnectionError anstatt eines echten Fills (fuer das
        Exchange-Timeout/Netzwerkfehler-Szenario, siehe dortigen Test).
"""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from uuid import UUID


async def main() -> int:
    import redis.asyncio as aioredis

    from sgr.core.database import close_db, init_db
    from sgr.core.event_bus import get_event_bus
    from sgr.core.repositories import OrderRepository
    from sgr.core.types import ExchangeID, OrderRequest, OrderType, Side, Symbol, TradingMode
    from sgr.exchanges.base import ExchangeConnectionError
    from sgr.exchanges.binance import BinanceAdapter
    from sgr.execution.engine import ExecutionEngine

    signal_key = os.environ["CRASHTEST_SIGNAL_KEY"]
    redis_client = aioredis.from_url(
        f"redis://{os.environ['REDIS_HOST']}:{os.environ['REDIS_PORT']}",
        decode_responses=True,
    )

    await init_db()

    # EventBus (Redis Streams) VOR der Order-Verarbeitung verbinden -
    # spiegelt die echte Init-Reihenfolge in api/main.py::lifespan()
    # wider. ExecutionEngine._on_fill() publiziert OrderFilledEvent ueber
    # denselben get_event_bus() Singleton (siehe execution/engine.py) -
    # ohne diese Verbindung waere der Redis-Ausfall-Testfall ein reines
    # No-Op (die Order-Ausfuehrung selbst beruehrt sonst nirgends Redis,
    # siehe KillSwitch.is_active - synchron, rein in-memory).
    event_bus_connected = False
    try:
        await get_event_bus().connect()
        event_bus_connected = True
    except Exception as e:  # noqa: BLE001
        print(f"crashtest: event bus connect failed (continuing): {e}", file=sys.stderr)

    base, quote = os.environ["CRASHTEST_SYMBOL"].split("/")
    symbol = Symbol(base=base, quote=quote, exchange=ExchangeID.BINANCE)

    order = OrderRequest(
        id=UUID(os.environ["CRASHTEST_ORDER_ID"]),
        signal_id=UUID(os.environ["CRASHTEST_SIGNAL_ID"]),
        symbol=symbol,
        side=Side(os.environ["CRASHTEST_SIDE"]),
        order_type=OrderType.MARKET,
        quantity=Decimal(os.environ["CRASHTEST_QTY"]),
        trading_mode=TradingMode.PAPER,
        metadata={"strategy": "crashtest"},
    )

    adapter = BinanceAdapter(
        api_key="crashtest", secret="crashtest", trading_mode=TradingMode.PAPER
    )
    await adapter.connect()

    delay = float(os.environ.get("CRASHTEST_DELAY_SECONDS", "3"))
    fault = os.environ.get("CRASHTEST_FAULT", "")
    real_place_order = adapter.place_order

    async def instrumented_place_order(o: OrderRequest):  # type: ignore[no-untyped-def]
        # Marker VOR der Verzoegerung setzen - der Treiber wartet exakt
        # hierauf, um den Container in einem deterministischen,
        # reproduzierbaren Moment zu killen (siehe Modul-Docstring).
        await redis_client.set(signal_key, "started", ex=180)
        if delay > 0:
            await asyncio.sleep(delay)
        if fault == "timeout":
            raise ExchangeConnectionError(
                exchange="binance", detail="crashtest: injected timeout fault"
            )
        return await real_place_order(o)

    adapter.place_order = instrumented_place_order  # type: ignore[method-assign]

    class _SinglePool:
        def get(self, exchange_id: ExchangeID, trading_mode: TradingMode) -> BinanceAdapter:
            return adapter

    order_repo = OrderRepository()
    engine = ExecutionEngine(_SinglePool(), TradingMode.PAPER, order_repository=order_repo)  # type: ignore[arg-type]

    # WICHTIG: dies ist der einzige Aufruf des echten Produktionscode-
    # Pfads - alles danach ist reines Test-Harness-Reporting (Ergebnis
    # fuer den Treiber nach Redis zurueckschreiben), NICHT Teil dessen,
    # was getestet werden soll. Beide Phasen sind deshalb bewusst
    # getrennt: ein Fehler beim Zurueckschreiben (z.B. weil GENAU DAS
    # Redis-Ausfall-Szenario gerade laeuft) darf niemals so aussehen wie
    # ein Absturz der echten Order-Verarbeitung - sonst waere das
    # Testergebnis eine Verwechslung von Testinfrastruktur- und
    # Produktcode-Fehler (gefunden beim ersten Lauf des Redis-Ausfall-
    # Szenarios: dieselbe Ursache haette faelschlich als Produktbug
    # gemeldet werden koennen).
    try:
        result = await engine.execute(order)
    finally:
        await adapter.close()
        if event_bus_connected:
            try:
                await get_event_bus().close()
            except Exception:  # noqa: BLE001 - best-effort, Redis evtl. gerade down
                pass
        await close_db()

    try:
        await redis_client.set(f"{signal_key}:result", result.status.value, ex=180)
        await redis_client.set(
            f"{signal_key}:exchange_order_id", result.exchange_order_id or "", ex=180
        )
        await redis_client.set(
            f"{signal_key}:raw_response", str(dict(result.raw_response)), ex=180
        )
    except Exception as e:  # noqa: BLE001
        print(
            f"crashtest: reporting result to redis failed (order processing itself "
            f"already completed with status={result.status.value!r}): {e}",
            file=sys.stderr,
        )
    finally:
        try:
            await redis_client.aclose()
        except Exception:  # noqa: BLE001
            pass

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
