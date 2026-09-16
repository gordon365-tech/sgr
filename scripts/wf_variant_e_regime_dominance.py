#!/usr/bin/env python3
"""
Variante E — Preis-Regime-Dominanz-Check (Implementierung)
=============================================================
Siehe /tmp/sgr-wf-validation-decision.md, Abschnitt "Variante E" fuer die
konzeptionelle Herleitung. Rein additiv: kein neuer Optimierungslauf,
keine Gate-Aenderung, keine Aktivierung.

E1 (preisbasiert, trade-unabhaengig):
    Segmentiert das Pooled-OOS-Fenster (identisch zu wf_methodology_
    experiment.py: validation_start + max(20% total_bars, 2*warmup) bis
    validation_end) in feste Kalender-Chunks (mehrere Chunk-Groessen
    getestet, siehe CHUNK_SIZES_DAYS). Pro Chunk: High, Low, Preis-
    spannweite (high_max/low_min - 1), Anteil an der kumulierten
    absoluten Chunk-Bewegung, Abweichung vom Median. Extremchunk = Anteil
    an kumulierter Bewegung > EXTREME_SHARE_THRESHOLD ODER Spannweite >
    EXTREME_MEDIAN_MULTIPLE x Median-Spannweite.

E2 (performance-basiert, Removal-Test):
    Nutzt dieselben Pooled-OOS-Trades wie Variante D (erneuter Backtest
    mit identischen, bereits persistierten Parametern - kein neuer
    Parameter-Search). Trades, deren Entry-Timestamp in einen
    identifizierten Extremchunk faellt, werden entfernt. Trade Count,
    Return, Sharpe (pseudo, gleiche Formel wie Variante D) und PnL vor/
    nach Removal werden verglichen.

Usage:
    python scripts/wf_variant_e_regime_dominance.py --input winners.psv --output report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from datetime import datetime
from uuid import uuid4

# Mehrere Chunk-Groessen, wie explizit gefordert - keine einzelne wird als
# endgueltige Produktionsregel festgelegt, die Sensitivitaet wird
# stattdessen gemessen und berichtet (siehe summary["by_chunk_days"]).
CHUNK_SIZES_DAYS = [3, 7]

EXTREME_SHARE_THRESHOLD = 0.35
EXTREME_MEDIAN_MULTIPLE = 5.0


def _pseudo_sharpe(pnls: list[float], initial_capital: float) -> float:
    """Identisch zu wf_variant_d_tail_check.py's Formel, fuer
    Vergleichbarkeit zwischen Varianten D und E."""
    import math

    if not pnls or len(pnls) < 2:
        return 0.0
    returns = [p / initial_capital for p in pnls]
    std = statistics.pstdev(returns)
    if std == 0:
        return 0.0
    return statistics.mean(returns) / std * math.sqrt(252)


def _segment_into_chunks(candles, chunk_days: int):
    """Teilt eine Candle-Liste in feste Kalender-Chunks (chunk_days Tage
    je Chunk, letzter Chunk darf kuerzer sein). Gibt Liste von
    (chunk_start, chunk_end, candles_in_chunk) zurueck."""
    if not candles:
        return []
    chunk_seconds = chunk_days * 86400
    chunks = []
    chunk_start_ts = candles[0].timestamp
    current_chunk: list = []
    for c in candles:
        if (c.timestamp - chunk_start_ts).total_seconds() >= chunk_seconds and current_chunk:
            chunks.append((chunk_start_ts, current_chunk[-1].timestamp, current_chunk))
            current_chunk = []
            chunk_start_ts = c.timestamp
        current_chunk.append(c)
    if current_chunk:
        chunks.append((chunk_start_ts, current_chunk[-1].timestamp, current_chunk))
    return chunks


def _analyze_chunks_e1(candles, chunk_days: int) -> dict:
    """E1: reine Preisdaten-Analyse, keine Trades noetig."""
    chunks = _segment_into_chunks(candles, chunk_days)
    if len(chunks) < 2:
        return {"performed": False, "reason": "too_few_chunks"}

    chunk_stats = []
    for start, end, ccs in chunks:
        hi = max(float(c.high) for c in ccs)
        lo = min(float(c.low) for c in ccs)
        span = (hi / lo - 1.0) if lo > 0 else 0.0
        chunk_stats.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "high": hi,
                "low": lo,
                "span": span,
            }
        )

    total_span = sum(cs["span"] for cs in chunk_stats)
    spans = [cs["span"] for cs in chunk_stats]
    median_span = statistics.median(spans)

    for cs in chunk_stats:
        cs["share_of_total_movement"] = cs["span"] / total_span if total_span > 0 else 0.0
        cs["multiple_of_median"] = (cs["span"] / median_span) if median_span > 0 else 0.0
        cs["is_extreme"] = (
            cs["share_of_total_movement"] > EXTREME_SHARE_THRESHOLD
            or cs["multiple_of_median"] > EXTREME_MEDIAN_MULTIPLE
        )

    extreme_chunks = [cs for cs in chunk_stats if cs["is_extreme"]]
    return {
        "performed": True,
        "n_chunks": len(chunk_stats),
        "median_span": median_span,
        "n_extreme_chunks": len(extreme_chunks),
        "extreme_chunks": extreme_chunks,
        "chunk_stats": chunk_stats,
    }


async def _run_pooled_backtest_get_trades(
    engine, registry, strategy_class, params_class, overrides, symbol, timeframe,
    candles, warmup, trial_prefix,
):
    from sgr.backtesting.simulator import BacktestSimulator
    from sgr.backtesting.types import BacktestConfig
    from sgr.strategy.parameter_optimizer import build_trial_strategy

    total_bars = len(candles)
    pooled_is_bars = max(int(total_bars * 0.2), warmup * 2)
    pooled_candles = candles[pooled_is_bars:]
    if len(pooled_candles) < warmup * 2:
        return None, None

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
        return trades, pooled_candles
    finally:
        registry.unregister(trial_name)


def _e2_removal_test(trades, extreme_chunks: list[dict], initial_capital: float) -> dict:
    """E2: entfernt Trades, deren Entry-Timestamp in einen Extremchunk
    faellt, und berechnet Kennzahlen vor/nach erneut."""
    if not trades:
        return {"performed": False, "reason": "no_trades"}

    def in_extreme(ts: datetime) -> bool:
        for ch in extreme_chunks:
            start = datetime.fromisoformat(ch["start"])
            end = datetime.fromisoformat(ch["end"])
            if start <= ts <= end:
                return True
        return False

    all_pnls = [float(t.net_pnl) for t in trades]
    kept_trades = [t for t in trades if not in_extreme(t.entry_time)]
    kept_pnls = [float(t.net_pnl) for t in kept_trades]
    removed_count = len(trades) - len(kept_trades)

    return {
        "performed": True,
        "n_trades_before": len(trades),
        "n_trades_removed": removed_count,
        "n_trades_after": len(kept_trades),
        "total_pnl_before": round(sum(all_pnls), 2),
        "total_pnl_after": round(sum(kept_pnls), 2),
        "pseudo_sharpe_before": round(_pseudo_sharpe(all_pnls, initial_capital), 3),
        "pseudo_sharpe_after": round(_pseudo_sharpe(kept_pnls, initial_capital), 3),
        "sign_flip_to_negative": (
            _pseudo_sharpe(all_pnls, initial_capital) > 0
            and _pseudo_sharpe(kept_pnls, initial_capital) <= 0
        ),
        "regime_dominance_suspected": (
            removed_count > 0
            and _pseudo_sharpe(all_pnls, initial_capital) > 0
            and _pseudo_sharpe(kept_pnls, initial_capital) <= 0
        ),
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

        total_bars = len(candles)
        if total_bars < warmup * 3:
            technical_errors.append(f"{symbol}/{strategy_name}: insufficient candles")
            continue

        pooled_is_bars = max(int(total_bars * 0.2), warmup * 2)
        pooled_candles = candles[pooled_is_bars:]

        detail: dict = {"symbol": symbol, "strategy": strategy_name, "by_chunk_days": {}}

        try:
            trades, _ = await _run_pooled_backtest_get_trades(
                engine, registry, strategy_class, params_class, row["parameters"],
                symbol, "1h", candles, warmup, f"{strategy_name}__ve",
            )
        except Exception as e:  # noqa: BLE001
            technical_errors.append(f"{symbol}/{strategy_name} pooled-backtest: {e}")
            continue

        if trades is None:
            technical_errors.append(f"{symbol}/{strategy_name}: no pooled trades")
            continue

        for chunk_days in CHUNK_SIZES_DAYS:
            e1 = _analyze_chunks_e1(pooled_candles, chunk_days)
            if not e1.get("performed"):
                detail["by_chunk_days"][str(chunk_days)] = {"e1": e1, "e2": {"performed": False}}
                continue
            e2 = _e2_removal_test(trades, e1["extreme_chunks"], initial_capital)
            detail["by_chunk_days"][str(chunk_days)] = {
                "e1": {k: v for k, v in e1.items() if k != "chunk_stats"},
                "e2": e2,
            }

        per_candidate.append(detail)

        if (idx + 1) % 10 == 0:
            print(f"  processed {idx + 1}/{len(rows)}")

    # --- Aggregation pro Chunk-Groesse ---
    summary_by_chunk = {}
    for chunk_days in CHUNK_SIZES_DAYS:
        key = str(chunk_days)
        n_extreme_flagged = sum(
            1
            for c in per_candidate
            if c["by_chunk_days"].get(key, {}).get("e1", {}).get("n_extreme_chunks", 0) > 0
        )
        n_regime_dominance_suspected = sum(
            1
            for c in per_candidate
            if c["by_chunk_days"].get(key, {}).get("e2", {}).get("regime_dominance_suspected")
        )
        n_evaluated = sum(
            1
            for c in per_candidate
            if c["by_chunk_days"].get(key, {}).get("e1", {}).get("performed")
        )
        summary_by_chunk[key] = {
            "n_evaluated": n_evaluated,
            "n_with_extreme_chunk_e1": n_extreme_flagged,
            "n_regime_dominance_suspected_e2": n_regime_dominance_suspected,
            "selectivity_rate_e2": round(n_regime_dominance_suspected / n_evaluated, 3)
            if n_evaluated
            else None,
        }

    def find(symbol: str) -> dict | None:
        for c in per_candidate:
            if c["symbol"] == symbol:
                return c
        return None

    taiko = find("TAIKO/USDT")
    tut = find("TUT/USDT")

    result = {
        "summary": {
            "n_candidates_processed": len(per_candidate),
            "n_technical_errors": len(technical_errors),
            "technical_errors": technical_errors[:20],
            "chunk_sizes_tested_days": CHUNK_SIZES_DAYS,
            "by_chunk_days": summary_by_chunk,
            "TAIKO_USDT_result": taiko,
            "TUT_USDT_result": tut,
        },
        "per_candidate_detail": per_candidate,
    }

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(result["summary"], indent=2, default=str))
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
