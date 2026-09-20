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
    ) -> GridRiskAssessment:
        """
        Prueft, ob ein neues Grid mit `parameters` eroeffnet werden darf.
        Reduziert NIEMALS automatisch auf eine groessere Position (siehe
        sgr/execution/quantization.py Prinzip) - lehnt stattdessen ab und
        schlaegt im Warnungs-Text eine sicherere Alternative vor.
        """
        try:
            return self._evaluate_internal(
                parameters,
                snapshot,
                current_price,
                liquidity_usd,
                funding_rate_annualized_pct,
                volatility_atr_pct,
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

        return GridRiskAssessment(approved=True, warnings=warnings)

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
