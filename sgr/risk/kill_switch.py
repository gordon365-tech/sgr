"""
SGR Kill Switch
===============
Notfall-Stopp für alle Trading-Aktivitäten.

Trigger-Szenarien (automatisch):
    1. Max Portfolio Drawdown überschritten
    2. Daily Loss Limit erreicht
    3. Exchange-Verbindung > 30s unterbrochen
    4. Manuelle Auslösung (API, Dashboard)

Verhalten nach Trigger:
    1. Trading-Flag auf STOPPED setzen (synchron, sofort)
    2. Alle offenen Orders canceln (async, best-effort)
    3. Optional: alle Positionen schließen (konfigurierbar)
    4. Kill Switch Event auf Event Bus publizieren
    5. Alert senden (Telegram, PagerDuty)
    6. Kein automatischer Reset – nur manuell

Design-Entscheidung: Synchroner State, Async Side Effects
    Der killed-State wird synchron gesetzt (in-memory boolean).
    Damit ist der Schutz sofort aktiv – bevor erste async Operation.
    Alle nachgelagerten Aktionen (Cancel Orders) sind async aber
    der trading_allowed Check ist O(1) ohne Await.

    WICHTIG (Multi-Prozess-Betrieb, seit sgr-api/sgr-worker-Trennung):
    Diese Garantie gilt weiterhin fuer den PROZESS, der tatsaechlich
    Trades ausfuehrt (sgr-worker) - is_active bleibt dort synchron,
    in-memory, ohne Await, im Hot Path jedes einzelnen Trades. Ein Redis-
    Roundtrip bei JEDEM is_active-Check wuerde diese Garantie brechen
    und einen neuen Fehlermodus einfuehren (Redis nicht erreichbar =
    Kill-Switch-Status unbekannt, genau im kritischsten Moment).

    Redis wird stattdessen NUR fuer die Cross-Prozess-Synchronisation
    verwendet: trigger()/reset() schreiben den neuen State zusaetzlich
    nach Redis (SET, fuer andere Prozesse zum Lesen, z.B. sgr-api fuer
    Status-Anzeige) und publizieren ihn per Pub/Sub (fuer Prozesse wie
    sgr-worker, die den State aktiv uebernehmen muessen, falls der
    Kill Switch von AUSSEN - z.B. ueber die API - ausgeloest wurde).
    Redis ist dabei rein additiv: faellt Redis aus, funktioniert der
    In-Memory-Kill-Switch innerhalb des jeweiligen Prozesses unveraendert
    weiter (fail-safe, kein Hard-Fail auf Redis-Fehler). Ohne injizierten
    Redis-Client (redis_client=None, der Default) verhaelt sich KillSwitch
    exakt wie vor dieser Aenderung - rein in-memory, keine neue
    Abhaengigkeit fuer bestehende Tests/Call-Sites.

Thread-Safety:
    asyncio.Lock für State-Änderungen.
    killed-Flag als Python bool (GIL schützt atomaren Read).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sgr.core.event_bus import get_event_bus
from sgr.core.logging import audit_log, get_logger
from sgr.core.types import AlertSeverity, KillSwitchEvent, TradingMode
from sgr.risk.types import KillSwitchState

if TYPE_CHECKING:
    from redis.asyncio import Redis

log = get_logger(__name__)

_REDIS_KEY_PREFIX = "sgr:kill_switch:state"
_REDIS_CHANNEL_PREFIX = "sgr:kill_switch:changes"


def _redis_key(trading_mode: TradingMode) -> str:
    return f"{_REDIS_KEY_PREFIX}:{trading_mode.value}"


def _redis_channel(trading_mode: TradingMode) -> str:
    return f"{_REDIS_CHANNEL_PREFIX}:{trading_mode.value}"


class KillSwitch:
    """
    Singleton Kill Switch pro Trading Mode.
    Zwei Instanzen: eine für Paper, eine für Live.
    Niemals Cross-Contamination zwischen Modi.
    """

    def __init__(
        self,
        trading_mode: TradingMode,
        redis_client: Redis | None = None,
    ) -> None:
        self._trading_mode = trading_mode
        self._state = KillSwitchState(trading_mode=trading_mode)
        self._lock = asyncio.Lock()
        self._exchange_pool: Any = None  # Injiziert bei Startup
        self._redis: Redis | None = redis_client
        self._subscriber_task: asyncio.Task[None] | None = None

    def inject_exchange_pool(self, pool: Any) -> None:
        """Injiziert Exchange Pool für Order-Cancellation."""
        self._exchange_pool = pool

    def inject_redis(self, redis_client: Redis) -> None:
        """
        Injiziert einen Redis-Client fuer Cross-Prozess-Synchronisation.
        Rein additiv - ohne diesen Aufruf verhaelt sich KillSwitch wie
        zuvor (reines In-Memory, kein Redis-Zugriff).
        """
        self._redis = redis_client

    async def start_remote_sync(self) -> None:
        """
        Startet einen Hintergrund-Task, der auf Redis Pub/Sub auf
        Kill-Switch-Aenderungen AUS ANDEREN PROZESSEN hoert und den
        lokalen In-Memory-State entsprechend uebernimmt.

        Nur relevant fuer den Prozess, der tatsaechlich Trades ausfuehrt
        (sgr-worker) - der lokale is_active-Check bleibt dabei synchron;
        dieser Task aktualisiert nur im Hintergrund, wenn sich der State
        AUSSERHALB dieses Prozesses aendert (z.B. Trigger ueber die API).
        No-op, falls kein Redis-Client injiziert wurde.
        """
        if self._redis is None:
            log.info("kill_switch.remote_sync_skipped_no_redis")
            return
        if self._subscriber_task is not None:
            return

        self._subscriber_task = asyncio.create_task(
            self._remote_sync_loop(), name=f"kill_switch_sync_{self._trading_mode.value}"
        )

    async def stop_remote_sync(self) -> None:
        if self._subscriber_task is not None:
            self._subscriber_task.cancel()
            try:
                await self._subscriber_task
            except asyncio.CancelledError:
                pass
            self._subscriber_task = None

    async def _remote_sync_loop(self) -> None:
        assert self._redis is not None
        try:
            pubsub = self._redis.pubsub()
            await pubsub.subscribe(_redis_channel(self._trading_mode))
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
                except (json.JSONDecodeError, TypeError) as e:
                    log.warning("kill_switch.remote_sync_bad_payload", error=str(e))
                    continue
                await self._apply_remote_state(payload)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("kill_switch.remote_sync_loop_failed", error=str(e))

    async def _apply_remote_state(self, payload: dict[str, Any]) -> None:
        """Uebernimmt einen von einem ANDEREN Prozess publizierten State,
        ohne dessen Side Effects (Order-Cancellation etc.) hier erneut
        auszufuehren - die hat der urspruengliche Trigger-Aufrufer bereits
        gemacht. Dieser Prozess soll nur den is_active-Flag synchron
        halten."""
        async with self._lock:
            is_active = bool(payload.get("is_active"))
            if is_active == self._state.is_active:
                return  # Bereits synchron, nichts zu tun
            if is_active:
                self._state.trigger(
                    payload.get("reason") or "remote_trigger", self._trading_mode
                )
                log.warning(
                    "kill_switch.remote_state_applied",
                    is_active=True,
                    reason=self._state.reason,
                )
            else:
                self._state.reset()
                log.warning("kill_switch.remote_state_applied", is_active=False)

    async def _publish_to_redis(self) -> None:
        """Schreibt den aktuellen State nach Redis (fuer Reads durch andere
        Prozesse, z.B. sgr-api) und broadcastet ihn per Pub/Sub (fuer
        aktive Uebernahme durch sgr-worker). Fail-safe: ein Redis-Fehler
        hier darf den bereits erfolgten In-Memory-State-Change niemals
        rueckgaengig machen oder die trigger()/reset()-Aufrufe fehlschlagen
        lassen."""
        if self._redis is None:
            return
        payload = json.dumps(
            {
                "is_active": self._state.is_active,
                "reason": self._state.reason,
                "triggered_at": (
                    self._state.triggered_at.isoformat() if self._state.triggered_at else None
                ),
            }
        )
        try:
            await self._redis.set(_redis_key(self._trading_mode), payload)
            await self._redis.publish(_redis_channel(self._trading_mode), payload)
        except Exception as e:
            log.error("kill_switch.redis_publish_failed", error=str(e))

    # ------------------------------------------------------------------
    # State (synchron, O(1))
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """
        Synchroner Check – kein Await nötig.
        Wird im hot path (jedem Trade) aufgerufen.
        """
        return self._state.is_active

    @property
    def trading_allowed(self) -> bool:
        return not self._state.is_active

    @property
    def state(self) -> KillSwitchState:
        return self._state

    # ------------------------------------------------------------------
    # Trigger (async – Side Effects)
    # ------------------------------------------------------------------

    async def trigger(
        self,
        reason: str,
        triggered_by: str = "system",
        close_positions: bool = False,
    ) -> None:
        """
        Löst Kill Switch aus.
        Idempotent: zweimaliges Triggern hat keinen weiteren Effekt.

        Args:
            reason: Warum der Kill Switch ausgelöst wurde
            triggered_by: "system" | "risk_engine" | "user:{user_id}"
            close_positions: True = alle Positionen mit Market Order schließen
        """
        async with self._lock:
            if self._state.is_active:
                log.warning(
                    "kill_switch.already_active",
                    previous_reason=self._state.reason,
                )
                return

            # 1. State sofort setzen (synchron)
            self._state.trigger(reason, self._trading_mode)

            log.critical(
                "kill_switch.triggered",
                reason=reason,
                triggered_by=triggered_by,
                trading_mode=self._trading_mode.value,
            )

            # 2. Audit Log (immutable record)
            audit_log.log_kill_switch(
                reason=reason,
                trading_mode=self._trading_mode,
                triggered_by=triggered_by,
            )

        # 2b. Redis: Cross-Prozess sichtbar machen (fail-safe, siehe oben)
        await self._publish_to_redis()

        # 3. Orders canceln (async, best-effort außerhalb Lock)
        await self._cancel_all_orders()

        # 4. Positionen schließen wenn gewünscht
        if close_positions:
            await self._close_all_positions()

        # 5. Event Bus – andere Module informieren
        await self._publish_event(reason)

    async def _cancel_all_orders(self) -> None:
        """Cancelt alle offenen Orders auf allen Exchanges. Best-effort."""
        if self._exchange_pool is None:
            log.warning("kill_switch.no_exchange_pool_injected")
            return

        try:
            from sgr.exchanges.factory import ExchangePool

            if not isinstance(self._exchange_pool, ExchangePool):
                return

            # Alle Adapters im Pool
            for (exchange_id, mode), adapter in self._exchange_pool._adapters.items():
                if mode != self._trading_mode:
                    continue
                try:
                    cancelled = await adapter.cancel_all_orders()
                    log.info(
                        "kill_switch.orders_cancelled",
                        exchange=exchange_id.value,
                        count=cancelled,
                    )
                except Exception as e:
                    log.error(
                        "kill_switch.cancel_orders_failed",
                        exchange=exchange_id.value,
                        error=str(e),
                    )

        except Exception as e:
            log.error("kill_switch.cancel_all_failed", error=str(e))

    async def _close_all_positions(self) -> None:
        """
        Schließt alle offenen Positionen mit Market Orders.
        Nur wenn explizit requested (close_positions=True).
        In Krisensituationen kann Market-Close selbst Slippage verursachen.
        """
        log.warning(
            "kill_switch.closing_all_positions",
            trading_mode=self._trading_mode.value,
            note="Market orders will be placed for all open positions",
        )
        # Implementierung durch Portfolio Engine (circular dep vermeiden)
        # Event wird publiziert → Portfolio Engine hört zu
        # Hier nur Log – Portfolio Engine schließt bei KillSwitchEvent

    async def _publish_event(self, reason: str) -> None:
        """Publiziert KillSwitchEvent auf Event Bus."""
        try:
            event = KillSwitchEvent(
                timestamp=datetime.now(tz=UTC),
                source="kill_switch",
                reason=reason,
                severity=AlertSeverity.KILL_SWITCH,
                trading_mode=self._trading_mode,
            )
            bus = get_event_bus()
            await bus.publish(event)
        except Exception as e:
            log.error("kill_switch.publish_event_failed", error=str(e))

    # ------------------------------------------------------------------
    # Reset (manuell)
    # ------------------------------------------------------------------

    async def reset(self, reset_by: str) -> None:
        """
        Manuelle Zurücksetzung des Kill Switch.
        Erfordert expliziten Aufruf – kein Auto-Reset.

        Vor Reset muss:
        1. Ursache des Triggers analysiert sein
        2. System-State verifiziert sein (Positionen, Kapital)
        3. Risk Limits neu gesetzt sein falls nötig
        """
        async with self._lock:
            if not self._state.is_active:
                log.info("kill_switch.already_inactive")
                return

            previous_reason = self._state.reason
            self._state.reset()

            log.warning(
                "kill_switch.reset",
                reset_by=reset_by,
                previous_reason=previous_reason,
                trading_mode=self._trading_mode.value,
            )

            audit_log.log_auth_event(
                event="kill_switch_reset",
                user_id=reset_by,
                ip_address="system",
                success=True,
            )

        # Redis: Cross-Prozess sichtbar machen (fail-safe, siehe trigger())
        await self._publish_to_redis()


# ---------------------------------------------------------------------------
# Singletons (eine Instanz pro Trading Mode)
# ---------------------------------------------------------------------------

_kill_switches: dict[TradingMode, KillSwitch] = {}


def get_kill_switch(trading_mode: TradingMode) -> KillSwitch:
    if trading_mode not in _kill_switches:
        _kill_switches[trading_mode] = KillSwitch(trading_mode)
    return _kill_switches[trading_mode]


async def read_kill_switch_state_from_redis(
    redis_client: Redis,
    trading_mode: TradingMode,
) -> dict[str, Any] | None:
    """
    Rein lesender Zugriff auf den zuletzt von einem trigger()/reset()
    nach Redis geschriebenen Kill-Switch-State - fuer Prozesse (z.B.
    sgr-api), die keine eigene, den Trading Lifecycle besitzende
    KillSwitch-Instanz mehr halten und daher nichts anderes als den
    zuletzt bekannten State brauchen (keine Trigger-Faehigkeit, kein
    Exchange Pool, kein Lock).

    Gibt None zurueck, wenn noch nie ein State geschrieben wurde (z.B.
    frisches Deployment vor dem ersten Worker-Start) oder bei Redis-
    Fehlern (fail-safe: der Aufrufer sollte das als 'Status unbekannt',
    NICHT als 'Kill Switch inaktiv' behandeln).
    """
    try:
        raw = await redis_client.get(_redis_key(trading_mode))
        if raw is None:
            return None
        result: dict[str, Any] = json.loads(raw)
        return result
    except Exception as e:
        log.error("kill_switch.redis_read_failed", error=str(e))
        return None
