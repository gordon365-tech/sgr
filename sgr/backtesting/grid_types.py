"""
SGR Futures Grid Backtesting - Types
======================================
Konfiguration fuer den Futures-Grid-Backtest (siehe grid_simulator.py).

Wiederverwendung statt Neuerfindung: BacktestTrade/EquityCurvePoint/
BacktestConfig/BacktestResult (sgr/backtesting/types.py) sind bereits
generisch genug (string-basierte symbol/strategy/side-Felder), um jeden
abgeschlossenen Grid-Zyklus als BacktestTrade zu repraesentieren -
GridBacktestSimulator erzeugt deshalb eine normale BacktestTrade-Liste
und laesst PerformanceAnalyzer (unveraendert) daraus einen BacktestResult
berechnen. Kein Parallel-Code fuer Sharpe/Sortino/Drawdown.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sgr.core.grid_types import FuturesGridParameters


@dataclass(frozen=True)
class GridBacktestConfig:
    """Konfiguration eines einzelnen Futures-Grid-Backtests."""

    symbol: str  # z.B. "BTC/USDT"
    timeframe: str
    strategy_name: str
    parameters: FuturesGridParameters

    initial_capital: Decimal = Decimal("10000")
    maker_fee: Decimal = Decimal("0.0002")  # Grid-Orders sind ueberwiegend Maker-Fills
    taker_fee: Decimal = Decimal("0.0005")  # bei erzwungenem Markt-Exit (SL/Liquidation)
    slippage_pct: Decimal = Decimal("0.0003")

    # Funding: SGR hat in Backtests keine historische Funding-Rate-Zeitreihe
    # verfuegbar (Candles tragen keine Funding-Daten) - ein konstanter,
    # konfigurierbarer Durchschnittswert wird stattdessen angenommen. Das
    # ist eine bewusste, dokumentierte Vereinfachung (siehe Modul-Docstring
    # von grid_simulator.py) - kein Ersatz fuer eine spaetere Integration
    # echter historischer Funding-Raten.
    assumed_funding_rate_per_interval: Decimal = Decimal("0.0001")
    funding_interval_hours: int = 8

    # Breakout-Schutz: wird der Preis um mehr als diesen Faktor der
    # urspruenglichen Range-Breite ausserhalb der Grid-Range gehandelt,
    # wird das Grid als "Range gebrochen" geschlossen (siehe
    # Aufgabenstellung: "extreme Volatilitaet -> Grid ggf. deaktivieren").
    range_breakout_buffer_factor: float = 0.5
