"""
SGR Execution Engine
====================
Verarbeitet OrderRequests von der Risk Engine bis zum bestätigten Fill.

Verantwortlichkeiten:
    1. OrderRequest entgegennehmen (von Risk Engine)
    1b. Preflight Validation (Baustein 6, siehe execution/preflight.py)
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
    Side,
    TradingMode,
)
from sgr.exchanges.base import ExchangeError
from sgr.exchanges.factory import ExchangePool
from sgr.execution.order_safety import SafeOrderExecutor
from sgr.execution.preflight import PreflightValidator
from sgr.execution.quantization import quantize_and_validate, quantize_price
from sgr.monitoring.trading_metrics import (
    record_duplicate_blocked,
    record_order_filled,
    record_order_rejected,
    record_order_submitted,
)
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
        # tenant_id fuer Kill-Switch-Scoping (siehe Audit nach Commit 5,
        # sgr/risk/kill_switch.py): get_config() statt eines zusaetzlichen
        # Konstruktor-Parameters, um die bestehende Aufrufstelle in
        # lifespan() (ExecutionEngine(pool, config.trading_mode, ...))
        # unveraendert zu lassen - tenant_id ist ohnehin nur ueber
        # get_config() im selben Prozess verfuegbar.
        from sgr.core.config import get_config

        self._tenant_id = get_config().tenant_id
        self._kill_switch = get_kill_switch(trading_mode, tenant_id=self._tenant_id)
        # Optional: OrderRepository fuer Persistenz. None = rein
        # In-Memory/Event-basiert (Tests, isolierte Nutzung) - additiv,
        # analog zu PortfolioEngine._position_repo. Ohne Injektion
        # verhaelt sich die Engine exakt wie vor diesem Feature.
        self._order_repo = order_repository
        # Baustein 6: letzte deterministische Prüfung, ob eine bereits
        # von der Risk Engine genehmigte Order gerade jetzt technisch
        # sicher sendbar ist. Siehe sgr/execution/preflight.py
        # Modul-Docstring für die vollständige Architekturbegründung und
        # Abgrenzung zu RiskEngine/StartupSafetyChecker/confirm_live.
        self._preflight = PreflightValidator(pool, trading_mode)
        # Baustein 7: In-Process Duplicate Order Protection + Shutdown
        # Safety Middleware. Siehe sgr/execution/order_safety.py
        # Modul-Docstring fuer die vollstaendige Architekturbegruendung
        # (Idempotency-Key = order.id, Unknown-State-Handling bei
        # Submit-Fehlern, Abgrenzung zur Exchange-seitigen clientOrderId,
        # sowie Punkt 5 fuer den DB-gestuetzten Idempotenz-Fix, der
        # PAPER-Order-Requests ueber einen Prozess-Neustart hinweg
        # absichert - dieselbe order_repository-Injektion wie fuer
        # _persist_order_create()/_persist_order_status() oben).
        self._safety = SafeOrderExecutor(
            order_repository=order_repository, tenant_id=self._tenant_id
        )
        # Leverage-Cache (Symbol -> zuletzt erfolgreich gesetzte Leverage
        # DIESES Prozesses). Vermeidet einen redundanten set_leverage()-
        # Exchange-Call vor jeder einzelnen Order, wenn der Zielwert sich
        # nicht geaendert hat. Bewusst kein Redis/DB-Backing - im
        # schlimmsten Fall (Prozess-Neustart) wird einmal mehr gesetzt,
        # niemals seltener als noetig; ein leerer Cache nach Neustart ist
        # also fail-safe in die sichere Richtung.
        self._leverage_cache: dict[str, Decimal] = {}

    async def execute(
        self, order: OrderRequest, bypass_kill_switch: bool = False
    ) -> OrderResult:
        """
        Hauptmethode: OrderRequest → OrderResult.

        Fail-Safe: jede Exception → REJECTED Result (kein uncontrolled State).

        bypass_kill_switch: fuer PositionLiquidator (siehe
            sgr/risk/position_liquidator.py) UND PositionProtectionWatchdog
            (siehe sgr/risk/position_protection.py) gedacht - eine
            de-risking/reduce_only Order (Kill-Switch-Flatten, Stop-Loss,
            Take-Profit, Max-Holding-Time-Exit) darf nicht an der
            is_active-Sperre scheitern, die genau diese Order eigentlich
            ausloesen soll ("bestehende Positionen bleiben verwaltbar,
            nur neue Entries werden blockiert"). Default False haelt
            jeden bestehenden Aufrufer (Orchestrator, Tests) unveraendert
            streng: neues Risiko bleibt
            bei aktivem Kill Switch blockiert.
        """
        # Sanity check: trading_mode muss übereinstimmen
        if order.trading_mode != self._trading_mode:
            raise ValueError(
                f"Order trading_mode {order.trading_mode} "
                f"does not match engine mode {self._trading_mode}"
            )

        # Live Trading Safety Gate (siehe sgr/risk/live_trading_gate.py
        # Modul-Docstring): No-op fuer PAPER, aber fuer LIVE die letzte,
        # harte Absicherung VOR dem Kill-Switch-Check und jedem
        # Exchange-Call - verweigert insbesondere jede Strategie, die nur
        # per STRATEGY_FORCE_ACTIVATE-Operator-Override aktiv ist. Kein
        # bypass_kill_switch-Sonderfall: eine schliessende Order des
        # PositionLiquidator ist immer noch eine LIVE-Order und muss
        # denselben Nachweis (live_approved, kein Override) erbringen.
        from sgr.risk.live_trading_gate import check_live_trading_allowed
        from sgr.strategy.registry import StrategyRegistry

        gate_result = check_live_trading_allowed(
            order,
            registry=StrategyRegistry.get(),
            kill_switch=self._kill_switch,
            exchange_pool=self._pool,
        )
        if not gate_result.allowed:
            log.critical(
                "execution_engine.blocked_by_live_trading_gate",
                order_id=str(order.id),
                reason=gate_result.reason,
            )
            record_order_rejected(
                exchange=order.symbol.exchange.value,
                symbol=str(order.symbol),
                reason="live_trading_gate",
            )
            return self._rejected_result(order, gate_result.reason or "Live trading blocked")

        # Kill Switch (letzte Absicherung vor Exchange-Call)
        if self._kill_switch.is_active and not bypass_kill_switch:
            log.warning(
                "execution_engine.blocked_by_kill_switch",
                order_id=str(order.id),
            )
            record_order_rejected(
                exchange=order.symbol.exchange.value,
                symbol=str(order.symbol),
                reason="kill_switch_active",
            )
            return self._rejected_result(order, "Kill switch active")

        # Preflight Validation (Baustein 6): letzte technische Prüfung
        # unmittelbar vor dem Exchange-Call. Sendet selbst keine Order.
        # LIVE ist fail-closed: nur bei result.eligible wird überhaupt
        # versucht zu senden. PAPER überspringt die meisten Checks
        # (siehe preflight.py Modul-Docstring), schlägt aber bei
        # strukturell ungültigen Orders (z.B. quantity <= 0) trotzdem fehl.
        preflight_result = await self._preflight.validate(order)
        if not preflight_result.eligible:
            log.warning(
                "execution_engine.blocked_by_preflight",
                order_id=str(order.id),
                reason=preflight_result.rejection_summary,
            )
            record_order_rejected(
                exchange=order.symbol.exchange.value,
                symbol=str(order.symbol),
                reason="preflight_failed",
            )
            return self._rejected_result(
                order, f"Preflight validation failed: {preflight_result.rejection_summary}"
            )

        # Leverage (TEST_1X / zentrales Risk-Profile, siehe RiskLimitsConfig.
        # default_leverage): nur fuer eroeffnende/vergroessernde Orders -
        # eine reduce_only-Order (Close, SL, TP, Kill-Switch-Flatten) darf
        # niemals die Account-Leverage veraendern. Faengt bewusst VOR
        # _execute_internal() ab (nicht danach), damit bei einem Fehler
        # gar keine Order gesendet wird - fail-closed, siehe
        # CCXTBaseAdapter.set_leverage() Docstring: niemals stillschweigend
        # von einer bereits korrekten Leverage ausgehen.
        if not order.reduce_only:
            leverage_error = await self._ensure_leverage(order)
            if leverage_error is not None:
                log.error(
                    "execution_engine.blocked_by_leverage_setting",
                    order_id=str(order.id),
                    error=leverage_error,
                )
                record_order_rejected(
                    exchange=order.symbol.exchange.value,
                    symbol=str(order.symbol),
                    reason="leverage_set_failed",
                )
                return self._rejected_result(order, f"Could not set leverage: {leverage_error}")

        # Exchange-Precision/Minimum-Order-Groesse (Paper/Live-Parity-Fix):
        # rundet quantity auf die von der Exchange gemeldete Precision ab
        # und lehnt ab, wenn das Ergebnis unter min_amount/min_notional
        # faellt - niemals aufrunden (siehe quantization.py Docstring).
        # Nur fuer eroeffnende Orders: eine reduce_only-Order muss die
        # Menge der bestehenden Position treffen, nicht neu bemessen
        # werden. Laeuft in BEIDEN Modi identisch (PAPER nutzt Binance
        # Testnet-Marktdaten, dieselben LOT_SIZE/MIN_NOTIONAL-Filter wie
        # LIVE) - vorher wurde diese Pruefung in PAPER komplett
        # uebersprungen.
        if not order.reduce_only:
            order, quantization_error = await self._quantize_order(order)
            if quantization_error is not None:
                log.warning(
                    "execution_engine.blocked_by_min_order_size",
                    order_id=str(order.id),
                    error=quantization_error,
                )
                record_order_rejected(
                    exchange=order.symbol.exchange.value,
                    symbol=str(order.symbol),
                    reason="min_order_size",
                )
                return self._rejected_result(order, quantization_error)

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
        finally:
            # Order ist terminiert (FILLED/CANCELLED/REJECTED/Fehler) -
            # Duplicate-Guard-Tracking freigeben (Baustein 7). Symmetrisch
            # zum Placeholder-Eintrag, den execute_safely() setzt.
            self._safety.release(order)

    async def _ensure_leverage(self, order: OrderRequest) -> str | None:
        """
        Stellt sicher, dass die Account-Leverage fuer order.symbol auf
        config.risk_limits.default_leverage steht, BEVOR eine
        eroeffnende Order gesendet wird (siehe execute() Aufrufstelle).

        Returns:
            None bei Erfolg (oder wenn die Exchange kein Leverage-Konzept
            kennt, z.B. Spot-only wie Pionex - dort ist "keine Aenderung
            noetig" das korrekte, nicht-blockierende Ergebnis).
            Ein Fehlertext bei einem echten Exchange-Fehler - der
            Aufrufer (execute()) lehnt die Order dann ab, statt
            stillschweigend mit unbekannter Leverage fortzufahren.
        """
        from sgr.core.config import get_config
        from sgr.exchanges.base import NotSupportedFeatureError

        target = get_config().risk_limits.default_leverage
        symbol_key = str(order.symbol)

        if self._leverage_cache.get(symbol_key) == target:
            return None

        adapter = self._pool.get(order.symbol.exchange, self._trading_mode)
        try:
            await adapter.set_leverage(order.symbol.ccxt_symbol, target)
        except NotSupportedFeatureError:
            # Spot-only Exchange (z.B. Pionex) - kein Leverage-Konzept,
            # kein Fehler. Cache trotzdem setzen, um den wiederholten
            # (wirkungslosen) Call bei jeder Order zu vermeiden.
            self._leverage_cache[symbol_key] = target
            return None
        except Exception as e:
            return str(e)

        self._leverage_cache[symbol_key] = target
        return None

    async def _quantize_order(self, order: OrderRequest) -> tuple[OrderRequest, str | None]:
        """
        Siehe sgr/execution/quantization.py Modul-Docstring. Holt
        SymbolLimits ueber das bereits gecachte get_exchange_info() (kein
        zusaetzlicher Netzwerk-Call) und - fuer Market Orders ohne
        limit_price - einen frischen Ticker als Preis-Schaetzung fuer den
        Notional-Check (analoge, bereits akzeptierte Einschraenkung wie
        PreflightValidator._check_balance_and_capital: der exakte
        Fill-Preis ist vor Ausfuehrung nicht bekannt).

        Returns:
            (order, None) bei Erfolg - order.quantity ggf. abgerundet.
            (order, reason) wenn die Order unter die Exchange-Minimalgroesse
            faellt - der Aufrufer lehnt dann ab, ohne die Order zu senden.
        """
        adapter = self._pool.get(order.symbol.exchange, self._trading_mode)
        try:
            info = await adapter.get_exchange_info()
            # order.symbol.ccxt_symbol ("BTC/USDT"), NICHT str(order.symbol)
            # ("BTC/USDT:binance", Symbol.__str__ inkl. Exchange-Namen) -
            # symbol_limits ist im ccxt-Format geschluesselt (siehe
            # CCXTBaseAdapter._extract_symbol_limits). Identischer Fund/Fix
            # wie in PreflightValidator._check_symbol_availability/
            # _check_symbol_precision_and_limits, siehe dortiger Kommentar.
            limits = info.symbol_limits.get(order.symbol.ccxt_symbol)
            if limits is None:
                # Keine Limits-Daten fuer dieses Symbol - nichts zu
                # quantisieren/validieren, kein Grund fuer den
                # zusaetzlichen Ticker-Call unten.
                return order, None
            price = order.limit_price
            if price is None:
                ticker = await adapter.get_ticker(order.symbol.ccxt_symbol)
                price = ticker.ask if order.side == Side.BUY else ticker.bid
        except Exception as e:
            # Fail-safe wie an allen anderen Best-effort-Marktdaten-
            # Stellen dieser Engine: ohne Limits/Preis kann nicht
            # quantisiert werden, aber ein Marktdaten-Ausfall darf eine
            # ansonsten gueltige, bereits von Risk Engine/Preflight
            # genehmigte Order nicht blockieren - unveraendert
            # durchlassen, nicht ablehnen.
            log.warning(
                "execution_engine.quantization_skipped",
                order_id=str(order.id),
                error=str(e),
            )
            return order, None

        quantized_qty, reason = quantize_and_validate(order.quantity, price, limits)
        if reason is not None:
            return order, reason

        updates: dict[str, Any] = {}
        if quantized_qty != order.quantity:
            updates["quantity"] = quantized_qty

        # Preis-Quantisierung (2026-09-23, siehe quantize_price()
        # Docstring): nur fuer Orders MIT explizitem limit_price - eine
        # MARKET-Order (order.limit_price is None) hat keinen Preis zu
        # quantisieren, unveraendertes Verhalten fuer den heutigen
        # Grid-Controller (submittet ausschliesslich MARKET-Orders) und
        # jede direktionale Strategie.
        if order.limit_price is not None:
            quantized_price, price_reason = quantize_price(order.limit_price, order.side, limits)
            if price_reason is not None:
                return order, price_reason
            if quantized_price != order.limit_price:
                updates["limit_price"] = quantized_price

        if updates:
            order = order.model_copy(update=updates)
        return order, None

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

        # Submit Order (Baustein 7: via SafeOrderExecutor - blockt
        # In-Process-Duplikate derselben order.id und behandelt
        # Submit-Fehler als expliziten Unknown-State statt automatischem
        # Retry, siehe order_safety.py Modul-Docstring).
        result = await self._safety.execute_safely(order, adapter.place_order)

        # Duplicate- oder Unknown-State-Result: kein echter Exchange-
        # Kontakt (Duplicate) bzw. unklarer Ausgang (Unknown) - in beiden
        # Faellen sofort zurueckgeben, kein Fill-Monitoring/Persist fuer
        # eine Order, die entweder nicht submittet wurde oder deren
        # tatsaechlicher Status ungeklaert ist.
        if result.raw_response.get("duplicate") or result.raw_response.get("unknown"):
            log.warning(
                "execution_engine.safety_blocked_or_unknown",
                order_id=str(order.id),
                duplicate=result.raw_response.get("duplicate", False),
                unknown=result.raw_response.get("unknown", False),
            )
            if result.raw_response.get("duplicate"):
                record_duplicate_blocked(
                    exchange=order.symbol.exchange.value,
                    reason="in_process_duplicate_order_id",
                )
            return result

        # Strategy-Attribution in raw_response uebertragen, BEVOR das
        # Result an _on_fill()/PortfolioEngine.on_order_filled() geht.
        # order.metadata["strategy"] (siehe RiskEngine.build_order_request())
        # ist der einzige Ort, an dem der Strategiename zu diesem Zeitpunkt
        # noch bekannt ist - result.raw_response stammt vom Adapter (bei
        # echten ccxt-Adaptern die rohe Exchange-Antwort, die die Exchange
        # selbst natuerlich nicht kennt) und wuerde diese Information sonst
        # verlieren. PortfolioEngine._open_position() liest
        # raw_response.get("strategy", "unknown") fuer positions.strategy_name -
        # ohne diese Zeile war das IMMER "unknown", auch produktiv (siehe
        # tests/integration/test_orchestrator_pipeline.py Happy-Path-Test).
        #
        # exit_reason (siehe ExitReason, sgr/core/types.py) - analoges
        # Muster: PositionProtectionManager/Watchdog (sgr/risk/
        # position_protection.py) setzen order.metadata["exit_reason"] auf
        # eine schliessende Order, PortfolioEngine._update_position() liest
        # es aus raw_response fuer close_reason. Bewusst OHNE Default -
        # fehlt der Key (normaler Entry oder ein gegenlaeufiges Strategie-
        # Signal), faellt PortfolioEngine selbst auf STRATEGY_SIGNAL zurueck.
        # target_price/stop_price (2026-09-23, strategiegetriebener Exit):
        # gleiches Weiterreich-Muster wie "strategy"/"exit_reason" oben -
        # siehe RiskEngine.build_order_request() Kommentar fuer den
        # Ursprung. PortfolioEngine._open_position() liest sie aus
        # result.raw_response und reicht sie an PositionProtectionManager.
        # on_position_opened() weiter.
        extra_attribution: dict[str, Any] = {"strategy": order.metadata.get("strategy", "unknown")}
        for key in ("exit_reason", "target_price", "stop_price", "entry_regime"):
            if key in order.metadata:
                extra_attribution[key] = order.metadata[key]
        result = result.model_copy(
            update={"raw_response": {**result.raw_response, **extra_attribution}}
        )

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

        record_order_submitted(
            exchange=order.symbol.exchange.value,
            symbol=str(order.symbol),
            side=order.side.value,
            trading_mode=self._trading_mode.value,
        )

        # Persistenz (create als PENDING + finales update_status) laeuft
        # jetzt VOLLSTAENDIG innerhalb von SafeOrderExecutor.execute_safely()
        # oben ab, nicht mehr hier (siehe order_safety.py Modul-Docstring
        # Punkt 5) - der PENDING-Record MUSS vor dem Exchange-Call
        # existieren, nicht erst danach, sonst hinterlaesst ein Crash
        # zwischen Exchange-Call und diesem Punkt gar keinen DB-Record.

        # Falls sofort filled (Market Order, Paper Mode)
        if result.status == OrderStatus.FILLED:
            await self._on_fill(result, side=order.side.value)
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
                # adapter.get_order() liefert raw_response direkt von der
                # Exchange (die kein "strategy"-Feld kennt) - der Tag aus
                # execute()/_execute_internal() ginge sonst bei jedem Poll
                # wieder verloren (siehe dortiger Kommentar).
                poll_attribution: dict[str, Any] = {
                    "strategy": order.metadata.get("strategy", "unknown")
                }
                for key in ("exit_reason", "target_price", "stop_price", "entry_regime"):
                    if key in order.metadata:
                        poll_attribution[key] = order.metadata[key]
                current = current.model_copy(
                    update={"raw_response": {**current.raw_response, **poll_attribution}}
                )
            except ExchangeError as e:
                log.warning(
                    "execution_engine.fill_poll_error",
                    order_id=str(order.id),
                    error=str(e),
                )
                continue
            finally:
                # Tracking bei jedem Poll aktuell halten (Baustein 7),
                # damit shutdown() jederzeit den zuletzt bekannten
                # Order-Status/-ID sieht.
                self._safety.update_inflight(order, current)

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
            await self._on_fill(current, side=order.side.value)

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

    async def _on_fill(self, result: OrderResult, side: str) -> None:
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

        latency_seconds = max(
            0.0, (datetime.now(tz=UTC) - result.submitted_at).total_seconds()
        )
        record_order_filled(
            exchange=result.symbol.exchange.value,
            symbol=str(result.symbol),
            side=side,
            trading_mode=result.trading_mode.value,
            latency_seconds=latency_seconds,
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

    async def shutdown(self) -> None:
        """
        Shutdown Safety (Baustein 7): best-effort Cancel aller Orders, die
        gerade im Fill-Monitoring aktiv sind, bevor die Exchange-Verbindung
        geschlossen wird. Muss VOR dem Schliessen der Exchange-Adapter in
        api/main.py lifespan aufgerufen werden.

        Best-effort: ein Fehler bei einem einzelnen Cancel darf die
        anderen nicht verhindern und darf den Shutdown-Prozess nicht
        blockieren (fail-safe, analog zu allen anderen Persistenz-/
        Cleanup-Pfaden in dieser Engine).
        """
        inflight = list(self._safety.all_inflight().items())
        if not inflight:
            log.info("execution_engine.shutdown_no_inflight_orders")
            return

        log.warning(
            "execution_engine.shutdown_cancelling_inflight_orders",
            count=len(inflight),
            order_ids=[oid for oid, _ in inflight],
        )

        for order_id, result in inflight:
            if result.status in (
                OrderStatus.FILLED,
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
            ):
                continue
            if not result.exchange_order_id:
                # execute_safely()-Placeholder: place_order() ist noch
                # nicht zurueckgekehrt, es existiert noch keine
                # Exchange-seitige Order, die gecancelt werden koennte.
                log.warning(
                    "execution_engine.shutdown_skips_unsubmitted_order",
                    order_id=order_id,
                )
                continue
            try:
                adapter = self._pool.get(result.symbol.exchange, self._trading_mode)
                await adapter.cancel_order(
                    result.exchange_order_id,
                    result.symbol.ccxt_symbol,
                )
                log.info(
                    "execution_engine.shutdown_order_cancelled",
                    order_id=order_id,
                    exchange_order_id=result.exchange_order_id,
                )
            except Exception as e:
                log.error(
                    "execution_engine.shutdown_cancel_failed",
                    order_id=order_id,
                    exchange_order_id=result.exchange_order_id,
                    error=str(e),
                )

        self._safety.clear()

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
