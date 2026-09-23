"""
SGR Futures Grid Risk Engine
==============================
Eigenes Risikoprofil fuer Futures Grid (siehe Aufgabenstellung: "Die Risk
Engine muss Futures Grid als eigenes Risikoprofil verstehen"). Ergaenzt
- ersetzt nicht - die bestehende sgr.risk.engine.RiskEngine: jede
tatsaechliche Order, die aus einem genehmigten Grid entsteht, durchlaeuft
weiterhin den unveraenderten ExecutionEngine/OrderSafety/Preflight/
KillSwitch-Pfad (siehe sgr/execution/grid_controller.py).

Zwei Pruefzeitpunkte:
    1. evaluate_new_grid()   - VOR Eroeffnung eines neuen Grids: darf
                               dieses Grid mit diesen Parametern ueberhaupt
                               gestartet werden?
    2. check_ongoing_grid()  - waehrend das Grid laeuft (jeder Monitoring-
                               Zyklus, siehe GridController.monitor_grids()):
                               muss es aufgrund veraenderter Bedingungen
                               (Funding, Liquidationsdistanz, Volatilitaet,
                               Liquiditaet) geschlossen/pausiert werden?

Fail-Safe-Prinzip identisch zu sgr.risk.engine.RiskEngine: bei einem
internen Fehler wird REJECTED/geschlossen, nie stillschweigend erlaubt.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from sgr.core.grid_types import FuturesGridParameters, GridRiskAssessment, GridState, GridViolation
from sgr.core.logging import get_logger
from sgr.core.types import GridDirection

log = get_logger(__name__)


class GridRiskLimitsConfig(BaseSettings):
    """
    Tenant-/Deployment-weite Grid-Risikolimits. Eigene Konfigurationsklasse
    (statt Erweiterung von RiskLimitsConfig) - Grid-Limits betreffen eine
    fundamental andere Groessenordnung (viele kleine Orders statt einer
    Position) und sollen unabhaengig von den bestehenden Directional-
    Risk-Limits konfigurierbar sein, ohne deren Defaults/Env-Var-Namen zu
    veraendern (Nullrisiko fuer bestehende Deployments).
    """

    model_config = SettingsConfigDict(env_prefix="GRID_RISK_", extra="ignore")

    # Absolute Kapitalbindungs-Grenzen (Quote-Currency, z.B. USDT)
    max_grid_exposure_usd: Decimal = Field(default=Decimal("500"), gt=0)
    max_grid_position_usd: Decimal = Field(default=Decimal("2000"), gt=0)
    max_open_grids: int = Field(default=3, ge=1, le=50)
    max_grid_orders: int = Field(default=20, ge=2, le=200)
    max_grid_loss_usd: Decimal = Field(default=Decimal("100"), gt=0)

    # Kosten-/Marktqualitaets-Limits
    max_funding_cost_pct: float = Field(default=0.0008, ge=0.0, le=0.01)
    max_liquidation_distance_pct: float = Field(default=0.15, ge=0.01, le=0.9)
    max_leverage: Decimal = Field(default=Decimal("5"), ge=Decimal("1"))
    min_liquidity_usd: Decimal = Field(default=Decimal("100000"), ge=0)
    max_volatility_atr_pct: float = Field(default=0.08, ge=0.0, le=1.0)

    # Grid-Cost-Guard (2026-09-23, Phase F - Architekturbericht "Futures-
    # Grid-Strategie fuer SGR", Abschnitt E): Sicherheitsmarge, mit der
    # der erwartete Bruttoertrag pro Grid-Zyklus (grid_spacing) die
    # Round-Trip-Kosten uebersteigen muss, BEVOR ein Grid ueberhaupt
    # eroeffnet werden darf. 1.5 = Grid-Spacing muss mindestens das
    # 1.5-fache der geschaetzten Kosten betragen - bewusst kein fixer
    # globaler TP-Wert (siehe _check_cost_guard() Docstring), nur diese
    # eine Sicherheitsmarge ist konfigurierbar.
    cost_guard_safety_margin: Decimal = Field(default=Decimal("1.5"), ge=Decimal("1"))
    # Ab welchem Anteil des Grid-Zyklus-Ertrags (grid_spacing, relativ)
    # die GESCHAETZTE Funding-Kosten EINES einzelnen Intervalls (8h,
    # Binance-Standard) als "die Edge signifikant verschlechternd" gilt
    # und das Grid ablehnt. 0.5 = Funding-Kosten pro Intervall duerfen
    # nicht mehr als 50% des (bereits sicherheitsmarge-bereinigten)
    # Grid-Zyklus-Ertrags auffressen.
    max_funding_share_of_cycle_edge: float = Field(default=0.5, ge=0.0, le=1.0)

    # Kombinierte Kapital-Obergrenze ueber Grid- UND direktionale
    # RiskEngine-Exposure (2026-09-23, Phase L - explizite Anweisung:
    # "Ein Tenant darf nicht durch max_open_positions + max_open_grids
    # unbeabsichtigt eine unkontrollierte Gesamt-Exposure erzeugen").
    # None (Default) = deaktiviert, KEINE Verhaltensaenderung fuer
    # bestehende Deployments (Gordon/Sumo) - rein opt-in. Bewusst
    # NOTIONAL-basiert (nicht "Anzahl Positionen + Anzahl Grids addiert",
    # was ohne Kapitalbezug waere, siehe Aufgabenstellung "Keine
    # pauschale Addition ohne Kapitalbezug") - siehe evaluate_new_grid()
    # directional_exposure_usd-Parameter.
    max_combined_exposure_usd: Decimal | None = Field(default=None, gt=0)

    # Breakout-Schutz (2026-09-23, Phase O - Backtest-Live-Konsistenz):
    # GridBacktestSimulator schliesst ein Grid zwangsweise, wenn der Preis
    # mehr als diesen Faktor der urspruenglichen Range-Breite ausserhalb
    # von [grid_lower_price, grid_upper_price] gehandelt wird (siehe
    # sgr/backtesting/grid_types.py::GridBacktestConfig.
    # range_breakout_buffer_factor). Diese Pruefung existierte bisher NUR
    # im Backtest, nicht im Live-Pfad - identischer Default (0.5) wie dort,
    # damit ein im Backtest validiertes Grid im Live-/Paper-Betrieb
    # nachweislich denselben Schutz erfaehrt (siehe check_ongoing_grid()).
    range_breakout_buffer_factor: float = Field(default=0.5, ge=0.0, le=5.0)


@dataclass
class GridPortfolioSnapshot:
    """Momentaufnahme aller aktuell offenen Grids eines Tenants -
    Eingabe fuer evaluate_new_grid() (Portfolio-weite Limits)."""

    open_grids: list[GridState]
    portfolio_value: Decimal


class GridRiskEngine:
    """Stateless - alle benoetigten Zahlen werden explizit uebergeben
    (analog zum Stil von sgr.risk.position_sizer.PositionSizer)."""

    def __init__(self, limits: GridRiskLimitsConfig | None = None) -> None:
        self._limits = limits or GridRiskLimitsConfig()

    @property
    def limits(self) -> GridRiskLimitsConfig:
        return self._limits

    def evaluate_new_grid(
        self,
        parameters: FuturesGridParameters,
        snapshot: GridPortfolioSnapshot,
        current_price: Decimal,
        liquidity_usd: Decimal | None = None,
        funding_rate_annualized_pct: float | None = None,
        volatility_atr_pct: float | None = None,
        directional_exposure_usd: Decimal | None = None,
    ) -> GridRiskAssessment:
        """
        Prueft, ob ein neues Grid mit `parameters` eroeffnet werden darf.
        Reduziert NIEMALS automatisch auf eine groessere Position (siehe
        sgr/execution/quantization.py Prinzip) - lehnt stattdessen ab und
        schlaegt im Warnungs-Text eine sicherere Alternative vor.

        directional_exposure_usd (Phase 9, 2026-09-24, revidiert nach
        kritischer Pruefung): aktuelle Notional-Exposure der DIREKTIONALEN
        Strategien desselben Tenants (aus PortfolioEngine, siehe
        GridScheduler._directional_exposure_usd()). None (Default) bedeutet
        AUSDRUECKLICH "nicht zuverlaessig bestimmbar", NICHT "Exposure ist
        0" - ein frueherer Default von Decimal("0") haette bei aktiviertem
        max_combined_exposure_usd faelschlich einen sicheren Zustand
        vorgetaeuscht (Fail-Open-Bug), obwohl die tatsaechliche Exposure
        schlicht unbekannt war. Siehe Check 5b unten: nur relevant, wenn
        max_combined_exposure_usd ueberhaupt konfiguriert ist (Default
        None = Feature deaktiviert, dieser Parameter hat dann keinen
        Effekt).
        """
        try:
            return self._evaluate_internal(
                parameters,
                snapshot,
                current_price,
                liquidity_usd,
                funding_rate_annualized_pct,
                volatility_atr_pct,
                directional_exposure_usd,
            )
        except Exception as e:
            log.error("grid_risk_engine.evaluate.unexpected_error", error=str(e), exc_info=True)
            return GridRiskAssessment(approved=False, reason=f"Grid risk engine error: {e}")

    def _evaluate_internal(
        self,
        parameters: FuturesGridParameters,
        snapshot: GridPortfolioSnapshot,
        current_price: Decimal,
        liquidity_usd: Decimal | None,
        funding_rate_annualized_pct: float | None,
        volatility_atr_pct: float | None,
        directional_exposure_usd: Decimal | None = None,
    ) -> GridRiskAssessment:
        warnings: list[str] = []

        # 1. Maximum offener Grids (Portfolio-weit)
        active_grids = [g for g in snapshot.open_grids if g.is_active]
        if len(active_grids) >= self._limits.max_open_grids:
            return GridRiskAssessment(
                approved=False,
                reason=(
                    f"Maximum offener Grids erreicht ({len(active_grids)}/"
                    f"{self._limits.max_open_grids})"
                ),
            )

        # 2. Leverage-Limit
        if parameters.leverage > self._limits.max_leverage:
            return GridRiskAssessment(
                approved=False,
                reason=(
                    f"Leverage {parameters.leverage}x ueberschreitet Grid-Limit "
                    f"{self._limits.max_leverage}x"
                ),
            )

        # 3. Maximum Grid Orders
        if parameters.grid_count > self._limits.max_grid_orders:
            return GridRiskAssessment(
                approved=False,
                reason=(
                    f"grid_count {parameters.grid_count} ueberschreitet max_grid_orders "
                    f"{self._limits.max_grid_orders}"
                ),
            )

        # 4. Kapitalbindung dieses EINEN Grids
        this_grid_notional = parameters.total_notional()
        if this_grid_notional > self._limits.max_grid_position_usd:
            return GridRiskAssessment(
                approved=False,
                reason=(
                    f"Grid-Notional {this_grid_notional} ueberschreitet "
                    f"max_grid_position_usd {self._limits.max_grid_position_usd}"
                ),
            )

        # 5. Portfolio-weite Grid-Exposure (Summe aller offenen Grids +
        # dieses neuen Grids)
        existing_exposure = sum(
            (Decimal(str(g.parameters.get("total_notional", 0))) for g in active_grids),
            Decimal("0"),
        )
        total_exposure = existing_exposure + this_grid_notional
        if total_exposure > self._limits.max_grid_exposure_usd:
            return GridRiskAssessment(
                approved=False,
                reason=(
                    f"Portfolio-weite Grid-Exposure {total_exposure} wuerde "
                    f"max_grid_exposure_usd {self._limits.max_grid_exposure_usd} ueberschreiten "
                    f"(bereits gebunden: {existing_exposure})"
                ),
            )

        # 5b. Kombinierte Kapital-Obergrenze ueber Grid- UND direktionale
        # Exposure (Phase 9) - nur aktiv, wenn max_combined_exposure_usd
        # konfiguriert ist (opt-in, siehe Feld-Docstring). Notional-
        # basiert, keine pauschale Zaehl-Addition. FAIL-CLOSED: wenn das
        # Limit konfiguriert ist, aber die direktionale Exposure nicht
        # zuverlaessig bestimmbar war (directional_exposure_usd is None),
        # wird abgelehnt - niemals stillschweigend als 0 angenommen (siehe
        # evaluate_new_grid() Docstring).
        if self._limits.max_combined_exposure_usd is not None:
            if directional_exposure_usd is None:
                return GridRiskAssessment(
                    approved=False,
                    reason=(
                        "max_combined_exposure_usd ist konfiguriert, aber die direktionale "
                        "Exposure konnte nicht zuverlaessig bestimmt werden (kein "
                        "PortfolioEngine-Zugriff im Aufrufer) - fail-closed, kein Default 0"
                    ),
                )
            combined_exposure = total_exposure + directional_exposure_usd
            if combined_exposure > self._limits.max_combined_exposure_usd:
                return GridRiskAssessment(
                    approved=False,
                    reason=(
                        f"Kombinierte Exposure (Grid {total_exposure} + direktional "
                        f"{directional_exposure_usd} = {combined_exposure}) wuerde "
                        f"max_combined_exposure_usd {self._limits.max_combined_exposure_usd} "
                        f"ueberschreiten"
                    ),
                )

        # 6. Liquiditaet
        if liquidity_usd is not None and liquidity_usd < self._limits.min_liquidity_usd:
            return GridRiskAssessment(
                approved=False,
                reason=(
                    f"Geschaetzte Liquiditaet {liquidity_usd} unter Mindestschwelle "
                    f"{self._limits.min_liquidity_usd} - Grid deaktiviert (illiquider Markt)"
                ),
            )

        # 7. Funding Cost
        if funding_rate_annualized_pct is not None:
            max_annualized_pct = self._limits.max_funding_cost_pct * 100 * 3 * 365
            if abs(funding_rate_annualized_pct) > max_annualized_pct:
                return GridRiskAssessment(
                    approved=False,
                    reason=(
                        f"Annualisierte Funding Rate {funding_rate_annualized_pct:.1f}% "
                        f"ueberschreitet Grid-Limit {max_annualized_pct:.1f}%"
                    ),
                )
            elif abs(funding_rate_annualized_pct) > max_annualized_pct * 0.7:
                warnings.append(
                    f"Funding Rate {funding_rate_annualized_pct:.1f}% naeher am Limit"
                )

        # 8. Volatilitaet
        max_volatility = self._limits.max_volatility_atr_pct
        if volatility_atr_pct is not None and volatility_atr_pct > max_volatility:
            return GridRiskAssessment(
                approved=False,
                reason=(
                    f"ATR% {volatility_atr_pct:.2%} ueberschreitet Grid-Volatilitaetslimit "
                    f"{self._limits.max_volatility_atr_pct:.2%} - Grid deaktiviert "
                    f"(extreme Volatilitaet)"
                ),
            )

        # 9. Liquidationsdistanz (Naeherung wie im Backtest-Simulator:
        # avg_entry * (1 -/+ 1/leverage))
        liq_distance_pct = float(Decimal("1") / parameters.leverage)
        if liq_distance_pct < self._limits.max_liquidation_distance_pct:
            return GridRiskAssessment(
                approved=False,
                reason=(
                    f"Liquidationsdistanz {liq_distance_pct:.1%} bei Leverage "
                    f"{parameters.leverage}x unter Mindestabstand "
                    f"{self._limits.max_liquidation_distance_pct:.1%}"
                ),
            )

        # 10. Grid-Cost-Guard (Phase F): siehe _check_cost_guard() Docstring.
        cost_guard_reason = self._check_cost_guard(
            parameters, current_price, funding_rate_annualized_pct
        )
        if cost_guard_reason is not None:
            return GridRiskAssessment(approved=False, reason=cost_guard_reason)

        return GridRiskAssessment(approved=True, warnings=warnings)

    def _check_cost_guard(
        self,
        parameters: FuturesGridParameters,
        current_price: Decimal,
        funding_rate_annualized_pct: float | None,
    ) -> str | None:
        """
        Grid-spezifischer Cost Guard (2026-09-23, explizite Anweisung:
        "NICHT den Single-Trade-Cost-Guard kopieren" - siehe
        sgr/risk/position_protection.py::PositionProtectionManager.
        _apply_cost_guard() fuer den direktionalen Guard, dessen
        Reaktion "auf Kostenschwelle hochklammern" fuer Grid NICHT passt:
        ein Grid hat keinen einzelnen TP-Preis, den man verschieben
        koennte, ohne den kompletten, bereits validierten Level-Plan neu
        zu berechnen - eine automatische Parameter-Mutation waere eine
        versteckte Aenderung der vom Nutzer/der Validierungspipeline
        bestaetigten Grid-Parameter (explizit verboten). Dieser Guard
        LEHNT daher ab, statt zu klammern.

        Rechnet AUSSCHLIESSLICH mit tatsaechlich konfigurierten Werten
        (RiskLimitsConfig.paper_maker_fee_pct/paper_taker_fee_pct/
        paper_slippage_pct) - keine hartkodierten Binance-/Pionex-Werte.

        Kostenmodell pro Grid-Zyklus (Open + Close eines einzelnen
        Levels):
            - Open-Fill: Maker-Fee (siehe Phase I - ein Level-Open ist
              ein per Crossing ausgeloester, wirtschaftlich ruhender
              Limit-Fill).
            - Close-Fill: bewusst KONSERVATIV mit der Taker-Fee gerechnet
              (nicht Maker) - ein Close kann sowohl ein normaler
              Crossing-Exit (Maker) als auch ein dringlicher Force-Exit
              (Taker, siehe GRID_FILL_TYPE_FORCE_EXIT) sein; der Guard
              darf nicht von der guenstigeren Annahme ausgehen.
            - Zusaetzlich 2x Slippage (Open + Close).
            - Multipliziert mit cost_guard_safety_margin (Default 1.5x) -
              die geforderte "Sicherheitsmarge oberhalb der Kosten".

        Der erwartete Bruttoertrag pro Zyklus ist das Grid-Spacing
        (effective_grid_spacing(), relativ zum aktuellen Preis) - IST
        dieser kleiner als die so berechnete Kostenschwelle, wird das
        Grid komplett abgelehnt (kein Teilzustand, keine automatische
        Neuberechnung).

        Funding: eine grobe, aber nicht ignorierte Schaetzung - die
        Funding-Kosten EINES einzelnen 8h-Intervalls (Binance-Standard,
        siehe GridBacktestSimulator.funding_interval_hours fuer die
        identische Annahme im Backtest) werden gegen einen Anteil des
        Zyklus-Ertrags geprueft (max_funding_share_of_cycle_edge) - ein
        Grid, dessen Funding-Kosten pro Intervall bereits einen grossen
        Teil des Edge pro Zyklus auffressen, wird abgelehnt, auch wenn
        die reinen Trade-Kosten fuer sich genommen gedeckt waeren.
        """
        if current_price <= 0:
            return None  # kann nicht sinnvoll bewertet werden, andere Checks greifen bereits

        from sgr.core.config import get_config

        limits = get_config().risk_limits
        maker_fee = Decimal(str(limits.paper_maker_fee_pct))
        taker_fee = Decimal(str(limits.paper_taker_fee_pct))
        slippage = Decimal(str(limits.paper_slippage_pct))

        round_trip_cost_pct = maker_fee + taker_fee + (Decimal("2") * slippage)
        min_required_cycle_pct = round_trip_cost_pct * self._limits.cost_guard_safety_margin

        grid_spacing_pct = parameters.effective_grid_spacing() / current_price

        if grid_spacing_pct < min_required_cycle_pct:
            return (
                f"Grid-Spacing {grid_spacing_pct:.4%} (relativ) unterschreitet die "
                f"Kostenschwelle inkl. Sicherheitsmarge {min_required_cycle_pct:.4%} "
                f"(Round-Trip-Kosten {round_trip_cost_pct:.4%} x "
                f"{self._limits.cost_guard_safety_margin} Sicherheitsmarge) - "
                f"jeder Grid-Zyklus waere nach Kosten voraussichtlich defizitaer"
            )

        if funding_rate_annualized_pct is not None:
            funding_per_interval_pct = Decimal(str(abs(funding_rate_annualized_pct))) / (
                Decimal("100") * Decimal("3") * Decimal("365")
            )
            max_funding_share = Decimal(str(self._limits.max_funding_share_of_cycle_edge))
            if funding_per_interval_pct > grid_spacing_pct * max_funding_share:
                return (
                    f"Geschaetzte Funding-Kosten pro Intervall {funding_per_interval_pct:.4%} "
                    f"uebersteigen {max_funding_share:.0%} des Grid-Zyklus-Ertrags "
                    f"({grid_spacing_pct:.4%}) - Edge wird durch Funding voraussichtlich "
                    f"aufgezehrt"
                )

        return None

    def check_ongoing_grid(
        self,
        grid: GridState,
        current_price: Decimal,
        funding_rate_annualized_pct: float | None = None,
        volatility_atr_pct: float | None = None,
    ) -> list[GridViolation]:
        """
        Laufzeit-Check fuer ein bereits aktives Grid (siehe
        GridController.monitor_grids()). Gibt eine Liste von Verletzungen
        zurueck (leer = alles im gruenen Bereich). "hard" Severity ->
        Aufrufer MUSS das Grid schliessen; "soft" -> nur Warnung/Metrik.
        """
        violations: list[GridViolation] = []

        params_dict = grid.parameters
        max_loss = self._limits.max_grid_loss_usd
        if grid.realized_pnl < -max_loss:
            violations.append(
                GridViolation(
                    code="max_grid_loss_exceeded",
                    message=f"Realized PnL {grid.realized_pnl} unterschreitet -{max_loss}",
                    severity="hard",
                )
            )

        stop_loss = params_dict.get("stop_loss")
        if stop_loss is not None:
            direction = grid.direction
            stop_loss_dec = Decimal(str(stop_loss))
            if direction == GridDirection.LONG and current_price <= stop_loss_dec:
                violations.append(
                    GridViolation(code="stop_loss_hit", message="Grid stop-loss erreicht")
                )
            elif direction == GridDirection.SHORT and current_price >= stop_loss_dec:
                violations.append(
                    GridViolation(code="stop_loss_hit", message="Grid stop-loss erreicht")
                )

        # Take-Profit (Root-Cause-Fund, Live-Verification-Anweisung
        # Grid-Checkliste): FuturesGridParameters.take_profit war seit
        # jeher definiert, in der Grid-Metadata persistiert und im
        # Report-/UI-Pfad sichtbar - wurde aber NIRGENDS tatsaechlich
        # ausgewertet (kein entsprechender Check existierte hier, anders
        # als stop_loss direkt darueber). Symmetrisch zum bestehenden
        # stop_loss-Check: Preis-basiertes Gesamt-Grid-Ziel, nicht mit
        # dem Pflicht-Feld verwechseln - "Gesamt-Grid-Ziel (Preis oder
        # PnL-Grenze, siehe Nutzung)" im Feld-Docstring wird hier als
        # Preis interpretiert (identische Semantik wie stop_loss).
        take_profit = params_dict.get("take_profit")
        if take_profit is not None:
            direction = grid.direction
            take_profit_dec = Decimal(str(take_profit))
            if direction == GridDirection.LONG and current_price >= take_profit_dec:
                violations.append(
                    GridViolation(code="take_profit_hit", message="Grid take-profit erreicht")
                )
            elif direction == GridDirection.SHORT and current_price <= take_profit_dec:
                violations.append(
                    GridViolation(code="take_profit_hit", message="Grid take-profit erreicht")
                )

        if (
            funding_rate_annualized_pct is not None
            and abs(funding_rate_annualized_pct)
            > self._limits.max_funding_cost_pct * 100 * 3 * 365
        ):
            violations.append(
                GridViolation(
                    code="funding_cost_limit_exceeded",
                    message=f"Funding Rate {funding_rate_annualized_pct:.1f}% ueber Limit",
                    severity="hard",
                )
            )

        max_volatility = self._limits.max_volatility_atr_pct
        if volatility_atr_pct is not None and volatility_atr_pct > max_volatility:
            violations.append(
                GridViolation(
                    code="volatility_limit_exceeded",
                    message=f"ATR% {volatility_atr_pct:.2%} ueber Grid-Volatilitaetslimit",
                    severity="hard",
                )
            )

        # Range-Breakout (Phase O - Backtest-Live-Konsistenz, siehe
        # range_breakout_buffer_factor Feld-Docstring): identische Logik
        # zu GridBacktestSimulator.run()'s breakout_lower/breakout_upper.
        grid_lower_raw = params_dict.get("grid_lower_price")
        grid_upper_raw = params_dict.get("grid_upper_price")
        if grid_lower_raw is not None and grid_upper_raw is not None:
            grid_lower = Decimal(str(grid_lower_raw))
            grid_upper = Decimal(str(grid_upper_raw))
            range_width = grid_upper - grid_lower
            buffer = range_width * Decimal(str(self._limits.range_breakout_buffer_factor))
            breakout_lower = grid_lower - buffer
            breakout_upper = grid_upper + buffer
            if current_price < breakout_lower or current_price > breakout_upper:
                violations.append(
                    GridViolation(
                        code="range_breakout",
                        message=(
                            f"Preis {current_price} liegt ausserhalb der Breakout-Grenzen "
                            f"[{breakout_lower}, {breakout_upper}] (Range [{grid_lower}, "
                            f"{grid_upper}] x {self._limits.range_breakout_buffer_factor})"
                        ),
                        severity="hard",
                    )
                )

        max_holding = params_dict.get("maximum_holding_time")
        if max_holding is not None:
            from datetime import UTC, datetime

            elapsed = (datetime.now(tz=UTC) - grid.opened_at).total_seconds()
            if elapsed >= float(max_holding):
                violations.append(
                    GridViolation(
                        code="max_holding_time_exceeded", message="Max Holding Time erreicht"
                    )
                )

        return violations


_default_engine: GridRiskEngine | None = None


def get_grid_risk_engine() -> GridRiskEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = GridRiskEngine()
    return _default_engine
