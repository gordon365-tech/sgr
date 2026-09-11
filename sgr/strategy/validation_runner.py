"""
SGR Strategy Validation Runner
===============================
Schliesst die Lücke zwischen der vorhandenen BacktestingEngine
(Backtest -> Walk-Forward -> Monte Carlo -> Go/No-Go Report) und der
StrategyRegistry (is_validated-Gate für Paper-Trading-Aktivierung,
siehe sgr/api/main.py Startup-Sequenz).

Vorher: registry.mark_validated() wurde ausschliesslich in Tests
aufgerufen. Kein Produktionscode-Pfad setzte jemals is_validated=True,
wodurch der Aktivierungs-Loop im Lifespan (main.py) strukturell immer
0 aktive Strategien ergab ("active_strategies: 0").

Dieser Runner führt für jede registrierte, noch nicht validierte
Strategie einen vollen Backtest + Walk-Forward gegen historische
Marktdaten aus (via BacktestingEngine.run_full_validation(), bereits
vorhanden und ungenutzt) und übersetzt das Ergebnis in einen
ValidationStatus, den er per registry.mark_validated() einträgt.

Bewusste Architekturentscheidung (siehe Rücksprache mit Gordon,
Session zu "active_strategies: 0"):
    ValidationStatus.can_go_live prüft normalerweise
    backtest_passed AND walk_forward_passed AND paper_trading_passed.
    paper_trading_passed hat aktuell KEINE Datenquelle (keine
    Performance-Zeitreihe für Paper-Betrieb in der DB, siehe
    StrategyRepository.update_performance() - nur ein Snapshot, keine
    Historie). Ziel dieser Phase ist es, überhaupt aktive Strategien
    im Paper-Betrieb zu bekommen. Deshalb wird paper_trading_passed
    hier bewusst als Platzhalter auf True gesetzt, bis eine echte
    Paper-Trading-Performance-Historie existiert (separates, späteres
    Gate). Das betrifft AUSSCHLIESSLICH die Paper-Trading-Aktivierung
    über dieses Gate - Live-Trading-Gates (live_approved) sind davon
    unberührt und bleiben strenger.

Fail-safe: Wenn kein Exchange Pool verbunden ist (z.B. fehlende
Credentials) oder keine historischen Daten geladen werden können,
bleibt die betroffene Strategie unvalidiert (is_validated=False) statt
den Startup zu blockieren. Dies ist ein Startup-Nebenschritt, kein
Safety-kritischer Pfad - Fehler werden geloggt, nicht propagiert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sgr.backtesting.engine import BacktestingEngine, FullValidationReport
from sgr.core.logging import get_logger
from sgr.core.types import ExchangeID
from sgr.strategy.base import ValidationStatus
from sgr.strategy.registry import StrategyRegistry

log = get_logger(__name__)

# Default-Validierungszeitraum: 180 Tage historische Daten sind der
# Kompromiss zwischen "genug Bars für einen aussagekräftigen
# Walk-Forward-Split (WalkForwardAnalyzer verlangt >= 200 Bars, siehe
# sgr/backtesting/validation.py)" und "vertretbare Startup-Dauer für
# einen Nebenschritt, der die API-Verfügbarkeit nicht blockieren darf".
DEFAULT_LOOKBACK_DAYS = 180
DEFAULT_TIMEFRAME = "1h"
DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT"]


@dataclass
class ValidationRunSummary:
    """Ergebnis eines vollständigen Validierungslaufs (für Logging/API)."""

    validated: list[str]
    skipped: list[str]
    failed: dict[str, str]


class StrategyValidationRunner:
    """
    Orchestriert Go-Live-Gate-1-Validierung (Backtest + Walk-Forward)
    für alle registrierten, noch nicht validierten Strategien und
    trägt das Ergebnis in die StrategyRegistry ein.

    Nutzt die bereits vorhandene BacktestingEngine - keine parallele
    Zweitimplementierung von Backtest-/Walk-Forward-Logik.
    """

    def __init__(
        self,
        exchange_pool: Any,
        exchange_id: ExchangeID = ExchangeID.PIONEX,
        symbols: list[str] | None = None,
        timeframe: str = DEFAULT_TIMEFRAME,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ) -> None:
        self._pool = exchange_pool
        self._exchange_id = exchange_id
        self._symbols = symbols or list(DEFAULT_SYMBOLS)
        self._timeframe = timeframe
        self._lookback_days = lookback_days
        self._engine = BacktestingEngine()

    async def validate_pending_strategies(self) -> ValidationRunSummary:
        """
        Führt Backtest + Walk-Forward für jede registrierte Strategie
        aus, die noch nicht validiert ist (entry.is_validated is False).
        Bereits validierte Strategien werden übersprungen (kein
        wiederholter Backtest bei jedem Neustart).
        """
        registry = StrategyRegistry.get()
        summary = ValidationRunSummary(validated=[], skipped=[], failed={})

        if self._pool is None or not getattr(self._pool, "_adapters", None):
            log.warning(
                "strategy_validation_runner.no_exchange_pool",
                note="Kein verbundener Exchange Pool - Strategien bleiben unvalidiert.",
            )
            summary.skipped = [
                name for name, entry in registry.get_all().items() if not entry.is_validated
            ]
            return summary

        pending = [
            name for name, entry in registry.get_all().items() if not entry.is_validated
        ]

        if not pending:
            return summary

        end_date = datetime.now(tz=UTC)
        start_date = end_date - timedelta(days=self._lookback_days)

        for name in pending:
            try:
                report = await self._engine.run_full_validation(
                    strategy_names=[name],
                    symbols=self._symbols,
                    timeframe=self._timeframe,
                    start_date=start_date,
                    end_date=end_date,
                    exchange_pool=self._pool,
                    initial_capital=Decimal("10000"),
                    run_walk_forward=True,
                    run_monte_carlo=False,
                )
            except Exception as e:
                log.error(
                    "strategy_validation_runner.validation_failed",
                    strategy=name,
                    error=str(e),
                )
                summary.failed[name] = str(e)
                continue

            status = self._to_validation_status(report)
            registry.mark_validated(name, status)

            if status.can_go_live:
                summary.validated.append(name)
            else:
                summary.skipped.append(name)

            log.info(
                "strategy_validation_runner.strategy_evaluated",
                strategy=name,
                go_live_decision=report.go_live_decision,
                backtest_passed=status.backtest_passed,
                walk_forward_passed=status.walk_forward_passed,
                can_go_live=status.can_go_live,
            )

        return summary

    def _to_validation_status(self, report: FullValidationReport) -> ValidationStatus:
        """
        Übersetzt einen FullValidationReport in einen ValidationStatus
        für die Registry.

        backtest_passed: BacktestResult.is_acceptable (bereits
            vorhandenes Kriterium: Sharpe >= 1.0, PF >= 1.3,
            MaxDD <= 20%, HitRate >= 40%, >= 30 Trades).
        walk_forward_passed: WalkForwardResult.is_consistent, falls
            Walk-Forward durchgeführt wurde. Kein Walk-Forward-Ergebnis
            (z.B. < 20 Trades im Hauptbacktest, siehe
            BacktestingEngine.run_full_validation) zählt als nicht
            bestanden - keine Strategie darf ohne belastbare
            Out-of-Sample-Prüfung aktiv werden.
        paper_trading_passed: bewusster Platzhalter True, siehe
            Modul-Docstring.
        """
        backtest_passed = report.backtest.is_acceptable
        walk_forward_passed = (
            report.walk_forward is not None and report.walk_forward.is_consistent
        )

        return ValidationStatus(
            backtest_passed=backtest_passed,
            walk_forward_passed=walk_forward_passed,
            paper_trading_passed=True,  # Platzhalter, siehe Modul-Docstring
            live_approved=False,
            notes=report.decision_summary,
        )
