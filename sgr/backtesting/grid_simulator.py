"""
SGR Futures Grid Backtesting Simulator
=========================================
Event-driven Backtest-Simulation fuer Futures Grid Strategien (siehe
sgr/strategy/futures_grid.py) - analog zu sgr/backtesting/simulator.py
(BacktestSimulator) fuer klassische Directional-Strategien, aber mit
grundlegend anderer Mechanik: mehrere gleichzeitig offene Preis-Levels
statt einer einzelnen Position.

Wichtig - was dieser Backtest NICHT ist:
    Kein reines Aufsummieren des theoretischen Grid-Gewinns (siehe
    Aufgabenstellung: "Ein Backtest darf nicht nur den theoretischen
    Grid Gewinn summieren"). Stattdessen wird explizit modelliert:
        - tatsaechliche Kapitalbindung (Margin pro offenem Level)
        - Fees pro Fill
        - Slippage pro Fill
        - Leverage (Margin = Notional / Leverage)
        - ein NAEHERUNGSWEISES Liquidationsrisiko (siehe
          _estimate_liquidation_price() Docstring fuer die
          Vereinfachungen)
        - Funding-Kosten (angenommene konstante Rate - siehe
          GridBacktestConfig.assumed_funding_rate_per_interval
          Docstring: SGR hat keine historische Funding-Zeitreihe in
          Candles verfuegbar)
        - Stop-Loss/Take-Profit/Max-Holding-Time auf Grid-Ebene
        - Range-Breakout-Schutz (Grid wird geschlossen, wenn der Preis
          weit ausserhalb der urspruenglichen Range laeuft)

Bekannte, explizit dokumentierte Vereinfachungen (analog zum bestehenden
BacktestSimulator, der ebenfalls "vereinfachend, genug fuer MVP" an
mehreren Stellen dokumentiert):
    - Intrabar-Fill-Reihenfolge wird aus der Bar-Richtung (Close vs.
      Open) approximiert, nicht aus echten Tick-Daten abgeleitet.
    - Liquidationspreis ist eine vereinfachte Naeherung (kein exaktes
      Maintenance-Margin-Modell der jeweiligen Exchange).
    - Funding-Rate ist ein konstant angenommener Durchschnittswert.
    - Keine partiellen Fills pro Level (ein Level ist entweder
      vollstaendig gefuellt oder nicht - realistisch fuer die relativ
      kleinen, gleich grossen Order-Groessen eines Grids).

Diese Vereinfachungen sind bewusst und muessen bei einer spaeteren
Kapital-Freigabe (Shadow Trading, Paper Trading, Small Capital - siehe
Aufgabenstellung Validierungspfad) durch echte Ausfuehrungsdaten bestaetigt
werden, nicht nur durch dieses Backtest-Ergebnis allein.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import numpy as np

from sgr.backtesting.grid_types import GridBacktestConfig
from sgr.backtesting.performance import PerformanceAnalyzer
from sgr.backtesting.types import BacktestConfig, BacktestResult, BacktestTrade, EquityCurvePoint
from sgr.core.grid_types import FuturesGridParameters
from sgr.core.logging import get_logger
from sgr.core.types import Candle, GridDirection, MarketRegime
from sgr.market_data.feature_engineering import calc_adx, candles_to_arrays

log = get_logger(__name__)

_TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "8h": 480,
    "12h": 720,
    "1d": 1440,
}


@dataclass
class _CellState:
    index: int
    entry_price: Decimal
    exit_price: Decimal
    is_open: bool = False
    opened_at: datetime | None = None
    opened_bar_index: int = 0
    quantity: Decimal = Decimal("0")


@dataclass
class GridBacktestRunResult:
    """Rohergebnis eines Grid-Backtest-Laufs, bevor PerformanceAnalyzer
    daraus KPIs berechnet (siehe GridBacktestSimulator.run())."""

    trades: list[BacktestTrade]
    equity_curve: list[EquityCurvePoint]
    close_reason: str
    total_funding_paid: Decimal
    fills_count: int
    max_concurrent_open_cells: int
    liquidated: bool = False
    metadata: dict = field(default_factory=dict)


class GridBacktestSimulator:
    """
    Event-driven Simulator fuer EIN Futures Grid ueber eine Candle-Serie.

    Usage:
        sim = GridBacktestSimulator(config)
        run_result = sim.run(candles)
        backtest_result = sim.to_backtest_result(run_result, candles)
    """

    WARMUP_BARS = 20  # nur fuer ADX/ATR-Regime-Tagging, kein Signal-Warmup noetig

    def __init__(self, config: GridBacktestConfig) -> None:
        self._config = config
        self._params: FuturesGridParameters = config.parameters
        self._levels = self._params.compute_levels()
        self._n_cells = len(self._levels) - 1
        self._is_long = self._params.long_or_short == GridDirection.LONG

    def run(self, candles: list[Candle]) -> GridBacktestRunResult:
        if len(candles) < 2:
            return GridBacktestRunResult(
                trades=[], equity_curve=[], close_reason="insufficient_data",
                total_funding_paid=Decimal("0"), fills_count=0, max_concurrent_open_cells=0,
            )

        regimes = self._precompute_regimes(candles)

        cells: list[_CellState] = []
        for i in range(self._n_cells):
            if self._is_long:
                cells.append(
                    _CellState(index=i, entry_price=self._levels[i], exit_price=self._levels[i + 1])
                )
            else:
                cells.append(
                    _CellState(index=i, entry_price=self._levels[i + 1], exit_price=self._levels[i])
                )

        trades: list[BacktestTrade] = []
        equity_curve: list[EquityCurvePoint] = []
        capital = self._config.initial_capital
        realized_pnl = Decimal("0")
        total_funding = Decimal("0")
        fills_count = 0
        max_open = 0

        range_width = self._params.grid_upper_price - self._params.grid_lower_price
        buffer = range_width * Decimal(str(self._config.range_breakout_buffer_factor))
        breakout_lower = self._params.grid_lower_price - buffer
        breakout_upper = self._params.grid_upper_price + buffer

        bar_minutes = _TIMEFRAME_MINUTES.get(self._config.timeframe, 60)
        funding_interval_bars = max(
            1, int(self._config.funding_interval_hours * 60 / bar_minutes)
        )

        start_time = candles[0].timestamp
        close_reason = "backtest_end"
        liquidated = False

        for bar_idx, bar in enumerate(candles):
            regime = regimes[bar_idx]

            # 1. Fills fuer diesen Bar (Reihenfolge approximiert aus
            # Bar-Richtung - siehe Modul-Docstring).
            bar_is_up = bar.close >= bar.open
            if self._is_long:
                actions = (
                    [self._try_close_cells, self._try_open_cells]
                    if bar_is_up
                    else [self._try_open_cells, self._try_close_cells]
                )
            else:
                actions = (
                    [self._try_open_cells, self._try_close_cells]
                    if bar_is_up
                    else [self._try_close_cells, self._try_open_cells]
                )

            for action in actions:
                new_trades, delta_realized, n_fills = action(cells, bar, bar_idx, regime)
                trades.extend(new_trades)
                realized_pnl += delta_realized
                fills_count += n_fills

            open_count = sum(1 for c in cells if c.is_open)
            max_open = max(max_open, open_count)

            # 2. Funding (periodisch, auf aktuell offene Notional-Exposure)
            if bar_idx > 0 and bar_idx % funding_interval_bars == 0:
                open_notional = sum(
                    (c.quantity * bar.close for c in cells if c.is_open), Decimal("0")
                )
                if self._config.funding_rate_provider is not None:
                    funding_rate = self._config.funding_rate_provider(bar.timestamp)
                else:
                    funding_rate = self._config.assumed_funding_rate_per_interval
                funding_cost = open_notional * funding_rate
                total_funding += funding_cost
                realized_pnl -= funding_cost

            # 3. Liquidations-Check (Naeherung)
            liq_price = self._estimate_liquidation_price(cells, bar.close)
            if liq_price is not None:
                if (self._is_long and bar.low <= liq_price) or (
                    not self._is_long and bar.high >= liq_price
                ):
                    close_trades, delta = self._force_close_all(
                        cells, liq_price, bar.timestamp, bar_idx, regime, "liquidation"
                    )
                    trades.extend(close_trades)
                    realized_pnl += delta
                    close_reason = "liquidation"
                    liquidated = True
                    equity_curve.append(
                        self._equity_point(bar.timestamp, capital, realized_pnl, cells, bar.close)
                    )
                    break

            # 4. Stop-Loss / Take-Profit auf Grid-Ebene
            forced_reason = self._check_grid_level_exits(bar)
            if forced_reason is not None:
                exit_price = (
                    self._params.take_profit
                    if forced_reason == "take_profit"
                    else self._params.stop_loss
                ) or bar.close
                close_trades, delta = self._force_close_all(
                    cells, exit_price, bar.timestamp, bar_idx, regime, forced_reason
                )
                trades.extend(close_trades)
                realized_pnl += delta
                close_reason = forced_reason
                equity_curve.append(
                    self._equity_point(bar.timestamp, capital, realized_pnl, cells, bar.close)
                )
                break

            # 5. Maximum Holding Time
            if self._params.maximum_holding_time is not None:
                elapsed = (bar.timestamp - start_time).total_seconds()
                if elapsed >= self._params.maximum_holding_time:
                    close_trades, delta = self._force_close_all(
                        cells, bar.close, bar.timestamp, bar_idx, regime, "max_holding_time"
                    )
                    trades.extend(close_trades)
                    realized_pnl += delta
                    close_reason = "max_holding_time"
                    equity_curve.append(
                        self._equity_point(bar.timestamp, capital, realized_pnl, cells, bar.close)
                    )
                    break

            # 6. Range-Breakout-Schutz
            if bar.close < breakout_lower or bar.close > breakout_upper:
                close_trades, delta = self._force_close_all(
                    cells, bar.close, bar.timestamp, bar_idx, regime, "range_breakout"
                )
                trades.extend(close_trades)
                realized_pnl += delta
                close_reason = "range_breakout"
                equity_curve.append(
                    self._equity_point(bar.timestamp, capital, realized_pnl, cells, bar.close)
                )
                break

            equity_curve.append(
                self._equity_point(bar.timestamp, capital, realized_pnl, cells, bar.close)
            )
        else:
            # Schleife vollstaendig durchlaufen (kein break) - am Ende alle
            # offenen Cells zum letzten Close schliessen.
            last_bar = candles[-1]
            close_trades, delta = self._force_close_all(
                cells, last_bar.close, last_bar.timestamp, len(candles) - 1,
                regimes[-1], "backtest_end",
            )
            trades.extend(close_trades)
            realized_pnl += delta

        return GridBacktestRunResult(
            trades=trades,
            equity_curve=equity_curve,
            close_reason=close_reason,
            total_funding_paid=total_funding,
            fills_count=fills_count,
            max_concurrent_open_cells=max_open,
            liquidated=liquidated,
        )

    # ------------------------------------------------------------------
    # Fill logic
    # ------------------------------------------------------------------

    def _try_open_cells(
        self, cells: list[_CellState], bar: Candle, bar_idx: int, regime: MarketRegime
    ) -> tuple[list[BacktestTrade], Decimal, int]:
        fills = 0
        for cell in cells:
            if cell.is_open:
                continue
            touched = bar.low <= cell.entry_price <= bar.high
            if not touched:
                continue
            fill_price = self._apply_slippage(cell.entry_price, opening=True)
            qty = self._params.position_size / fill_price if fill_price > 0 else Decimal("0")
            if qty <= 0:
                continue
            cell.is_open = True
            cell.opened_at = bar.timestamp
            cell.opened_bar_index = bar_idx
            cell.quantity = qty
            fills += 1
        return [], Decimal("0"), fills

    def _try_close_cells(
        self, cells: list[_CellState], bar: Candle, bar_idx: int, regime: MarketRegime
    ) -> tuple[list[BacktestTrade], Decimal, int]:
        trades: list[BacktestTrade] = []
        realized = Decimal("0")
        fills = 0
        for cell in cells:
            if not cell.is_open:
                continue
            touched = bar.low <= cell.exit_price <= bar.high
            if not touched:
                continue
            fill_price = self._apply_slippage(cell.exit_price, opening=False)
            trade, pnl = self._close_cell(
                cell, fill_price, bar.timestamp, bar_idx, regime, "grid_cycle"
            )
            trades.append(trade)
            realized += pnl
            fills += 1
        return trades, realized, fills

    def _close_cell(
        self,
        cell: _CellState,
        exit_price: Decimal,
        exit_time: datetime,
        bar_idx: int,
        regime: MarketRegime,
        reason: str,
    ) -> tuple[BacktestTrade, Decimal]:
        side_factor = Decimal("1") if self._is_long else Decimal("-1")
        gross_pnl = (exit_price - cell.entry_price) * cell.quantity * side_factor
        entry_notional = cell.entry_price * cell.quantity
        exit_notional = exit_price * cell.quantity
        fee_rate = self._config.taker_fee if reason != "grid_cycle" else self._config.maker_fee
        fees = (entry_notional + exit_notional) * fee_rate
        net_pnl = gross_pnl - fees

        trade = BacktestTrade(
            id=str(uuid.uuid4()),
            symbol=self._config.symbol,
            strategy=self._config.strategy_name,
            side="long" if self._is_long else "short",
            entry_time=cell.opened_at or exit_time,
            exit_time=exit_time,
            entry_price=cell.entry_price,
            exit_price=exit_price,
            quantity=cell.quantity,
            gross_pnl=gross_pnl,
            fees=fees,
            slippage=abs(exit_notional - cell.exit_price * cell.quantity),
            net_pnl=net_pnl,
            holding_bars=max(bar_idx - cell.opened_bar_index, 0),
            regime=regime,
            max_adverse_excursion=Decimal("0"),
            max_favorable_excursion=Decimal("0"),
            entry_signal_confidence=1.0,
            metadata={"exit_reason": reason, "grid_cell_index": cell.index},
        )

        cell.is_open = False
        cell.quantity = Decimal("0")
        return trade, net_pnl

    def _force_close_all(
        self,
        cells: list[_CellState],
        exit_price: Decimal,
        exit_time: datetime,
        bar_idx: int,
        regime: MarketRegime,
        reason: str,
    ) -> tuple[list[BacktestTrade], Decimal]:
        trades: list[BacktestTrade] = []
        realized = Decimal("0")
        for cell in cells:
            if not cell.is_open:
                continue
            trade, pnl = self._close_cell(cell, exit_price, exit_time, bar_idx, regime, reason)
            trades.append(trade)
            realized += pnl
        return trades, realized

    def _apply_slippage(self, price: Decimal, *, opening: bool) -> Decimal:
        slip = self._config.slippage_pct
        # Slippage verschlechtert den Fill immer aus Trader-Sicht: beim
        # Oeffnen eines Long-Buys (oder Short-Sells) zahlt/erhaelt man
        # etwas schlechter, symmetrisch beim Schliessen.
        worse_direction = Decimal("1") if (opening == self._is_long) else Decimal("-1")
        return price * (Decimal("1") + worse_direction * slip)

    def _estimate_liquidation_price(
        self, cells: list[_CellState], current_price: Decimal
    ) -> Decimal | None:
        """
        Stark vereinfachte Liquidationspreis-Naeherung: der Durchschnitts-
        Entry-Preis der aktuell offenen Cells minus/plus 1/leverage
        (isolierte Margin, keine Maintenance-Margin-Staffelung der
        jeweiligen Exchange beruecksichtigt - siehe Modul-Docstring).
        None, wenn keine Cell offen ist (kein Liquidationsrisiko ohne
        Exposure).
        """
        open_cells = [c for c in cells if c.is_open]
        if not open_cells or self._params.leverage <= 1:
            return None
        total_qty = sum((c.quantity for c in open_cells), Decimal("0"))
        if total_qty <= 0:
            return None
        avg_entry = sum((c.entry_price * c.quantity for c in open_cells), Decimal("0")) / total_qty
        margin_fraction = Decimal("1") / self._params.leverage
        if self._is_long:
            return avg_entry * (Decimal("1") - margin_fraction)
        return avg_entry * (Decimal("1") + margin_fraction)

    def _check_grid_level_exits(self, bar: Candle) -> str | None:
        if self._params.take_profit is not None:
            if self._is_long and bar.high >= self._params.take_profit:
                return "take_profit"
            if not self._is_long and bar.low <= self._params.take_profit:
                return "take_profit"
        if self._params.stop_loss is not None:
            if self._is_long and bar.low <= self._params.stop_loss:
                return "stop_loss"
            if not self._is_long and bar.high >= self._params.stop_loss:
                return "stop_loss"
        return None

    def _equity_point(
        self,
        timestamp: datetime,
        capital: Decimal,
        realized_pnl: Decimal,
        cells: list[_CellState],
        mark_price: Decimal,
    ) -> EquityCurvePoint:
        unrealized = Decimal("0")
        side_factor = Decimal("1") if self._is_long else Decimal("-1")
        for cell in cells:
            if cell.is_open:
                unrealized += (mark_price - cell.entry_price) * cell.quantity * side_factor
        portfolio_value = capital + realized_pnl + unrealized
        return EquityCurvePoint(
            timestamp=timestamp,
            portfolio_value=portfolio_value,
            cash=capital + realized_pnl,
            open_positions_value=unrealized,
            drawdown_pct=0.0,  # wird von PerformanceAnalyzer aus der Kurve neu berechnet
            daily_return=0.0,
        )

    def _precompute_regimes(self, candles: list[Candle]) -> list[MarketRegime]:
        """Grobe Regime-Klassifikation je Bar (ADX/ATR-basiert, gleiche
        Heuristik wie BacktestSimulator._detect_regime_simple) - dient
        hier ausschliesslich der Trade-Attribution (regime_breakdown im
        BacktestResult), nicht der Grid-Entscheidung selbst (die liegt
        bereits vor dem Backtest fest, siehe GridBacktestConfig.parameters)."""
        n = len(candles)
        if n < 20:
            return [MarketRegime.UNKNOWN] * n

        arrays = candles_to_arrays(candles)
        adx_arr, dip_arr, dim_arr = calc_adx(arrays.high, arrays.low, arrays.close, 14)

        regimes: list[MarketRegime] = []
        for i in range(n):
            adx = adx_arr[i]
            if np.isnan(adx):
                regimes.append(MarketRegime.UNKNOWN)
                continue
            if adx > 25:
                if dip_arr[i] > dim_arr[i]:
                    regimes.append(MarketRegime.TRENDING_UP)
                else:
                    regimes.append(MarketRegime.TRENDING_DOWN)
            elif adx < 20:
                regimes.append(MarketRegime.RANGING)
            else:
                regimes.append(MarketRegime.RANGING)
        return regimes

    def to_backtest_result(
        self, run_result: GridBacktestRunResult, candles: list[Candle]
    ) -> BacktestResult:
        """Uebersetzt in einen Standard-BacktestResult via PerformanceAnalyzer
        (siehe Modul-Docstring - keine Parallel-KPI-Implementierung)."""
        config = BacktestConfig(
            start_date=candles[0].timestamp,
            end_date=candles[-1].timestamp,
            symbols=[self._config.symbol],
            timeframe=self._config.timeframe,
            initial_capital=self._config.initial_capital,
            maker_fee=self._config.maker_fee,
            taker_fee=self._config.taker_fee,
            slippage_pct=self._config.slippage_pct,
            strategy_names=[self._config.strategy_name],
        )
        return PerformanceAnalyzer().analyze(run_result.trades, run_result.equity_curve, config)
