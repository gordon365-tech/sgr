#!/usr/bin/env python3
"""
Diagnose-Script: Split-Level Walk-Forward-Analyse fuer bereits
optimierte, alle-Backtest-Gates-bestehende Kandidaten.

Kein neuer Optimierungslauf, keine Parametersuche, keine Gate-
Aenderung: laedt fuer jeden Kandidaten die EXAKT selben persistierten
Parameter und das EXAKT selbe validation_window, die der Batch bereits
verwendet hat, und ruft dieselbe BacktestingEngine.run_full_validation()
Pipeline erneut auf - nur um das intern bereits berechnete, aber nicht
persistierte split_results Detail (pro-Split Sharpe/Trades) sichtbar
zu machen. Deterministisch (kein RNG im Walk-Forward-Pfad), reproduziert
also exakt das Ergebnis, das beim urspruenglichen Batch-Lauf bereits
einmal berechnet wurde.

Usage:
    python scripts/diagnose_walk_forward_splits.py --input winners.psv --output report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from statistics import median
from uuid import uuid4


async def _main(input_path: str, output_path: str, exchange: str) -> int:
    import sgr.strategy.breakout  # noqa: F401
    import sgr.strategy.mean_reversion  # noqa: F401
    import sgr.strategy.momentum  # noqa: F401
    import sgr.strategy.trend_following  # noqa: F401
    import sgr.strategy.volatility_adjusted_momentum  # noqa: F401
    from sgr.backtesting.data_loader import BacktestDataLoader
    from sgr.backtesting.engine import BacktestingEngine
    from sgr.core.database import close_db, init_db
    from sgr.core.types import ExchangeID
    from sgr.strategy.parameter_optimizer import build_trial_strategy
    from sgr.strategy.regime_profile import build_regime_profile
    from sgr.strategy.registry import StrategyRegistry

    await init_db()
    registry = StrategyRegistry.get()
    engine = BacktestingEngine()
    loader = BacktestDataLoader()
    exchange_id = ExchangeID(exchange)

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

    print(f"Loaded {len(rows)} winning candidates from {input_path}")

    split_fail_counts: Counter[int] = Counter()
    split_trade_counts: dict[int, list[int]] = defaultdict(list)
    split_sharpes: dict[int, list[float]] = defaultdict(list)
    split_regime_diversity: dict[int, list[int]] = defaultdict(list)
    split_regime_dominant: dict[int, Counter[str]] = defaultdict(Counter)
    degradations: list[float] = []
    is_sharpes: list[float] = []
    oos_sharpes: list[float] = []
    failure_bucket = Counter()  # "0_neg_splits_but_degradation", "1_neg_split", "2plus_neg_splits", "insufficient_split_data"
    per_candidate_detail = []
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
        start = datetime.fromisoformat(vw[0])
        end = datetime.fromisoformat(vw[1])

        trial_name = f"{strategy_name}__diag_{uuid4().hex[:8]}"
        try:
            trial = build_trial_strategy(
                strategy_class, params_class, row["parameters"], trial_name
            )
            registry.register_instance(trial)
            try:
                report = await engine.run_full_validation(
                    strategy_names=[trial_name],
                    symbols=[symbol],
                    timeframe="1h",
                    start_date=start,
                    end_date=end,
                    exchange_pool=None,
                    exchange_id=exchange_id,
                    run_walk_forward=True,
                    run_monte_carlo=False,
                )
            finally:
                registry.unregister(trial_name)
        except Exception as e:  # noqa: BLE001
            technical_errors.append(f"{symbol}/{strategy_name}: {e}")
            continue

        wf = report.walk_forward
        if wf is None or not wf.split_results:
            failure_bucket["insufficient_split_data"] += 1
            continue

        # Split-Fenster-Grenzen fuer Regime-Diversitaet nachbauen (gleiche
        # Formel wie WalkForwardAnalyzer.run(), rein lesend).
        try:
            candles = await loader.load_public_history(
                symbol=symbol,
                timeframe="1h",
                start=start,
                end=end,
                exchange_id=exchange_id,
            )
            warmup = 200
            n_splits = 6
            total_bars = len(candles)
            split_size = total_bars // (n_splits + 1)
            oos_size = max(split_size // 6, warmup * 2)
            for i in range(n_splits):
                is_end = (i + 1) * split_size
                oos_start = is_end
                oos_end = min(oos_start + oos_size, total_bars)
                if oos_end <= oos_start or is_end <= warmup:
                    continue
                oos_slice = candles[oos_start:oos_end]
                if len(oos_slice) < 10:
                    continue
                profile = build_regime_profile(oos_slice)
                split_regime_diversity[i].append(profile.diversity_count)
                split_regime_dominant[i][profile.dominant_regime.value] += 1
        except Exception as e:  # noqa: BLE001
            technical_errors.append(f"{symbol}/{strategy_name} regime-calc: {e}")

        neg_splits = []
        for i, r in enumerate(wf.split_results):
            split_trade_counts[i].append(r.total_trades)
            split_sharpes[i].append(round(r.sharpe_ratio, 3))
            if r.sharpe_ratio <= 0:
                split_fail_counts[i] += 1
                neg_splits.append(i)

        degradations.append(wf.degradation_factor)
        is_sharpes.append(wf.in_sample_sharpe)
        oos_sharpes.append(wf.out_of_sample_sharpe)

        if len(neg_splits) == 0:
            failure_bucket["0_neg_splits_still_failed_degradation"] += (
                0 if wf.is_consistent else 1
            )
            if wf.is_consistent:
                failure_bucket["actually_passed"] += 1
        elif len(neg_splits) == 1:
            failure_bucket["exactly_1_neg_split"] += 1
        else:
            failure_bucket["2plus_neg_splits"] += 1

        per_candidate_detail.append(
            {
                "symbol": symbol,
                "strategy": strategy_name,
                "is_consistent": wf.is_consistent,
                "degradation_factor": wf.degradation_factor,
                "in_sample_sharpe": wf.in_sample_sharpe,
                "out_of_sample_sharpe": wf.out_of_sample_sharpe,
                "n_neg_splits": len(neg_splits),
                "neg_split_indices": neg_splits,
                "split_sharpes": [round(r.sharpe_ratio, 3) for r in wf.split_results],
                "split_trades": [r.total_trades for r in wf.split_results],
            }
        )

        if (idx + 1) % 10 == 0:
            print(f"  processed {idx + 1}/{len(rows)}")

    result = {
        "n_candidates_processed": len(per_candidate_detail),
        "n_technical_errors": len(technical_errors),
        "technical_errors": technical_errors[:20],
        "failure_bucket_counts": dict(failure_bucket),
        "split_fail_counts_by_index": {
            str(i): split_fail_counts.get(i, 0) for i in range(6)
        },
        "split_trade_stats_by_index": {
            str(i): {
                "median": median(v) if v else None,
                "min": min(v) if v else None,
                "max": max(v) if v else None,
                "n": len(v),
            }
            for i, v in split_trade_counts.items()
        },
        "split_sharpe_median_by_index": {
            str(i): round(median(v), 3) if v else None
            for i, v in split_sharpes.items()
        },
        "split_regime_diversity_median_by_index": {
            str(i): round(median(v), 2) if v else None
            for i, v in split_regime_diversity.items()
        },
        "split_regime_dominant_by_index": {
            str(i): dict(c) for i, c in split_regime_dominant.items()
        },
        "degradation_factor_median": round(median(degradations), 3)
        if degradations
        else None,
        "degradation_factor_min_max": [min(degradations), max(degradations)]
        if degradations
        else None,
        "is_sharpe_median": round(median(is_sharpes), 3) if is_sharpes else None,
        "oos_sharpe_median": round(median(oos_sharpes), 3) if oos_sharpes else None,
        "per_candidate_detail": per_candidate_detail,
    }

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(json.dumps({k: v for k, v in result.items() if k != "per_candidate_detail"}, indent=2))
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
