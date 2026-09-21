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

5. DB-GESTUETZTE IDEMPOTENZ FUER PROZESS-NEUSTARTS (Root-Cause-Fix,
   Docker-Crash-Test-Audit 2026-09-16)
   Punkt 3 (Exchange-seitige clientOrderId-Pruefung) gilt NUR fuer den
   LIVE-Zweig von ccxt_base.py::place_order - PAPER-Order-Requests
   nehmen dort einen fruehen Sonderpfad (_simulate_order()) und
   erreichen die clientOrderId/fetchOrder-Pruefung nie. Da diese
   Produktion ausschliesslich in PAPER laeuft, existierte dadurch de
   facto KEINE prozessuebergreifende Idempotenz: nach einem Worker-
   Neustart (frischer, leerer In-Memory-State aus Punkt 2) haette ein
   erneutes execute_safely() mit identischer order.id (z.B. ein vom
   Aufrufer wiederholtes OrderRequest-Objekt) die Order ein zweites Mal
   simuliert/gefuellt. Gefunden beim Aufbau echter Crash-Tests gegen die
   laufende Infrastruktur (tests/docker_crash_tests/test_crash_
   scenarios.py::TestWorkerRestartDuringOrderProcessing).

   Fix: vor jedem Submit wird - falls ein OrderRepository injiziert ist -
   zusaetzlich der DB-Record fuer order.id geprueft:
     - Terminalstatus (FILLED/CANCELLED/REJECTED/EXPIRED) bereits
       persistiert -> das Ergebnis wird aus der DB rekonstruiert und
       zurueckgegeben, KEIN erneuter Submit.
     - Nicht-terminaler Status (PENDING/SUBMITTED/PARTIALLY_FILLED)
       persistiert, aber kein In-Memory-Tracking in DIESEM Prozess ->
       exakt der Crash-Recovery-Fall (eine fruehere Prozessinstanz ist
       waehrend der Bearbeitung gestorben). Wird wie ein Unknown-State
       behandelt (Punkt 4) - kein blindes Neu-Submitten, da unklar ist,
       ob der Fill vor dem Crash noch stattgefunden hat.
     - Kein DB-Record -> normaler, neuer Order-Flow.
   Best-effort/fail-safe: schlaegt die DB-Abfrage selbst fehl (DB down),
   wird das wie "kein Record gefunden" behandelt und normal fortgefahren
   - ein DB-Ausfall darf laut Projektgrundsatz den Trading-Betrieb nicht
   blockieren, siehe PortfolioEngine._persist_position_upsert() fuer das
   identische Muster.

Fail-Safe Rule:
Bei Unsicherheit -> STOP und zur Reconciliation, niemals automatisch
erneut submitten.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sgr.core.logging import get_logger
from sgr.core.types import OrderRequest, OrderResult, OrderStatus

log = get_logger(__name__)

ExchangeSubmitFn = Callable[[OrderRequest], Awaitable[OrderResult]]

_TERMINAL_STATUSES = {
    OrderStatus.FILLED.value,
    OrderStatus.CANCELLED.value,
    OrderStatus.REJECTED.value,
    OrderStatus.EXPIRED.value,
}


class SafeOrderExecutor:
    """
    Sichere Order-Execution mit In-Process Duplicate Detection,
    Unknown-State-Handling bei Submit-Fehlern und (bei injiziertem
    OrderRepository) DB-gestuetzter Idempotenz ueber Prozess-Neustarts
    hinweg.

    Wird als Middleware VOR dem tatsaechlichen Exchange-Call in
    ExecutionEngine._execute_internal() eingeschaltet.

    order_repository ist optional (Default None = reines In-Memory-
    Verhalten wie vor dem DB-Fix, z.B. fuer isolierte Unit-Tests ohne
    DB-Zugriff) - siehe Modul-Docstring Punkt 5 fuer die Begruendung,
    warum dies fuer PAPER-Betrieb trotzdem notwendig ist und nicht nur
    "zusaetzliche Absicherung" wie urspruenglich fuer LIVE angenommen.
    """

    def __init__(self, order_repository: Any = None, tenant_id: str | None = None) -> None:
        self._in_flight: dict[str, OrderResult] = {}
        self._order_repo = order_repository
        self._tenant_id = tenant_id

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

        # DB-gestuetzte Idempotenz (Modul-Docstring Punkt 5): faengt den
        # Fall ab, dass eine FRUEHERE Prozessinstanz diese order.id schon
        # einmal bearbeitet/persistiert hat, bevor sie (Crash/Neustart)
        # ihren In-Memory-State verlor. Ohne diesen Check waere obiger
        # In-Memory-Check nach jedem Neustart wirkungslos.
        db_row = await self._lookup_persisted(order_key)
        if db_row is not None:
            if db_row["status"] in _TERMINAL_STATUSES:
                log.warning(
                    "safe_executor.duplicate_blocked_via_db",
                    order_id=order_key,
                    persisted_status=db_row["status"],
                )
                return self._result_from_db_row(db_row, order)
            log.error(
                "safe_executor.unknown_state_recovered_from_db",
                order_id=order_key,
                persisted_status=db_row["status"],
            )
            return self._make_unknown_result(
                order,
                f"Order {order_key} already persisted with non-terminal "
                f"status {db_row['status']!r} by a previous process "
                f"instance - not resubmitting blindly.",
            )

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

        # DB-Persistenz VOR dem Exchange-Call (Modul-Docstring Punkt 5):
        # muss HIER passieren (nach den Duplicate-Checks oben, vor
        # exchange_submit_fn), nicht wie zuvor erst danach in
        # ExecutionEngine._execute_internal(). Andernfalls hinterlaesst
        # ein Crash zwischen Exchange-Call und dem alten Post-Call-
        # Persist-Zeitpunkt GAR KEINEN DB-Record - der obige DB-Check
        # haette nach einem Neustart nichts zu finden und wuerde einen
        # kompletten Neuversuch faelschlich zulassen, obwohl die
        # Exchange den ersten Versuch ggf. bereits verarbeitet hat.
        await self._persist_pending(order)

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
            # BUG-FIX (Produktions-Audit 2026-09-21, 1496 verwaiste PENDING-
            # Orders bei Sumo): _persist_pending() oben hat den Record
            # bereits in einer EIGENEN, committeten Transaktion angelegt -
            # ohne diesen Aufruf blieb er bei jedem Submit-Fehler auf
            # PENDING stehen, für immer, da execute_safely() hier direkt
            # zurückkehrte, statt den bereits vorhandenen _persist_final()-
            # Pfad (der REJECTED/unknown-Ergebnisse genauso persistiert wie
            # FILLED-Ergebnisse) auch im Fehlerfall zu nutzen. Historisch
            # ausgelöst durch wiederholte Binance-IP-Bans (siehe Commit
            # 22a71fe) während get_ticker() in _simulate_order()
            # (ccxt_base.py) exceptions warf, aber jeder zukünftige
            # Submit-Fehler (Netzwerk, Timeout, Exchange-Fehler) hätte
            # denselben Effekt gehabt.
            await self._persist_final(order, unknown_result)
            self._in_flight.pop(order_key, None)
            return unknown_result

        self._in_flight[order_key] = result
        await self._persist_final(order, result)
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

    async def _persist_pending(self, order: OrderRequest) -> None:
        """
        Legt den initialen Order-Record VOR dem Exchange-Call als PENDING
        an (Modul-Docstring Punkt 5). Best-effort/fail-safe: ein DB-
        Fehler hier darf den Exchange-Call nicht verhindern, nur die
        Cross-Restart-Idempotenz-Absicherung entfaellt fuer diesen einen
        Versuch (gleiches Muster wie ueberall sonst in dieser Codebase,
        siehe PortfolioEngine._persist_position_upsert()).

        id wird explizit auf order.id gesetzt (nicht die von create()
        zurueckgegebene ID), damit _persist_final() per order.id
        dieselbe Zeile trifft.
        """
        if self._order_repo is None:
            return
        try:
            await self._order_repo.create(
                {
                    "id": str(order.id),
                    "signal_id": str(order.signal_id),
                    "exchange_order_id": None,
                    "symbol": str(order.symbol),
                    "exchange": order.symbol.exchange.value,
                    "side": order.side.value,
                    "order_type": order.order_type.value,
                    "quantity": order.quantity,
                    "limit_price": order.limit_price,
                    "filled_quantity": Decimal("0"),
                    "status": OrderStatus.PENDING.value,
                    "trading_mode": order.trading_mode.value,
                    "strategy_name": str(order.metadata.get("strategy", "unknown")),
                    "submitted_at": datetime.now(tz=UTC),
                    "user_id": self._tenant_id,
                }
            )
        except Exception as e:
            log.error(
                "safe_executor.persist_pending_failed",
                order_id=str(order.id),
                error=str(e),
            )

    async def _persist_final(self, order: OrderRequest, result: OrderResult) -> None:
        """Aktualisiert den PENDING-Record (siehe _persist_pending()) mit
        dem tatsaechlichen Exchange-Ergebnis. Best-effort/fail-safe."""
        if self._order_repo is None:
            return
        try:
            await self._order_repo.update_status(
                order_id=str(order.id),
                status=result.status.value,
                filled_quantity=result.filled_quantity,
                average_fill_price=result.average_fill_price,
                fees=result.fees,
                filled_at=result.filled_at,
                exchange_order_id=result.exchange_order_id or None,
            )
        except Exception as e:
            log.error(
                "safe_executor.persist_final_failed",
                order_id=str(order.id),
                error=str(e),
            )

    async def _lookup_persisted(self, order_key: str) -> dict[str, Any] | None:
        """Best-effort DB-Lookup (Modul-Docstring Punkt 5). Jeder Fehler
        (kein Repository injiziert, DB nicht erreichbar) wird als "kein
        Record gefunden" behandelt - ein DB-Ausfall darf die Order-
        Verarbeitung nicht blockieren, nur die zusaetzliche Cross-Restart-
        Absicherung entfaellt fuer diesen einen Aufruf."""
        if self._order_repo is None:
            return None
        try:
            return await self._order_repo.get_by_id(order_key)  # type: ignore[no-any-return]
        except Exception as e:
            log.warning(
                "safe_executor.db_idempotency_check_failed",
                order_id=order_key,
                error=str(e),
            )
            return None

    def _result_from_db_row(self, row: dict[str, Any], order: OrderRequest) -> OrderResult:
        """Rekonstruiert ein OrderResult aus einem bereits persistierten
        Order-Record fuer eine als Duplikat erkannte order.id."""
        return OrderResult(
            request_id=order.id,
            exchange_order_id=row.get("exchange_order_id") or "",
            symbol=order.symbol,
            status=OrderStatus(row["status"]),
            filled_quantity=row.get("filled_quantity") or Decimal("0"),
            average_fill_price=row.get("average_fill_price"),
            fees=row.get("fees") or Decimal("0"),
            submitted_at=row["submitted_at"],
            filled_at=row.get("filled_at"),
            trading_mode=order.trading_mode,
            raw_response={
                **(row.get("raw_response") or {}),
                "duplicate": True,
                "duplicate_source": "db_idempotency_check",
                "rejection_reason": (
                    "Duplicate order submission blocked (already persisted "
                    "by a previous process instance)"
                ),
            },
        )

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
