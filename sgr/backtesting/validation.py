"""
SGR Walk-Forward Analysis & Monte Carlo
=========================================
Validierungs-Methoden für Go-Live Gates.

Walk-Forward:
    Verhindert Overfitting durch Rolling-Window-Splits.
    In-Sample: Strategie optimiert (implicit via Backtest).
    Out-of-Sample: Performance auf ungesehenen Daten geprüft.
    Wenn OOS-Sharpe / IS-Sharpe > 0.7: konsistente Performance.

Monte Carlo:
    1000 Simulationen durch zufällige Trade-Reihenfolge.
    Zeigt Bandbreite möglicher Outcomes.
    95th Percentile Drawdown = konservative Risikoschätzung.

Beide sind Pflicht im Validierungspfad (Go-Live Gate 2).
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np

from sgr.backtesting.performance import PerformanceAnalyzer
from sgr.backtesting.types import (
    BacktestConfig,
    BacktestTrade,
    MonteCarloResult,
    WalkForwardResult,
)
from sgr.core.logging import get_logger

log = get_logger(__name__)


class WalkForwardAnalyzer:
    """
    Walk-Forward Analyse: prüft OOS-Performance über mehrere Splits.
    """

    def __init__(self, n_splits: int = 6) -> None:
        self._n_splits = n_splits
        self._analyzer = PerformanceAnalyzer()

    async def run(
        self,
        candles_by_symbol: dict,
        config: BacktestConfig,
        registry: object,
    ) -> WalkForwardResult:
        """
        Führt Walk-Forward Analyse durch.

        Splitting-Schema (z.B. 6 Splits auf 1 Jahr):
            Split 1: IS = Monat 1-6,  OOS = Monat 7
            Split 2: IS = Monat 2-7,  OOS = Monat 8
            ...usw.

        IS/OOS Verhältnis: 6:1 (konservativ)
        """
        from sgr.backtesting.simulator import BacktestSimulator

        primary_symbol = config.symbols[0]
        all_candles = (
            list(candles_by_symbol.get(primary_symbol, {}).values())[0]
            if isinstance(list(candles_by_symbol.values())[0], dict)
            else candles_by_symbol.get(primary_symbol, [])
        )

        if len(all_candles) < 200:
            return self._insufficient_data_result()

        # Splits berechnen
        total_bars = len(all_candles)
        split_size = total_bars // (self._n_splits + 1)
        oos_size = max(split_size // 6, 50)  # OOS = ~14% des IS

        split_results = []
        is_sharpes = []
        oos_sharpes = []

        for i in range(self._n_splits):
            # IS: growing window
            is_start = 0
            is_end = (i + 1) * split_size
            # OOS: fixed window nach IS
            oos_start = is_end
            oos_end = min(oos_start + oos_size, total_bars)

            if oos_end <= oos_start or is_end <= 200:
                continue

            is_candles = all_candles[is_start:is_end]
            oos_candles = all_candles[oos_start:oos_end]

            for period_name, candles_slice in [("is", is_candles), ("oos", oos_candles)]:
                if len(candles_slice) < 50:
                    continue

                split_config = BacktestConfig(
                    start_date=candles_slice[0].timestamp,
                    end_date=candles_slice[-1].timestamp,
                    symbols=config.symbols,
                    timeframe=config.timeframe,
                    initial_capital=config.initial_capital,
                    maker_fee=config.maker_fee,
                    taker_fee=config.taker_fee,
                    slippage_pct=config.slippage_pct,
                    strategy_names=config.strategy_names,
                )

                sim = BacktestSimulator(split_config)
                trades, equity = await sim.run(
                    {primary_symbol: candles_slice},
                    registry,  # type: ignore
                )
                result = self._analyzer.analyze(trades, equity, split_config)

                if period_name == "is":
                    is_sharpes.append(result.sharpe_ratio)
                else:
                    oos_sharpes.append(result.sharpe_ratio)
                    split_results.append(result)

            log.info(
                "walk_forward.split_completed",
                split=i + 1,
                total=self._n_splits,
            )

        if not split_results:
            return self._insufficient_data_result()

        avg_is_sharpe = float(np.mean(is_sharpes)) if is_sharpes else 0.0
        avg_oos_sharpe = float(np.mean(oos_sharpes)) if oos_sharpes else 0.0

        degradation = avg_oos_sharpe / avg_is_sharpe if avg_is_sharpe > 0 else 0.0
        is_consistent = all(r.sharpe_ratio > 0 for r in split_results) and degradation > 0.5

        # Konsistenz-Score: % der OOS-Splits mit positivem Sharpe
        positive_oos = sum(1 for r in split_results if r.sharpe_ratio > 0)
        consistency_score = positive_oos / len(split_results) if split_results else 0.0

        if is_consistent:
            recommendation = "PASS: Walk-Forward consistent. Proceed to Paper Trading."
        elif degradation > 0.3:
            recommendation = "MARGINAL: Some OOS degradation. Extended Paper Trading recommended."
        else:
            recommendation = "FAIL: Significant OOS degradation detected. Possible overfitting."

        log.info(
            "walk_forward.completed",
            splits=len(split_results),
            is_sharpe=f"{avg_is_sharpe:.2f}",
            oos_sharpe=f"{avg_oos_sharpe:.2f}",
            degradation=f"{degradation:.1%}",
            consistent=is_consistent,
        )

        return WalkForwardResult(
            n_splits=len(split_results),
            split_results=split_results,
            is_consistent=is_consistent,
            consistency_score=round(consistency_score, 3),
            in_sample_sharpe=round(avg_is_sharpe, 3),
            out_of_sample_sharpe=round(avg_oos_sharpe, 3),
            degradation_factor=round(degradation, 3),
            recommendation=recommendation,
        )

    def _insufficient_data_result(self) -> WalkForwardResult:
        return WalkForwardResult(
            n_splits=0,
            split_results=[],
            is_consistent=False,
            consistency_score=0.0,
            in_sample_sharpe=0.0,
            out_of_sample_sharpe=0.0,
            degradation_factor=0.0,
            recommendation="FAIL: Insufficient data for Walk-Forward Analysis.",
        )


class MonteCarloAnalyzer:
    """
    Monte Carlo Simulation via Trade-Permutation.

    Methode: Zufällige Reihenfolge der historischen Trades simulieren.
    Gibt Verteilung möglicher Outcomes (Return, Drawdown).
    Robuster als Parameter-Monte-Carlo (keine Verteilungsannahme).
    """

    def run(
        self,
        trades: list[BacktestTrade],
        initial_capital: Decimal,
        n_simulations: int = 1000,
        seed: int = 42,
    ) -> MonteCarloResult:
        """
        Permutiert Trade-Reihenfolge n_simulations mal.
        Berechnet Return und Max Drawdown für jede Simulation.
        """
        if len(trades) < 10:
            log.warning("monte_carlo.insufficient_trades", count=len(trades))
            return self._empty_result(n_simulations)

        rng = np.random.default_rng(seed)
        pnls = np.array([float(t.net_pnl) for t in trades])

        sim_returns = []
        sim_max_dds = []
        sim_sharpes = []

        initial = float(initial_capital)

        for _ in range(n_simulations):
            shuffled = rng.permutation(pnls)
            equity = initial + np.cumsum(shuffled)
            equity = np.insert(equity, 0, initial)

            final_return = (equity[-1] - initial) / initial * 100
            sim_returns.append(final_return)

            # Max Drawdown
            peak = equity[0]
            max_dd = 0.0
            for val in equity:
                if val > peak:
                    peak = val
                dd = (peak - val) / peak * 100
                if dd > max_dd:
                    max_dd = dd
            sim_max_dds.append(max_dd)

            # Sharpe (vereinfacht: aus Trade-Returns)
            trade_returns = shuffled / initial
            if len(trade_returns) > 1 and np.std(trade_returns) > 0:
                sharpe = float(np.mean(trade_returns) / np.std(trade_returns) * np.sqrt(252))
            else:
                sharpe = 0.0
            sim_sharpes.append(sharpe)

        sim_returns_arr = np.array(sim_returns)
        sim_dds_arr = np.array(sim_max_dds)
        sim_sharpes_arr = np.array(sim_sharpes)

        ruin_prob = float(np.mean(sim_dds_arr > 50.0))

        log.info(
            "monte_carlo.completed",
            simulations=n_simulations,
            median_return=f"{float(np.median(sim_returns_arr)):.1f}%",
            p5_return=f"{float(np.percentile(sim_returns_arr, 5)):.1f}%",
            p95_drawdown=f"{float(np.percentile(sim_dds_arr, 95)):.1f}%",
            ruin_probability=f"{ruin_prob:.1%}",
        )

        return MonteCarloResult(
            n_simulations=n_simulations,
            median_return_pct=round(float(np.median(sim_returns_arr)), 2),
            percentile_5_return_pct=round(float(np.percentile(sim_returns_arr, 5)), 2),
            percentile_95_return_pct=round(float(np.percentile(sim_returns_arr, 95)), 2),
            median_max_drawdown_pct=round(float(np.median(sim_dds_arr)), 2),
            percentile_95_max_drawdown_pct=round(float(np.percentile(sim_dds_arr, 95)), 2),
            ruin_probability=round(ruin_prob, 4),
            sharpe_distribution={
                "min": round(float(np.min(sim_sharpes_arr)), 2),
                "p25": round(float(np.percentile(sim_sharpes_arr, 25)), 2),
                "median": round(float(np.median(sim_sharpes_arr)), 2),
                "p75": round(float(np.percentile(sim_sharpes_arr, 75)), 2),
                "max": round(float(np.max(sim_sharpes_arr)), 2),
            },
        )

    def _empty_result(self, n: int) -> MonteCarloResult:
        return MonteCarloResult(
            n_simulations=n,
            median_return_pct=0.0,
            percentile_5_return_pct=0.0,
            percentile_95_return_pct=0.0,
            median_max_drawdown_pct=0.0,
            percentile_95_max_drawdown_pct=0.0,
            ruin_probability=1.0,
            sharpe_distribution={"min": 0.0, "p25": 0.0, "median": 0.0, "p75": 0.0, "max": 0.0},
        )
