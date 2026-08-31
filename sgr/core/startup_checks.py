"""
SGR Startup Safety Checks
=========================
Fail-fast Prüfungen, die unmittelbar nach dem Logging-Setup laufen, bevor
irgendeine Verbindung (DB, Redis, Exchange) aufgebaut wird.

Ziel: Live Trading darf niemals durch eine unvollständige, inkonsistente
oder versehentliche Konfiguration starten. Ein Fehler hier verhindert den
kompletten Boot (raise StartupSafetyError), statt den Server mit einer
unsicheren Konfiguration laufen zu lassen.

Abgrenzung zu bestehenden Mechanismen:
    - SGRConfig.validate_production_constraints() prüft bereits Secret-
      Defaults und Debug-Flag bei PRODUCTION+LIVE (Pydantic-Validator,
      läuft implizit beim Config-Load). Diese Checks hier laufen
      zusätzlich, explizit im Lifespan, und decken andere Aspekte ab:
      Exchange-Credentials-Vollständigkeit, Kill-Switch-Ausgangszustand,
      Risk-Limit-Plausibilität. Kein Duplikat, keine Überschneidung.
    - Der confirm_live-Schutz in api/routers/trading.py schützt den
      manuellen Trading-Cycle-Endpoint pro Request. Diese Checks hier
      schützen den Boot selbst - unabhängig davon, ob je ein Request
      gestellt wird.

Designentscheidung: Fail-Fast statt Fail-Safe
    Anders als z.B. Crash Recovery (fail-safe: unvollständige Recovery
    ist besser als kein Server) sind Startup Safety Checks bewusst
    fail-fast: eine unsichere Live-Konfiguration darf den Server gar
    nicht erst starten lassen. Ein nicht hochfahrender Server ist immer
    sicherer als ein Server, der mit deaktivierten Schutzmechanismen
    Live-Trades ausführt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sgr.core.config import SGRConfig
from sgr.core.logging import get_logger
from sgr.core.types import ExchangeID, TradingMode
from sgr.risk.kill_switch import get_kill_switch

log = get_logger(__name__)


class StartupSafetyError(RuntimeError):
    """
    Wird geworfen, wenn eine kritische Startup-Bedingung verletzt ist.
    Muss den Boot-Prozess stoppen - niemals abfangen und ignorieren.
    """


@dataclass
class StartupCheckResult:
    """Ergebnis eines einzelnen Checks (für Logging/Audit)."""

    name: str
    passed: bool
    detail: str


@dataclass
class StartupSafetyReport:
    """Gesamtergebnis aller Startup Safety Checks."""

    trading_mode: TradingMode
    checks: list[StartupCheckResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[StartupCheckResult]:
        return [c for c in self.checks if not c.passed]


class StartupSafetyChecker:
    """
    Führt alle Startup Safety Checks gegen die geladene Config aus.

    Usage:
        checker = StartupSafetyChecker(config)
        report = checker.run()
        if not report.all_passed:
            raise StartupSafetyError(...)  # wird von run_or_raise() erledigt
    """

    def __init__(self, config: SGRConfig) -> None:
        self._config = config

    def run(self) -> StartupSafetyReport:
        """Führt alle Checks aus, unabhängig vom Ergebnis einzelner Checks
        (damit ein vollständiger Report entsteht statt beim ersten Fehler
        abzubrechen - besser für Debugging einer fehlgeschlagenen Config)."""
        report = StartupSafetyReport(trading_mode=self._config.trading_mode)

        report.checks.append(self._check_kill_switch_not_preactivated())

        if self._config.trading_mode == TradingMode.LIVE:
            report.checks.append(self._check_live_credentials_present())
            report.checks.append(self._check_risk_limits_not_disabled())
            report.checks.append(self._check_max_leverage_sane())
            report.checks.append(self._check_max_order_notional_sane())

        return report

    def run_or_raise(self) -> StartupSafetyReport:
        """Führt run() aus und wirft StartupSafetyError bei Fehlern.
        Loggt jeden einzelnen Check (Audit-Trail für Boot-Entscheidungen)."""
        report = self.run()

        for check in report.checks:
            log_fn = log.info if check.passed else log.error
            log_fn(
                "sgr.startup_check",
                name=check.name,
                passed=check.passed,
                detail=check.detail,
                trading_mode=report.trading_mode.value,
            )

        if not report.all_passed:
            reasons = "; ".join(f"{c.name}: {c.detail}" for c in report.failures)
            raise StartupSafetyError(
                f"Startup safety checks failed ({len(report.failures)} of "
                f"{len(report.checks)}): {reasons}"
            )

        log.info(
            "sgr.startup_checks_passed",
            count=len(report.checks),
            trading_mode=report.trading_mode.value,
        )
        return report

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_kill_switch_not_preactivated(self) -> StartupCheckResult:
        """
        Der Kill Switch für den aktiven Trading Mode darf beim Boot nicht
        bereits ausgelöst sein. Das wäre nur bei einem In-Memory-State aus
        einem vorherigen Import in derselben Prozesslaufzeit möglich (z.B.
        Test-Leck oder Reimport) - aber genau deshalb explizit geprüft:
        ein stiller, unbemerkter "getriggerter" Zustand beim Start wäre
        verwirrend und würde jeden Trade sofort ablehnen, ohne dass der
        Betreiber merkt warum.
        """
        kill_switch = get_kill_switch(self._config.trading_mode)
        if kill_switch.is_active:
            return StartupCheckResult(
                name="kill_switch_not_preactivated",
                passed=False,
                detail=(
                    f"Kill switch for {self._config.trading_mode.value} is "
                    "already active at startup - refusing to boot with a "
                    "silently disabled trading path"
                ),
            )
        return StartupCheckResult(
            name="kill_switch_not_preactivated",
            passed=True,
            detail="Kill switch inactive",
        )

    def _check_live_credentials_present(self) -> StartupCheckResult:
        """
        LIVE Mode: Pionex-Live-Credentials müssen vollständig konfiguriert
        sein. Nutzt die bestehende, bereits getestete
        ExchangeCredentials.get_credentials()-Validierung wieder, statt sie
        zu duplizieren - schlägt hier früh und explizit fehl statt erst
        beim ersten Exchange-Connect-Versuch mitten im Boot.
        """
        try:
            self._config.credentials.get_credentials(
                ExchangeID.PIONEX.value, TradingMode.LIVE
            )
        except ValueError as e:
            return StartupCheckResult(
                name="live_credentials_present",
                passed=False,
                detail=str(e),
            )
        return StartupCheckResult(
            name="live_credentials_present",
            passed=True,
            detail="Pionex live credentials configured",
        )

    def _check_risk_limits_not_disabled(self) -> StartupCheckResult:
        """
        LIVE Mode: die wichtigsten Hard Limits dürfen nicht auf einen Wert
        stehen, der sie faktisch wirkungslos macht. RiskLimitsConfig
        erzwingt bereits sinnvolle Wertebereiche per Pydantic Field(ge=,
        le=), diese Prüfung schützt zusätzlich davor, dass jemand die
        Limits absichtlich oder versehentlich an den oberen Rand des
        erlaubten Bereichs setzt und damit Live Trading faktisch
        ungebremst laufen lässt.
        """
        limits = self._config.risk_limits
        problems: list[str] = []

        if limits.max_portfolio_drawdown >= 0.50:
            problems.append(
                f"max_portfolio_drawdown={limits.max_portfolio_drawdown:.0%} too permissive"
            )
        if limits.daily_loss_limit >= 0.20:
            problems.append(f"daily_loss_limit={limits.daily_loss_limit:.0%} too permissive")

        if problems:
            return StartupCheckResult(
                name="risk_limits_not_disabled",
                passed=False,
                detail="; ".join(problems),
            )
        return StartupCheckResult(
            name="risk_limits_not_disabled",
            passed=True,
            detail="Hard limits within sane bounds",
        )

    def _check_max_leverage_sane(self) -> StartupCheckResult:
        """LIVE Mode: max_leverage darf keinen absurd hohen Wert haben, der
        den in Baustein 2 hinzugefügten Leverage Guard faktisch nutzlos
        macht."""
        max_leverage = self._config.risk_limits.max_leverage
        if max_leverage > Decimal("10"):
            return StartupCheckResult(
                name="max_leverage_sane",
                passed=False,
                detail=f"max_leverage={max_leverage} exceeds sane ceiling of 10x for live trading",
            )
        return StartupCheckResult(
            name="max_leverage_sane",
            passed=True,
            detail=f"max_leverage={max_leverage} within sane bounds",
        )

    def _check_max_order_notional_sane(self) -> StartupCheckResult:
        """
        LIVE Mode: max_order_notional (Baustein 4) sollte in LIVE nicht
        deaktiviert (None) sein - ein fehlender absoluter Order-Size-Cap
        wäre in Live Trading ein direkter Fat-Finger-Schutz weniger, ohne
        dass der Betreiber es beim Start bemerken würde.
        """
        max_order_notional = self._config.risk_limits.max_order_notional
        if max_order_notional is None:
            return StartupCheckResult(
                name="max_order_notional_sane",
                passed=False,
                detail=(
                    "max_order_notional is disabled (None) in LIVE mode - "
                    "no absolute per-order fat-finger protection configured"
                ),
            )
        return StartupCheckResult(
            name="max_order_notional_sane",
            passed=True,
            detail=f"max_order_notional={max_order_notional} configured",
        )
