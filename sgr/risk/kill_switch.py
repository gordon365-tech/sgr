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

Thread-Safety:
    asyncio.Lock für State-Änderungen.
    killed-Flag als Python bool (GIL schützt atomaren Read).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from sgr.core.event_bus import get_event_bus
from sgr.core.logging import audit_log, get_logger
from sgr.core.types import AlertSeverity, KillSwitchEvent, TradingMode
from sgr.risk.types import KillSwitchState

log = get_logger(__name__)


class KillSwitch:
    """
    Singleton Kill Switch pro Trading Mode.
    Zwei Instanzen: eine für Paper, eine für Live.
    Niemals Cross-Contamination zwischen Modi.
    """

    def __init__(self, trading_mode: TradingMode) -> None:
        self._trading_mode = trading_mode
        self._state = KillSwitchState(trading_mode=trading_mode)
        self._lock = asyncio.Lock()
        self._exchange_pool: Any = None  # Injiziert bei Startup

    def inject_exchange_pool(self, pool: Any) -> None:
        """Injiziert Exchange Pool für Order-Cancellation."""
        self._exchange_pool = pool

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


# ---------------------------------------------------------------------------
# Singletons (eine Instanz pro Trading Mode)
# ---------------------------------------------------------------------------

_kill_switches: dict[TradingMode, KillSwitch] = {}


def get_kill_switch(trading_mode: TradingMode) -> KillSwitch:
    if trading_mode not in _kill_switches:
        _kill_switches[trading_mode] = KillSwitch(trading_mode)
    return _kill_switches[trading_mode]
