"""
SGR Order Safety & Idempotency (Baustein 7)
=============================================

Verhindert echte Doppel-Orders bei Retries und blockiert doppelte
Submissions derselben order.id, die aus dem Prozess selbst kommen
(z.B. Orchestrator-Retry-Logik, Event-Replay).

Architektur:

1. IDEMPOTENCY KEY = order.id
   order.id ist eine stabile UUID pro OrderRequest und bleibt bei einem
   Retry derselben Order identisch. Bewusst NICHT signal_id/symbol/side,
   da das faelschlich unterschiedliche, legitime Orders blockieren wuerde
   (z.B. zwei verschiedene Signale, gleiches Symbol/Seite kurz
   hintereinander - Cooldown/Kill-Switch regeln Trading-Frequenz bereits
   an anderer Stelle, siehe Baustein 3/2). Idempotency-Schutz betrifft
   ausschliesslich Netzwerk-Retries DERSELBEN OrderRequest.

2. IN-PROCESS DUPLICATE DETECTION
   Vor jedem Order-Submit: ist order.id bereits als in-flight bekannt
   (dieser Prozess, seit dessen Start)? Falls ja: Submission wird sofort
   blockiert, ohne die Exchange zu kontaktieren.

3. EXCHANGE-SEITIGE IDEMPOTENCY (persistent, prozessuebergreifend)
   Die eigentliche Crash-/Neustart-resistente Wahrheit liegt NICHT hier,
   sondern bei der Exchange selbst: sgr/exchanges/ccxt_base.py::place_order
   sendet order.id als clientOrderId und prueft bei einem Retry per
   fetchOrder, ob die Exchange die Order bereits akzeptiert hat, bevor
   eine neue erzeugt wird. Das ist robuster als ein zusaetzliches
   DB-Feld, das synchron zur Exchange gehalten werden muesste - die
   Exchange ist hier die Quelle der Wahrheit, kein SGR-Duplikat davon.

4. UNKNOWN STATE HANDLING
   Wenn der Submit-Call selbst fehlschlaegt (Netzwerkfehler, Timeout),
   ist der tatsaechliche Order-Status unklar: die Exchange koennte die
   Order trotzdem angenommen haben. SafeOrderExecutor gibt in diesem
   Fall KEINEN automatischen Retry, sondern ein REJECTED-Result mit
   raw_response["unknown"]=True als expliziten Marker fuer Reconciliation
   - kein blindes Neu-Submitten.

Fail-Safe Rule:
Bei Unsicherheit -> STOP und zur Reconciliation, niemals automatisch
erneut submitten.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

from sgr.core.logging import get_logger
from sgr.core.types import OrderRequest, OrderResult, OrderStatus

log = get_logger(__name__)

ExchangeSubmitFn = Callable[[OrderRequest], Awaitable[OrderResult]]


class SafeOrderExecutor:
    """
    Sichere Order-Execution mit In-Process Duplicate Detection und
    Unknown-State-Handling bei Submit-Fehlern.

    Wird als Middleware VOR dem tatsaechlichen Exchange-Call in
    ExecutionEngine._execute_internal() eingeschaltet.

    In-Memory only: der Tracking-State ist bewusst nicht persistent -
    Crash-/Neustart-Resistenz liefert die Exchange-seitige clientOrderId
    in ccxt_base.py::place_order, nicht dieses Modul (siehe Modul-
    Docstring, Punkt 3).
    """

    def __init__(self) -> None:
        self._in_flight: dict[str, OrderResult] = {}

    async def execute_safely(
        self,
        order: OrderRequest,
        exchange_submit_fn: ExchangeSubmitFn,
    ) -> OrderResult:
        """
        Fuehrt eine Order aus mit In-Process Duplicate-Detection.

        Args:
            order: Der OrderRequest.
            exchange_submit_fn: async callable(order) -> OrderResult,
                typischerweise adapter.place_order.

        Returns:
            OrderResult - entweder das echte Submit-Ergebnis, ein
            REJECTED-Result mit raw_response["duplicate"]=True, oder ein
            REJECTED-Result mit raw_response["unknown"]=True bei
            Submit-Fehler.
        """
        order_key = str(order.id)

        if order_key in self._in_flight:
            cached = self._in_flight[order_key]
            log.warning(
                "safe_executor.duplicate_blocked",
                order_id=order_key,
                exchange_order_id=cached.exchange_order_id,
            )
            return self._make_duplicate_result(cached)

        # Placeholder VOR dem Exchange-Call setzen (nicht erst danach) -
        # sonst greift der Duplicate-Check nicht waehrend ein erster
        # Aufruf noch auf die Exchange-Antwort wartet (die eigentliche
        # Race, die dieser Schutz verhindern soll).
        placeholder = OrderResult(
            request_id=order.id,
            exchange_order_id="",
            symbol=order.symbol,
            status=OrderStatus.PENDING,
            filled_quantity=Decimal("0"),
            trading_mode=order.trading_mode,
            submitted_at=datetime.now(tz=UTC),
            raw_response={},
        )
        self._in_flight[order_key] = placeholder

        try:
            result = await exchange_submit_fn(order)
        except Exception as e:
            log.error(
                "safe_executor.submit_error",
                order_id=order_key,
                error=str(e),
                exc_info=True,
            )
            unknown_result = self._make_unknown_result(order, str(e))
            self._in_flight.pop(order_key, None)
            return unknown_result

        self._in_flight[order_key] = result
        return result

    def release(self, order: OrderRequest) -> None:
        """
        Gibt das In-Process-Tracking fuer eine Order frei, sobald sie
        terminiert ist (FILLED/CANCELLED/REJECTED). Muss vom Aufrufer
        (ExecutionEngine) explizit aufgerufen werden, sobald der
        Order-Lifecycle abgeschlossen ist - Symmetrie zu execute_safely().
        """
        self._in_flight.pop(str(order.id), None)

    def get_inflight(self, order: OrderRequest) -> OrderResult | None:
        """Gibt das aktuellste bekannte In-Flight-Result zurueck, falls
        vorhanden (z.B. fuer Shutdown-Safety-Cancel-Zwecke)."""
        return self._in_flight.get(str(order.id))

    def update_inflight(self, order: OrderRequest, result: OrderResult) -> None:
        """Aktualisiert das getrackte Result waehrend des Fill-Monitorings
        (z.B. nach jedem Poll), damit shutdown() immer den zuletzt
        bekannten Status/exchange_order_id sieht."""
        if str(order.id) in self._in_flight:
            self._in_flight[str(order.id)] = result

    def all_inflight(self) -> dict[str, OrderResult]:
        """Alle aktuell getrackten In-Flight-Orders (fuer Shutdown)."""
        return dict(self._in_flight)

    def clear(self) -> None:
        """Verwirft das gesamte In-Flight-Tracking (nach Shutdown-Cleanup)."""
        self._in_flight.clear()

    def _make_duplicate_result(self, original: OrderResult) -> OrderResult:
        """Erstellt ein REJECTED-Result mit Duplicate-Markierung aus dem
        Original-Ergebnis (kein neuer OrderStatus-Wert noetig)."""
        return OrderResult(
            request_id=original.request_id,
            exchange_order_id=original.exchange_order_id,
            symbol=original.symbol,
            status=OrderStatus.REJECTED,
            filled_quantity=Decimal("0"),
            fees=Decimal("0"),
            submitted_at=original.submitted_at,
            trading_mode=original.trading_mode,
            raw_response={
                "duplicate": True,
                "original_exchange_order_id": original.exchange_order_id,
                "rejection_reason": "Duplicate order submission blocked",
            },
        )

    def _make_unknown_result(self, order: OrderRequest, error: str) -> OrderResult:
        """Erstellt ein REJECTED-Result mit Unknown-State-Markierung bei
        Submit-Fehler (kein neuer OrderStatus-Wert noetig - REJECTED plus
        raw_response["unknown"]=True signalisiert eindeutig: dies ist
        KEINE bestaetigte Ablehnung durch die Exchange, sondern ein
        unklarer Zustand, der Reconciliation braucht, bevor erneut
        submittet werden darf)."""
        return OrderResult(
            request_id=order.id,
            exchange_order_id="",
            symbol=order.symbol,
            status=OrderStatus.REJECTED,
            filled_quantity=Decimal("0"),
            fees=Decimal("0"),
            submitted_at=datetime.now(tz=UTC),
            trading_mode=order.trading_mode,
            raw_response={
                "unknown": True,
                "error": error,
                "rejection_reason": f"Execution error: {error}",
                "action_required": "Reconciliation needed to determine actual status",
            },
        )
