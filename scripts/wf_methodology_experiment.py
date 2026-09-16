#!/usr/bin/env python3
"""
Walk-Forward Methodology Experiment (rein additiv, KEINE Gate-Aenderung)
==========================================================================
Auftrag: nach der Split-Level-Diagnose (siehe diagnose_walk_forward_splits.py)
soll GENAU auf den 89 bereits optimierten, alle-Backtest-Gates-bestehenden
Kandidaten eine ALTERNATIVE Walk-Forward-Methodik danebengestellt werden -
die bestehende Gate-Logik (WalkForwardAnalyzer, score_validation(), 6 Splits
a 16.7 Tage) bleibt vollstaendig unveraendert. Dieses Script schreibt NICHTS
in strategy_symbol_validations, aendert keine Gates, aktiviert keine
Strategie - reine Read-Compute-Report-Diagnose auf denselben, bereits
persistierten Parametern/Fenstern.

Drei Messungen pro Kandidat, alle mit den EXAKT persistierten optimierten
Parametern:

  A. Pooled-OOS: EIN einzelner, durchgehender Backtest ueber den groessten
     Teil des Validierungsfensters (nach einem kleinen initialen IS-Puffer)
     statt 6 kurzer 16.7-Tage-Fenster - liefert Sharpe/Profit-Factor/
     MaxDrawdown/Trades auf Basis von deutlich mehr Trades als jeder
     einzelne Split.

  B. Alternative Split-Konfiguration: 3 statt 6 Splits, OOS-Fenstergroesse
     = split_size (nicht split_size//6 wie im Produktions-Walk-Forward) -
     ca. 1.7x laengere OOS-Fenster pro Split, bei weniger Splits. Gleiche
     Pass/Fail-Formel (alle Splits positiv UND Degradation > 0.5) wird NUR
     zu Vergleichszwecken auf diese alternative Konfiguration angewendet -
     das ist eine Kennzahl fuer dieses Experiment, keine Aenderung an
     score_validation() oder WalkForwardAnalyzer.

  C. Parameter-Stabilitaet: derselbe 9-Kombinationen-Suchraum aus
     parameter_optimizer.PARAMETER_SEARCH_SPACE wird ZUSAETZLICH auf einem
     dritten, disjunkten Zeitfenster (mittleres Drittel des
     Validierungsfensters - wurde weder fuer die urspruengliche
     Parametersuche noch fuer den Backtest-Gate-Pass verwendet) erneut
     ausgefuehrt. Verglichen wird, ob dieselbe Kombination gewinnt wie die
     bereits persistierte - starke Uebereinstimmung spricht fuer echtes
     Signal, staendig wechselnde Gewinner-Kombination spricht fuer
     Overfitting auf das urspruengliche Optimierungsfenster.

Usage:
    python scripts/wf_methodology_experiment.py --input winners.psv --output report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from statistics import mean, median
from uuid import uuid4


async def _run_single_backtest(
    engine, registry, strategy_class, params_class, overrides, symbol, timeframe,
    start, end, exchange_id, trial_prefix,
):
    from sgr.strategy.parameter_optimizer import build_trial_strategy

    trial_name = f"{trial_prefix}_{uuid4().hex[:8]}"
    trial = build_trial_strategy(strategy_class, params_class, overrides, trial_name)
    registry.register_instance(trial)
    try:
        report = await engine.run_full_validation(
            strategy_names=[trial_name],
            symbols=[symbol],
            timeframe=timeframe,
            start_date=start,
            end_date=end,
            exchange_pool=None,
            exchange_id=exchange_id,
            run_walk_forward=False,
            run_monte_carlo=False,
        )
        return report.backtest
    finally:
        registry.unregister(trial_name)


async def _run_custom_walk_forward(
    candles, symbol, timeframe, strategy_class, params_class, overrides,
    registry, n_splits, warmup, trial_prefix,
):
    from sgr.backtesting.performance import PerformanceAnalyzer
    from sgr.backtesting.simulator import BacktestSimulator
    from sgr.backtesting.types import BacktestConfig
    from sgr.strategy.parameter_optimizer import build_trial_strategy

    total_bars = len(candles)
    split_size = total_bars // (n_splits + 1)
    # KEIN //6 wie im Produktions-WalkForwardAnalyzer - das ist genau die
    # hier zu testende Variable (laengere OOS-Fenster statt ~16.7 Tage).
    oos_size = max(split_size, warmup * 2)

    analyzer = PerformanceAnalyzer()
    split_results = []
    is_sharpes: list[float] = []
    oos_sharpes: list[float] = []

    trial_name = f"{trial_prefix}_{uuid4().hex[:8]}"
    trial = build_trial_strategy(strategy_class, params_class, overrides, trial_name)
    registry.register_instance(trial)
    # register_instance() allein aktiviert die Strategie NICHT
    # (StrategyEntry.is_active=False per Default) - BacktestSimulator
    # generiert fuer inaktive Strategien keine Signale. engine.run_full_
    # validation() (siehe _run_single_backtest oben) macht das intern,
    # hier beim direkten BacktestSimulator-Aufruf muss es explizit
    # passieren, sonst liefert jeder Split 0 Trades.
    await registry.activate(trial_name)
    try:
        for i in range(n_splits):
            is_end = (i + 1) * split_size
            oos_start = is_end
            oos_end = min(oos_start + oos_size, total_bars)
            if oos_end <= oos_start or is_end <= warmup:
                continue
            is_candles = candles[0:is_end]
            oos_candles = candles[oos_start:oos_end]
            for period_name, slice_ in (("is", is_candles), ("oos", oos_candles)):
                if len(slice_) < warmup * 2:
                    continue
                cfg = BacktestConfig(
                    start_date=slice_[0].timestamp,
                    end_date=slice_[-1].timestamp,
                    symbols=[symbol],
                    timeframe=timeframe,
                    strategy_names=[trial_name],
                )
                sim = BacktestSimulator(cfg)
                trades, equity = await sim.run({symbol: slice_}, registry)
                result = analyzer.analyze(trades, equity, cfg)
                if period_name == "is":
                    is_sharpes.append(result.sharpe_ratio)
                else:
                    oos_sharpes.append(result.sharpe_ratio)
                    split_results.append(result)
    finally:
        registry.unregister(trial_name)

    if not split_results:
        return {"insufficient_data": True}

    avg_is = mean(is_sharpes) if is_sharpes else 0.0
    avg_oos = mean(oos_sharpes) if oos_sharpes else 0.0
    degradation = avg_oos / avg_is if avg_is > 0 else 0.0
    neg_splits = [i for i, r in enumerate(split_results) if r.sharpe_ratio <= 0]
    is_consistent = (len(neg_splits) == 0) and degradation > 0.5

    return {
        "n_splits_achieved": len(split_results),
        "oos_window_days_approx": round(oos_size / 24, 1),
        "split_sharpes": [round(r.sharpe_ratio, 3) for r in split_results],
        "split_trades": [r.total_trades for r in split_results],
        "n_neg_splits": len(neg_splits),
        "would_pass_all_positive_and_degradation": is_consistent,
        "degradation_factor": round(degradation, 3),
        "median_oos_sharpe": round(median(oos_sharpes), 3) if oos_sharpes else None,
        "mean_oos_sharpe": round(avg_oos, 3),
    }


async def _check_parameter_stability(
    engine, registry, strategy_name, strategy_class, params_class, symbol,
    timeframe, candles, exchange_id, original_parameters,
):
    from sgr.strategy.parameter_optimizer import PARAMETER_SEARCH_SPACE, _param_grid

    space = PARAMETER_SEARCH_SPACE.get(strategy_name)
    if space is None:
        return {"performed": False, "reason": "no_search_space"}

    combos = _param_grid(space)
    total_bars = len(candles)
    third = total_bars // 3
    chunk_candles = candles[third : 2 * third]
    if len(chunk_candles) < 400:
        return {"performed": False, "reason": "insufficient_chunk_data"}

    chunk_start = chunk_candles[0].timestamp
    chunk_end = chunk_candles[-1].timestamp

    scored = []
    for overrides in combos:
        bt = await _run_single_backtest(
            engine, registry, strategy_class, params_class, overrides, symbol,
            timeframe, chunk_start, chunk_end, exchange_id, f"{strategy_name}__stab"
        )
        scored.append((overrides, bt.sharpe_ratio, bt.total_trades))

    qualifying = [s for s in scored if s[2] >= 10]
    if not qualifying:
        return {
            "performed": True,
            "reason": "no_qualifying_candidate_in_chunk",
            "chunk_window": [chunk_start.isoformat(), chunk_end.isoformat()],
        }

    best_overrides, best_sharpe, best_trades = max(qualifying, key=lambda s: s[1])
    original_relevant = {k: original_parameters.get(k) for k in space}
    matches_original = best_overrides == original_relevant

    return {
        "performed": True,
        "chunk_window": [chunk_start.isoformat(), chunk_end.isoformat()],
        "best_overrides_in_chunk": best_overrides,
        "best_sharpe_in_chunk": round(best_sharpe, 3),
        "best_trades_in_chunk": best_trades,
        "original_overrides": original_relevant,
        "matches_original": matches_original,
    }


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

    print(f"Loaded {len(rows)} candidates from {input_path}")

    per_candidate = []
    technical_errors = []
    warmup = 200

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

        total_bars = len(candles)
        if total_bars < warmup * 3:
            technical_errors.append(f"{symbol}/{strategy_name}: insufficient candles ({total_bars})")
            continue

        detail: dict = {"symbol": symbol, "strategy": strategy_name}

        try:
            pooled_is_bars = max(int(total_bars * 0.2), warmup * 2)
            pooled_start_ts = candles[pooled_is_bars].timestamp
            pooled_bt = await _run_single_backtest(
                engine, registry, strategy_class, params_class, row["parameters"],
                symbol, "1h", pooled_start_ts, vend, exchange_id, f"{strategy_name}__pooled",
            )
            detail["pooled_oos"] = {
                "start": pooled_start_ts.isoformat(),
                "window_days_approx": round((vend - pooled_start_ts).days, 1),
                "sharpe": pooled_bt.sharpe_ratio,
                "profit_factor": pooled_bt.profit_factor,
                "max_drawdown_pct": pooled_bt.max_drawdown_pct,
                "hit_rate_pct": pooled_bt.hit_rate_pct,
                "total_trades": pooled_bt.total_trades,
                "total_return_pct": pooled_bt.total_return_pct,
            }
        except Exception as e:  # noqa: BLE001
            technical_errors.append(f"{symbol}/{strategy_name} pooled-oos: {e}")
            detail["pooled_oos"] = {"error": str(e)[:200]}

        try:
            alt = await _run_custom_walk_forward(
                candles, symbol, "1h", strategy_class, params_class, row["parameters"],
                registry, n_splits=3, warmup=warmup, trial_prefix=f"{strategy_name}__alt3",
            )
            detail["alt_3split_longer_oos"] = alt
        except Exception as e:  # noqa: BLE001
            technical_errors.append(f"{symbol}/{strategy_name} alt-split: {e}")
            detail["alt_3split_longer_oos"] = {"error": str(e)[:200]}

        try:
            stability = await _check_parameter_stability(
                engine, registry, strategy_name, strategy_class, params_class,
                symbol, "1h", candles, exchange_id, row["parameters"],
            )
            detail["parameter_stability"] = stability
        except Exception as e:  # noqa: BLE001
            technical_errors.append(f"{symbol}/{strategy_name} stability: {e}")
            detail["parameter_stability"] = {"error": str(e)[:200]}

        per_candidate.append(detail)

        if (idx + 1) % 10 == 0:
            print(f"  processed {idx + 1}/{len(rows)}")

    # --- Aggregation ---
    pooled_sharpes = [
        c["pooled_oos"]["sharpe"] for c in per_candidate if "sharpe" in c.get("pooled_oos", {})
    ]
    pooled_pf = [
        c["pooled_oos"]["profit_factor"] for c in per_candidate if "profit_factor" in c.get("pooled_oos", {})
    ]
    pooled_dd = [
        c["pooled_oos"]["max_drawdown_pct"] for c in per_candidate if "max_drawdown_pct" in c.get("pooled_oos", {})
    ]
    pooled_trades = [
        c["pooled_oos"]["total_trades"] for c in per_candidate if "total_trades" in c.get("pooled_oos", {})
    ]
    pooled_pass_gate = sum(
        1
        for c in per_candidate
        if c.get("pooled_oos", {}).get("sharpe", -999) >= 1.0
        and c.get("pooled_oos", {}).get("profit_factor", 0) >= 1.3
        and c.get("pooled_oos", {}).get("max_drawdown_pct", 999) <= 20.0
        and c.get("pooled_oos", {}).get("total_trades", 0) >= 30
    )

    alt_would_pass = sum(
        1
        for c in per_candidate
        if c.get("alt_3split_longer_oos", {}).get("would_pass_all_positive_and_degradation")
    )
    alt_trade_counts = [
        t
        for c in per_candidate
        for t in c.get("alt_3split_longer_oos", {}).get("split_trades", [])
    ]

    stability_rows = [
        c["parameter_stability"] for c in per_candidate if c.get("parameter_stability", {}).get("performed")
    ]
    stability_evaluable = [s for s in stability_rows if "matches_original" in s]
    stability_matches = sum(1 for s in stability_evaluable if s["matches_original"])

    summary = {
        "n_candidates_processed": len(per_candidate),
        "n_technical_errors": len(technical_errors),
        "technical_errors": technical_errors[:20],
        "A_pooled_oos": {
            "median_sharpe": round(median(pooled_sharpes), 3) if pooled_sharpes else None,
            "mean_sharpe": round(mean(pooled_sharpes), 3) if pooled_sharpes else None,
            "median_profit_factor": round(median(pooled_pf), 3) if pooled_pf else None,
            "median_max_drawdown_pct": round(median(pooled_dd), 2) if pooled_dd else None,
            "median_trades": round(median(pooled_trades), 1) if pooled_trades else None,
            "n_passing_all_backtest_gates_when_pooled": pooled_pass_gate,
            "n_evaluated": len(pooled_sharpes),
        },
        "B_alt_3split_longer_oos": {
            "n_would_pass_all_positive_and_degradation": alt_would_pass,
            "n_evaluated": sum(
                1 for c in per_candidate if "would_pass_all_positive_and_degradation" in c.get("alt_3split_longer_oos", {})
            ),
            "median_trades_per_split": round(median(alt_trade_counts), 1) if alt_trade_counts else None,
            "min_trades_per_split": min(alt_trade_counts) if alt_trade_counts else None,
            "max_trades_per_split": max(alt_trade_counts) if alt_trade_counts else None,
        },
        "C_parameter_stability": {
            "n_evaluable": len(stability_evaluable),
            "n_matches_original_winner": stability_matches,
            "match_rate": round(stability_matches / len(stability_evaluable), 3)
            if stability_evaluable
            else None,
            "n_no_qualifying_candidate_in_chunk": sum(
                1 for s in stability_rows if s.get("reason") == "no_qualifying_candidate_in_chunk"
            ),
        },
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
