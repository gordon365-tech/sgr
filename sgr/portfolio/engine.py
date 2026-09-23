"""
SGR Portfolio Engine
====================
Echtzeit-Positionsmanagement und PnL-Berechnung.

Verantwortlichkeiten:
    1. Positionen tracken (öffnen, aktualisieren, schließen)
    2. PnL berechnen (unrealized + realized, per Position + gesamt)
    3. Closed Trades als immutable Records speichern (Audit + Fees)
    4. Portfolio-Wert berechnen (Cash + offene Positionen)
    5. Auf OrderFilledEvent reagieren (öffnet/schließt Positionen)
    6. Auf KillSwitchEvent reagieren (schließt alle Positionen)

State-Management:
    - Primär in-memory (Redis Backup für Crash-Recovery)
    - Bei Startup: Reconciliation mit Exchange-State
      (DB vs. Exchange → Abweichungen werden geloggt)
    - Paper und Live: komplett getrennte State-Instanzen

PnL-Berechnung:
    Unrealized PnL: (current_price - entry_price) * qty * side_factor
    Realized PnL:   (exit_price - entry_price) * qty * side_factor - fees
    Net PnL:        Realized PnL - Fees

Portfolio Value:
    Cash (USDT) + Σ(Position Notional Value) + Unrealized PnL
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sgr.core.logging import get_logger
from sgr.core.types import (
    ExitReason,
    OrderResult,
    OrderStatus,
    Position,
    PositionSide,
    Side,
    Symbol,
    TradingMode,
)
from sgr.monitoring.metrics import record_trade_executed

log = get_logger(__name__)


class PortfolioState:
    """
    In-Memory Portfolio State.
    Immutable nach außen – nur Portfolio Engine mutiert intern.
    """

    def __init__(
        self,
        trading_mode: TradingMode,
        initial_cash: Decimal = Decimal("10000"),
    ) -> None:
        self.trading_mode = trading_mode
        self._cash: Decimal = initial_cash
        self._positions: dict[str, Position] = {}  # key: symbol str
        self._peak_value: Decimal = initial_cash

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def positions(self) -> list[Position]:
        return list(self._positions.values())

    @property
    def portfolio_value(self) -> Decimal:
        """
        Cash + Marktwert offener Positionen.

        LONG: der Marktwert ist ein Aktivum (du haeltst quantity Einheiten)
        - wird addiert. SHORT: der Marktwert ist eine Verbindlichkeit (du
        musst quantity Einheiten zum aktuellen Preis zurueckkaufen, um die
        Position zu schliessen) - wird abgezogen. Symmetrisch zum
        Cash-Fix in PortfolioEngine._open_position()/_update_position():
        mit dem dort beim Open bereits gutgeschriebenen Verkaufserloes
        (Cash steigt) muss der noch offene Rueckkaufbedarf hier als Minus
        gefuehrt werden, sonst wuerde eine offene Short-Position
        faelschlich doppelt als Vermoegen gezaehlt (Cash-Gutschrift UND
        positiver Positionswert gleichzeitig) - siehe analoger Fix in
        sgr/backtesting/simulator.py::_compute_portfolio_value
        (Commit 1584e08).
        """
        position_value = sum(
            p.notional_value if p.side == PositionSide.LONG else -p.notional_value
            for p in self._positions.values()
        )
        return self._cash + position_value

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum(p.unrealized_pnl for p in self._positions.values())

    @property
    def peak_value(self) -> Decimal:
        return self._peak_value

    def update_peak(self) -> None:
        v = self.portfolio_value
        if v > self._peak_value:
            self._peak_value = v


class PortfolioEngine:
    """
    Portfolio Engine – verwaltet Positionen und PnL.

    Subscribed auf OrderFilledEvent vom Event Bus.
    Publiziert keine Events selbst (pull-basiert via REST API).
    """

    def __init__(
        self,
        trading_mode: TradingMode,
        initial_cash: Decimal = Decimal("10000"),
        position_repository: Any = None,
        tenant_id: str | None = None,
        trade_repository: Any = None,
        on_position_opened: Any = None,
        on_position_closed: Any = None,
        live_verification_gate: Any = None,
    ) -> None:
        self._trading_mode = trading_mode
        # LiveVerificationGate (optional, additiv - siehe set_live_
        # verification_gate() fuer die post-construction Injection, gleiche
        # Begruendung wie set_protection_hooks() unten). None (Default) ist
        # fuer JEDEN bestehenden Aufrufer/Test unveraendertes Verhalten -
        # record_realized_loss() wird nur aufgerufen, wenn ein Gate
        # tatsaechlich injiziert wurde UND die Order LIVE war.
        self._live_verification_gate: Any = live_verification_gate
        self._state = PortfolioState(trading_mode, initial_cash)
        self._trade_history: list[dict] = []  # Closed trades
        # Optional: PositionRepository fuer Crash-Recovery und Phase 7B
        # Reconciliation. None = rein in-memory (Tests, Backtesting).
        self._position_repo: Any = position_repository
        # Optional: TradeRepository fuer persistente Trade-Historie (siehe
        # sgr/core/repositories.py::TradeRepository Modul-Docstring - vorher
        # nur In-Memory self._trade_history, verloren bei jedem Neustart).
        # None = unveraendertes Verhalten (Tests, Backtesting).
        self._trade_repo: Any = trade_repository
        # Position-Protection-Hooks (additiv, siehe sgr/risk/
        # position_protection.py::PositionProtectionManager - das ist der
        # einzige vorgesehene Aufrufer). None = unveraendertes Verhalten.
        #
        # on_position_opened(position: Position, order_metadata: dict | None)
        #   -> Position | None:
        #   wird direkt (kein Event Bus - siehe Docstring-Hinweis unten)
        #   aufgerufen, NACHDEM eine neue Position gespeichert/persistiert
        #   wurde. order_metadata ist result.raw_response der ausloesenden
        #   Order (seit 2026-09-23, optional target_price/stop_price - siehe
        #   ExecutionEngine.execute() Kommentar) - additiv, alle bisherigen
        #   1-Argument-Aufrufer/Tests bleiben gueltig. Ein Rueckgabewert
        #   != None ERSETZT die gespeicherte Position (z.B. mit gesetzten
        #   stop_loss_price/take_profit_price/max_holding_until/
        #   sl_order_id/tp_order_id) und wird erneut persistiert -
        #   PortfolioEngine bleibt dadurch alleiniger Schreibpunkt fuer
        #   _state._positions/DB, statt dass der Hook selbst mutiert.
        #
        # on_position_closed(position: Position, close_reason: str) -> None:
        #   wird aufgerufen, NACHDEM eine Position VOLLSTAENDIG geschlossen
        #   (nicht teilweise reduziert) und persistiert wurde - fuer OCO-
        #   Cleanup (verwaisten SL- oder TP-Order stornieren).
        self._on_position_opened = on_position_opened
        self._on_position_closed = on_position_closed
        # Phantom-Fill-Guard (Design-Review-Befund, siehe Modul-Docstring
        # von position_protection.py): wenn SL und TP nahezu gleichzeitig
        # triggern, schliesst der erste Fill die Position bereits
        # vollstaendig; faengt der zweite (Sibling-)Fill danach noch ein,
        # waere er ohne diesen Guard faelschlich eine NEUE Position (da
        # _state._positions fuer dieses Symbol bereits leer ist). IDs
        # werden beim vollstaendigen Schliessen einer geschuetzten Position
        # hier eingetragen und beim ersten passenden Fill wieder entfernt -
        # bewusst kein TTL/Ablauf, da hoechstens 2 Eintraege pro Position-
        # Close entstehen und sie beim erwarteten Sibling-Fill sofort
        # wieder verschwinden.
        self._recently_closed_protective_order_ids: set[str] = set()
        # Multi-Tenant-Isolation (Audit nach Commit 5/6): OHNE tenant_id
        # schrieb _persist_position_upsert() jede Position mit
        # user_id=NULL in die DB, UND restore_from_persistence() las beim
        # naechsten Worker-Neustart ausnahmslos ALLE offenen Positionen
        # ueber ALLE Tenants (get_open_positions() ohne user_id-Filter) in
        # den in-memory State DIESES Prozesses ein - bei getrennten
        # Worker-Prozessen pro Tenant (Gordon/Sumo, siehe Commit 5) haette
        # das Gordons Positionen faelschlich in Sumos Portfolio geladen
        # (und umgekehrt), sobald beide gleichzeitig offene Positionen
        # haben und einer der beiden Worker neu startet. Die zugehoerigen
        # DB-Row-Level-Security-Policies (user_isolation_positions, siehe
        # init-db.sql) greifen dabei NICHT als Sicherheitsnetz: die App
        # verbindet sich als Tabelleneigentuemer "sgr" ohne
        # FORCE ROW LEVEL SECURITY und setzt an keiner Stelle
        # app.current_user_id/app.is_admin - RLS ist aktuell rein
        # dekorativ (siehe Go-Live-Report). tenant_id=None erhaelt das
        # bisherige Single-Tenant-Verhalten (kein Filter) unveraendert.
        self._tenant_id = tenant_id
        # Entry-Fee pro offener Position (Symbol -> noch nicht durch einen
        # Close verrechnete Fee-Anteile). realized_pnl/net_pnl wurde vorher
        # ausschliesslich mit der Exit-Fee berechnet (result.fees beim
        # Close), die Entry-Fee (beim Open bereits vom Cash abgezogen bzw.
        # beim Short-Open gutgeschrieben) floss nie in net_pnl ein - jeder
        # Trade wurde dadurch um die Entry-Fee zu gut ausgewiesen. Bei
        # Teilschliessungen wird der verbleibende Anteil proportional
        # weitergefuehrt; beim vollstaendigen Close wird der Eintrag
        # entfernt.
        self._entry_fees: dict[str, Decimal] = {}

    def set_protection_hooks(
        self,
        on_position_opened: Any = None,
        on_position_closed: Any = None,
    ) -> None:
        """
        Post-Construction-Injection fuer die Position-Protection-Hooks
        (siehe __init__ Docstring) - analog zum bestehenden
        RiskEngine.inject_redis()-Muster in sgr/api/main.py. Notwendig,
        weil PositionProtectionManager/-Watchdog eine bereits
        konstruierte ExecutionEngine brauchen, PortfolioEngine in
        lifespan() aber VOR der ExecutionEngine konstruiert wird
        (zirkulaere Konstruktions-Reihenfolge ohne diesen Setter).
        """
        self._on_position_opened = on_position_opened
        self._on_position_closed = on_position_closed

    def set_live_verification_gate(self, gate: Any) -> None:
        """
        Post-Construction-Injection fuer das LiveVerificationGate (siehe
        __init__ Docstring) - identisches Konstruktions-Reihenfolge-
        Problem wie set_protection_hooks(): das Gate (falls vom Operator
        ueberhaupt konfiguriert) existiert typischerweise erst nach der
        ExecutionEngine, PortfolioEngine aber vorher. gate=None ist
        explizit erlaubt (deaktiviert die Buchung wieder), nicht nur ein
        impliziter Default.
        """
        self._live_verification_gate = gate

    # ------------------------------------------------------------------
    # Event Handlers
    # ------------------------------------------------------------------

    async def on_order_filled(self, result: OrderResult) -> None:
        """
        Wird bei jedem gefüllten Order aufgerufen.
        Öffnet neue Position oder schließt/reduziert bestehende.
        """
        if result.status != OrderStatus.FILLED:
            return
        if result.average_fill_price is None or result.filled_quantity <= 0:
            log.warning(
                "portfolio.invalid_fill",
                order_id=str(result.request_id),
            )
            return

        symbol_key = str(result.symbol)

        # Ist eine Position für dieses Symbol bereits offen?
        existing = self._state._positions.get(symbol_key)

        if existing is None:
            # Phantom-Fill-Guard (siehe __init__ Docstring): dieser Fill
            # koennte der verspaetete Sibling (SL oder TP) einer bereits
            # ueber den anderen Order geschlossenen Position sein - dann
            # KEINE neue Position eroeffnen, sondern verwerfen.
            if result.exchange_order_id in self._recently_closed_protective_order_ids:
                self._recently_closed_protective_order_ids.discard(result.exchange_order_id)
                log.warning(
                    "portfolio.phantom_sibling_fill_dropped",
                    symbol=symbol_key,
                    exchange_order_id=result.exchange_order_id,
                )
                return
            # Neue Position öffnen
            await self._open_position(result, symbol_key)
        else:
            # Bestehende Position anpassen (reduce/close/flip)
            await self._update_position(existing, result, symbol_key)

        self._state.update_peak()

        log.info(
            "portfolio.position_updated",
            symbol=symbol_key,
            portfolio_value=str(self._state.portfolio_value),
            open_positions=len(self._state.positions),
        )

    async def _open_position(self, result: OrderResult, symbol_key: str) -> None:
        """Öffnet neue Position nach Fill."""
        side = PositionSide.LONG if self._infer_side(result) == Side.BUY else PositionSide.SHORT

        position = Position(
            symbol=result.symbol,
            side=side,
            quantity=result.filled_quantity,
            entry_price=result.average_fill_price,  # type: ignore[arg-type]
            current_price=result.average_fill_price,  # type: ignore[arg-type]
            opened_at=datetime.now(tz=UTC),
            strategy_name=str(result.raw_response.get("strategy", "unknown")),
            trading_mode=self._trading_mode,
        )

        self._state._positions[symbol_key] = position

        # Cash-Buchung: LONG zahlt das Notional (Kauf) - SHORT erhaelt das
        # Notional (abzueglich Fee) als Verkaufserloes gutgeschrieben
        # (verkaufen zuerst, zurueckkaufen beim Close). Vorher wurde hier
        # fuer beide Seiten identisch abgebucht (cash -= notional + fees),
        # was fuer Short-Positionen wirtschaftlich falsch war - siehe
        # analoger, bereits behobener Bug in
        # sgr/backtesting/simulator.py::_open_position (Commit 1584e08,
        # docs/ANALYSIS-mean-reversion-v1-schritt16-fundamental-suitability.md).
        notional = result.filled_quantity * result.average_fill_price  # type: ignore[operator]
        if side == PositionSide.LONG:
            self._state._cash -= notional + result.fees
        else:
            self._state._cash += notional - result.fees
        self._entry_fees[symbol_key] = result.fees

        await self._persist_position_upsert(position)

        log.info(
            "portfolio.position_opened",
            symbol=symbol_key,
            side=side.value,
            qty=str(result.filled_quantity),
            price=str(result.average_fill_price),
            fees=str(result.fees),
        )

        # Position-Protection-Hook (siehe __init__ Docstring): darf die
        # gespeicherte Position um SL/TP/Max-Holding-Felder ergaenzen.
        # Fail-safe wie jeder andere optionale Hook in dieser Klasse - ein
        # Fehler hier darf die bereits erfolgreich eroeffnete/persistierte
        # Position nicht rueckgaengig machen, nur die Protection-Anreicherung
        # entfaellt fuer diese eine Position.
        if self._on_position_opened is not None:
            try:
                # result.raw_response traegt seit 2026-09-23 optional
                # target_price/stop_price (siehe ExecutionEngine.execute()
                # Kommentar) - PositionProtectionManager.on_position_opened()
                # nutzt sie, um strategiegetriebene Exit-Preise statt des
                # globalen Flat-%-Fallbacks zu setzen, sofern vorhanden.
                updated = await self._on_position_opened(position, result.raw_response)
                if updated is not None:
                    self._state._positions[symbol_key] = updated
                    await self._persist_position_upsert(updated)
            except Exception as e:
                log.error(
                    "portfolio.on_position_opened_hook_failed",
                    symbol=symbol_key,
                    error=str(e),
                    exc_info=True,
                )

    async def _update_position(
        self,
        existing: Position,
        result: OrderResult,
        symbol_key: str,
    ) -> None:
        """Aktualisiert oder schließt bestehende Position."""
        fill_side = self._infer_side(result)
        is_closing = (existing.side == PositionSide.LONG and fill_side == Side.SELL) or (
            existing.side == PositionSide.SHORT and fill_side == Side.BUY
        )

        if is_closing:
            # Position schließen / reduzieren
            fill_qty = result.filled_quantity
            close_qty = min(fill_qty, existing.quantity)

            # Realized PnL berechnen (Entry-Fee anteilig + Exit-Fee, siehe
            # self._entry_fees Docstring in __init__ - sonst waere jeder
            # Trade um die Entry-Fee zu gut ausgewiesen).
            side_factor = Decimal("1") if existing.side == PositionSide.LONG else Decimal("-1")
            entry = existing.entry_price
            exit_price = result.average_fill_price  # type: ignore[assignment]
            remaining_entry_fee = self._entry_fees.get(symbol_key, Decimal(0))
            entry_fee_share = (
                remaining_entry_fee * (close_qty / existing.quantity)
                if existing.quantity > 0
                else Decimal(0)
            )
            realized = (
                (exit_price - entry) * close_qty * side_factor - result.fees - entry_fee_share
            )
            total_fees = result.fees + entry_fee_share

            # LiveVerificationGate-Buchung (Live-Verification-Anweisung,
            # Abschnitt 5 "Realized Loss Accounting"): GENAU EINMAL pro
            # tatsaechlich schliessendem Fill, exakt hier wo realized
            # bereits final berechnet ist - kein zweiter Aufrufer bucht
            # denselben Verlust nochmal (siehe grid_controller.py fuer den
            # analogen Grid-Fall, ein getrenntes Gate-Objekt pro
            # Verifikationslauf wird ohnehin nie gleichzeitig fuer beide
            # Pfade verwendet). Nur LIVE, nur echte Gewinne/Verluste (die
            # Gate-Methode selbst ignoriert bereits Nicht-Verluste, siehe
            # dortigen Docstring) - fuer PAPER (self._live_verification_gate
            # ist in Produktion aktuell IMMER None, siehe main.py) folgenlos.
            if self._live_verification_gate is not None and result.trading_mode == (
                TradingMode.LIVE
            ):
                try:
                    self._live_verification_gate.record_realized_loss(-realized)
                except Exception as e:
                    log.error(
                        "portfolio.live_verification_loss_recording_failed",
                        symbol=symbol_key,
                        error=str(e),
                    )

            # Exit-Grund (siehe ExitReason, sgr/core/types.py): von
            # ExecutionEngine aus order.metadata["exit_reason"] in
            # result.raw_response uebertragen (analog zum bestehenden
            # "strategy"-Muster dort). Fehlt der Key (normaler Exit durch
            # ein gegenlaeufiges Strategie-Signal via Orchestrator), ist
            # STRATEGY_SIGNAL der korrekte Default.
            close_reason = str(
                result.raw_response.get("exit_reason", ExitReason.STRATEGY_SIGNAL.value)
            )

            # Trade Record speichern
            await self._record_trade(
                existing, result, close_qty, realized, total_fees, close_reason
            )

            if close_qty >= existing.quantity:
                # Vollständig geschlossen
                del self._state._positions[symbol_key]
                self._entry_fees.pop(symbol_key, None)
                # Cash-Buchung symmetrisch zu _open_position(): LONG
                # erhaelt beim Verkauf den Exit-Erloes zurueck (Cash
                # steigt); SHORT muss zum Exit-Preis zurueckkaufen, um die
                # beim Open erhaltenen Verkaufserloese abzuloesen (Cash
                # sinkt).
                if existing.side == PositionSide.LONG:
                    self._state._cash += exit_price * close_qty - result.fees
                else:
                    self._state._cash -= exit_price * close_qty + result.fees

                await self._persist_position_close(
                    existing.id, existing.realized_pnl + realized, close_reason
                )

                log.info(
                    "portfolio.position_closed",
                    symbol=symbol_key,
                    realized_pnl=str(realized),
                    fees=str(result.fees),
                    close_reason=close_reason,
                )

                # Phantom-Fill-Guard (siehe __init__ Docstring): der
                # Sibling-Order (falls vorhanden) koennte server-seitig
                # bereits/gleichzeitig gefuellt worden sein, bevor der
                # OCO-Cancel unten greift.
                for protective_id in (existing.sl_order_id, existing.tp_order_id):
                    if protective_id:
                        self._recently_closed_protective_order_ids.add(protective_id)

                # Position-Protection-Hook: OCO-Cleanup (verwaisten
                # Sibling-Order stornieren). Fail-safe wie jeder andere
                # optionale Hook - ein Fehler hier darf den bereits
                # abgeschlossenen Close nicht rueckgaengig machen.
                if self._on_position_closed is not None:
                    try:
                        await self._on_position_closed(existing, close_reason)
                    except Exception as e:
                        log.error(
                            "portfolio.on_position_closed_hook_failed",
                            symbol=symbol_key,
                            error=str(e),
                            exc_info=True,
                        )
            else:
                # Teilweise geschlossen
                remaining_qty = existing.quantity - close_qty
                self._entry_fees[symbol_key] = remaining_entry_fee - entry_fee_share
                updated = Position(
                    id=existing.id,
                    symbol=existing.symbol,
                    side=existing.side,
                    quantity=remaining_qty,
                    entry_price=existing.entry_price,
                    current_price=exit_price,
                    opened_at=existing.opened_at,
                    strategy_name=existing.strategy_name,
                    trading_mode=existing.trading_mode,
                    realized_pnl=existing.realized_pnl + realized,
                    # Protection-Felder unveraendert weiterfuehren (siehe
                    # Position-Docstring in sgr/core/types.py) - sonst
                    # gingen SL/TP/Max-Holding-Schutz bei einer TEIL-
                    # Schliessung verloren, obwohl die Position weiter
                    # offen bleibt.
                    leverage=existing.leverage,
                    stop_loss_price=existing.stop_loss_price,
                    take_profit_price=existing.take_profit_price,
                    max_holding_until=existing.max_holding_until,
                    sl_order_id=existing.sl_order_id,
                    tp_order_id=existing.tp_order_id,
                    entry_regime=existing.entry_regime,
                )
                self._state._positions[symbol_key] = updated
                if existing.side == PositionSide.LONG:
                    self._state._cash += exit_price * close_qty - result.fees
                else:
                    self._state._cash -= exit_price * close_qty + result.fees

                await self._persist_position_upsert(updated)

    async def _record_trade(
        self,
        position: Position,
        close_result: OrderResult,
        qty: Decimal,
        realized_pnl: Decimal,
        total_fees: Decimal,
        close_reason: str = "",
    ) -> None:
        """Speichert geschlossenen Trade als immutable Record.

        total_fees = Entry-Fee-Anteil + Exit-Fee (siehe _entry_fees
        Docstring in __init__) - realized_pnl hat beide bereits abgezogen.
        """
        closed_at = datetime.now(tz=UTC)
        self._trade_history.append(
            {
                "id": str(uuid4()),
                "symbol": str(position.symbol),
                "side": position.side.value,
                "entry_price": str(position.entry_price),
                "exit_price": str(close_result.average_fill_price),
                "quantity": str(qty),
                "realized_pnl": str(realized_pnl),
                "fees": str(total_fees),
                "net_pnl": str(realized_pnl),
                "strategy": position.strategy_name,
                "opened_at": position.opened_at.isoformat(),
                "closed_at": closed_at.isoformat(),
                "trading_mode": self._trading_mode.value,
                "close_reason": close_reason,
            }
        )

        # Persistenz in die `trades`-Tabelle (Root-Cause-Fix, siehe
        # sgr/core/repositories.py::TradeRepository Modul-Docstring -
        # vorher landeten geschlossene Trades ausschliesslich in
        # self._trade_history oben, verloren bei jedem Neustart).
        # Best-effort/fail-safe: ein DB-Fehler hier darf das bereits
        # erfolgte Schliessen der Position nicht rueckgaengig machen,
        # gleiches Muster wie _persist_position_upsert()/_persist_position_close().
        if self._trade_repo is not None:
            holding_seconds = int((closed_at - position.opened_at).total_seconds())
            await self._persist_trade(
                position=position,
                close_result=close_result,
                qty=qty,
                realized_pnl=realized_pnl,
                total_fees=total_fees,
                close_reason=close_reason,
                holding_seconds=holding_seconds,
                closed_at=closed_at,
            )

        # Grafana-Observability-Audit: sgr_trades_executed_total/
        # sgr_trades_winning_total/sgr_trades_losing_total (metrics.py)
        # waren vor diesem Fix definiert, aber an KEINER Stelle im Code
        # jemals inkrementiert worden - der natuerliche Aufrufpunkt ist
        # hier, wo realized_pnl fuer einen geschlossenen Trade bereits
        # feststeht. Rein additiv/lesend fuer den Trading-Ablauf - ein
        # Fehler hier darf das Schliessen der Position niemals verhindern
        # (Fail-Safe-Prinzip wie ueberall sonst in sgr/monitoring/).
        try:
            cumulative = sum(
                (Decimal(t["realized_pnl"]) for t in self._trade_history), Decimal(0)
            )
            record_trade_executed(
                side=position.side.value,
                pnl=realized_pnl,
                winning=realized_pnl > 0,
                cumulative_realized_pnl=cumulative,
                exit_reason=close_reason or None,
            )
        except Exception as e:
            log.warning("portfolio.trade_metric_record_failed", error=str(e))

    async def _persist_trade(
        self,
        position: Position,
        close_result: OrderResult,
        qty: Decimal,
        realized_pnl: Decimal,
        total_fees: Decimal,
        close_reason: str,
        holding_seconds: int,
        closed_at: datetime,
    ) -> None:
        """Best-effort Persistenz eines geschlossenen Trades. Fail-safe wie
        _persist_position_upsert()/_persist_position_close(): ein DB-Fehler
        wird geloggt, aber niemals propagiert - das bereits erfolgte
        Schliessen der Position darf davon nicht rueckgaengig gemacht
        werden."""
        try:
            await self._trade_repo.create(
                {
                    "position_id": str(position.id),
                    "symbol": position.symbol.ccxt_symbol,
                    "exchange": position.symbol.exchange.value,
                    "side": position.side.value,
                    "entry_price": position.entry_price,
                    "exit_price": close_result.average_fill_price,
                    "quantity": qty,
                    "realized_pnl": realized_pnl,
                    "fees_total": total_fees,
                    "net_pnl": realized_pnl,
                    "holding_seconds": holding_seconds,
                    "strategy_name": position.strategy_name,
                    "regime": "unknown",
                    "trading_mode": position.trading_mode.value,
                    "opened_at": position.opened_at,
                    "closed_at": closed_at,
                    "trade_metadata": {"exit_reason": close_reason},
                    "user_id": self._tenant_id,
                }
            )
        except Exception as e:
            log.error(
                "portfolio.persist_trade_failed",
                symbol=str(position.symbol),
                error=str(e),
            )

    # ------------------------------------------------------------------
    # Price Updates
    # ------------------------------------------------------------------

    async def update_prices(self, prices: dict[str, Decimal]) -> None:
        """
        Aktualisiert aktuelle Preise für alle Positionen.
        Berechnet Unrealized PnL neu.

        Wird von TradingOrchestrator.on_candle_event() für JEDEN
        eingehenden Candle aufgerufen (nicht nur wenn dieser Candle ein
        neues Signal erzeugt) - siehe orchestrator/engine.py. Ohne diesen
        Aufruf blieb current_price/unrealized_pnl einer offenen Position
        auf dem Stand ihrer Eroeffnung eingefroren, sobald die Strategie
        auf diesem Symbol kein neues Signal mehr erzeugte (der Normalfall
        fuer eine bereits offene Position) - Portfolio-Wert, Daily-PnL und
        die darauf basierenden Risk-Metriken (Drawdown, Portfolio-Heat)
        waren dadurch dauerhaft falsch, nicht nur verzoegert.

        Args:
            prices: {"BTC/USDT": Decimal("50000"), ...}
        """
        updated_positions: list[Position] = []
        for symbol_key, position in list(self._state._positions.items()):
            symbol_str = position.symbol.ccxt_symbol
            if symbol_str not in prices:
                continue

            new_price = prices[symbol_str]
            side_factor = Decimal("1") if position.side == PositionSide.LONG else Decimal("-1")
            unrealized = (new_price - position.entry_price) * position.quantity * side_factor

            updated = Position(
                id=position.id,
                symbol=position.symbol,
                side=position.side,
                quantity=position.quantity,
                entry_price=position.entry_price,
                current_price=new_price,
                leverage=position.leverage,
                unrealized_pnl=unrealized,
                realized_pnl=position.realized_pnl,
                opened_at=position.opened_at,
                strategy_name=position.strategy_name,
                trading_mode=position.trading_mode,
                # Protection-Felder unveraendert weiterfuehren (siehe
                # Position-Docstring in sgr/core/types.py) - Position wird
                # hier bei JEDEM Preis-Tick neu konstruiert; ohne diese
                # Zeilen wuerden SL/TP/Max-Holding-Schutz beim naechsten
                # Candle stillschweigend verloren gehen.
                stop_loss_price=position.stop_loss_price,
                take_profit_price=position.take_profit_price,
                max_holding_until=position.max_holding_until,
                sl_order_id=position.sl_order_id,
                tp_order_id=position.tp_order_id,
                entry_regime=position.entry_regime,
            )
            self._state._positions[symbol_key] = updated
            updated_positions.append(updated)

        self._state.update_peak()

        # Persistenz best-effort, gleiches Fail-Safe-Muster wie an anderen
        # _persist_position_upsert()-Aufrufstellen dieser Klasse: ein
        # DB-Fehler hier darf den Live-Preis-Stand im In-Memory-State
        # (bereits oben aktualisiert, u.a. Basis fuer die Prometheus-
        # Metriken) nicht rueckgaengig machen oder verzoegern.
        for position in updated_positions:
            await self._persist_position_upsert(position)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def portfolio_value(self) -> Decimal:
        return self._state.portfolio_value

    @property
    def cash(self) -> Decimal:
        return self._state.cash

    @property
    def positions(self) -> list[Position]:
        return self._state.positions

    @property
    def trade_history(self) -> list[dict]:
        return list(self._trade_history)

    def get_position(self, symbol: Symbol) -> Position | None:
        return self._state._positions.get(str(symbol))

    def summary(self) -> dict:
        """Portfolio Summary für Dashboard / API."""
        return {
            "portfolio_value": str(self._state.portfolio_value),
            "cash": str(self._state.cash),
            "unrealized_pnl": str(self._state.unrealized_pnl),
            "open_positions": len(self._state.positions),
            "total_trades": len(self._trade_history),
            "peak_value": str(self._state.peak_value),
            "drawdown": str(
                (self._state.peak_value - self._state.portfolio_value) / self._state.peak_value
                if self._state.peak_value > 0
                else Decimal("0")
            ),
            "trading_mode": self._trading_mode.value,
        }

    # ------------------------------------------------------------------
    # Persistence (Crash-Recovery, Phase 7B Reconciliation)
    # ------------------------------------------------------------------

    async def restore_from_persistence(self) -> int:
        """
        Laedt offene Positionen aus der DB in den in-memory State.
        Muss beim Startup aufgerufen werden, BEVOR Trading beginnt.

        Fail-Closed: Ein DB-Fehler wird NICHT geschluckt. Silent-Empty-Start
        waere gefaehrlicher als ein expliziter Crash, weil das System sonst
        mit leerem Portfolio-State startet, obwohl real offene Positionen
        existieren (doppeltes Hedging, falsche Risk-Berechnung, verwaiste
        Exchange-Positionen ohne lokale Kill-Switch-Kontrolle).

        Returns: Anzahl wiederhergestellter Positionen.

        Raises:
            RuntimeError: wenn kein PositionRepository injiziert wurde.
            Exception: jede DB-Exception wird weitergereicht (fail-closed).
        """
        if self._position_repo is None:
            raise RuntimeError(
                "restore_from_persistence() aufgerufen ohne injiziertes "
                "PositionRepository. Fail-closed: kein impliziter Empty-Start."
            )

        rows = await self._position_repo.get_open_positions(
            self._trading_mode, user_id=self._tenant_id
        )

        restored = 0
        for row in rows:
            position = self._position_from_row(row)
            symbol_key = str(position.symbol)
            self._state._positions[symbol_key] = position
            restored += 1

            # BUG (gefunden 2026-09-15, live beobachtet): diese Methode
            # restauriert Positionen, aber NIE self._state._cash - das
            # blieb beim vollen initial_cash, obwohl die wiederhergestellten
            # Positionen beim urspruenglichen Open bereits Kapital
            # gebunden (LONG) bzw. freigesetzt (SHORT) hatten. Nach jedem
            # Worker-Neustart mit offenen Positionen wurde portfolio_value
            # dadurch um etwa die Summe der Positions-Notionale zu hoch
            # ausgewiesen (live: $9996 -> $14014 bei 10 offenen Positionen
            # nach einem Neustart). Symmetrisch zur Cash-Buchung in
            # _open_position() nachgeholt: LONG zahlte das Entry-Notional
            # (Cash sinkt), SHORT erhielt es (Cash steigt).
            #
            # Bekannte Einschraenkung: die Entry-Fee wird NICHT
            # nachgebucht - sie ist nicht Teil des positions-Tabellen-
            # schemas (nur symbol/side/quantity/entry_price/... siehe
            # PositionModel), es gibt keine persistierte Quelle dafuer.
            # Der verbleibende Fehler ist dadurch auf die Groessenordnung
            # der Fee begrenzt (~0.1% des Notionals pro Position), nicht
            # mehr auf das volle Notional wie zuvor.
            notional = position.quantity * position.entry_price
            if position.side == PositionSide.LONG:
                self._state._cash -= notional
            else:
                self._state._cash += notional

        self._state.update_peak()

        log.info(
            "portfolio.restored_from_persistence",
            trading_mode=self._trading_mode.value,
            restored_positions=restored,
        )
        return restored

    @staticmethod
    def _position_from_row(row: dict[str, Any]) -> Position:
        """Rekonstruiert Position (Domain) aus PositionRepository-Row (dict)."""
        from sgr.core.types import ExchangeID, MarketRegime

        base, _, quote = row["symbol"].partition("/")
        symbol = Symbol(base=base, quote=quote, exchange=ExchangeID(row["exchange"]))

        raw_entry_regime = row.get("entry_regime")
        entry_regime = MarketRegime(raw_entry_regime) if raw_entry_regime else None

        return Position(
            id=row["id"],
            symbol=symbol,
            side=PositionSide(row["side"]),
            quantity=row["quantity"],
            entry_price=row["entry_price"],
            current_price=row["current_price"],
            leverage=row["leverage"],
            unrealized_pnl=row["unrealized_pnl"],
            realized_pnl=row["realized_pnl"],
            opened_at=row["opened_at"],
            strategy_name=row["strategy_name"],
            trading_mode=TradingMode(row["trading_mode"]),
            stop_loss_price=row.get("stop_loss_price"),
            take_profit_price=row.get("take_profit_price"),
            max_holding_until=row.get("max_holding_until"),
            sl_order_id=row.get("sl_order_id"),
            tp_order_id=row.get("tp_order_id"),
            entry_regime=entry_regime,
        )

    async def _persist_position_upsert(self, position: Position) -> None:
        """
        Schreibt eine offene/aktualisierte Position in die DB.
        Best-effort: DB-Fehler dürfen den Trading-Betrieb NICHT blockieren
        (gleiches Fail-Safe-Muster wie KillSwitch._cancel_all_orders --
        Persistenz-Fehler werden geloggt, nicht propagiert).
        """
        if self._position_repo is None:
            return
        try:
            await self._position_repo.upsert_open(
                {
                    "id": str(position.id),
                    "symbol": position.symbol.ccxt_symbol,
                    "exchange": position.symbol.exchange.value,
                    "side": position.side.value,
                    "quantity": position.quantity,
                    "entry_price": position.entry_price,
                    "current_price": position.current_price,
                    "leverage": position.leverage,
                    "unrealized_pnl": position.unrealized_pnl,
                    "realized_pnl": position.realized_pnl,
                    "opened_at": position.opened_at,
                    "strategy_name": position.strategy_name,
                    "trading_mode": position.trading_mode.value,
                    "user_id": self._tenant_id,
                    "stop_loss_price": position.stop_loss_price,
                    "take_profit_price": position.take_profit_price,
                    "max_holding_until": position.max_holding_until,
                    "sl_order_id": position.sl_order_id,
                    "tp_order_id": position.tp_order_id,
                    "entry_regime": (
                        position.entry_regime.value if position.entry_regime else None
                    ),
                }
            )
        except Exception as e:
            log.error(
                "portfolio.persist_position_failed",
                symbol=str(position.symbol),
                error=str(e),
            )

    async def _persist_position_close(
        self, position_id: Any, realized_pnl: Decimal, close_reason: str | None = None
    ) -> None:
        """Markiert eine Position in der DB als geschlossen. Best-effort."""
        if self._position_repo is None:
            return
        try:
            await self._position_repo.close(
                position_id=str(position_id),
                closed_at=datetime.now(tz=UTC),
                realized_pnl=realized_pnl,
                close_reason=close_reason,
            )
        except Exception as e:
            log.error(
                "portfolio.persist_close_failed",
                position_id=str(position_id),
                error=str(e),
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _infer_side(self, result: OrderResult) -> Side:
        """Inferiert Side aus Raw Response (CCXT liefert 'buy'/'sell')."""
        raw_side = result.raw_response.get("side", "")
        if isinstance(raw_side, str) and raw_side.lower() == "sell":
            return Side.SELL
        return Side.BUY
