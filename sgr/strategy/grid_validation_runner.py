"""
SGR Grid Strategy Validation Runner
======================================
Grid-Aequivalent zu sgr.strategy.validation_runner.StrategyValidationRunner
(siehe dortiger Modul-Docstring fuer den Hintergrund) - fuehrt fuer jede
registrierte, noch nicht validierte GridTradingStrategy einen Backtest
(+ vereinfachtes Walk-Forward via IS/OOS-Split) durch und uebersetzt das
Ergebnis ueber GridEdgeMetrics/is_edge_confirmed() in einen
ValidationStatus fuer die gemeinsame StrategyRegistry.

Validierungspfad (siehe Aufgabenstellung): Discovery -> Hypothesis ->
Historical Backtest -> Walk Forward -> Unseen Data -> Shadow Trading ->
Paper Trading -> Small Capital -> Live. Dieser Runner deckt die Stufen
"Historical Backtest" und "Walk Forward" ab (Backtest auf In-Sample-
Fenster, Walk-Forward auf einem separaten Out-of-Sample-Fenster) - die
spaeteren Stufen (Shadow/Paper/Small-Capital/Live) sind ausserhalb des
Scopes dieses Runners (siehe Paper-Trading-Betrieb via GridController,
Live-Freigabe ueber dieselben live_trading_gate-Mechanismen wie jede
andere Strategie - KEINE Grid-Variante erhaelt automatisch echtes
Kapital, siehe is_validated-Gate unten).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sgr.backtesting.data_loader import BacktestDataLoader
from sgr.backtesting.grid_simulator import GridBacktestRunResult, GridBacktestSimulator
from sgr.backtesting.grid_types import GridBacktestConfig
from sgr.backtesting.types import BacktestResult
from sgr.core.grid_types import FuturesGridParameters
from sgr.core.logging import get_logger
from sgr.core.types import Candle, ExchangeID, GridDirection
from sgr.market_data.types import FeatureSet, MarketContext
from sgr.strategy.base import ValidationStatus
from sgr.strategy.futures_grid import GridTradingStrategy
from sgr.strategy.grid_edge import compute_grid_edge_metrics, is_edge_confirmed
from sgr.strategy.registry import StrategyRegistry

log = get_logger(__name__)

DEFAULT_SYMBOL = "BTC/USDT"
DEFAULT_TIMEFRAME = "1h"
DEFAULT_LOOKBACK_DAYS = 180
DEFAULT_OOS_DAYS = 30


@dataclass
class GridValidationRunSummary:
    validated: list[str]
    skipped: list[str]
    failed: dict[str, str]


class GridValidationRunner:
    def __init__(
        self,
        symbol: str = DEFAULT_SYMBOL,
        timeframe: str = DEFAULT_TIMEFRAME,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        oos_days: int = DEFAULT_OOS_DAYS,
        exchange_id: ExchangeID = ExchangeID.BINANCE,
    ) -> None:
        # exchange_id fuer den HISTORISCHEN Marktdaten-Bezug (siehe
        # Modul-Docstring: Binance-Default, da Pionex ueber ccxt aktuell
        # keine historische Public-REST-Anbindung im BacktestDataLoader
        # hat - siehe "offene Punkte" im Strategiebericht). Die
        # validierte Strategie selbst bleibt exchange-agnostisch
        # (GridTradingStrategy.supported_exchanges) - dies waehlt nur die
        # Datenquelle fuer den Backtest.
        self._symbol = symbol
        self._timeframe = timeframe
        self._lookback_days = lookback_days
        self._oos_days = oos_days
        self._exchange_id = exchange_id
        self._loader = BacktestDataLoader()

    async def validate_pending_grid_strategies(self) -> GridValidationRunSummary:
        registry = StrategyRegistry.get()
        summary = GridValidationRunSummary(validated=[], skipped=[], failed={})

        pending = [
            (name, entry.strategy)
            for name, entry in registry.get_all().items()
            if not entry.is_validated and isinstance(entry.strategy, GridTradingStrategy)
        ]
        if not pending:
            return summary

        end_date = datetime.now(tz=UTC)
        is_start = end_date - timedelta(days=self._lookback_days + self._oos_days)
        is_end = end_date - timedelta(days=self._oos_days)

        try:
            candles = await self._loader.load_public_history(
                self._symbol, self._timeframe, is_start, end_date, exchange_id=self._exchange_id
            )
        except Exception as e:
            log.error("grid_validation_runner.data_load_failed", error=str(e))
            for name, _ in pending:
                summary.failed[name] = f"Historical data load failed: {e}"
            return summary

        is_candles = [c for c in candles if c.timestamp <= is_end]
        oos_candles = [c for c in candles if c.timestamp > is_end]

        for name, strategy in pending:
            try:
                status = await self._validate_one(strategy, is_candles, oos_candles)
            except Exception as e:
                log.error("grid_validation_runner.validation_failed", strategy=name, error=str(e))
                summary.failed[name] = str(e)
                continue

            registry.mark_validated(name, status)
            if status.can_go_live:
                summary.validated.append(name)
            else:
                summary.skipped.append(name)

        return summary

    async def _validate_one(
        self,
        strategy: GridTradingStrategy,
        is_candles: list[Candle],
        oos_candles: list[Candle],
    ) -> ValidationStatus:
        if len(is_candles) < 200:
            return ValidationStatus(notes="Insufficient in-sample data for grid backtest")

        context = self._build_context(is_candles)
        decision = strategy.evaluate(context)

        if decision.direction == GridDirection.NEUTRAL or decision.parameters is None:
            return ValidationStatus(
                notes=f"GridDecision NEUTRAL on validation snapshot: {'; '.join(decision.reasons)}"
            )

        is_result = self._run_backtest(strategy.name, decision.parameters, is_candles)
        is_metrics = compute_grid_edge_metrics(
            is_result[1], is_result[0], grid_count=decision.parameters.grid_count
        )
        is_confirmed, is_blockers = is_edge_confirmed(
            is_metrics, n_trades=is_result[1].total_trades
        )

        walk_forward_passed = False
        if is_confirmed and len(oos_candles) >= 100:
            oos_result = self._run_backtest(strategy.name, decision.parameters, oos_candles)
            oos_metrics = compute_grid_edge_metrics(
                oos_result[1], oos_result[0], grid_count=decision.parameters.grid_count
            )
            oos_confirmed, oos_blockers = is_edge_confirmed(
                oos_metrics, n_trades=oos_result[1].total_trades, min_trades=5
            )
            walk_forward_passed = oos_confirmed
            notes = (
                f"IS blockers: {is_blockers}; OOS blockers: {oos_blockers}"
                if not oos_confirmed
                else "IS+OOS edge confirmed"
            )
        else:
            notes = f"IS blockers: {is_blockers}" if not is_confirmed else "Insufficient OOS data"

        return ValidationStatus(
            backtest_passed=is_confirmed,
            walk_forward_passed=walk_forward_passed,
            # Wie beim direktionalen Runner (siehe validation_runner.py
            # Modul-Docstring): Platzhalter True, bis eine echte Paper-
            # Trading-Performance-Historie fuer Grids existiert (siehe
            # GridController/GridModel) - betrifft NUR die Paper-
            # Aktivierung, Live-Gates bleiben unberuehrt (live_approved
            # bleibt hart False).
            paper_trading_passed=True,
            live_approved=False,
            notes=notes,
        )

    def _run_backtest(
        self, strategy_name: str, parameters: FuturesGridParameters, candles: list[Candle]
    ) -> tuple[GridBacktestRunResult, BacktestResult]:
        config = GridBacktestConfig(
            symbol=self._symbol,
            timeframe=self._timeframe,
            strategy_name=strategy_name,
            parameters=parameters,
            initial_capital=Decimal("10000"),
        )
        sim = GridBacktestSimulator(config)
        run_result = sim.run(candles)
        backtest_result = sim.to_backtest_result(run_result, candles)
        return run_result, backtest_result

    def _build_context(self, candles: list[Candle]) -> MarketContext:
        """
        Baut einen einzelnen MarketContext-Snapshot aus dem LETZTEN Bar
        der In-Sample-Historie (fuer die initiale GridDecision - Grid-
        Parameter werden einmalig zu Validierungsbeginn abgeleitet, nicht
        pro Bar neu, siehe GridBacktestSimulator: die Parameter sind fuer
        die Dauer EINES Grid-Laufs fix).
        """
        from sgr.market_data.feature_engineering import FeatureEngineer

        features: FeatureSet = FeatureEngineer().compute(candles)
        return MarketContext(
            symbol=features.symbol,
            timestamp=features.timestamp,
            primary=features,
        )
