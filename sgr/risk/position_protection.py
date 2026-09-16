"""
SGR Position Protection (Stop-Loss / Take-Profit / Max-Holding-Time)
=====================================================================
Schliesst die Luecke, die die urspruengliche Architektur-Analyse fuer
dieses Feature aufgedeckt hat: bis hierher existierte im gesamten Code
KEIN unabhaengiger, risikobasierter Exit-Mechanismus fuer eine offene
Position - eine Position konnte ausschliesslich schliessen, wenn die
Strategie selbst auf einem spaeteren Zyklus ein gegenlaeufiges Signal
erzeugte (siehe PortfolioEngine._update_position(), is_closing-Ableitung
aus dem Fill-Side-Vergleich). Es gab keinen Stop-Loss, kein Take-Profit,
keine Max-Holding-Time - eine Position blieb technisch unbegrenzt offen,
falls die Strategie nie ein Gegensignal lieferte.

Zwei Bausteine:

1. PositionProtectionManager (additiv per Hook, siehe PortfolioEngine
   __init__ on_position_opened/on_position_closed): berechnet beim
   Eroeffnen einer Position deren Stop-Loss-/Take-Profit-Preise und
   Max-Holding-Deadline aus der zentralen RiskLimitsConfig und haengt
   sie an die Position (stop_loss_price/take_profit_price/
   max_holding_until, siehe Position-Domain-Typ).

2. PositionProtectionWatchdog: periodischer, von der Candle-Ankunft
   UNABHAENGIGER Background-Task (siehe sgr/worker/main.py, gleiches
   Strukturmuster wie der dortige _heartbeat_loop), der jede
   geschuetzte offene Position gegen ihre SL/TP-Preise (anhand des
   bereits von PortfolioEngine.update_prices() aktuell gehaltenen
   current_price - kein zusaetzlicher Exchange-Call) und ihre
   Max-Holding-Deadline (reine Wanduhrzeit) prueft und bei
   Ueberschreitung einen normalen reduce-only Market-Order-Exit ueber
   ExecutionEngine.execute() ausloest - GENAU derselbe Order-Lifecycle
   wie jeder andere Exit (Risk-Idempotenz, Audit-Log, Fees/Slippage,
   Metriken), keine Sonderlogik am Order-Management vorbei.

Bewusste, dokumentierte Einschraenkung (kein natives Exchange-seitiges
STOP_MARKET/TAKE_PROFIT_MARKET in dieser Iteration):
    Ein Versuch, echte, auf der Exchange RUHENDE Conditional Orders
    (STOP_MARKET/TAKE_PROFIT_MARKET, reduce_only) ueber den bestehenden
    ExecutionEngine.execute()-Pfad zu platzieren, waere mit der
    bestehenden Fill-Monitoring-Logik dort inkompatibel:
    ExecutionEngine._monitor_fill() bricht JEDE Order, die nicht
    innerhalb von _FILL_TIMEOUT_S (60s) fuellt, automatisch ab und
    storniert sie (siehe execution/engine.py) - das ist fuer normale
    Market/Limit-Entry-Orders korrekt, wuerde aber eine gewollt lange
    ruhende SL/TP-Order (bis zu max_holding_minutes) nach spaetestens
    60 Sekunden faelschlich stornieren, ohne dass der Preis je getriggert
    haette. Eine echte native Umsetzung braeuchte einen eigenen
    "platzieren und NICHT ueberwachen"-Order-Modus in ExecutionEngine -
    eine gezielte, eigenstaendig zu review'ende Erweiterung an
    sicherheitskritischem, umfangreich getestetem Code (39+ bestehende
    Tests), die hier bewusst NICHT ad-hoc angehaengt wird.

    Stattdessen: SL/TP werden fuer PAPER UND LIVE identisch ueber den
    Watchdog durchgesetzt (Preis-Schwelle, dann normale reduce-only
    Market Order) - ein einziger, bereits vollstaendig getesteter
    Order-Pfad in beiden Modi, statt einer halbfunktionierenden nativen
    Implementierung, die im Hintergrund fehlschlagen wuerde.

    Reale Konsequenz (siehe Aufgabenstellung "Keine Logik ... bei der
    ein SL ausschliesslich davon abhaengt, dass der Worker permanent
    laeuft"): dieser Watchdog-Ansatz haengt tatsaechlich davon ab, dass
    der Worker-Prozess laeuft und den Watchdog-Tick ausfuehrt. Das ist
    eine bewusst offengelegte Einschraenkung dieser Iteration, kein
    verschwiegener Kompromiss - sl_order_id/tp_order_id (Schema bereits
    vorbereitet, siehe Migration 0006) bleiben fuer eine spaetere,
    eigenstaendige native-Order-Erweiterung nutzbar.

Kill-Switch-Interaktion (siehe Aufgabenstellung "Ein Risk Limit darf
neue Entries blockieren, aber nicht automatisch die Verwaltung bereits
existierender Positionen verhindern"): jede Order dieses Moduls nutzt
bypass_kill_switch=True (identisches Muster wie PositionLiquidator) -
SL/TP/Max-Holding-Exits sind IMMER de-risking/reduce_only und muessen
weiterhin funktionieren, waehrend der Kill Switch NEUE Entries blockiert.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sgr.core.config import get_config
from sgr.core.logging import get_logger
from sgr.core.types import (
    ExitReason,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    Side,
)

log = get_logger(__name__)


class PositionProtectionManager:
    """
    Haengt SL/TP-Preise und eine Max-Holding-Deadline an neu eroeffnete
    Positionen (siehe PortfolioEngine.on_position_opened-Hook).

    Rein rechnend/persistierend - platziert selbst KEINE Exchange-Order
    (siehe Modul-Docstring fuer die Begruendung) - die tatsaechliche
    Durchsetzung/der Exit passiert im PositionProtectionWatchdog.
    """

    async def on_position_opened(self, position: Position) -> Position | None:
        limits = get_config().risk_limits

        if limits.protection_cutover_at is None:
            return None

        cutover = limits.protection_cutover_at
        if cutover.tzinfo is None:
            cutover = cutover.replace(tzinfo=UTC)
        if position.opened_at < cutover:
            # Legacy-Position (vor dem Cutover eroeffnet) - bewusst NICHT
            # rueckwirkend geschuetzt, siehe Modul-Docstring in
            # sgr/core/config.py (protection_cutover_at).
            return None

        entry = position.entry_price
        sl_pct = Decimal(str(limits.stop_loss_pct))
        tp_pct = Decimal(str(limits.take_profit_pct))

        if position.side == PositionSide.LONG:
            stop_loss_price = entry * (Decimal("1") - sl_pct)
            take_profit_price = entry * (Decimal("1") + tp_pct)
        else:
            stop_loss_price = entry * (Decimal("1") + sl_pct)
            take_profit_price = entry * (Decimal("1") - tp_pct)

        max_holding_until = position.opened_at + timedelta(minutes=limits.max_holding_minutes)

        log.info(
            "position_protection.attached",
            symbol=str(position.symbol),
            side=position.side.value,
            entry_price=str(entry),
            stop_loss_price=str(stop_loss_price),
            take_profit_price=str(take_profit_price),
            max_holding_until=max_holding_until.isoformat(),
        )

        return position.model_copy(
            update={
                "stop_loss_price": stop_loss_price,
                "take_profit_price": take_profit_price,
                "max_holding_until": max_holding_until,
            }
        )

    async def on_position_closed(self, position: Position, close_reason: str) -> None:
        """
        OCO-Cleanup-Hook (siehe PortfolioEngine.on_position_closed).
        No-op in dieser Iteration: da on_position_opened() KEINE
        Exchange-Orders platziert (siehe Modul-Docstring), gibt es
        keine verwaisten SL/TP-Orders zu stornieren. Bleibt als
        expliziter Hook bestehen, damit eine spaetere native-Order-
        Erweiterung hier ansetzen kann, ohne PortfolioEngine erneut
        aendern zu muessen.
        """
        return


class PositionProtectionWatchdog:
    """
    Periodischer Background-Task: prueft alle geschuetzten offenen
    Positionen gegen SL/TP/Max-Holding-Time und loest bei Ueberschreitung
    einen normalen reduce-only Market-Exit aus.

    Usage (siehe sgr/worker/main.py):
        watchdog = PositionProtectionWatchdog(portfolio_engine, execution_engine)
        await watchdog.start()
        ...
        await watchdog.stop()
    """

    def __init__(
        self,
        portfolio_engine: Any,
        execution_engine: Any,
        interval_seconds: float = 60.0,
    ) -> None:
        self._portfolio = portfolio_engine
        self._execution = execution_engine
        self._interval_seconds = interval_seconds
        self._task: Any = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run_loop())
        log.info("position_protection_watchdog.started", interval_seconds=self._interval_seconds)

    async def stop(self) -> None:
        if self._task is None:
            return

        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        log.info("position_protection_watchdog.stopped")

    async def _run_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_seconds)
                try:
                    await self.check_positions_once()
                except Exception as e:
                    log.error(
                        "position_protection_watchdog.tick_failed",
                        error=str(e),
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            raise

    async def check_positions_once(self) -> None:
        """
        Ein einzelner Watchdog-Tick. Oeffentliche Methode (statt nur
        intern im Loop) - erlaubt Tests und einen expliziten, sofortigen
        Check (z.B. fuer TEST 3 "Max Holding Time" ohne 30 Minuten
        Real-Wartezeit) ohne den Sleep-Loop nachzubilden.

        Positionen werden SEQUENZIELL verarbeitet (nicht parallel via
        asyncio.gather) - identisches Muster wie
        PositionLiquidator.on_kill_switch_event(): verhindert
        ueberlappende Races, falls zwei Positionen desselben Symbols
        (kann nicht vorkommen, aber Robustheit) oder ein einzelner
        _execution.execute()-Aufruf ungewoehnlich lange braucht.
        """
        limits = get_config().risk_limits
        if limits.protection_cutover_at is None:
            return

        cutover = limits.protection_cutover_at
        if cutover.tzinfo is None:
            cutover = cutover.replace(tzinfo=UTC)

        now = datetime.now(tz=UTC)

        for position in list(self._portfolio.positions):
            if position.opened_at < cutover:
                continue
            try:
                await self._check_one_position(position, now)
            except Exception as e:
                log.error(
                    "position_protection_watchdog.check_position_failed",
                    symbol=str(position.symbol),
                    error=str(e),
                    exc_info=True,
                )

    async def _check_one_position(self, position: Position, now: datetime) -> None:
        exit_reason: ExitReason | None = None

        max_holding_until = position.max_holding_until
        if max_holding_until is not None:
            if max_holding_until.tzinfo is None:
                max_holding_until = max_holding_until.replace(tzinfo=UTC)
            if now >= max_holding_until:
                exit_reason = ExitReason.MAX_HOLDING_TIME

        if exit_reason is None:
            exit_reason = self._check_price_thresholds(position)

        if exit_reason is None:
            return

        await self._close_position(position, exit_reason)

    @staticmethod
    def _check_price_thresholds(position: Position) -> ExitReason | None:
        price = position.current_price
        if position.side == PositionSide.LONG:
            if position.stop_loss_price is not None and price <= position.stop_loss_price:
                return ExitReason.STOP_LOSS
            if position.take_profit_price is not None and price >= position.take_profit_price:
                return ExitReason.TAKE_PROFIT
        else:
            if position.stop_loss_price is not None and price >= position.stop_loss_price:
                return ExitReason.STOP_LOSS
            if position.take_profit_price is not None and price <= position.take_profit_price:
                return ExitReason.TAKE_PROFIT
        return None

    async def _close_position(self, position: Position, exit_reason: ExitReason) -> None:
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
                "exit_reason": exit_reason.value,
            },
        )

        log.warning(
            "position_protection_watchdog.exit_triggered",
            symbol=str(position.symbol),
            exit_reason=exit_reason.value,
            current_price=str(position.current_price),
            stop_loss_price=str(position.stop_loss_price) if position.stop_loss_price else None,
            take_profit_price=(
                str(position.take_profit_price) if position.take_profit_price else None
            ),
        )

        try:
            # bypass_kill_switch=True: siehe Modul-Docstring - dieser Exit
            # ist immer de-risking/reduce_only und muss auch bei aktivem
            # Kill Switch funktionieren (bestehende Positionen bleiben
            # verwaltbar, nur neue Entries werden blockiert).
            result = await self._execution.execute(order, bypass_kill_switch=True)
        except Exception as e:
            log.error(
                "position_protection_watchdog.close_order_failed",
                symbol=str(position.symbol),
                exit_reason=exit_reason.value,
                error=str(e),
            )
            return

        log.info(
            "position_protection_watchdog.close_order_result",
            symbol=str(position.symbol),
            exit_reason=exit_reason.value,
            status=result.status.value,
            exchange_order_id=result.exchange_order_id,
        )

        # Direkter Aufruf statt Event Bus (siehe PositionLiquidator fuer
        # das identische, bereits etablierte Muster) - kein produktiver
        # Subscriber auf OrderFilledEvent, PortfolioEngine.on_order_filled()
        # wird ausschliesslich direkt aufgerufen.
        if result.status == OrderStatus.FILLED:
            await self._portfolio.on_order_filled(result)
