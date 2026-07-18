"""SGR Backtesting Engine"""

from sgr.backtesting.data_loader import BacktestDataLoader
from sgr.backtesting.engine import BacktestingEngine, FullValidationReport
from sgr.backtesting.performance import PerformanceAnalyzer
from sgr.backtesting.simulator import BacktestSimulator
from sgr.backtesting.types import (
    BacktestConfig,
    BacktestResult,
    BacktestTrade,
    EquityCurvePoint,
    MonteCarloResult,
    WalkForwardResult,
)
from sgr.backtesting.validation import MonteCarloAnalyzer, WalkForwardAnalyzer

__all__ = [
    "BacktestingEngine",
    "FullValidationReport",
    "BacktestSimulator",
    "PerformanceAnalyzer",
    "WalkForwardAnalyzer",
    "MonteCarloAnalyzer",
    "BacktestDataLoader",
    "BacktestConfig",
    "BacktestResult",
    "BacktestTrade",
    "EquityCurvePoint",
    "WalkForwardResult",
    "MonteCarloResult",
]
