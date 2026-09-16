#!/usr/bin/env python3
"""
Variante D — Tail-/Event-Konzentrationscheck (rein additiv, KEINE Gate-Aenderung)
====================================================================================
Auftrag: nach Variante A (Produktions-Walk-Forward), B (laengere Splits) und
C (B + Parameterstabilitaet) soll eine vierte, direkt auf Single-Event-
Erkennung zielende Messung auf denselben 91 bereits geprueften Kandidaten
(89 aus near_miss_opt_20260915 + 2 aus control60_20260915) ausgefuehrt
werden. Motivation (siehe /tmp/sgr-wf-validation-decision.md): weder B noch
C wurden GEZIELT fuer Single-Event-Erkennung gebaut - LAB/USDT und (mit
Abstrichen) BANK/USDT zeigen, dass ein einzelnes extremes Kursereignis
einen Grossteil der Performance tragen kann, ohne dass A/B/C das direkt
messen.

Zwei Messungen pro Kandidat, auf denselben Trades des bereits definierten
"Pooled-OOS"-Backtests (validation_start + 20% IS-Puffer bis
validation_end, siehe wf_methodology_experiment.py):

  1. Top-N-Trade-Konzentration: welcher Anteil des Bruttogewinns stammt aus
     dem einzelnen besten bzw. den drei besten Trades? Und: bleibt ein
     pseudo-Sharpe (dieselbe Formel wie MonteCarloAnalyzer.run() intern
     verwendet: mean(pnl/capital)/std(pnl/capital)*sqrt(252)) positiv,
     wenn man die 1 bzw. 3 besten Trades entfernt?

  2. Monte-Carlo-Trade-Resampling (bereits vorhandene
     sgr.backtesting.validation.MonteCarloAnalyzer, hier zum ersten Mal
     auf diese Kandidaten angewendet): Ruin-Wahrscheinlichkeit,
     5./95.-Perzentil Return/Drawdown, Sharpe-Verteilung ueber 1000
     zufaellige Trade-Reihenfolgen.

Kein neuer Backtest mit anderen Parametern, keine neue Optimierung - nur
eine ANDERE Auswertung derselben bereits einmal gelaufenen Pooled-OOS-
Trades (Trade-Liste wurde bisher verworfen, nur Aggregat-Metriken wurden
behalten).

Usage:
    python scripts/wf_variant_d_tail_check.py --input winners.psv --output report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from datetime import datetime
from decimal import Decimal
from uuid import uuid4


def _pseudo_sharpe(pnls: list[float], initial_capital: float) -> float:
    """Identische Formel wie MonteCarloAnalyzer.run() intern verwendet
    (trade_returns = pnl/initial, dann mean/std*sqrt(252)) - fuer
    Konsistenz mit der bereits vorhandenen Komponente, keine neue
    Sharpe-Definition."""
    if not pnls:
        return 0.0
    returns = [p / initial_capital for p in pnls]
    if len(returns) < 2:
        return 0.0
    std = statistics.pstdev(returns)
    if std == 0:
        return 0.0
    import math

    return statistics.mean(returns) / std * math.sqrt(252)


async def _run_pooled_backtest_get_trades(
    engine, registry, strategy_class, params_class, overrides, symbol, timeframe,
    candles, warmup, trial_prefix,
):
    from sgr.backtesting.performance import PerformanceAnalyzer
    from sgr.backtesting.simulator import BacktestSimulator
    from sgr.backtesting.types import BacktestConfig
    from sgr.strategy.parameter_optimizer import build_trial_strategy

    total_bars = len(candles)
    pooled_is_bars = max(int(total_bars * 0.2), warmup * 2)
    pooled_candles = candles[pooled_is_bars:]
    if len(pooled_candles) < warmup * 2:
        return None, None, None

    cfg = BacktestConfig(
        start_date=pooled_candles[0].timestamp,
        end_date=pooled_candles[-1].timestamp,
        symbols=[symbol],
        timeframe=timeframe,
        strategy_names=["placeholder"],
    )

    trial_name = f"{trial_prefix}_{uuid4().hex[:8]}"
    trial = build_trial_strategy(strategy_class, params_class, overrides, trial_name)
    registry.register_instance(trial)
    await registry.activate(trial_name)
    try:
        cfg = BacktestConfig(
            start_date=pooled_candles[0].timestamp,
            end_date=pooled_candles[-1].timestamp,
            symbols=[symbol],
            timeframe=timeframe,
            strategy_names=[trial_name],
        )
        sim = BacktestSimulator(cfg)
        trades, equity = await sim.run({symbol: pooled_candles}, registry)
        result = PerformanceAnalyzer().analyze(trades, equity, cfg)
        return trades, cfg, result
    finally:
        registry.unregister(trial_name)


async def _main(input_path: str, output_path: str, exchange: str) -> int:
    import sgr.strategy.breakout  # noqa: F401
    import sgr.strategy.mean_reversion  # noqa: F401
    import sgr.strategy.momentum  # noqa: F401
    import sgr.strategy.trend_following  # noqa: F401
    import sgr.strategy.volatility_adjusted_momentum  # noqa: F401
    from sgr.backtesting.data_loader import BacktestDataLoader
    from sgr.backtesting.engine import BacktestingEngine
    from sgr.backtesting.validation import MonteCarloAnalyzer
    from sgr.core.database import close_db, init_db
    from sgr.core.types import ExchangeID
    from sgr.strategy.registry import StrategyRegistry

    await init_db()
    registry = StrategyRegistry.get()
    engine = BacktestingEngine()
    loader = BacktestDataLoader()
    exchange_id = ExchangeID(exchange)
    mc = MonteCarloAnalyzer()
    warmup = 200
    initial_capital = 10000.0

    rows = []
    with open(input_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            symbol, strategy_name, params_json, vw_json = line.split("|", 3)
            rows.append(
                {
                    "symbol": symbol,
                    "strategy": strategy_name,
                    "parameters": json.loads(params_json),
                    "validation_window": json.loads(vw_json),
                }
            )

    print(f"Loaded {len(rows)} candidates from {input_path}")

    per_candidate = []
    technical_errors = []

    for idx, row in enumerate(rows):
        symbol = row["symbol"]
        strategy_name = row["strategy"]
        entry = registry.get_entry(strategy_name)
        if entry is None:
            technical_errors.append(f"{symbol}/{strategy_name}: not registered")
            continue
        strategy_class = type(entry.strategy)
        params_class = type(entry.strategy._params)  # noqa: SLF001

        vw = row["validation_window"]
        vstart = datetime.fromisoformat(vw[0])
        vend = datetime.fromisoformat(vw[1])

        try:
            candles = await loader.load_public_history(
                symbol=symbol, timeframe="1h", start=vstart, end=vend, exchange_id=exchange_id,
            )
        except Exception as e:  # noqa: BLE001
            technical_errors.append(f"{symbol}/{strategy_name} candle-load: {e}")
            continue

        try:
            trades, cfg, result = await _run_pooled_backtest_get_trades(
                engine, registry, strategy_class, params_class, row["parameters"],
                symbol, "1h", candles, warmup, f"{strategy_name}__vd",
            )
        except Exception as e:  # noqa: BLE001
            technical_errors.append(f"{symbol}/{strategy_name} pooled-backtest: {e}")
            continue

        if trades is None or not trades:
            technical_errors.append(f"{symbol}/{strategy_name}: no trades in pooled window")
            continue

        pnls = [float(t.net_pnl) for t in trades]
        sorted_desc = sorted(pnls, reverse=True)
        gross_profit = sum(p for p in pnls if p > 0)
        total_pnl = sum(pnls)

        top1_pnl = sorted_desc[0]
        top3_pnl = sum(sorted_desc[:3])

        sharpe_all = _pseudo_sharpe(pnls, initial_capital)
        sharpe_excl_top1 = _pseudo_sharpe(sorted_desc[1:], initial_capital)
        sharpe_excl_top3 = _pseudo_sharpe(sorted_desc[3:], initial_capital)

        single_event_suspected = sharpe_all > 0 and sharpe_excl_top3 <= 0

        try:
            mc_result = mc.run(trades, Decimal(str(initial_capital)), n_simulations=1000)
            mc_dict = {
                "median_return_pct": mc_result.median_return_pct,
                "percentile_5_return_pct": mc_result.percentile_5_return_pct,
                "percentile_95_return_pct": mc_result.percentile_95_return_pct,
                "median_max_drawdown_pct": mc_result.median_max_drawdown_pct,
                "percentile_95_max_drawdown_pct": mc_result.percentile_95_max_drawdown_pct,
                "ruin_probability": mc_result.ruin_probability,
                "sharpe_distribution": mc_result.sharpe_distribution,
            }
        except Exception as e:  # noqa: BLE001
            mc_dict = {"error": str(e)[:200]}

        per_candidate.append(
            {
                "symbol": symbol,
                "strategy": strategy_name,
                "n_trades": len(pnls),
                "total_pnl": round(total_pnl, 2),
                "gross_profit": round(gross_profit, 2),
                "top1_trade_pnl": round(top1_pnl, 2),
                "top3_trade_pnl": round(top3_pnl, 2),
                "top1_share_of_gross_profit": round(top1_pnl / gross_profit, 3)
                if gross_profit > 0
                else None,
                "top3_share_of_gross_profit": round(top3_pnl / gross_profit, 3)
                if gross_profit > 0
                else None,
                "pseudo_sharpe_all_trades": round(sharpe_all, 3),
                "pseudo_sharpe_excl_top1": round(sharpe_excl_top1, 3),
                "pseudo_sharpe_excl_top3": round(sharpe_excl_top3, 3),
                "single_event_suspected": single_event_suspected,
                "monte_carlo": mc_dict,
            }
        )

        if (idx + 1) % 10 == 0:
            print(f"  processed {idx + 1}/{len(rows)}")

    n = len(per_candidate)
    flagged = sum(1 for c in per_candidate if c["single_event_suspected"])
    top1_shares = [c["top1_share_of_gross_profit"] for c in per_candidate if c["top1_share_of_gross_profit"] is not None]
    top3_shares = [c["top3_share_of_gross_profit"] for c in per_candidate if c["top3_share_of_gross_profit"] is not None]
    ruin_probs = [c["monte_carlo"].get("ruin_probability") for c in per_candidate if "ruin_probability" in c["monte_carlo"]]

    summary = {
        "n_candidates_processed": n,
        "n_technical_errors": len(technical_errors),
        "technical_errors": technical_errors[:20],
        "n_single_event_suspected": flagged,
        "single_event_suspected_rate": round(flagged / n, 3) if n else None,
        "median_top1_share_of_gross_profit": round(statistics.median(top1_shares), 3) if top1_shares else None,
        "median_top3_share_of_gross_profit": round(statistics.median(top3_shares), 3) if top3_shares else None,
        "median_ruin_probability": round(statistics.median(ruin_probs), 4) if ruin_probs else None,
    }

    result = {"summary": summary, "per_candidate_detail": per_candidate}
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(summary, indent=2))
    await close_db()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--exchange", default="binance")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.input, args.output, args.exchange)))


if __name__ == "__main__":
    main()
