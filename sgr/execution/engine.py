"""
SGR Execution Engine
====================
Verarbeitet OrderRequests von der Risk Engine bis zum bestätigten Fill.

Verantwortlichkeiten:
    1. OrderRequest entgegennehmen (von Risk Engine)
    2. Order an Exchange übermitteln
    3. Fill-Monitoring bis Completion
    4. OrderResult an Portfolio Engine weiterleiten
    5. Audit-Log für jeden Order-Lifecycle-Schritt
    6. Slippage berechnen und loggen

Design-Entscheidungen:
    - Execution Engine ist zustandslos bezgl. Portfolio
      (Portfolio Engine hält den State)
    - Paper Mode: identischer Code-Pfad wie Live, nur Adapter unterscheidet sich
    - Retry nur für Verbindungsfehler, nie für abgelehnte Orders
    - Kill Switch Check vor jeder Submission (doppelte Absicherung)
    - Timeout nach 60s → Order canceln, Report Partial Fill

Order Lifecycle:
    PENDING → SUBMITTED → [PARTIALLY_FILLED] → FILLED | CANCELLED | REJECTED

Fill Monitoring:
    Polling-basiert (alle 2s) für max. 60s.
    Market Orders: sofort gefüllt in 99% der Fälle.
    Limit Orders: können lange offen bleiben.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sgr.core.event_bus import get_event_bus
from sgr.core.logging import audit_log, get_logger
from sgr.core.types import (
    OrderFilledEvent,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    TradingMode,
)
from sgr.exchanges.base import ExchangeError
from sgr.exchanges.factory import ExchangePool
from sgr.risk.kill_switch import get_kill_switch

log = get_logger(__name__)

_FILL_POLL_INTERVAL_S = 2.0  # Wie oft nach Fill-Status fragen
_FILL_TIMEOUT_S = 60.0  # Nach dieser Zeit: cancel + report
_MARKET_ORDER_FAST_TIMEOUT = 5.0  # Market Orders sehr kurz


class ExecutionEngine:
    """
    Führt Orders aus. Strikter Paper/Live-Trennung via Adapter.

    Usage:
        engine = ExecutionEngine(pool, TradingMode.PAPER)
        result = await engine.execute(order_request)
    """

    def __init__(
        self,
        pool: ExchangePool,
        trading_mode: TradingMode,
        order_repository: Any = None,
    ) -> None:
        self._pool = pool
        self._trading_mode = trading_mode
        self._kill_switch = get_kill_switch(trading_mode)
        # Optional: OrderRepository fuer Persistenz. None = rein
        # In-Memory/Event-basiert (Tests, isolierte Nutzung) - additiv,
        # analog zu PortfolioEngine._position_repo. Ohne Injektion
        # verhaelt sich die Engine exakt wie vor diesem Feature.
        self._order_repo = order_repository

    async def execute(self, order: OrderRequest) -> OrderResult:
        """
        Hauptmethode: OrderRequest → OrderResult.

        Fail-Safe: jede Exception → REJECTED Result (kein uncontrolled State).
        """
        # Sanity check: trading_mode muss übereinstimmen
        if order.trading_mode != self._trading_mode:
            raise ValueError(
                f"Order trading_mode {order.trading_mode} "
                f"does not match engine mode {self._trading_mode}"
            )

        # Kill Switch (letzte Absicherung vor Exchange-Call)
        if self._kill_switch.is_active:
            log.warning(
                "execution_engine.blocked_by_kill_switch",
                order_id=str(order.id),
            )
            return self._rejected_result(order, "Kill switch active")

        try:
            return await self._execute_internal(order)
        except Exception as e:
            log.error(
                "execution_engine.unexpected_error",
                order_id=str(order.id),
                error=str(e),
                exc_info=True,
            )
            return self._rejected_result(order, f"Execution error: {e}")

    async def _execute_internal(self, order: OrderRequest) -> OrderResult:
        adapter = self._pool.get(order.symbol.exchange, self._trading_mode)

        # Audit: Order submitted
        audit_log.log_trade(
            event="submitted",
            order_id=str(order.id),
            symbol=str(order.symbol),
            side=order.side.value,
            quantity=str(order.quantity),
            price=str(order.limit_price) if order.limit_price else "market",
            trading_mode=self._trading_mode,
            strategy=str(order.metadata.get("strategy", "unknown")),
        )

        # Submit Order
        result = await adapter.place_order(order)

        log.info(
            "execution_engine.order_submitted",
            order_id=str(order.id),
            exchange_order_id=result.exchange_order_id,
            symbol=str(order.symbol),
            side=order.side.value,
            qty=str(order.quantity),
            type=order.order_type.value,
            mode=self._trading_mode.value,
        )

        await self._persist_order_create(order, result)

        # Falls sofort filled (Market Order, Paper Mode)
        if result.status == OrderStatus.FILLED:
            await self._on_fill(result)
            return result

        # Fill Monitoring für nicht sofort gefüllte Orders
        timeout = (
            _MARKET_ORDER_FAST_TIMEOUT if order.order_type == OrderType.MARKET else _FILL_TIMEOUT_S
        )
        final_result = await self._monitor_fill(
            order=order,
            initial_result=result,
            timeout=timeout,
        )

        return final_result

    async def _persist_order_create(self, order: OrderRequest, result: OrderResult) -> None:
        """
        Legt den initialen Order-Record in der DB an (best-effort).
        Bisher schrieb ExecutionEngine Orders NIE in die DB - nur Events
        und Audit-Log-Zeilen, die keinen abfragbaren State darstellen.
        OrderRepository.create()/update_status() existierten, wurden aber
        nirgends aufgerufen. Ohne diesen Schritt ist Order-Recovery nach
        einem Crash unmoeglich, da keine Datenquelle existiert.

        id wird explizit auf order.id gesetzt (nicht die von create()
        zurueckgegebene generierte ID), damit spaetere update_status()-
        Aufrufe via order.id dieselbe Row treffen.
        """
        if self._order_repo is None:
            return
        try:
            await self._order_repo.create(
                {
                    "id": str(order.id),
                    "signal_id": str(order.signal_id),
                    "exchange_order_id": result.exchange_order_id,
                    "symbol": str(order.symbol),
                    "exchange": order.symbol.exchange.value,
                    "side": order.side.value,
                    "order_type": order.order_type.value,
                    "quantity": order.quantity,
                    "limit_price": order.limit_price,
                    "filled_quantity": result.filled_quantity,
                    "status": result.status.value,
                    "trading_mode": self._trading_mode.value,
                    "strategy_name": str(order.metadata.get("strategy", "unknown")),
                    "submitted_at": result.submitted_at,
                }
            )
        except Exception as e:
            log.error(
                "execution_engine.persist_order_create_failed",
                order_id=str(order.id),
                error=str(e),
            )

    async def _persist_order_status(self, order_id: str, result: OrderResult) -> None:
        """Aktualisiert Order-Status in der DB (best-effort, fail-safe)."""
        if self._order_repo is None:
            return
        try:
            await self._order_repo.update_status(
                order_id=order_id,
                status=result.status.value,
                filled_quantity=result.filled_quantity,
                average_fill_price=result.average_fill_price,
                fees=result.fees,
                filled_at=datetime.now(tz=UTC) if result.status == OrderStatus.FILLED else None,
            )
        except Exception as e:
            log.error(
                "execution_engine.persist_order_status_failed",
                order_id=order_id,
                error=str(e),
            )

    async def _monitor_fill(
        self,
        order: OrderRequest,
        initial_result: OrderResult,
        timeout: float,
    ) -> OrderResult:
        """
        Pollt Exchange bis Order gefüllt oder Timeout.
        Bei Timeout: cancel Order, return was gefüllt wurde.
        """
        adapter = self._pool.get(order.symbol.exchange, self._trading_mode)
        elapsed = 0.0
        current = initial_result

        while elapsed < timeout:
            if self._kill_switch.is_active:
                log.warning(
                    "execution_engine.kill_switch_during_monitoring",
                    order_id=str(order.id),
                )
                await self._cancel_order(order, current)
                await self._persist_order_status(str(order.id), current)
                return current

            await asyncio.sleep(_FILL_POLL_INTERVAL_S)
            elapsed += _FILL_POLL_INTERVAL_S

            try:
                current = await adapter.get_order(
                    current.exchange_order_id,
                    order.symbol.ccxt_symbol,
                )
            except ExchangeError as e:
                log.warning(
                    "execution_engine.fill_poll_error",
                    order_id=str(order.id),
                    error=str(e),
                )
                continue

            if current.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
                if current.status != OrderStatus.FILLED:
                    await self._persist_order_status(str(order.id), current)
                break

        # Timeout erreicht: cancel offene Order
        if current.status not in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            log.warning(
                "execution_engine.fill_timeout",
                order_id=str(order.id),
                elapsed=elapsed,
                status=current.status.value,
            )
            await self._cancel_order(order, current)
            await self._persist_order_status(str(order.id), current)

        if current.status == OrderStatus.FILLED:
            await self._on_fill(current)

        return current

    async def _cancel_order(
        self,
        order: OrderRequest,
        result: OrderResult,
    ) -> None:
        """Best-effort Cancel."""
        try:
            adapter = self._pool.get(order.symbol.exchange, self._trading_mode)
            await adapter.cancel_order(
                result.exchange_order_id,
                order.symbol.ccxt_symbol,
            )
            log.info(
                "execution_engine.order_cancelled",
                order_id=str(order.id),
                exchange_order_id=result.exchange_order_id,
            )
        except Exception as e:
            log.error(
                "execution_engine.cancel_failed",
                order_id=str(order.id),
                error=str(e),
            )

    async def _on_fill(self, result: OrderResult) -> None:
        """
        Wird aufgerufen wenn Order vollständig gefüllt.
        1. Slippage berechnen + loggen
        2. Audit Log
        3. OrderFilledEvent auf Event Bus
        """
        # Audit
        audit_log.log_trade(
            event="filled",
            order_id=str(result.request_id),
            symbol=str(result.symbol),
            side="filled",
            quantity=str(result.filled_quantity),
            price=str(result.average_fill_price),
            trading_mode=result.trading_mode,
            strategy="",
            fees=str(result.fees),
            fee_currency=result.fee_currency,
        )

        log.info(
            "execution_engine.order_filled",
            order_id=str(result.request_id),
            exchange_order_id=result.exchange_order_id,
            qty=str(result.filled_quantity),
            price=str(result.average_fill_price),
            fees=str(result.fees),
        )

        # Event publizieren → Portfolio Engine updated State
        try:
            event = OrderFilledEvent(
                timestamp=datetime.now(tz=UTC),
                result=result,
            )
            await get_event_bus().publish(event)
        except Exception as e:
            log.error("execution_engine.publish_fill_failed", error=str(e))

        await self._persist_order_status(str(result.request_id), result)

    def _rejected_result(self, order: OrderRequest, reason: str) -> OrderResult:
        """Erstellt REJECTED OrderResult ohne Exchange-Kontakt."""
        return OrderResult(
            request_id=order.id,
            exchange_order_id=f"REJECTED-{order.id}",
            symbol=order.symbol,
            status=OrderStatus.REJECTED,
            filled_quantity=Decimal("0"),
            fees=Decimal("0"),
            submitted_at=datetime.now(tz=UTC),
            trading_mode=self._trading_mode,
            raw_response={"rejection_reason": reason},
        )
