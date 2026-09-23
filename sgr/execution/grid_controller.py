"""
SGR Futures Grid Controller
==============================
Orchestriert den vollstaendigen Lifecycle einer Futures-Grid-Instanz:
erstellen, Level-Orders erzeugen, Fills verarbeiten, Level neu
auffuellen (Rebalancing), Funding/Stop-Loss/Take-Profit/Max-Holding-Time/
Liquidationsrisiko ueberwachen, schliessen.

KEINE STRATEGIE UMGEHT DIE RISIKOSCHICHT (siehe Aufgabenstellung):
    open_grid() prueft IMMER, in dieser Reihenfolge:
        1. Exchange/Produkt-Capability (sgr.exchanges.capabilities)
        2. Compliance/Jurisdiktion/Account-Eligibility (sgr.compliance)
        3. GridRiskEngine (sgr.risk.grid_risk)
    Erst danach werden ueberhaupt Order-Level generiert.

KEINE FAKE-SHORTCUT-EXECUTION:
    Jede tatsaechliche Order (Level-Open, Level-Close, Notfall-Exit)
    laeuft durch DIESELBE sgr.execution.engine.ExecutionEngine.execute()
    wie jede andere SGR-Order - inklusive Preflight, Kill-Switch, Order
    Safety/Idempotency, Quantization. Der Controller selbst platziert
    NIEMALS eine Order direkt auf einem Exchange-Adapter.

PAPER-Trading-Besonderheit (siehe sgr/exchanges/ccxt_base.py
_simulate_order() Docstring: JEDE simulierte Order fuellt sofort zum
aktuellen Marktpreis, unabhaengig vom Order-Typ - es gibt keine
"ruhenden" Limit-Orders in der Simulation). Ein Futures Grid besteht
aber gerade aus ruhenden Limit-Orders auf mehreren Preis-Leveln. Dieser
Controller loest das, OHNE _simulate_order() selbst zu veraendern (Null
Regressionsrisiko fuer alle bestehenden, direktionalen Strategien):
er haelt den Grid-Zustand (welche Level sind "gefuellt") selbst und
entscheidet bei jedem Preis-Tick (on_price_tick()), ob ein Level JETZT
ausgeloest werden soll - erst dann wird eine (Markt-)Order tatsaechlich
durch ExecutionEngine geschickt. Fuer LIVE Trading (siehe
sgr/exchanges/pionex.py: aktuell nicht implementiert) waere der gleiche
Controller-Code unveraendert nutzbar, nur dass echte ruhende
Limit-Orders auf der Exchange platziert und ueber get_open_orders()
ueberwacht wuerden - dieser MVP fokussiert auf den PAPER-Pfad, siehe
"offene Punkte" im Strategiebericht.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sgr.compliance.engine import ComplianceEngine, get_compliance_engine
from sgr.compliance.types import AccountEligibility, ComplianceCheckResult
from sgr.core.grid_types import (
    FuturesGridParameters,
    GridDecision,
    GridLevelState,
    GridRiskAssessment,
    GridState,
)
from sgr.core.logging import get_logger
from sgr.core.types import (
    ExchangeID,
    GridDirection,
    GridSpacingMode,
    GridStatus,
    OrderRequest,
    OrderStatus,
    OrderType,
    ProductType,
    Side,
    Symbol,
    TradingMode,
)
from sgr.exchanges.capabilities import CapabilityStatus, check_capability
from sgr.execution.engine import ExecutionEngine
from sgr.risk.grid_risk import GridPortfolioSnapshot, GridRiskEngine, get_grid_risk_engine

log = get_logger(__name__)

# Fill-Typ-Attribution (2026-09-23, Paper-Limit-Order-Semantik - siehe
# CCXTBaseAdapter._simulate_order()): ein "level_cross"-Fill entspricht
# einer ruhenden Limit-Order, die durch Preis-Crossing ausgeloest wurde
# (siehe on_price_tick()) - wirtschaftlich ein Maker-Fill. Ein
# "force_exit"-Fill (close_grid() bei Risk-Violation/Kill-Switch) ist ein
# dringlicher Markt-Exit - wirtschaftlich ein Taker-Fill. Diese Strings
# sind die vertragliche Schnittstelle zu CCXTBaseAdapter._simulate_order()
# (dort per order.metadata["grid_fill_type"] gelesen) - nicht aendern,
# ohne beide Seiten synchron zu halten.
GRID_FILL_TYPE_LEVEL_CROSS = "level_cross"
GRID_FILL_TYPE_FORCE_EXIT = "force_exit"


class GridOpenResult:
    """Ergebnis von GridController.open_grid() - entweder ein laufendes
    GridState oder eine eindeutige Ablehnung (nie eine Exception fuer
    den regulaeren Ablehnungsfall - fail-safe, analog zu RiskAssessment)."""

    __slots__ = ("grid", "approved", "reason", "compliance_status")

    def __init__(
        self,
        grid: GridState | None,
        approved: bool,
        reason: str,
        compliance_status: str | None = None,
    ) -> None:
        self.grid = grid
        self.approved = approved
        self.reason = reason
        self.compliance_status = compliance_status


class GridController:
    def __init__(
        self,
        execution_engine: ExecutionEngine,
        trading_mode: TradingMode,
        tenant_id: str | None = None,
        grid_risk_engine: GridRiskEngine | None = None,
        compliance_engine: ComplianceEngine | None = None,
        grid_repository: Any = None,
        order_repository: Any = None,
        symbol_kill_switch: Any = None,
        exchange_pool: Any = None,
        rate_limiter: Any = None,
    ) -> None:
        self._execution = execution_engine
        self._trading_mode = trading_mode
        self._tenant_id = tenant_id
        self._grid_risk = grid_risk_engine or get_grid_risk_engine()
        self._compliance = compliance_engine or get_compliance_engine()
        self._grid_repo = grid_repository
        # Alle drei optional/additiv, Default None = identisches Verhalten
        # zu vorher fuer jeden bestehenden Aufrufer/Test:
        #   order_repository: Recovery-Konsistenzpruefung (siehe
        #       restore_from_persistence()) - Terminal-Status der im
        #       Fill-Ledger referenzierten Orders.
        #   symbol_kill_switch: blockt neue Grid-Level-Oeffnungen fuer
        #       deaktivierte Symbole (siehe _symbol_blocked()) UND wird
        #       als Deaktivierungs-Hook registriert (siehe
        #       close_all_grids_for_symbol(), Aufrufstelle in
        #       sgr/api/main.py).
        #   exchange_pool: NUR fuer read-only Kontroll-Calls (Position-
        #       Mode-Check, offene Exchange-Orders bei Recovery) - JEDE
        #       tatsaechliche Order laeuft weiterhin ausschliesslich durch
        #       self._execution (siehe Modul-Docstring "KEINE FAKE-
        #       SHORTCUT-EXECUTION" - dieses Prinzip bleibt unveraendert).
        self._order_repo = order_repository
        self._symbol_kill_switch = symbol_kill_switch
        self._exchange_pool = exchange_pool
        # Phase J: optional, additiv - None (Default) = Rate-Limiter
        # deaktiviert, identisches Verhalten zu vorher (siehe
        # sgr/execution/grid_rate_limiter.py Modul-Docstring).
        self._rate_limiter = rate_limiter
        # In-Memory-Registry aller vom Controller verwalteten Grids
        # (Prozess-lokal - Persistenz best-effort via _grid_repo, analog
        # zu PortfolioEngine._state._positions).
        self._grids: dict[str, GridState] = {}

    # ------------------------------------------------------------------
    # Lifecycle: open
    # ------------------------------------------------------------------

    async def open_grid(
        self,
        decision: GridDecision,
        symbol: Symbol,
        strategy_name: str,
        account: AccountEligibility,
        portfolio_snapshot: GridPortfolioSnapshot,
        current_price: Decimal,
        liquidity_usd: Decimal | None = None,
        funding_rate_annualized_pct: float | None = None,
        volatility_atr_pct: float | None = None,
        directional_exposure_usd: Decimal | None = None,
    ) -> GridOpenResult:
        if decision.direction == GridDirection.NEUTRAL or decision.parameters is None:
            return GridOpenResult(
                None, False, "GridDecision.direction ist NEUTRAL - kein Grid vorgeschlagen."
            )

        # 0. Symbol-Kill-Switch (2026-09-23, Phase G): identische
        # Semantik wie der bereits bestehende Check in
        # TradingOrchestrator.run_cycle() fuer direktionale Strategien -
        # ein deaktiviertes Symbol darf kein neues Grid eroeffnen.
        if self._symbol_blocked(symbol):
            log.warning("grid_controller.symbol_kill_switch_blocked_open", symbol=str(symbol))
            return GridOpenResult(
                None,
                False,
                f"Symbol {symbol} ist per Symbol-Kill-Switch deaktiviert",
                compliance_status="symbol_kill_switch_active",
            )

        parameters = decision.parameters
        exchange = symbol.exchange
        product_type = ProductType.FUTURES_GRID

        # 0b. Hedge-/Position-Mode-Kompatibilitaet (2026-09-23, Phase K):
        # dieser Controller setzt niemals ein explizites positionSide auf
        # der OrderRequest (siehe _fill_level()) - er wurde ausschliesslich
        # gegen One-Way-Mode verifiziert. Ein Account im Hedge-Mode wuerde
        # mit dieser Order-Semantik auf Binance mit einem Fehler
        # abgelehnt bzw. auf einer Exchange, die stillschweigend eine
        # Standard-Seite annimmt, zu falsch zugeordneten Positionen
        # fuehren - beides nicht akzeptabel. Read-only Check (kein
        # automatischer Moduswechsel, siehe Modul-Docstring "KEINE
        # FAKE-SHORTCUT-EXECUTION" - ein Wechsel wuerde ausserdem
        # bestehende Gordon-/Sumo-Positionen auf demselben Account
        # gefaehrden koennen). NUR fuer LIVE relevant - PAPER simuliert
        # keinen echten Account-Modus.
        if self._trading_mode == TradingMode.LIVE and self._exchange_pool is not None:
            try:
                adapter = self._exchange_pool.get(exchange, self._trading_mode)
                mode_info = await adapter.get_position_mode()
                if mode_info.hedged:
                    log.error(
                        "grid_controller.hedge_mode_not_supported",
                        symbol=str(symbol),
                        exchange=exchange.value,
                    )
                    return GridOpenResult(
                        None,
                        False,
                        (
                            f"Account fuer {exchange.value} ist im Hedge-Mode - "
                            "GridController ist nur fuer One-Way-Mode verifiziert "
                            "(kein automatischer Moduswechsel, siehe Aufgabenstellung)"
                        ),
                        compliance_status="hedge_mode_unsupported",
                    )
            except Exception as e:
                # NotSupportedFeatureError (Exchange kennt kein Hedge/
                # One-Way-Konzept, z.B. Pionex) UND jeder andere Fehler
                # werden identisch behandelt: fail-closed fuer LIVE waere
                # hier zu aggressiv (viele Exchanges kennen das Konzept
                # schlicht nicht) - stattdessen nur geloggt, das Grid darf
                # weiterlaufen. Der eigentliche Schutz ist der explizite
                # hedged=True-Fall oben, nicht die Abwesenheit einer
                # Antwort.
                log.debug(
                    "grid_controller.position_mode_check_skipped",
                    symbol=str(symbol),
                    error=str(e),
                )

        # 1. Capability
        cap = check_capability(
            exchange,
            product_type,
            requires_long=parameters.long_or_short == GridDirection.LONG,
            requires_short=parameters.long_or_short == GridDirection.SHORT,
            requires_leverage=parameters.leverage > 1,
        )
        if cap.status != CapabilityStatus.OK:
            log.warning("grid_controller.capability_rejected", reason=cap.reason)
            return GridOpenResult(
                None, False, cap.reason, compliance_status="exchange_capability_missing"
            )

        # 2. Compliance
        compliance_result: ComplianceCheckResult = self._compliance.check(
            account,
            exchange,
            product_type,
            requires_long=parameters.long_or_short == GridDirection.LONG,
            requires_short=parameters.long_or_short == GridDirection.SHORT,
            requires_leverage=parameters.leverage > 1,
        )
        if not compliance_result.allowed:
            log.warning(
                "grid_controller.compliance_rejected",
                status=compliance_result.status.value,
                reason=compliance_result.reason,
            )
            return GridOpenResult(
                None,
                False,
                compliance_result.reason,
                compliance_status=compliance_result.status.value,
            )

        # 3. Risk
        risk_assessment: GridRiskAssessment = self._grid_risk.evaluate_new_grid(
            parameters,
            portfolio_snapshot,
            current_price,
            liquidity_usd=liquidity_usd,
            funding_rate_annualized_pct=funding_rate_annualized_pct,
            volatility_atr_pct=volatility_atr_pct,
            directional_exposure_usd=directional_exposure_usd,
        )
        if not risk_assessment.approved:
            log.warning("grid_controller.risk_rejected", reason=risk_assessment.reason)
            reason_text = risk_assessment.reason or ""
            if "Kostenschwelle" in reason_text or "Funding" in reason_text:
                # Diskriminiert den Cost-Guard-Ablehnungsgrund (Phase F)
                # per Text-Marker, da GridRiskEngine._evaluate_internal()
                # bewusst symbol-/strategie-agnostisch bleibt (identisches
                # Signatur-Prinzip wie sgr.risk.position_sizer.PositionSizer) -
                # die Metrik-Label (exchange/symbol/strategy) sind hier am
                # GridController, nicht in der Risk Engine, verfuegbar.
                try:
                    from sgr.monitoring.metrics import record_futures_grid_cost_guard_reject

                    record_futures_grid_cost_guard_reject(
                        exchange=exchange.value,
                        symbol=symbol.ccxt_symbol,
                        strategy=strategy_name,
                    )
                except Exception as e:
                    log.debug("grid_controller.cost_guard_metric_failed", error=str(e))
            return GridOpenResult(None, False, risk_assessment.reason or "Risk rejected")

        grid = self._build_grid_state(decision, symbol, strategy_name, parameters)
        grid.last_price = current_price
        self._grids[str(grid.id)] = grid
        await self._persist(grid)

        log.info(
            "grid_controller.grid_opened",
            grid_id=str(grid.id),
            symbol=str(symbol),
            direction=parameters.long_or_short.value,
            levels=len(grid.levels),
            leverage=str(parameters.leverage),
        )
        return GridOpenResult(grid, True, "")

    def _symbol_blocked(self, symbol: Symbol) -> bool:
        """True wenn der Symbol-Kill-Switch fuer dieses Symbol aktiv ist
        (siehe sgr/risk/symbol_kill_switch.py) - identisches Key-Format
        wie TradingOrchestrator.on_candle_event() (f"{exchange}:{ccxt_symbol}")."""
        if self._symbol_kill_switch is None:
            return False
        symbol_key = f"{symbol.exchange.value}:{symbol.ccxt_symbol}"
        return not self._symbol_kill_switch.is_active(symbol_key)

    async def close_all_grids_for_symbol(self, symbol_key: str, reason: str) -> list[GridState]:
        """
        Deaktivierungs-Hook fuer SymbolKillSwitch (siehe
        SymbolKillSwitch.register_deactivation_hook(), Aufrufstelle in
        sgr/api/main.py). Schliesst ALLE aktiven Grids auf diesem Symbol
        vollstaendig (alle gefuellten Level via Reduce-Only-Orders,
        identisch zu close_grid()) - Phase G, Punkt 3 ("offene Grid-
        Exposure gemaess bestehendem Kill-Switch-Verhalten behandeln").

        "Ruhende Grid Orders canceln" (Punkt 2 der Aufgabenstellung)
        entfaellt strukturell: dieser Controller haelt aktuell KEINE
        ruhenden Exchange-Orders (siehe Modul-Docstring - Level werden
        erst bei Crossing als MARKET-Order gesendet) - es gibt nichts zu
        stornieren, das nicht ohnehin bereits abgeschlossen ist.

        Idempotent: close_grid() selbst ist idempotent (no-op fuer ein
        bereits nicht-aktives Grid) - mehrfache Aufrufe (z.B. bei
        wiederholter Deaktivierung) sind sicher.
        """
        closed: list[GridState] = []
        for grid in list(self._grids.values()):
            if not grid.is_active:
                continue
            grid_symbol_key = f"{grid.exchange.value}:{grid.symbol.ccxt_symbol}"
            if grid_symbol_key != symbol_key:
                continue
            price = grid.last_price
            if price is None:
                log.error(
                    "grid_controller.close_on_symbol_kill_switch_no_price",
                    grid_id=str(grid.id),
                    symbol_key=symbol_key,
                )
                continue
            closed_grid = await self.close_grid(str(grid.id), f"symbol_kill_switch:{reason}", price)
            closed.append(closed_grid)
            log.warning(
                "grid_controller.closed_on_symbol_kill_switch",
                grid_id=str(grid.id),
                symbol_key=symbol_key,
                reason=reason,
            )
            try:
                from sgr.monitoring.metrics import record_futures_grid_kill_switch_event

                record_futures_grid_kill_switch_event(
                    exchange=grid.exchange.value, symbol=grid.symbol.ccxt_symbol, scope="symbol"
                )
            except Exception as e:
                log.debug("grid_controller.kill_switch_metric_failed", error=str(e))
        return closed

    def _build_grid_state(
        self,
        decision: GridDecision,
        symbol: Symbol,
        strategy_name: str,
        parameters: FuturesGridParameters,
    ) -> GridState:
        levels_prices = parameters.compute_levels()
        is_long = parameters.long_or_short == GridDirection.LONG
        levels = [
            GridLevelState(
                index=i,
                price=price,
                side="buy" if is_long else "sell",
            )
            for i, price in enumerate(levels_prices)
        ]
        return GridState(
            tenant_id=self._tenant_id,
            exchange=symbol.exchange,
            symbol=symbol,
            strategy_name=strategy_name,
            trading_mode=self._trading_mode,
            direction=parameters.long_or_short,
            status=GridStatus.ACTIVE,
            parameters={
                "grid_lower_price": str(parameters.grid_lower_price),
                "grid_upper_price": str(parameters.grid_upper_price),
                "grid_count": parameters.grid_count,
                "grid_mode": parameters.grid_mode.value,
                "leverage": str(parameters.leverage),
                "margin_mode": parameters.margin_mode.value,
                "position_size": str(parameters.position_size),
                "max_notional": str(parameters.max_notional),
                "take_profit": str(parameters.take_profit) if parameters.take_profit else None,
                "stop_loss": str(parameters.stop_loss) if parameters.stop_loss else None,
                "maximum_holding_time": parameters.maximum_holding_time,
                "total_notional": str(parameters.total_notional()),
            },
            levels=levels,
            opened_at=datetime.now(tz=UTC),
        )

    # ------------------------------------------------------------------
    # Runtime: price ticks -> fills
    # ------------------------------------------------------------------

    async def on_price_tick(self, grid_id: str, current_price: Decimal) -> GridState:
        """
        Prueft bei jedem eingehenden Preis (siehe TradingOrchestrator.
        on_candle_event() fuer das analoge Muster bei Directional-
        Strategien), ob ein Grid-Level seit dem LETZTEN bekannten Preis
        (grid.last_price) UEBERQUERT wurde - nicht ob es "erreichbar"
        waere. Ohne diese Unterscheidung wuerden bei der allerersten
        Preisabfrage nach Grid-Eroeffnung faelschlich ALLE Level auf der
        "richtigen" Seite des Eroeffnungspreises gleichzeitig ausloesen
        (jedes still nicht gefuellte Level oberhalb des Startpreises
        erfuellt "current_price <= level.price" trivial).

        LONG Grid: ein "buy"-Level oeffnet, wenn der Preis fallend durch
            es hindurchlaeuft; das naechsthoehere Level schliesst die
            Position wieder (Grid Capture), wenn der Preis steigend
            durch es hindurchlaeuft. SHORT Grid: gespiegelt.
        """
        grid = self._require_grid(grid_id)
        if not grid.is_active:
            return grid

        previous_price = grid.last_price if grid.last_price is not None else current_price
        moving_down = current_price < previous_price
        moving_up = current_price > previous_price

        def _crossed(level_price: Decimal) -> bool:
            """
            True nur bei tatsaechlicher UEBERQUERUNG seit dem letzten
            bekannten Preis - bewusst mit STRIKTER Grenze auf der Seite
            von previous_price, damit ein Level, das zufaellig exakt
            dem Eroeffnungspreis des Grids entspricht (z.B. ein
            symmetrisches Grid um den aktuellen Preis, siehe
            sgr.strategy.futures_grid._BaseFuturesGridStrategy.
            _build_parameters()), NICHT bereits beim allerersten Tick
            als "ueberquert" gilt, nur weil previous_price==level_price
            zufaellig zutrifft (kein echter Trigger, keine Order haette
            in der Realitaet an diesem Punkt gefuellt werden muessen -
            das Grid startet dort "auf der Kante", nicht darueber
            hinweg bewegt).
            """
            if moving_down:
                return current_price <= level_price < previous_price
            if moving_up:
                return previous_price < level_price <= current_price
            return False

        is_long = grid.direction == GridDirection.LONG
        levels = sorted(grid.levels, key=lambda lv: lv.index)

        # Symbol-Kill-Switch (Phase G, Punkt 1 "keine neue Grid-Order
        # mehr"): blockt NUR das Oeffnen neuer Level. Bereits gefuellte
        # Level duerfen weiterhin schliessen (de-risking bleibt erlaubt -
        # identisches Prinzip wie bypass_kill_switch bei
        # PositionProtectionWatchdog/PositionLiquidator: eine Order, die
        # Exposure REDUZIERT, darf nicht durch denselben Mechanismus
        # blockiert werden, der sie eigentlich ausloesen soll).
        new_opens_blocked = self._symbol_blocked(grid.symbol)

        # Cell-Modell (identisch zu sgr.backtesting.grid_simulator):
        # Cell i liegt zwischen levels[i] und levels[i+1]. Der
        # Fuellzustand ("is_filled") wird auf dem EINTRITTS-Level der
        # Cell gefuehrt (levels[i] fuer LONG, levels[i+1] fuer SHORT) -
        # dasselbe Objekt wird sowohl beim Oeffnen als auch beim
        # Schliessen dieser Cell an _fill_level() uebergeben.
        for i in range(len(levels) - 1):
            entry_state = levels[i] if is_long else levels[i + 1]
            entry_price = entry_state.price
            exit_price = levels[i + 1].price if is_long else levels[i].price

            if not entry_state.is_filled and _crossed(entry_price) and not new_opens_blocked:
                opening_direction_ok = moving_down if is_long else moving_up
                if opening_direction_ok:
                    await self._fill_level(grid, entry_state, entry_price, opening=True)
                    continue

            if entry_state.is_filled and _crossed(exit_price):
                closing_direction_ok = moving_up if is_long else moving_down
                if closing_direction_ok:
                    await self._fill_level(grid, entry_state, exit_price, opening=False)

        grid.last_price = current_price
        self._update_mark_to_market(grid, current_price)
        await self._persist(grid)
        self._record_snapshot_metric(grid)
        return grid

    def _update_mark_to_market(self, grid: GridState, current_price: Decimal) -> None:
        """
        Grid-Mark-to-Market (2026-09-24, Phase 8 - ersetzt die vorherigen
        0.0-Platzhalter fuer unrealized_pnl/drawdown). Berechnet bei JEDEM
        Preis-Tick (nicht nur bei einem Fill) neu, damit z.B. ein Preis,
        der sich zwischen zwei Fills bewegt, sofort in der Beobachtbarkeit
        sichtbar ist, nicht erst beim naechsten Fill.

        Verwendet EXAKT dasselbe Entry-Preis/Menge-Modell wie realized_pnl
        beim tatsaechlichen Level-Close (siehe _fill_level()) - kein neues
        PnL-Konzept, nur ohne einen echten Exit: fuer jedes aktuell
        gefuellte Level wird (current_price - level.price) * level.quantity
        * side_factor aufsummiert.

        peak_value ist ein monoton wachsendes Maximum von
        (realized_pnl + unrealized_pnl) seit Grid-Eroeffnung - Drawdown
        wird vom Aufrufer (record_futures_grid_extended_snapshot()) daraus
        abgeleitet, nicht hier gespeichert (kein zweiter, redundanter
        Zustand).
        """
        is_long = grid.direction == GridDirection.LONG
        side_factor = Decimal("1") if is_long else Decimal("-1")
        unrealized = Decimal("0")
        for level in grid.levels:
            if not level.is_filled or level.quantity <= 0:
                continue
            unrealized += (current_price - level.price) * level.quantity * side_factor

        grid.unrealized_pnl = unrealized
        total_value = grid.realized_pnl + unrealized
        if total_value > grid.peak_value:
            grid.peak_value = total_value

    def _record_snapshot_metric(self, grid: GridState) -> None:
        """Best-effort Metrik-Update (siehe record_futures_grid_extended_snapshot()
        Docstring) - separat von _fill_level()'s try/except, damit ein
        reiner Preis-Tick ohne Fill die Observability trotzdem aktuell
        haelt (vorher wurden unrealized_pnl/drawdown nur bei einem
        tatsaechlichen Fill aktualisiert)."""
        try:
            from sgr.monitoring.metrics import record_futures_grid_extended_snapshot

            levels_active = sum(1 for lv in grid.levels if lv.is_filled)
            levels_total = len(grid.levels)
            closed_cycles = sum(lv.cycle_count for lv in grid.levels) - levels_active
            avg_profit_per_cycle = (
                float(grid.realized_pnl) / closed_cycles if closed_cycles > 0 else 0.0
            )
            total_value = grid.realized_pnl + grid.unrealized_pnl
            drawdown_pct = (
                float((grid.peak_value - total_value) / grid.peak_value)
                if grid.peak_value > 0
                else 0.0
            )
            record_futures_grid_extended_snapshot(
                exchange=grid.exchange.value,
                symbol=grid.symbol.ccxt_symbol,
                strategy=grid.strategy_name,
                direction=grid.direction.value,
                trading_mode=grid.trading_mode.value,
                levels_active=levels_active,
                levels_total=levels_total,
                unrealized_pnl_usd=float(grid.unrealized_pnl),
                drawdown_pct=max(drawdown_pct, 0.0),
                avg_profit_per_cycle_usd=avg_profit_per_cycle,
                fees_usd=float(grid.fees_paid),
            )
        except Exception as e:
            log.debug("grid_controller.snapshot_metric_failed", grid_id=str(grid.id), error=str(e))

    async def _fill_level(
        self,
        grid: GridState,
        level: GridLevelState,
        current_price: Decimal,
        *,
        opening: bool,
        fill_type: str = GRID_FILL_TYPE_LEVEL_CROSS,
    ) -> None:
        """
        Fill-Semantik (Phase 7, 2026-09-24, Revision der vorherigen "AON"-
        Einordnung nach kritischer Pruefung: MARKET-Orders auf Binance
        Futures KOENNEN in der Realitaet (duennes Orderbuch, extreme
        Volatilitaet) mit status=PARTIALLY_FILLED zurueckkommen - siehe
        ExecutionEngine._monitor_fill(): dessen Poll-Loop bricht NUR bei
        FILLED/CANCELLED/REJECTED, ein dauerhaft PARTIALLY_FILLED-Zustand
        laeuft in den Timeout, die Restmenge wird auf der Exchange
        storniert, aber der bereits gefuellte Teil bleibt real auf dem
        Account offen. Die vorherige Behandlung ("alles ausser FILLED ist
        ein kompletter No-Op") haette genau diesen real gefuellten Teil
        SGR-seitig verloren - ein echter, live-relevanter Bug, kein
        theoretisches Risiko.

        Neue Regel: JEDER Fill mit result.filled_quantity > 0 wird mit der
        TATSAECHLICH gefuellten Menge verarbeitet (result.filled_quantity),
        NICHT mit der urspruenglich beabsichtigten qty. Nur ein Ergebnis
        mit filled_quantity == 0 (REJECTED/CANCELLED ohne jeden Fill) bleibt
        ein vollstaendiger No-Op. Dies ist KEIN Aufbau einer zweiten
        Orderpfad-Architektur (weiterhin ausschliesslich MARKET-Orders bei
        erkanntem Crossing, siehe Modul-Docstring) - nur eine korrekte
        Behandlung eines bereits moeglichen Exchange-Antwortzustands
        innerhalb desselben Pfads:

            - Opening: level.quantity = tatsaechlich gefuellte Menge (kann
              kleiner als das urspruengliche position_size-Ziel sein).
              level.is_filled=True auch bei einem Partial-Open - die
              Restmenge des urspruenglichen Ziels wird NICHT nachbestellt
              (kein automatisches Nachfassen, das waere ein neuer,
              spekulativer Order-Pfad) - das Level haelt schlicht eine
              kleinere Position als geplant, bis es beim naechsten
              Crossing wieder schliesst.
            - Closing: reduziert level.quantity um die tatsaechlich
              geschlossene Menge. Bleibt eine Restmenge > 0, bleibt
              is_filled=True (Level ist weiterhin, nur kleiner, offen) -
              realized_pnl wird NUR fuer den tatsaechlich geschlossenen
              Anteil gebucht, nicht fuer die urspruenglich beabsichtigte
              Gesamtmenge.
            - Ledger/Recovery: record_level_fill() persistiert die
              TATSAECHLICHE Menge - Ledger-Replay (restore_from_persistence())
              rekonstruiert dadurch automatisch korrekt, auch nach einem
              Partial Fill, ohne eigene Sonderlogik dort.
            - Backtest (GridBacktestSimulator): bleibt bewusst bei der
              All-or-Nothing-Vereinfachung pro Bar (dokumentiert dort) -
              Bar-Aufloesung kann einen echten Tick-Level-Partial-Fill
              ohnehin nicht abbilden; das ist eine separate, bereits
              dokumentierte Backtest-Grenze, keine Inkonsistenz zum
              Live-Pfad.
            - Paper (CCXTBaseAdapter._simulate_order()): erzeugt weiterhin
              nie PARTIALLY_FILLED (liefert MARKET-Orders immer sofort
              vollstaendig oder einen Fehler) - Paper kann diesen Pfad
              nicht ausloesen, LIVE kann es.
        """
        is_long = grid.direction == GridDirection.LONG

        if opening:
            position_size = Decimal(str(grid.parameters.get("position_size", "0")))
            if position_size <= 0:
                return
            qty = position_size / current_price
        else:
            # Beim Schliessen wird IMMER die beim Oeffnen tatsaechlich
            # gefuellte Menge zurueckgegeben (siehe GridLevelState.quantity
            # Docstring) - niemals eine neue, am Exit-Preis berechnete
            # Menge. Sonst driftet net_position_qty nie zurueck auf 0 und
            # die Order trifft nicht die tatsaechlich gehaltene Menge.
            qty = level.quantity
            if qty <= 0:
                return

        # opening: LONG kauft, SHORT verkauft. closing: jeweils umgekehrt.
        if (opening and is_long) or (not opening and not is_long):
            side = Side.BUY
        else:
            side = Side.SELL

        order = OrderRequest(
            signal_id=uuid4(),
            symbol=grid.symbol,
            side=side,
            order_type=OrderType.MARKET,
            quantity=qty,
            trading_mode=grid.trading_mode,
            reduce_only=not opening,
            metadata={
                "strategy": grid.strategy_name,
                "product_type": ProductType.FUTURES_GRID.value,
                "grid_id": str(grid.id),
                "grid_level_index": level.index,
                "target_leverage": grid.parameters.get("leverage", "1"),
                # Phase I (Paper-Limit-Order-Semantik): ein "level_cross"-
                # Fill ist wirtschaftlich ein Maker-Fill (die Order haette
                # als ruhende Limit-Order genau an diesem Preis gefuellt -
                # siehe Modul-Docstring), ein "force_exit"-Fill (Risk-
                # Violation/Kill-Switch/Grid-Close) ist ein dringlicher
                # Taker-Exit. CCXTBaseAdapter._simulate_order() liest
                # dieses Feld, um die passende Paper-Fee anzuwenden.
                "grid_fill_type": fill_type,
            },
        )

        # Phase J: Rate-Limit-Budget-Check VOR jedem Order-Submit (siehe
        # sgr/execution/grid_rate_limiter.py Modul-Docstring - proaktive
        # Vorstufe, kein Ersatz fuer die bestehende ccxt-Retry-/Ban-Logik).
        # Budget erschoepft -> kein Submit, kein Retry-Sturm, dieser eine
        # Fill-Versuch entfaellt fuer diesen Preis-Tick (der naechste
        # Tick/Scheduler-Zyklus versucht es erneut - identisches
        # Fail-Safe-Prinzip wie ein fehlender Preis).
        if self._rate_limiter is not None:
            allowed = await self._rate_limiter.acquire(
                grid.exchange.value, self._tenant_id, "order_submit"
            )
            if not allowed:
                log.warning(
                    "grid_controller.level_fill_rate_limited",
                    grid_id=str(grid.id),
                    level_index=level.index,
                )
                return

        result = await self._execution.execute(order)
        actual_qty = result.filled_quantity
        is_partial = result.status == OrderStatus.PARTIALLY_FILLED
        fill_ok = result.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
        if not fill_ok or actual_qty <= 0:
            log.warning(
                "grid_controller.level_fill_rejected",
                grid_id=str(grid.id),
                level_index=level.index,
                status=result.status.value,
            )
            return
        if is_partial:
            log.warning(
                "grid_controller.level_partial_fill",
                grid_id=str(grid.id),
                level_index=level.index,
                requested_qty=str(qty),
                filled_qty=str(actual_qty),
            )

        now = datetime.now(tz=UTC)

        # Fill-Ledger-Eintrag VOR der aggregierten State-Mutation (Phase
        # B/C, Crash-Recovery): siehe GridRepository.get_fills() Docstring
        # - der Ledger ist dadurch die autoritativere Quelle bei einem
        # Absturz zwischen diesem Schreibvorgang und dem _persist(grid)
        # am Ende dieser Methode. Best-effort wie jede andere Persistenz
        # in dieser Klasse - ein Fehler hier darf den bereits erfolgten,
        # echten Exchange-Fill nicht rueckgaengig machen, nur die
        # Ledger-Eintragung entfaellt (Recovery faellt dann auf den
        # GridModel.levels-Snapshot zurueck, siehe restore_from_persistence()).
        if self._grid_repo is not None:
            try:
                await self._grid_repo.record_level_fill(
                    grid_id=str(grid.id),
                    level_index=level.index,
                    price=current_price,
                    side=side.value,
                    quantity=actual_qty,
                    is_opening=opening,
                    filled_at=now,
                    order_id=str(order.id),
                    cycle_pnl=None,  # unten nachgetragen, falls closing
                )
            except Exception as e:
                log.error(
                    "grid_controller.record_level_fill_failed",
                    grid_id=str(grid.id),
                    level_index=level.index,
                    error=str(e),
                )

        level.last_order_id = result.exchange_order_id
        level.last_filled_at = now
        grid.fills_count += 1
        grid.fees_paid += result.fees

        if opening:
            # is_filled=True auch bei Partial-Open (siehe Docstring) - das
            # Level haelt jetzt real actual_qty, nicht zwingend die
            # urspruenglich geplante qty.
            level.is_filled = True
            level.cycle_count += 1
            level.quantity = actual_qty
            grid.net_position_qty += actual_qty if is_long else -actual_qty
        else:
            # Schliessen: reduziert die gehaltene Menge um genau das, was
            # TATSAECHLICH geschlossen wurde. Bleibt ein Rest > 0 (Partial
            # Close), bleibt das Level offen (is_filled=True) - kein
            # Nachbestellen, kein zweiter Orderpfad, das naechste Crossing
            # behandelt den Rest ganz normal.
            grid.net_position_qty -= actual_qty if is_long else -actual_qty
            entry_price = level.price
            exit_price = current_price
            side_factor = Decimal("1") if is_long else Decimal("-1")
            cycle_pnl = (exit_price - entry_price) * actual_qty * side_factor - result.fees
            grid.realized_pnl += cycle_pnl

            remaining_qty = level.quantity - actual_qty
            if remaining_qty <= Decimal("0.00000001"):
                level.is_filled = False
                level.quantity = Decimal("0")
            else:
                level.is_filled = True
                level.quantity = remaining_qty

        # Sofortiger Persist NACH JEDEM einzelnen Fill (Phase B/C-Fix,
        # 2026-09-23): vorher wurde grid.levels erst am ENDE von
        # on_price_tick()/close_grid() persistiert, obwohl _fill_level()
        # dazwischen bereits echte Exchange-Orders ausloest - ein Absturz
        # zwischen zwei Fills innerhalb desselben Ticks haette den
        # bereits real ausgefuehrten Fill beim naechsten Neustart
        # verloren (GridModel.levels haette den alten Zustand gezeigt).
        # Der Ledger-Eintrag oben deckt denselben Fall zusaetzlich ab,
        # falls sogar DIESER Persist noch scheitert.
        await self._persist(grid)

        log.info(
            "grid_controller.level_filled",
            grid_id=str(grid.id),
            level_index=level.index,
            opening=opening,
            price=str(current_price),
            qty=str(actual_qty),
            partial=is_partial,
            fill_type=fill_type,
        )

        # Mark-to-Market NACH jeder Zustandsaenderung neu berechnen (Phase 8)
        # - current_price ist hier der tatsaechliche Fill-Preis, eine
        # bessere Momentaufnahme als der vorherige grid.last_price.
        self._update_mark_to_market(grid, current_price)

        try:
            from sgr.monitoring.metrics import record_futures_grid_fill

            record_futures_grid_fill(
                exchange=grid.exchange.value,
                symbol=grid.symbol.ccxt_symbol,
                strategy=grid.strategy_name,
                direction=grid.direction.value,
            )
            if not opening:
                from sgr.monitoring.metrics import record_futures_grid_cycle

                record_futures_grid_cycle(
                    exchange=grid.exchange.value,
                    symbol=grid.symbol.ccxt_symbol,
                    strategy=grid.strategy_name,
                    direction=grid.direction.value,
                )
            self._record_snapshot_metric(grid)
        except Exception as e:
            log.debug("grid_controller.fill_metric_failed", error=str(e))
            try:
                from sgr.monitoring.metrics import record_futures_grid_error

                record_futures_grid_error(
                    exchange=grid.exchange.value, symbol=grid.symbol.ccxt_symbol, operation="fill"
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Lifecycle: close
    # ------------------------------------------------------------------

    async def close_grid(
        self, grid_id: str, reason: str, current_price: Decimal, *, force: bool = True
    ) -> GridState:
        grid = self._require_grid(grid_id)
        if not grid.is_active:
            return grid

        fill_type = GRID_FILL_TYPE_FORCE_EXIT if force else GRID_FILL_TYPE_LEVEL_CROSS
        grid.status = GridStatus.CLOSING
        for level in grid.levels:
            if not level.is_filled:
                continue
            await self._fill_level(grid, level, current_price, opening=False, fill_type=fill_type)

        grid.status = GridStatus.CLOSED
        grid.closed_at = datetime.now(tz=UTC)
        grid.close_reason = reason
        # Nach vollstaendigem Close ist die Exposure per Definition 0 -
        # unrealized_pnl faellt korrekt auf 0 (keine gefuellten Level mehr).
        self._update_mark_to_market(grid, current_price)
        await self._persist(grid)
        self._record_snapshot_metric(grid)

        log.info(
            "grid_controller.grid_closed",
            grid_id=str(grid.id),
            reason=reason,
            realized_pnl=str(grid.realized_pnl),
            fills=grid.fills_count,
        )
        return grid

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    async def monitor_grids(
        self,
        prices: dict[str, Decimal],
        funding_rates_annualized_pct: dict[str, float] | None = None,
        volatility_atr_pct: dict[str, float] | None = None,
    ) -> list[GridState]:
        """
        Periodischer Wartungslauf (analog zu PositionProtectionWatchdog):
        prueft jedes aktive Grid gegen GridRiskEngine.check_ongoing_grid()
        und schliesst es bei einer "hard"-Verletzung. prices/funding/
        volatility sind pro symbol_key (ccxt_symbol) indiziert.
        """
        closed: list[GridState] = []
        for grid in list(self._grids.values()):
            if not grid.is_active:
                continue
            symbol_key = grid.symbol.ccxt_symbol
            price = prices.get(symbol_key)
            if price is None:
                continue

            violations = self._grid_risk.check_ongoing_grid(
                grid,
                price,
                funding_rate_annualized_pct=(funding_rates_annualized_pct or {}).get(symbol_key),
                volatility_atr_pct=(volatility_atr_pct or {}).get(symbol_key),
            )
            hard_violations = [v for v in violations if v.severity == "hard"]
            if hard_violations:
                reason = ";".join(v.code for v in hard_violations)
                await self.close_grid(str(grid.id), reason, price)
                closed.append(grid)
            else:
                await self.on_price_tick(str(grid.id), price)

        return closed

    # ------------------------------------------------------------------
    # Queries / persistence
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Crash Recovery (Phase C)
    # ------------------------------------------------------------------

    async def restore_from_persistence(self) -> list[GridState]:
        """
        Grid-Crash-Recovery nach einem Worker-Neustart. Analoges Muster zu
        PortfolioEngine.restore_from_persistence()/RecoveryManager.
        recover_after_crash(), aber grid-spezifisch, weil der einzige
        bisher bestehende Live-Order-Pfad dieses Controllers ausschliesslich
        MARKET-Orders bei erkanntem Preis-Crossing sendet (siehe
        Modul-Docstring) - es gibt aktuell KEINE ruhenden Exchange-Orders
        zu rekonzilieren (das waere erst fuer eine kuenftige Live-
        Implementierung mit echten Limit-Orders relevant, siehe
        "offene Punkte" im Strategiebericht). Die tatsaechlich vorhandene,
        wiederherzustellende Information ist der Level-FUELLSTATUS.

        Zustands-Herkunft, in Prioritaet:
        1. Fill-Ledger (GridRepository.get_fills(), append-only, VOR jeder
           GridModel-Aktualisierung geschrieben - siehe _fill_level()) -
           die autoritative Quelle. Level-Array wird komplett NEU aus
           den persistierten Grid-Parametern aufgebaut (identische
           compute_levels()-Berechnung wie bei der Eroeffnung) und dann
           durch chronologisches Replay des Ledgers auf den tatsaechlich
           bekannten Fuellstatus gebracht - NICHT aus dem levels-JSONB-
           Snapshot uebernommen, der zwischen dem letzten Fill und dem
           Absturz stale sein kann (siehe _fill_level()-Kommentar zur
           Persist-Reihenfolge).
        2. Order-Status-Kreuzcheck (nur wenn order_repository injiziert):
           jeder Ledger-Eintrag mit order_id wird gegen den tatsaechlichen
           OrderRepository-Status geprueft. Nicht "filled" oder gar nicht
           gefunden -> unklarer Zustand -> GESAMTES Grid wird NICHT
           wiederhergestellt (fail-closed, kein Teilzustand).

        Es wird bei Recovery NIEMALS eine neue Order platziert (weder
        fehlende Gegenorders werden "nachgeholt" noch irgendetwas
        storniert) - Levels, die laut Ledger offen sind, bleiben einfach
        korrekt als is_filled=True markiert und werden beim naechsten
        echten on_price_tick()-Aufruf ganz normal weiterverarbeitet (die
        "fehlende Gegenorder" IST der naechste Crossing-Tick, kein
        separater Rekonstruktionsschritt - siehe Klassen-Docstring).
        Bestehende Idempotenz (order.id als Client-Order-ID, siehe
        sgr/execution/order_safety.py) bleibt dadurch vollstaendig
        unangetastet und wird nicht dupliziert.

        Grids mit unklarem Zustand werden NICHT in self._grids
        aufgenommen (bleiben in der DB unveraendert im zuletzt bekannten
        Status stehen) - sie erfordern manuelle Pruefung, kein
        automatisches Resubmit/Cancel.
        """
        if self._grid_repo is None:
            log.warning("grid_controller.restore_skipped_no_repository")
            return []

        try:
            rows = await self._grid_repo.get_open_grids(
                self._trading_mode, user_id=self._tenant_id
            )
        except Exception as e:
            log.error("grid_controller.restore_load_open_grids_failed", error=str(e))
            return []

        restored: list[GridState] = []
        inconsistent = 0
        for row in rows:
            try:
                grid = await self._restore_one_grid(row)
            except Exception as e:
                log.error(
                    "grid_controller.restore_one_grid_unexpected_error",
                    grid_id=row.get("id"),
                    error=str(e),
                    exc_info=True,
                )
                grid = None
            try:
                from sgr.monitoring.metrics import record_futures_grid_recovery

                record_futures_grid_recovery(
                    exchange=str(row.get("exchange")),
                    symbol=str(row.get("symbol")),
                    trading_mode=self._trading_mode.value,
                    success=grid is not None,
                )
            except Exception as metric_err:
                log.debug("grid_controller.recovery_metric_failed", error=str(metric_err))
            if grid is None:
                inconsistent += 1
                continue
            self._grids[str(grid.id)] = grid
            restored.append(grid)

        log.info(
            "grid_controller.restore_completed",
            candidates=len(rows),
            restored=len(restored),
            inconsistent=inconsistent,
        )
        return restored

    async def _restore_one_grid(self, row: dict[str, Any]) -> GridState | None:
        grid = self._grid_state_from_row(row)

        try:
            fills = await self._grid_repo.get_fills(str(row["id"]))
        except Exception as e:
            log.error(
                "grid_controller.restore_fills_load_failed", grid_id=row["id"], error=str(e)
            )
            return None  # fail-closed: ohne Ledger-Zugriff keine sichere Rekonstruktion

        levels_by_index = {lv.index: lv for lv in grid.levels}
        for fill in fills:
            idx = fill["level_index"]
            level = levels_by_index.get(idx)
            if level is None:
                log.error(
                    "grid_controller.restore_fill_references_unknown_level",
                    grid_id=row["id"],
                    level_index=idx,
                )
                return None  # fail-closed: Ledger widerspricht der Parameter-Struktur

            order_id = fill.get("order_id")
            if order_id is not None and self._order_repo is not None:
                order_status = await self._lookup_order_status(str(order_id))
                if order_status != "filled":
                    log.error(
                        "grid_controller.restore_inconsistent_order_status",
                        grid_id=row["id"],
                        level_index=idx,
                        order_id=str(order_id),
                        status=order_status,
                    )
                    return None  # fail-closed: unklarer Order-Ausgang, kein Raten

            if fill["is_opening"]:
                # Ein zweiter "opening"-Fill fuer denselben Level-Index
                # VOR einem dazwischenliegenden Close ist ein weiterer
                # Partial-Fill desselben Opens (siehe _fill_level(): ein
                # Level kann in mehreren Teil-Fuellungen geoeffnet werden,
                # bevor es als vollstaendig "gefuellt" gilt) - Mengen
                # akkumulieren statt ueberschreiben, damit der Replay
                # dieselbe Endmenge ergibt wie die Live-Verarbeitung.
                level.is_filled = True
                level.quantity += fill["quantity"]
                level.cycle_count += 1
            else:
                # "Restart nach Partial Fill" (siehe Live-Verification-
                # Anweisung Abschnitt G): ein schliessender Fill kann
                # selbst nur ein TEIL-Close sein (siehe _fill_level(),
                # closing-Zweig) - die Restmenge muss wie live per
                # Subtraktion berechnet werden, NICHT pauschal auf 0
                # gesetzt werden. Sonst wuerde ein Neustart nach einem
                # Partial Close die tatsaechlich noch offene Restmenge
                # (und damit reale Exchange-Exposure) stillschweigend
                # verlieren.
                remaining_qty = level.quantity - fill["quantity"]
                if remaining_qty <= Decimal("0.00000001"):
                    level.is_filled = False
                    level.quantity = Decimal("0")
                else:
                    level.is_filled = True
                    level.quantity = remaining_qty
            level.last_order_id = str(order_id) if order_id else level.last_order_id
            level.last_filled_at = fill["filled_at"]

        grid.levels = sorted(levels_by_index.values(), key=lambda lv: lv.index)

        log.info(
            "grid_controller.grid_restored",
            grid_id=row["id"],
            symbol=row["symbol"],
            levels_filled=sum(1 for lv in grid.levels if lv.is_filled),
            levels_total=len(grid.levels),
            fills_replayed=len(fills),
        )
        return grid

    async def _lookup_order_status(self, order_id: str) -> str | None:
        """Best-effort DB-Status-Lookup (kein Exchange-Call) - fail-safe
        None bei jedem Fehler, vom Aufrufer als 'unklar' behandelt."""
        if self._order_repo is None:
            return None
        try:
            row = await self._order_repo.get_by_id(order_id)
        except Exception as e:
            log.error("grid_controller.order_status_lookup_failed", order_id=order_id, error=str(e))
            return None
        if row is None:
            return None
        status = row.get("status")
        return str(status) if status is not None else None

    def _grid_state_from_row(self, row: dict[str, Any]) -> GridState:
        """
        Rekonstruiert die GridState-Rahmendaten (Metadaten, Aggregatwerte,
        Parameter) aus einer GridRepository-Zeile. levels wird bewusst
        FRISCH aus den persistierten Parametern aufgebaut (compute_levels(),
        identisch zur urspruenglichen Eroeffnung in _build_grid_state()),
        NICHT aus dem levels-JSONB-Snapshot direkt uebernommen - der
        Ledger-Replay in _restore_one_grid() ist die einzige autoritative
        Quelle fuer den tatsaechlichen Fuellstatus (siehe dortigen
        Docstring).
        """
        base, _, quote = str(row["symbol"]).partition("/")
        exchange = ExchangeID(row["exchange"])
        symbol = Symbol(base=base, quote=quote, exchange=exchange)
        parameters_dict = row.get("parameters") or {}
        direction = GridDirection(row["direction"])

        levels_prices = self._compute_levels_from_parameters(parameters_dict, direction)
        is_long = direction == GridDirection.LONG
        levels = [
            GridLevelState(index=i, price=price, side="buy" if is_long else "sell")
            for i, price in enumerate(levels_prices)
        ]

        return GridState(
            id=row["id"],
            tenant_id=row.get("user_id"),
            exchange=exchange,
            symbol=symbol,
            strategy_name=row["strategy_name"],
            trading_mode=TradingMode(row["trading_mode"]),
            direction=direction,
            status=GridStatus(row["status"]),
            parameters=parameters_dict,
            levels=levels,
            net_position_qty=Decimal(str(row.get("net_position_qty") or "0")),
            realized_pnl=Decimal(str(row.get("realized_pnl") or "0")),
            # unrealized_pnl wird sofort beim naechsten on_price_tick()
            # neu berechnet (siehe _update_mark_to_market()) - hier trotzdem
            # aus der Zeile uebernommen statt hart auf 0 gesetzt, damit ein
            # Metrik-Read UNMITTELBAR nach Recovery (vor dem ersten Tick)
            # nicht faelschlich 0 zeigt. peak_value MUSS aus der Zeile
            # uebernommen werden - sonst verliert ein Neustart die bisherige
            # Drawdown-Historie (ein monoton wachsendes Maximum wuerde sonst
            # faelschlich auf 0 zurueckfallen).
            unrealized_pnl=Decimal(str(row.get("unrealized_pnl") or "0")),
            peak_value=Decimal(str(row.get("peak_value") or "0")),
            fees_paid=Decimal(str(row.get("fees_paid") or "0")),
            funding_paid=Decimal(str(row.get("funding_paid") or "0")),
            fills_count=int(row.get("fills_count") or 0),
            last_price=(
                Decimal(str(row["last_price"])) if row.get("last_price") is not None else None
            ),
            opened_at=row["opened_at"],
            closed_at=row.get("closed_at"),
            close_reason=row.get("close_reason"),
        )

    @staticmethod
    def _compute_levels_from_parameters(
        parameters_dict: dict[str, Any], direction: GridDirection
    ) -> list[Decimal]:
        params = FuturesGridParameters(
            grid_lower_price=Decimal(str(parameters_dict["grid_lower_price"])),
            grid_upper_price=Decimal(str(parameters_dict["grid_upper_price"])),
            grid_count=int(parameters_dict["grid_count"]),
            long_or_short=direction,
            leverage=Decimal(str(parameters_dict.get("leverage", "1"))),
            grid_mode=GridSpacingMode(parameters_dict.get("grid_mode", "arithmetic")),
        )
        return params.compute_levels()

    def get_grid(self, grid_id: str) -> GridState | None:
        return self._grids.get(grid_id)

    def active_grids(self) -> list[GridState]:
        return [g for g in self._grids.values() if g.is_active]

    def _require_grid(self, grid_id: str) -> GridState:
        grid = self._grids.get(grid_id)
        if grid is None:
            raise KeyError(f"Grid {grid_id} not tracked by this GridController instance")
        return grid

    async def _persist(self, grid: GridState) -> None:
        """Best-effort Persistenz (analog zu PortfolioEngine._persist_*)."""
        if self._grid_repo is None:
            return
        try:
            await self._grid_repo.upsert(grid)
        except Exception as e:
            log.error("grid_controller.persist_failed", grid_id=str(grid.id), error=str(e))
