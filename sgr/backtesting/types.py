"""
SGR Backtesting Types
=====================
Alle Domain-Types für die Backtesting Engine.

Design-Prinzipien:
- Event-driven (nicht vektorisiert): identisches Verhalten wie Live-System
- Keine Look-Ahead-Leaks: strikte Zeitstempel-Reihenfolge
- Realistic Simulation: Slippage, Fees, Funding Rates
- Immutable Records: einmal geschrieben, nie verändert

Warum Event-driven statt Vectorized?
    Vectorized Backtesting (pandas-basiert) ist 10-100x schneller,
    aber es ist leicht Look-Ahead-Bias einzubauen (z.B. Close des
    aktuellen Bars als Entry-Preis verwenden).
    Event-driven zwingt zu realistischer Simulation: der Bot
    sieht immer nur vergangene Bars, nie den aktuellen Close.
    Für finale Validierung ist Event-driven Pflicht.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from sgr.core.types import MarketRegime


class BacktestStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class BacktestConfig:
    """
    Vollständige Konfiguration eines Backtests.
    Immutable: einmal konfiguriert, nicht veränderbar.
    """

    # Zeitraum
    start_date: datetime
    end_date: datetime

    # Symbol + Timeframe
    symbols: list[str]  # z.B. ["BTC/USDT", "ETH/USDT"]
    timeframe: str  # z.B. "1h"

    # Kapital
    initial_capital: Decimal = Decimal("10000")

    # Kosten-Simulation
    maker_fee: Decimal = Decimal("0.001")  # 0.1%
    taker_fee: Decimal = Decimal("0.001")  # 0.1%
    slippage_pct: Decimal = Decimal("0.0005")  # 0.05% Slippage

    # Strategien (Namen aus Registry)
    strategy_names: list[str] = field(default_factory=list)

    # Risk Limits (für Backtest-Risk-Engine)
    max_position_pct: float = 0.10
    max_portfolio_heat: float = 0.70
    max_drawdown_limit: float = 0.15

    # Walk-Forward
    walk_forward_splits: int = 0  # 0 = kein Walk-Forward

    # Monte Carlo
    monte_carlo_runs: int = 0  # 0 = kein Monte Carlo


@dataclass
class BacktestTrade:
    """Ein geschlossener Trade im Backtest. Immutable nach Erstellung."""

    id: str
    symbol: str
    strategy: str
    side: str  # "long" | "short"
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    gross_pnl: Decimal
    fees: Decimal
    slippage: Decimal
    net_pnl: Decimal
    holding_bars: int
    regime: MarketRegime
    max_adverse_excursion: Decimal  # MAE: max Verlust während Trade
    max_favorable_excursion: Decimal  # MFE: max Gewinn während Trade
    entry_signal_confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_winner(self) -> bool:
        return self.net_pnl > 0

    @property
    def return_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return float((self.exit_price - self.entry_price) / self.entry_price)


@dataclass
class EquityCurvePoint:
    """Ein Punkt auf der Equity-Kurve."""

    timestamp: datetime
    portfolio_value: Decimal
    cash: Decimal
    open_positions_value: Decimal
    drawdown_pct: float
    daily_return: float


class BacktestResult(BaseModel):
    """
    Vollständiges Backtesting-Ergebnis.
    Enthält alle KPIs, Trade-Liste und Equity-Kurve.
    """

    config_summary: dict[str, Any]
    status: BacktestStatus
    start_date: str
    end_date: str
    duration_days: int

    # KPIs
    initial_capital: str
    final_capital: str
    total_return_pct: float
    cagr_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown_pct: float
    max_drawdown_duration_days: int
    profit_factor: float
    hit_rate_pct: float
    expected_value_per_trade: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    avg_winner: str
    avg_loser: str
    avg_holding_bars: float
    total_fees: str
    total_slippage: str

    # Per-Strategie Breakdown
    strategy_breakdown: dict[str, dict[str, Any]] = {}

    # Per-Regime Breakdown
    regime_breakdown: dict[str, dict[str, Any]] = {}

    # Equity Kurve (für Chart)
    equity_curve: list[dict[str, Any]] = []

    # Alle Trades
    trades: list[dict[str, Any]] = []

    # Validierung
    go_live_eligible: bool = False
    go_live_blockers: list[str] = []

    @property
    def is_acceptable(self) -> bool:
        """Erfüllt Mindestanforderungen für Live-Trading."""
        return (
            self.sharpe_ratio >= 1.0
            and self.profit_factor >= 1.3
            and self.max_drawdown_pct <= 20.0
            and self.hit_rate_pct >= 40.0
            and self.total_trades >= 30
        )


class WalkForwardResult(BaseModel):
    """Ergebnis einer Walk-Forward-Analyse."""

    n_splits: int
    split_results: list[BacktestResult]
    is_consistent: bool  # Performance konsistent über Splits?
    consistency_score: float  # 0.0–1.0
    in_sample_sharpe: float
    out_of_sample_sharpe: float
    degradation_factor: float  # OOS Sharpe / IS Sharpe (< 1 = Overfitting)
    recommendation: str


class MonteCarloResult(BaseModel):
    """Ergebnis einer Monte-Carlo-Simulation."""

    n_simulations: int
    median_return_pct: float
    percentile_5_return_pct: float  # Worst Case (5%)
    percentile_95_return_pct: float  # Best Case (95%)
    median_max_drawdown_pct: float
    percentile_95_max_drawdown_pct: float
    ruin_probability: float  # P(Drawdown > 50%)
    sharpe_distribution: dict[str, float]  # min, p25, median, p75, max
