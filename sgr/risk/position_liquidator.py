"""
SGR Position Liquidator
========================
Schliesst tatsaechlich alle offenen Positionen eines Tenants, wenn dessen
Kill Switch mit close_positions=True ausgeloest wurde.

Bestandsbefund (operative Validierung, 2026-09-14): KillSwitch publizierte
seit jeher ein KillSwitchEvent in der Annahme, die Portfolio Engine wuerde
darauf reagieren (siehe kill_switch.py Modul-Docstring, Punkt 3 "Optional:
alle Positionen schliessen"). Es existierte aber im gesamten Code keine
einzige event_bus.subscribe(KillSwitchEvent, ...)-Registrierung -
close_positions=True hatte dadurch nie eine tatsaechliche Wirkung, nur
einen Log-Eintrag. Dieser Liquidator schliesst diese Luecke.

Architektur: bewusst NICHT in PortfolioEngine oder KillSwitch selbst -
beide sollen frei von einer Abhaengigkeit auf ExecutionEngine bleiben
(siehe KillSwitch-Docstring "circular dep vermeiden"). Dieser Liquidator
sitzt eine Ebene darueber und verdrahtet PortfolioEngine (liest offene
Positionen) mit ExecutionEngine (sendet die schliessenden Market Orders) -
exakt das gleiche Muster wie TradingOrchestrator.run_cycle(): erst
execute(), dann bei FILLED direkt (nicht ueber den Event Bus)
portfolio_engine.on_order_filled() aufrufen, um Doppelverarbeitung/Race
auszuschliessen.

Tenant-Scoping: der Redis-Stream sgr:kill_switch_event (siehe
sgr/core/event_bus.py) ist NICHT tenant-partitioniert - anders als der
Redis-Key/Channel, den KillSwitch fuer die Cross-Prozess-State-Sync
verwendet. Ohne den tenant_id-Filter unten wuerde Gordons Kill-Switch-
Trigger auch in Sumos Worker-Prozess ankommen und dessen (Sumos)
Positionen schliessen, obwohl Sumos eigener Kill Switch nie ausgeloest
wurde. Jeder Prozess instanziiert daher genau einen Liquidator mit der
EIGENEN tenant_id und ignoriert jedes Event mit abweichender tenant_id.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sgr.core.logging import get_logger
from sgr.core.types import KillSwitchEvent, OrderRequest, OrderStatus, OrderType, PositionSide, Side

log = get_logger(__name__)


class PositionLiquidator:
    """
    Subscribed auf KillSwitchEvent. Schliesst bei close_positions=True
    jede aktuell offene Position des EIGENEN Tenants mit einer
    gegenlaeufigen Market Order (reduce_only).
    """

    def __init__(
        self,
        portfolio_engine: Any,
        execution_engine: Any,
        tenant_id: str | None,
    ) -> None:
        self._portfolio = portfolio_engine
        self._execution = execution_engine
        self._tenant_id = tenant_id

    async def on_kill_switch_event(self, event: KillSwitchEvent) -> None:
        if not event.close_positions:
            return

        if event.tenant_id != self._tenant_id:
            log.debug(
                "position_liquidator.ignored_foreign_tenant",
                event_tenant_id=event.tenant_id,
                own_tenant_id=self._tenant_id,
            )
            return

        positions = list(self._portfolio.positions)
        if not positions:
            log.info("position_liquidator.no_open_positions", reason=event.reason)
            return

        log.warning(
            "position_liquidator.closing_positions",
            count=len(positions),
            reason=event.reason,
            tenant_id=self._tenant_id,
        )

        for position in positions:
            await self._close_position(position, event.reason)

    async def _close_position(self, position: Any, kill_switch_reason: str) -> None:
        close_side = Side.SELL if position.side == PositionSide.LONG else Side.BUY

        order = OrderRequest(
            signal_id=uuid4(),
            symbol=position.symbol,
            side=close_side,
            order_type=OrderType.MARKET,
            quantity=position.quantity,
            trading_mode=position.trading_mode,
            reduce_only=True,
            metadata={
                "strategy": position.strategy_name,
                "reason": "kill_switch_liquidation",
                "kill_switch_reason": kill_switch_reason,
            },
        )

        try:
            # bypass_kill_switch=True: diese Order IST die Reaktion auf den
            # aktiven Kill Switch (de-risking), nicht neues Risiko - siehe
            # ExecutionEngine.execute() Docstring.
            result = await self._execution.execute(order, bypass_kill_switch=True)
        except Exception as e:
            log.error(
                "position_liquidator.close_order_failed",
                symbol=str(position.symbol),
                error=str(e),
            )
            return

        log.info(
            "position_liquidator.close_order_result",
            symbol=str(position.symbol),
            status=result.status.value,
            exchange_order_id=result.exchange_order_id,
        )

        # Direkter Aufruf statt Event Bus (siehe Modul-Docstring) - gleiches
        # Muster wie TradingOrchestrator.run_cycle().
        if result.status == OrderStatus.FILLED:
            await self._portfolio.on_order_filled(result)
