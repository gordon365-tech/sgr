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
    MarketRegime,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    Side,
    TradingMode,
)

log = get_logger(__name__)


class PositionProtectionManager:
    """
    Haengt SL/TP-Preise und eine Max-Holding-Deadline an neu eroeffnete
    Positionen (siehe PortfolioEngine.on_position_opened-Hook).

    Rein rechnend/persistierend - platziert selbst KEINE Exchange-Order
    (siehe Modul-Docstring fuer die Begruendung) - die tatsaechliche
    Durchsetzung/der Exit passiert im PositionProtectionWatchdog.

    Exit-Quelle (2026-09-23, explizite operative Anweisung - schliesst die
    von der Analyse identifizierte Luecke, dass mean_reversion_v1/
    breakout_v1 bereits ATR-basierte target_price/stop_price berechnen,
    diese aber bisher nie den PositionProtectionManager erreichten):
      1. Strategie-gelieferte Werte (order_metadata["target_price"]/
         ["stop_price"], via RiskEngine.build_order_request() ->
         ExecutionEngine -> result.raw_response -> PortfolioEngine
         durchgereicht) - falls vorhanden UND plausibel, siehe
         _extract_strategy_prices().
      2. Sonst: globaler RiskLimitsConfig-Fallback (stop_loss_pct/
         take_profit_pct) - unveraendertes Verhalten fuer
         trend_following_v1/momentum_v1/volatility_adjusted_momentum_v1,
         die keine Metadata liefern.
    In beiden Faellen greift danach der Kosten-Guard (_apply_cost_guard()).
    """

    async def on_position_opened(
        self, position: Position, order_metadata: dict[str, Any] | None = None
    ) -> Position | None:
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

        strategy_target, strategy_stop = self._extract_strategy_prices(
            order_metadata, position.side, entry
        )

        if strategy_target is not None and strategy_stop is not None:
            take_profit_price = strategy_target
            stop_loss_price = strategy_stop
            exit_source = "strategy_metadata"
        else:
            sl_pct = Decimal(str(limits.stop_loss_pct))
            tp_pct = Decimal(str(limits.take_profit_pct))
            if position.side == PositionSide.LONG:
                stop_loss_price = entry * (Decimal("1") - sl_pct)
                take_profit_price = entry * (Decimal("1") + tp_pct)
            else:
                stop_loss_price = entry * (Decimal("1") + sl_pct)
                take_profit_price = entry * (Decimal("1") - tp_pct)
            exit_source = "config_fallback"

        take_profit_price, cost_guard_applied = self._apply_cost_guard(
            entry, take_profit_price, position.side, limits
        )

        max_holding_until = position.opened_at + timedelta(minutes=limits.max_holding_minutes)
        entry_regime = self._extract_entry_regime(order_metadata)

        log.info(
            "position_protection.attached",
            symbol=str(position.symbol),
            side=position.side.value,
            entry_price=str(entry),
            stop_loss_price=str(stop_loss_price),
            take_profit_price=str(take_profit_price),
            max_holding_until=max_holding_until.isoformat(),
            exit_source=exit_source,
            cost_guard_applied=cost_guard_applied,
            entry_regime=entry_regime.value if entry_regime else None,
        )

        return position.model_copy(
            update={
                "stop_loss_price": stop_loss_price,
                "take_profit_price": take_profit_price,
                "max_holding_until": max_holding_until,
                "entry_regime": entry_regime,
            }
        )

    @staticmethod
    def _extract_entry_regime(order_metadata: dict[str, Any] | None) -> MarketRegime | None:
        """
        Liest das beim Entry klassifizierte Regime aus order_metadata
        (siehe RiskEngine.build_order_request(): signal.regime wird dort
        IMMER gesetzt, da Signal.regime ein Pflichtfeld ist - anders als
        target_price/stop_price ist hier also kein "Strategie liefert es
        manchmal nicht"-Fall zu behandeln, nur der generelle Fall
        "order_metadata fehlt komplett" oder ein ungueltiger Wert
        (defensiv, z.B. bei einem manuell/per Test konstruierten
        order_metadata ohne dieses Feld - fail-safe: None statt Exception,
        Regime-Exit bleibt dann fuer diese Position inaktiv, siehe
        PositionProtectionWatchdog._check_regime_exit()).
        """
        if not order_metadata:
            return None
        raw = order_metadata.get("entry_regime")
        if not raw:
            return None
        try:
            return MarketRegime(raw)
        except ValueError:
            return None

    @staticmethod
    def _extract_strategy_prices(
        order_metadata: dict[str, Any] | None,
        side: PositionSide,
        entry: Decimal,
    ) -> tuple[Decimal | None, Decimal | None]:
        """
        Liest target_price/stop_price aus order_metadata (result.raw_response
        der Entry-Order), falls von der Strategie geliefert (aktuell
        mean_reversion_v1/breakout_v1, beide ATR-basiert). Andere
        Strategien liefern nichts -> (None, None), Aufrufer faellt auf den
        globalen RiskLimitsConfig-Fallback zurueck.

        Nur akzeptiert, wenn BEIDE Werte vorhanden, positiv UND
        richtungskonsistent sind (Long: stop < entry < target; Short:
        target < entry < stop) - ein einzelner kaputter/unplausibler Wert
        (z.B. der bekannte round(...,2)-Rundungsfehler bei Mikro-Preis-
        Symbolen in mean_reversion_v1/breakout_v1, der target_price/
        stop_price auf 0.0 rundet) darf nicht zu einem Stop bei Preis 0
        oder einem Target auf der falschen Seite des Entries fuehren. Bei
        jeder Unplausibilitaet: beide verwerfen, kompletter Fallback auf
        die globale Config statt nur einem der beiden Werte zu vertrauen.
        """
        if not order_metadata:
            return None, None
        raw_target = order_metadata.get("target_price")
        raw_stop = order_metadata.get("stop_price")
        if not raw_target or not raw_stop:
            return None, None
        try:
            target = Decimal(str(raw_target))
            stop = Decimal(str(raw_stop))
        except (ValueError, ArithmeticError):
            return None, None
        if target <= 0 or stop <= 0:
            return None, None
        if side == PositionSide.LONG:
            if not (stop < entry < target):
                return None, None
        else:
            if not (target < entry < stop):
                return None, None
        return target, stop

    @staticmethod
    def _apply_cost_guard(
        entry: Decimal,
        take_profit_price: Decimal,
        side: PositionSide,
        limits: Any,
    ) -> tuple[Decimal, bool]:
        """
        Kosten-Guard (explizite operative Anweisung 2026-09-23): ein TP
        darf nicht so eng sein, dass ein tatsaechlich ERREICHTES TP nach
        den im Environment konfigurierten Kosten (RiskLimitsConfig.
        paper_taker_fee_pct + paper_slippage_pct, je Seite - KEINE hart
        codierten Binance/Pionex-Werte) trotzdem einen Nettoverlust
        ergibt. Empirisch bestaetigt (eigener Sweep gegen echte
        historische Daten, siehe Analysebericht): bei TP < Round-Trip-
        Kosten lag die Win-Rate bei 0% ueber jede getestete SL-Distanz,
        selbst wenn das TP-Level real getroffen wurde.

        Bewusst KEIN fester globaler TP-Wert (0,20%/0,25%/0,50% etc.) -
        stattdessen wird nur die minimale oekonomisch sinnvolle Distanz
        erzwungen: ein zu enges TP wird auf genau die Kostenschwelle
        angehoben, das Ziel selbst (strategiegetrieben oder Fallback)
        bleibt ansonsten unveraendert. Bei den aktuellen Produktions-
        Defaults (0,05%+0,05% je Seite = 0,20% Round-Trip,
        take_profit_pct=2%) greift dieser Guard nicht - unveraendertes
        Verhalten fuer die aktuelle Gordon/Sumo-Konfiguration.

        Hinweis/dokumentierte Grenze: paper_taker_fee_pct/
        paper_slippage_pct sind aktuell die einzigen in RiskLimitsConfig
        konfigurierten Kostenschaetzwerte im System (fuer PAPER benannt,
        aber es existiert kein separates LIVE-Pendant) - fuer LIVE-Trading
        ist das eine Naeherung, kein exakter Exchange-Wert. Kein
        spekulativer LIVE-spezifischer Kostenmechanismus wurde hier neu
        erfunden; das ist eine bewusst dokumentierte Luecke, kein
        stillschweigender Kompromiss.
        """
        round_trip_cost_pct = Decimal(
            str(2 * (limits.paper_taker_fee_pct + limits.paper_slippage_pct))
        )
        min_distance = entry * round_trip_cost_pct

        if side == PositionSide.LONG:
            distance = take_profit_price - entry
            if distance < min_distance:
                return entry + min_distance, True
        else:
            distance = entry - take_profit_price
            if distance < min_distance:
                return entry - min_distance, True
        return take_profit_price, False

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

    PAPER-Stress-Test Auto-Recovery (2026-09-22, explizite operative
    Anweisung fuer eine aggressive PAPER-Validierungsphase): optionales,
    additives Feature ueber enable_paper_stress_auto_recovery() - siehe
    dortigen Docstring. Standardmaessig deaktiviert (self._kill_switch
    bleibt None), unveraendertes Verhalten fuer alle bestehenden
    Call-Sites/Tests, die diese Methode nicht aufrufen.
    """

    def __init__(
        self,
        portfolio_engine: Any,
        execution_engine: Any,
        interval_seconds: float = 60.0,
        feature_store: Any = None,
        regime_check_timeframe: str = "1h",
    ) -> None:
        self._portfolio = portfolio_engine
        self._execution = execution_engine
        self._interval_seconds = interval_seconds
        self._task: Any = None
        # Live-Regime-Exit (2026-09-23): optional, additiv. feature_store=
        # None (Default) haelt _check_regime_exit() als reinen No-Op -
        # identisches Verhalten zu vorher fuer jeden bestehenden Aufrufer/
        # Test, der diesen Parameter nicht setzt. regime_check_timeframe
        # ist bewusst EIN globaler Wert (kein pro-Position-Timeframe, das
        # existiert im Position-Domain-Typ nicht) - deckt sich mit dem
        # de-facto-Standard-Timeframe (1h) fuer die meisten aktiven
        # Feeds (siehe LIVE_MARKET_DATA_SYMBOLS in sgr/api/main.py) und
        # ist explizit konfigurierbar fuer abweichende Deployments.
        self._feature_store = feature_store
        self._regime_check_timeframe = regime_check_timeframe

        # PAPER-Stress-Test Auto-Recovery (additiv, siehe
        # enable_paper_stress_auto_recovery()). _kill_switch bleibt None,
        # solange das Feature nicht explizit aktiviert wird.
        self._kill_switch: Any = None
        self._auto_recovery_tenant_id: str | None = None
        # Idempotenz (Anforderung 13): merkt sich, fuer welchen Trigger
        # (identifiziert durch dessen triggered_at-Zeitstempel) bereits
        # automatisch zurueckgesetzt wurde - verhindert doppelte reset()-
        # Aufrufe/Log-Eintraege bei ueberlappenden Watchdog-Ticks, auch
        # wenn KillSwitch.reset() selbst schon idempotent ist (frueher
        # Return bei is_active=False).
        self._last_auto_recovered_trigger_at: str | None = None
        # Grobe Zaehlung "seit dem letzten Auto-Recovery geschlossene
        # Positionen" fuer das Audit-Log (Anforderung 14) - bewusst kein
        # praeziser Zusammenhang mit EINEM spezifischen Trigger noetig,
        # da dieser Watchdog ohnehin sequenziell und pro Tenant isoliert
        # arbeitet (ein Tenant-Prozess hat immer nur einen aktiven Trigger
        # gleichzeitig).
        self._positions_closed_since_recovery = 0

    def enable_paper_stress_auto_recovery(
        self,
        kill_switch: Any,
        trading_mode: TradingMode,
        tenant_id: str | None,
    ) -> None:
        """
        Aktiviert automatisches Kill-Switch-Reset NUR fuer eine
        aggressive PAPER-Trading-Stress-Testphase (explizite operative
        Anweisung, 2026-09-22) - schliesst die Luecke, dass der Kill
        Switch nach max_open_positions permanent haengen bleibt und
        dadurch jede weitere Signal-Batch nach dem ersten Zyklus
        blockiert, obwohl alle ausloesenden Positionen laengst
        geschlossen sind.

        Bewusst KEIN Aenderung an KillSwitch selbst (dessen Trigger-
        Verhalten, Redis-Persistenz, Pub/Sub-Sync und Tenant-Scoping
        bleiben exakt wie zuvor, siehe sgr/risk/kill_switch.py) - dieser
        Watchdog ruft nach jedem regulaeren Tick nur zusaetzlich dessen
        bereits bestehende reset()-Methode auf, wenn:
          1. der Kill Switch aktiv ist, UND
          2. der Tenant aktuell 0 offene Positionen haelt (alle
             Positionen der ausloesenden Batch sind bereits ueber den
             normalen SL/TP/Max-Holding-Pfad oben geschlossen), UND
          3. fuer GENAU DIESEN Trigger noch kein Auto-Recovery
             stattgefunden hat (Idempotenz).

        Hard-Refuse fuer LIVE (Anforderung 9): trading_mode wird explizit
        geprueft, nicht nur vom Aufrufer vorausgesetzt - ein
        Konfigurationsfehler (versehentlich fuer einen LIVE-Worker
        aufgerufen) darf dieses Feature niemals aktivieren, komplett
        unabhaengig davon, wie der Aufrufer (main.py) selbst entscheidet.
        """
        if trading_mode != TradingMode.PAPER:
            log.error(
                "position_protection_watchdog.paper_stress_auto_recovery_refused_non_paper",
                trading_mode=trading_mode.value,
                tenant_id=tenant_id,
            )
            return

        self._kill_switch = kill_switch
        self._auto_recovery_tenant_id = tenant_id
        log.warning(
            "position_protection_watchdog.paper_stress_auto_recovery_enabled",
            tenant_id=tenant_id,
            trading_mode=trading_mode.value,
        )

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
                if self._kill_switch is not None:
                    try:
                        await self._maybe_paper_stress_auto_recover()
                    except Exception as e:
                        log.error(
                            "position_protection_watchdog.paper_stress_auto_recovery_tick_failed",
                            error=str(e),
                            exc_info=True,
                        )
        except asyncio.CancelledError:
            raise

    async def _maybe_paper_stress_auto_recover(self) -> None:
        """
        Ein einzelner Auto-Recovery-Check (siehe
        enable_paper_stress_auto_recovery() Docstring). Oeffentlich
        erreichbar ueber check_positions_once() hinaus fuer Tests
        (direkter Aufruf ohne den Sleep-Loop nachzubilden, identisches
        Muster wie check_positions_once() selbst).

        Laeuft immer NACH check_positions_once() in _run_loop() (siehe
        oben) - zum Zeitpunkt dieses Aufrufs sind alle SL/TP/Max-Holding-
        Exits dieses Ticks bereits vollstaendig abgeschlossen (sequenziell,
        kein asyncio.gather - siehe check_positions_once() Docstring),
        die Positionsanzahl hier ist daher konsistent, kein Race.
        """
        ks = self._kill_switch
        if ks is None or not ks.is_active:
            return

        if self._portfolio.positions:
            # Noch offene Positionen aus der ausloesenden Batch (oder
            # einer neueren) - NICHT zuruecksetzen (Anforderung 5).
            return

        state = ks.state
        trigger_key = state.triggered_at.isoformat() if state.triggered_at else None
        if trigger_key is not None and trigger_key == self._last_auto_recovered_trigger_at:
            # Idempotenz (Anforderung 13): dieser exakte Trigger wurde
            # bereits automatisch zurueckgesetzt - kein zweiter
            # reset()-Aufruf/Log-Eintrag.
            return

        previous_reason = state.reason
        previous_triggered_at = state.triggered_at
        positions_closed = self._positions_closed_since_recovery

        await ks.reset(reset_by="paper_stress_auto_recovery")

        self._last_auto_recovered_trigger_at = trigger_key
        self._positions_closed_since_recovery = 0

        log.warning(
            "position_protection_watchdog.paper_stress_auto_recovery_reset",
            tenant_id=self._auto_recovery_tenant_id,
            previous_reason=previous_reason,
            previous_triggered_at=(
                previous_triggered_at.isoformat() if previous_triggered_at else None
            ),
            positions_closed=positions_closed,
            reset_at=datetime.now(tz=UTC).isoformat(),
            reset_reason="paper_stress_auto_recovery",
        )

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
        """
        Explizite Exit-Prioritaet (2026-09-23, explizite operative
        Anweisung - portiert die bereits im BacktestSimulator vorhandene
        Reihenfolge, siehe dortiger _check_exits()-Docstring, in den
        Live/Paper-Pfad, statt sie neu zu erfinden):

            1. Take Profit
            2. Stop Loss
            3. Regime-Exit (nur fuer dafuer vorgesehene Strategien/
               Positionen - entry_regime == RANGING - siehe
               _check_regime_exit() Docstring; seit 2026-09-23 live
               implementiert ueber Position.entry_regime + FeatureStore.
               get_latest_regime())
            4. ATR/Trailing-Exit - deckt sich hier vollstaendig mit dem
               Stop-Loss-Check in Schritt 2: wenn eine Strategie einen
               ATR-basierten stop_price liefert (siehe
               PositionProtectionManager._extract_strategy_prices()), IST
               das bereits der in Schritt 2 gepruefte stop_loss_price -
               kein separater dritter Preis-Check noetig.
            5. Max-Holding-Time - AUSSCHLIESSLICH als letzter Fallback,
               nicht mehr das dominante Exit-Verhalten (siehe
               Analysebericht: im heutigen Stress-Test verliess praktisch
               jede Position ueber diesen Pfad, nie ueber SL/TP - genau
               das Gegenteil des gewuenschten Verhaltens).

        Erster Treffer gewinnt, wie im Simulator.
        """
        exit_reason = self._check_take_profit(position)
        if exit_reason is None:
            exit_reason = self._check_stop_loss(position)
        if exit_reason is None:
            exit_reason = await self._check_regime_exit(position)
        if exit_reason is None:
            exit_reason = self._check_max_holding_time(position, now)

        if exit_reason is None:
            return

        await self._close_position(position, exit_reason)

    @staticmethod
    def _check_take_profit(position: Position) -> ExitReason | None:
        price = position.current_price
        if position.take_profit_price is None:
            return None
        if position.side == PositionSide.LONG:
            if price >= position.take_profit_price:
                return ExitReason.TAKE_PROFIT
        else:
            if price <= position.take_profit_price:
                return ExitReason.TAKE_PROFIT
        return None

    @staticmethod
    def _check_stop_loss(position: Position) -> ExitReason | None:
        price = position.current_price
        if position.stop_loss_price is None:
            return None
        if position.side == PositionSide.LONG:
            if price <= position.stop_loss_price:
                return ExitReason.STOP_LOSS
        else:
            if price >= position.stop_loss_price:
                return ExitReason.STOP_LOSS
        return None

    async def _check_regime_exit(self, position: Position) -> ExitReason | None:
        """
        Live-Regime-Exit (2026-09-23, schliesst die zuvor dokumentierte
        Luecke: "Entry-Regime nicht gespeichert" + "kein Zugriff auf ein
        aktuelles Regime" - siehe Position.entry_regime, Migration 0008,
        und FeatureStore.save_regime()/get_latest_regime()).

        Portiert exakt das Backtest-Vorbild (BacktestSimulator._check_exits():
        Exit, wenn eine im RANGING-Regime eroeffnete Position nicht mehr
        im RANGING-Regime ist) - KEINE neue Regime-Logik, nur derselbe
        Vergleich gegen bereits existierende, bereits an anderer Stelle
        berechnete Werte (Position.entry_regime, FeatureStore.
        get_latest_regime()).

        Nur fuer Strategien/Positionen relevant, bei denen Regime-Exit
        fachlich Sinn ergibt (Mean-Reversion/Ranging-basiert, siehe
        Analysebericht) - operationalisiert als "entry_regime ==
        RANGING". Fuer trend_following_v1/momentum_v1/
        volatility_adjusted_momentum_v1 (kein RANGING-Entry) ist dieser
        Check dadurch strukturell ein No-Op, ohne eine explizite
        Strategie-Namensliste pflegen zu muessen.

        Fail-safe/keine stille Fallback-Logik: fehlen feature_store,
        entry_regime oder ein aktuell bekanntes Regime, wird KEIN Exit
        ausgeloest (nicht: "als ob unveraendert" vorgetaeuscht) - eine
        fehlende Information darf niemals einen Exit erzwingen. Jeder
        dieser Faelle ist explizit sichtbar (Log), nicht stillschweigend
        verschluckt.
        """
        if self._feature_store is None:
            return None
        if position.entry_regime is None:
            return None
        if position.entry_regime != MarketRegime.RANGING:
            return None

        symbol_key = f"{position.symbol.exchange.value}:{position.symbol.ccxt_symbol}"
        try:
            current_regime = await self._feature_store.get_latest_regime(
                symbol_key, self._regime_check_timeframe
            )
        except Exception as e:
            log.warning(
                "position_protection_watchdog.regime_exit_lookup_failed",
                symbol=str(position.symbol),
                error=str(e),
            )
            return None

        if current_regime is None or current_regime == MarketRegime.UNKNOWN:
            log.debug(
                "position_protection_watchdog.regime_exit_no_current_regime",
                symbol=str(position.symbol),
            )
            return None

        if current_regime != MarketRegime.RANGING:
            log.info(
                "position_protection_watchdog.regime_exit_triggered",
                symbol=str(position.symbol),
                entry_regime=position.entry_regime.value,
                current_regime=current_regime.value,
            )
            return ExitReason.REGIME_CHANGE

        return None

    @staticmethod
    def _check_max_holding_time(position: Position, now: datetime) -> ExitReason | None:
        max_holding_until = position.max_holding_until
        if max_holding_until is None:
            return None
        if max_holding_until.tzinfo is None:
            max_holding_until = max_holding_until.replace(tzinfo=UTC)
        if now >= max_holding_until:
            return ExitReason.MAX_HOLDING_TIME
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
        #
        # PARTIALLY_FILLED zaehlt bewusst mit (Root-Cause-Fund, siehe
        # PortfolioEngine.on_order_filled() Docstring): ein SL/TP/Max-
        # Holding-Exit, der nur teilweise gefuellt wird (Timeout, Rest
        # storniert), reduziert die Position trotzdem tatsaechlich real
        # auf der Exchange - das muss verfolgt werden, nicht verworfen.
        if result.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            await self._portfolio.on_order_filled(result)
            if self._kill_switch is not None:
                # Nur fuer das Auto-Recovery-Audit-Log gezaehlt (siehe
                # enable_paper_stress_auto_recovery()) - kein Effekt,
                # wenn das Feature nicht aktiviert ist.
                self._positions_closed_since_recovery += 1
