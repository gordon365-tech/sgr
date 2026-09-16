#!/usr/bin/env python3
"""
Batch-Ausfuehrung: Backtest+Walk-Forward-Validierung fuer ALLE dynamisch
entdeckten Symbole (Autonomous-Strategy-Universe-Rollout, Phase 17
"Batch Execution" + Phase 20 "Real Full Run").

Resumable + idempotent: ein --batch-id identifiziert einen Lauf; bei
einem Absturz/Abbruch setzt ein erneuter Aufruf mit demselben
--batch-id automatisch bei den noch nicht verarbeiteten Symbolen fort
(siehe StrategySymbolValidationRepository.get_processed_symbols() /
SymbolStrategyValidationRunner.run_batch(resume=True)). Ein Fehler bei
einem einzelnen Symbol bricht den Batch NICHT ab (siehe
SymbolStrategyValidationRunner._validate_symbol Fehlerbehandlung).

Usage (im sgr-api oder sgr-worker Container):

    python scripts/run_symbol_strategy_validation.py \\
        --batch-id full_universe_2026-09-15 \\
        [--limit 50]        # nur die ersten N Symbole (fuer Testlaeufe)
        [--symbols BTC/USDT,ETH/USDT]  # explizite Liste statt Discovery
        [--no-resume]        # erzwingt kompletten Neulauf, auch fuer
                              # bereits im Batch verarbeitete Symbole

Importiert alle registrierten Strategie-Module explizit (wie
sgr/api/main.py) - die @StrategyRegistry.register-Decorators laufen
zur Modul-Importzeit, ein Standalone-Skript ausserhalb des vollen
Worker-Lifespans muss das selbst anstossen.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime


async def _main(
    batch_id: str,
    limit: int | None,
    explicit_symbols: list[str] | None,
    resume: bool,
    timeframe: str,
    lookback_days: int,
) -> int:
    # Strategie-Registrierung anstossen (siehe Modul-Docstring).
    import sgr.strategy.breakout  # noqa: F401
    import sgr.strategy.mean_reversion  # noqa: F401
    import sgr.strategy.momentum  # noqa: F401
    import sgr.strategy.trend_following  # noqa: F401
    import sgr.strategy.volatility_adjusted_momentum  # noqa: F401
    from sgr.core.database import close_db, init_db
    from sgr.strategy.symbol_validation_runner import (
        SymbolStrategyValidationRunner,
        discover_binance_universe,
    )

    await init_db()
    try:
        if explicit_symbols:
            symbols = explicit_symbols
        else:
            print("Discovering dynamic Binance symbol universe (public mainnet API)...")
            symbols = await discover_binance_universe()
            print(f"Discovered {len(symbols)} TRADABLE+ USDT perpetual symbols.")

        if limit:
            symbols = symbols[:limit]

        print(
            f"Running batch_id={batch_id!r} for {len(symbols)} symbols "
            f"(timeframe={timeframe}, lookback_days={lookback_days}, resume={resume})"
        )

        runner = SymbolStrategyValidationRunner(timeframe=timeframe, lookback_days=lookback_days)
        started_at = datetime.now(tz=UTC)
        summary = await runner.run_batch(symbols, batch_id=batch_id, resume=resume)
        duration = (datetime.now(tz=UTC) - started_at).total_seconds()

        report = {
            "batch_id": batch_id,
            "started_at": started_at.isoformat(),
            "duration_seconds": round(duration, 1),
            "total_symbols_requested": len(symbols),
            "total_processed_this_run": summary.total,
            "by_status": summary.by_status,
            "technical_failures": summary.technical_failures,
        }
        print(json.dumps(report, indent=2))
        return 0
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--batch-id", required=True, help="Stable identifier for this run (resumable)"
    )
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N symbols")
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="Comma-separated explicit symbol list, skips dynamic discovery",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Force full re-run, even for symbols already processed in this batch",
    )
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--lookback-days", type=int, default=180)
    args = parser.parse_args()

    explicit_symbols = args.symbols.split(",") if args.symbols else None

    exit_code = asyncio.run(
        _main(
            batch_id=args.batch_id,
            limit=args.limit,
            explicit_symbols=explicit_symbols,
            resume=not args.no_resume,
            timeframe=args.timeframe,
            lookback_days=args.lookback_days,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
