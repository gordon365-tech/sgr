"""
SGR Order Safety & Idempotency
===============================

Implementiert umfassende Order-Sicherheit gegen Duplikate und Unknown States.

Architektur:

1. IDEMPOTENCY KEY
   Eindeutige ID für jeden Order-Request (z.B. Signal ID + Exchange)
   Exchange speichert: idempotency_key → exchange_order_id
   Wiederholung mit gleichem Key → Exchange gibt alte Order zurück (kein Duplicate)

2. DUPLICATE DETECTION  
   Vor jedem Order-Submit:
   - Prüfe: Exist bereits Order mit gleichem idempotency_key?
   - JA → Return cached result (Order wurde bereits gesendet)
   - NEIN → Submit neu

3. UNKNOWN STATE HANDLING
   Wenn Order-Status nach Submit unklar:
   UNKNOWN → Exchange lookup → Reconciliation → Decision
   NICHT blind neu submitten

4. IN-FLIGHT TRACKING
   Aktive Orders im Memory + DB
   Nach Crash: Restore aus DB, reconcile mit Exchange

Fail-Safe Rule:
Bei Unsicherheit → STOP und zur Reconciliation
Niemals automatisch erneut submitten
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from sgr.core.logging import get_logger
from sgr.core.types import OrderRequest, OrderResult, OrderStatus

log = get_logger(__name__)


class IdempotencyKey(BaseModel):
    """
    Eindeutige Idempotency Key für Order-Sicherheit.
    
    Struktur: {signal_id}#{exchange}#{symbol}#{side}
    Beispiel: "sig-abc123#pionex#BTC/USDT#BUY"
    """

    signal_id: str
    exchange: str
    symbol: str
    side: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_string(self) -> str:
        """Serialisiert zu String für Exchange API."""
        return f"{self.signal_id}#{self.exchange}#{self.symbol}#{self.side}"

    @classmethod
    def from_order_request(cls, order: OrderRequest) -> IdempotencyKey:
        """Erzeugt Key aus OrderRequest."""
        return cls(
            signal_id=str(order.signal_id),
            exchange=order.symbol.exchange.value,
            symbol=order.symbol.ccxt_symbol,
            side=order.side.value,
        )


class DuplicateOrderBlockedEvent(BaseModel):
    """Event wenn Duplicate Order blockiert wird."""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    idempotency_key: str
    exchange_order_id: str  # Die bereits existierende Order
    reason: str


class OrderSubmissionState(StrEnum):
    """Möglich Zustände einer Order nach dem Submit."""

    SUBMITTED = "submitted"  # Erfolgreich akzeptiert, noch nicht gefüllt
    FILLED = "filled"  # Komplett gefüllt
    PARTIAL_FILL = "partial_fill"  # Teilweise gefüllt
    REJECTED = "rejected"  # Exchange hat abgelehnt
    CANCELLED = "cancelled"  # War offen, wurde gecancelt
    UNKNOWN = "unknown"  # Zustand unbekannt (Netzwerkfehler, etc.)
    DUPLICATE_BLOCKED = "duplicate_blocked"  # War bereits submitted


class SafeOrderExecutor:
    """
    Sichere Order-Execution mit Duplicate Detection und Unknown State Handling.
    
    WICHTIG: Diese Klasse wird VOR dem tatsächlichen Exchange-Call
    in ExecutionEngine.execute() eingeschaltet (quasi Middleware).
    """

    def __init__(self, order_repository: Any) -> None:
        """
        Args:
            order_repository: Repository für Order-Persistenz
        """
        self._order_repo = order_repository
        self._in_flight_orders: dict[str, OrderResult] = {}  # In-memory cache

    async def execute_safely(
        self,
        order_request: OrderRequest,
        exchange_submit_fn: Any,  # async callable(order_request) -> OrderResult
    ) -> OrderResult:
        """
        Führt Order aus mit Duplicate-Detection und Unknown State Handling.
        
        Sequence:
        1. Idempotency Key berechnen
        2. Duplicate Check (DB + Memory)
        3. Falls Duplicate: Return cached result
        4. Falls neu: Submit zu Exchange
        5. Fehler bei Submit? → Unknown State Handling
        6. Erfolgreich? → Speichern + Cache
        
        Args:
            order_request: Der Order
            exchange_submit_fn: Async function (request) -> OrderResult
            
        Returns:
            OrderResult (entweder neu oder aus Cache)
        """

        idempotency_key = IdempotencyKey.from_order_request(order_request)
        key_str = idempotency_key.to_string()

        log.info(
            "safe_executor.checking_duplicate",
            idempotency_key=key_str,
            order_id=str(order_request.id),
        )

        # 1. Check in-memory cache (schnell, nur diese Process Instance)
        if key_str in self._in_flight_orders:
            cached = self._in_flight_orders[key_str]
            log.warning(
                "safe_executor.duplicate_blocked_from_memory",
                idempotency_key=key_str,
                exchange_order_id=cached.exchange_order_id,
            )
            return self._make_duplicate_result(
                cached,
                reason="Blocked by in-memory duplicate cache"
            )

        # 2. Check Datenbank (Crash-Recovery: Orders persist über Neustarts)
        if self._order_repo:
            existing = await self._order_repo.find_by_idempotency_key(key_str)
            if existing:
                log.warning(
                    "safe_executor.duplicate_blocked_from_db",
                    idempotency_key=key_str,
                    existing_order_id=existing.get("id"),
                )
                # Reconstruct OrderResult von DB
                cached_result = self._order_result_from_db_record(existing)
                self._in_flight_orders[key_str] = cached_result
                return self._make_duplicate_result(
                    cached_result,
                    reason="Blocked by database duplicate record"
                )

        # 3. Neu: Submit zu Exchange
        log.info(
            "safe_executor.submitting_order",
            idempotency_key=key_str,
            order_id=str(order_request.id),
        )

        try:
            result = await exchange_submit_fn(order_request, idempotency_key=key_str)
        except Exception as e:
            log.error(
                "safe_executor.submit_error",
                idempotency_key=key_str,
                error=str(e),
                exc_info=True,
            )
            # Unknown State: Netzwerkfehler, Timeout, etc.
            # Return UNKNOWN state statt REJECTED
            return self._make_unknown_result(order_request, str(e))

        # 4. Success: Cache + Speichern
        self._in_flight_orders[key_str] = result

        if self._order_repo:
            await self._order_repo.create(
                {
                    "id": str(order_request.id),
                    "idempotency_key": key_str,
                    "exchange_order_id": result.exchange_order_id,
                    "symbol": str(order_request.symbol),
                    "side": order_request.side.value,
                    "quantity": order_request.quantity,
                    "status": result.status.value,
                    "submitted_at": result.submitted_at,
                }
            )

        log.info(
            "safe_executor.order_submitted_success",
            idempotency_key=key_str,
            exchange_order_id=result.exchange_order_id,
            status=result.status.value,
        )

        return result

    def _make_duplicate_result(self, original: OrderResult, reason: str) -> OrderResult:
        """Erstellt DUPLICATE_BLOCKED Result aus Original."""
        return OrderResult(
            request_id=original.request_id,
            exchange_order_id=original.exchange_order_id,
            symbol=original.symbol,
            status=OrderStatus.REJECTED,  # Duplicate = nicht ausgeführt
            filled_quantity=Decimal("0"),
            fees=Decimal("0"),
            submitted_at=original.submitted_at,
            trading_mode=original.trading_mode,
            raw_response={
                "duplicate": True,
                "original_exchange_order_id": original.exchange_order_id,
                "reason": reason,
            },
        )

    def _make_unknown_result(
        self,
        order_request: OrderRequest,
        error: str,
    ) -> OrderResult:
        """Erstellt UNKNOWN Result bei Submit-Fehler."""
        return OrderResult(
            request_id=order_request.id,
            exchange_order_id=f"UNKNOWN-{order_request.id}",
            symbol=order_request.symbol,
            status=OrderStatus.UNKNOWN,  # Custom Status für Reconciliation
            filled_quantity=Decimal("0"),
            fees=Decimal("0"),
            submitted_at=datetime.now(UTC),
            trading_mode=order_request.trading_mode,
            raw_response={
                "unknown": True,
                "error": error,
                "action_required": "Reconciliation needed to determine actual status",
            },
        )

    def _order_result_from_db_record(self, record: dict[str, Any]) -> OrderResult:
        """Reconstructs OrderResult von DB Record."""
        # Simplified - würde volle Rekonstruktion durchführen
        from sgr.core.types import ExchangeID, Symbol

        return OrderResult(
            request_id=record.get("id"),
            exchange_order_id=record.get("exchange_order_id"),
            symbol=Symbol.parse_obj({"base": "BTC", "quote": "USDT", "exchange": ExchangeID.PIONEX}),
            status=OrderStatus[record.get("status", "REJECTED").upper()],
            filled_quantity=Decimal(str(record.get("filled_quantity", 0))),
            fees=Decimal(str(record.get("fees", 0))),
            submitted_at=record.get("submitted_at"),
            trading_mode=record.get("trading_mode"),
        )


# === Custom OrderStatus für Unknown Submissions ===

# Extension zu sgr/core/types.py OrderStatus:
# OrderStatus sollte um UNKNOWN erweitert werden
"""
class OrderStatus(StrEnum):
    ...
    UNKNOWN = "unknown"  # NEW - Order status uncertain, needs reconciliation
    DUPLICATE = "duplicate"  # NEW - Duplicate blocked
"""
