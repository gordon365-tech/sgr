"""
SGR Backtesting Engine
======================
Orchestriert den vollständigen Validierungspfad:

    Backtest → Walk-Forward → Monte Carlo → Go/No-Go Report

Verpflichtend vor Live-Trading (Go-Live Gate 2).
Produziert strukturierten Report mit Go/No-Go Entscheidung.

Usage:
    engine = BacktestingEngine()
    report = await engine.run_full_validation(
        strategy_names=["trend_following_v1"],
        symbols=["BTC/USDT"],
        timeframe="1h",
        start_date=datetime(2022, 1, 1),
        end_date=datetime(2023, 12, 31),
        exchange_pool=pool,
    )
    print(report.go_live_decision)  # "GO" | "NO-GO"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sgr.backtesting.data_loader import BacktestDataLoader
from sgr.backtesting.performance import PerformanceAnalyzer
from sgr.backtesting.simulator import BacktestSimulator
from sgr.backtesting.types import (
    BacktestConfig,
    BacktestResult,
    MonteCarloResult,
    WalkForwardResult,
)
from sgr.backtesting.validation import MonteCarloAnalyzer, WalkForwardAnalyzer
from sgr.core.logging import get_logger
from sgr.strategy.registry import StrategyRegistry

log = get_logger(__name__)


@dataclass
class FullValidationReport:
    """
    Vollständiger Validierungsbericht.
    Enthält alle Zwischenergebnisse und finale Go/No-Go Entscheidung.
    """

    strategy_names: list[str]
    symbols: list[str]
    timeframe: str
    start_date: str
    end_date: str

    # Ergebnisse
    backtest: BacktestResult
    walk_forward: WalkForwardResult | None = None
    monte_carlo: MonteCarloResult | None = None

    # Finale Entscheidung
    go_live_decision: str = "NO-GO"  # "GO" | "NO-GO" | "CONDITIONAL"
    decision_summary: str = ""
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Für API-Response und Logging."""
        return {
            "strategy_names": self.strategy_names,
            "symbols": self.symbols,
            "timeframe": self.timeframe,
            "period": f"{self.start_date} → {self.end_date}",
            "go_live_decision": self.go_live_decision,
            "decision_summary": self.decision_summary,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "backtest_kpis": {
                "total_return_pct": self.backtest.total_return_pct,
                "cagr_pct": self.backtest.cagr_pct,
                "sharpe_ratio": self.backtest.sharpe_ratio,
                "sortino_ratio": self.backtest.sortino_ratio,
                "calmar_ratio": self.backtest.calmar_ratio,
                "max_drawdown_pct": self.backtest.max_drawdown_pct,
                "profit_factor": self.backtest.profit_factor,
                "hit_rate_pct": self.backtest.hit_rate_pct,
                "total_trades": self.backtest.total_trades,
                "total_fees": self.backtest.total_fees,
            },
            "walk_forward": {
                "is_consistent": self.walk_forward.is_consistent,
                "consistency_score": self.walk_forward.consistency_score,
                "is_sharpe": self.walk_forward.in_sample_sharpe,
                "oos_sharpe": self.walk_forward.out_of_sample_sharpe,
                "degradation_factor": self.walk_forward.degradation_factor,
                "recommendation": self.walk_forward.recommendation,
            }
            if self.walk_forward
            else None,
            "monte_carlo": {
                "n_simulations": self.monte_carlo.n_simulations,
                "median_return_pct": self.monte_carlo.median_return_pct,
                "p5_return_pct": self.monte_carlo.percentile_5_return_pct,
                "p95_drawdown_pct": self.monte_carlo.percentile_95_max_drawdown_pct,
                "ruin_probability": self.monte_carlo.ruin_probability,
            }
            if self.monte_carlo
            else None,
        }


class BacktestingEngine:
    """
    Orchestriert den vollständigen Backtesting- und Validierungsprozess.
    """

    def __init__(self) -> None:
        self._loader = BacktestDataLoader()
        self._analyzer = PerformanceAnalyzer()
        self._wf_analyzer = WalkForwardAnalyzer(n_splits=6)
        self._mc_analyzer = MonteCarloAnalyzer()

    async def run_full_validation(
        self,
        strategy_names: list[str],
        symbols: list[str],
        timeframe: str,
        start_date: datetime,
        end_date: datetime,
        exchange_pool: Any,
        initial_capital: Decimal = Decimal("10000"),
        run_walk_forward: bool = True,
        run_monte_carlo: bool = True,
        monte_carlo_runs: int = 1000,
    ) -> FullValidationReport:
        """
        Vollständiger Validierungspfad: Backtest → WF → MC → Report.

        Args:
            strategy_names: Strategien aus Registry (müssen registriert sein)
            symbols: Trading-Symbole
            timeframe: OHLCV-Timeframe
            start_date / end_date: Backtest-Zeitraum
            exchange_pool: Verbundener Exchange Pool für Daten-Abruf
            run_walk_forward: Walk-Forward Analyse durchführen?
            run_monte_carlo: Monte Carlo Simulation durchführen?
        """
        log.info(
            "backtesting_engine.validation_started",
            strategies=strategy_names,
            symbols=symbols,
            timeframe=timeframe,
            period=f"{start_date.date()} → {end_date.date()}",
        )

        # 1. Registry vorbereiten
        registry = StrategyRegistry.get()
        for name in strategy_names:
            if not registry.is_active(name):
                await registry.activate(name)

        # 2. Daten laden
        candles_by_symbol: dict[str, Any] = {}
        for symbol in symbols:
            candles = await self._loader.load_from_exchange(
                symbol=symbol,
                timeframe=timeframe,
                start=start_date,
                end=end_date,
                exchange_pool=exchange_pool,
            )
            candles_by_symbol[symbol] = candles

        if not any(candles_by_symbol.values()):
            log.error("backtesting_engine.no_data_loaded")
            config = BacktestConfig(
                start_date=start_date,
                end_date=end_date,
                symbols=symbols,
                timeframe=timeframe,
                initial_capital=initial_capital,
                strategy_names=strategy_names,
            )
            empty = self._analyzer._empty_result(config)
            return FullValidationReport(
                strategy_names=strategy_names,
                symbols=symbols,
                timeframe=timeframe,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                backtest=empty,
                go_live_decision="NO-GO",
                decision_summary="No historical data available.",
                blockers=["Data loading failed"],
            )

        # 3. Haupt-Backtest
        config = BacktestConfig(
            start_date=start_date,
            end_date=end_date,
            symbols=symbols,
            timeframe=timeframe,
            initial_capital=initial_capital,
            strategy_names=strategy_names,
        )

        simulator = BacktestSimulator(config)
        trades, equity_curve = await simulator.run(candles_by_symbol, registry)
        backtest_result = self._analyzer.analyze(trades, equity_curve, config)

        log.info(
            "backtesting_engine.backtest_complete",
            total_return=f"{backtest_result.total_return_pct:.1f}%",
            sharpe=f"{backtest_result.sharpe_ratio:.2f}",
            trades=backtest_result.total_trades,
        )

        # 4. Walk-Forward
        wf_result: WalkForwardResult | None = None
        if run_walk_forward and backtest_result.total_trades >= 20:
            wf_result = await self._wf_analyzer.run(candles_by_symbol, config, registry)

        # 5. Monte Carlo
        mc_result: MonteCarloResult | None = None
        if run_monte_carlo and trades:
            mc_result = self._mc_analyzer.run(
                trades=trades,
                initial_capital=initial_capital,
                n_simulations=monte_carlo_runs,
            )

        # 6. Go/No-Go Entscheidung
        report = self._make_decision(
            strategy_names=strategy_names,
            symbols=symbols,
            timeframe=timeframe,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            backtest=backtest_result,
            walk_forward=wf_result,
            monte_carlo=mc_result,
        )

        log.info(
            "backtesting_engine.validation_complete",
            decision=report.go_live_decision,
            blockers=len(report.blockers),
            warnings=len(report.warnings),
        )

        return report

    async def run_quick_backtest(
        self,
        strategy_names: list[str],
        candles: list[Any],
        symbol: str,
        timeframe: str,
        initial_capital: Decimal = Decimal("10000"),
    ) -> BacktestResult:
        """
        Schneller Backtest ohne Daten-Abruf (für Tests und Entwicklung).
        Nimmt Candles direkt entgegen.
        """
        registry = StrategyRegistry.get()
        for name in strategy_names:
            if not registry.is_active(name):
                await registry.activate(name)

        if not candles:
            log.warning("backtesting_engine.quick.no_candles")
            config = BacktestConfig(
                start_date=datetime.now(),
                end_date=datetime.now(),
                symbols=[symbol],
                timeframe=timeframe,
                initial_capital=initial_capital,
            )
            return self._analyzer._empty_result(config)

        config = BacktestConfig(
            start_date=candles[0].timestamp,
            end_date=candles[-1].timestamp,
            symbols=[symbol],
            timeframe=timeframe,
            initial_capital=initial_capital,
            strategy_names=strategy_names,
        )

        simulator = BacktestSimulator(config)
        trades, equity_curve = await simulator.run({symbol: candles}, registry)
        return self._analyzer.analyze(trades, equity_curve, config)

    def _make_decision(
        self,
        strategy_names: list[str],
        symbols: list[str],
        timeframe: str,
        start_date: str,
        end_date: str,
        backtest: BacktestResult,
        walk_forward: WalkForwardResult | None,
        monte_carlo: MonteCarloResult | None,
    ) -> FullValidationReport:
        """Aggregiert alle Ergebnisse zu einer Go/No-Go Entscheidung."""
        blockers: list[str] = list(backtest.go_live_blockers)
        warnings: list[str] = []

        # Walk-Forward Ergebnis
        if walk_forward:
            if not walk_forward.is_consistent:
                blockers.append(
                    f"Walk-Forward inconsistent (degradation: {walk_forward.degradation_factor:.1%})"
                )
            elif walk_forward.degradation_factor < 0.7:
                warnings.append(
                    f"Walk-Forward degradation {walk_forward.degradation_factor:.1%} — monitor closely"
                )

        # Monte Carlo Ergebnis
        if monte_carlo:
            if monte_carlo.percentile_95_max_drawdown_pct > 25.0:
                blockers.append(
                    f"Monte Carlo P95 Drawdown {monte_carlo.percentile_95_max_drawdown_pct:.1f}% > 25%"
                )
            if monte_carlo.ruin_probability > 0.05:
                blockers.append(f"Ruin probability {monte_carlo.ruin_probability:.1%} > 5%")
            if monte_carlo.percentile_5_return_pct < -20:
                warnings.append(
                    f"Monte Carlo P5 scenario: {monte_carlo.percentile_5_return_pct:.1f}% return"
                )

        # Entscheidung
        if not blockers:
            decision = "GO"
            summary = (
                f"All gates passed. "
                f"Sharpe {backtest.sharpe_ratio:.2f}, "
                f"MaxDD {backtest.max_drawdown_pct:.1f}%, "
                f"PF {backtest.profit_factor:.2f}. "
                f"Proceed to Paper Trading (minimum 4 weeks)."
            )
        elif len(blockers) <= 2 and backtest.sharpe_ratio > 0.7:
            decision = "CONDITIONAL"
            summary = (
                f"Marginal result. {len(blockers)} blocker(s). "
                f"Parameter optimization and extended Paper Trading recommended."
            )
        else:
            decision = "NO-GO"
            summary = (
                f"Strategy does not meet Go-Live requirements. "
                f"{len(blockers)} blocker(s). Do not proceed to Live Trading."
            )

        return FullValidationReport(
            strategy_names=strategy_names,
            symbols=symbols,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
            backtest=backtest,
            walk_forward=walk_forward,
            monte_carlo=monte_carlo,
            go_live_decision=decision,
            decision_summary=summary,
            blockers=blockers,
            warnings=warnings,
        )
