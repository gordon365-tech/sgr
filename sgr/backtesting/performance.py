"""
SGR Performance Analyzer
=========================
Berechnet alle KPIs aus Backtest-Trades und Equity-Kurve.

KPIs:
    Return:     Total Return, CAGR
    Risk:       Max Drawdown, Max DD Duration, VaR
    Quality:    Sharpe, Sortino, Calmar
    Trading:    Hit Rate, Profit Factor, Avg Winner/Loser, Expected Value
    Costs:      Total Fees, Total Slippage (als % des Gewinns)

Alle Kennzahlen nach institutionellem Standard:
    Sharpe:  (Annualized Return - Risk Free Rate) / Annualized Volatility
             Risk Free Rate = 0% (Crypto: kein risikofreier Zins sinnvoll)
    Sortino: Annualized Return / Downside Deviation
    Calmar:  CAGR / Max Drawdown
"""

from __future__ import annotations

from typing import Any

import numpy as np

from sgr.backtesting.types import (
    BacktestConfig,
    BacktestResult,
    BacktestStatus,
    BacktestTrade,
    EquityCurvePoint,
)
from sgr.core.logging import get_logger

log = get_logger(__name__)

_ANNUALIZATION_FACTOR = {
    "1m": 525_600,
    "5m": 105_120,
    "15m": 35_040,
    "1h": 8_760,
    "4h": 2_190,
    "1d": 365,
}


class PerformanceAnalyzer:
    """
    Berechnet alle Performance-Metriken aus Backtest-Ergebnissen.
    Stateless – pure computation.
    """

    def analyze(
        self,
        trades: list[BacktestTrade],
        equity_curve: list[EquityCurvePoint],
        config: BacktestConfig,
    ) -> BacktestResult:
        """
        Hauptmethode: erzeugt vollständigen BacktestResult.
        """
        if not trades and not equity_curve:
            return self._empty_result(config)

        initial = float(config.initial_capital)
        final = float(equity_curve[-1].portfolio_value) if equity_curve else initial
        equity_values = np.array([float(p.portfolio_value) for p in equity_curve])

        # Zeitraum
        duration_days = max((config.end_date - config.start_date).days, 1)

        # Returns
        total_return_pct = (final - initial) / initial * 100
        cagr_pct = self._cagr(initial, final, duration_days)

        # Drawdown
        max_dd_pct, max_dd_days = self._max_drawdown(equity_values, equity_curve)

        # Bar Returns für Sharpe/Sortino
        bar_returns = (
            np.diff(equity_values) / equity_values[:-1] if len(equity_values) > 1 else np.array([])
        )
        ann_factor = _ANNUALIZATION_FACTOR.get(config.timeframe, 8_760)

        sharpe = self._sharpe(bar_returns, ann_factor)
        sortino = self._sortino(bar_returns, ann_factor)
        calmar = cagr_pct / max_dd_pct if max_dd_pct > 0 else 0.0

        # Trade Stats
        trade_stats = self._trade_stats(trades)

        # Per-Strategie + Per-Regime Breakdown
        strategy_breakdown = self._breakdown_by(trades, "strategy")
        regime_breakdown = self._breakdown_by(trades, "regime")

        # Go-Live Gates prüfen
        blockers = self._check_go_live_gates(
            sharpe=sharpe,
            profit_factor=trade_stats["profit_factor"],
            max_dd_pct=max_dd_pct,
            hit_rate=trade_stats["hit_rate_pct"],
            total_trades=len(trades),
        )

        result = BacktestResult(
            config_summary={
                "symbols": config.symbols,
                "timeframe": config.timeframe,
                "start_date": config.start_date.isoformat(),
                "end_date": config.end_date.isoformat(),
                "strategy_names": config.strategy_names,
                "initial_capital": str(config.initial_capital),
            },
            status=BacktestStatus.COMPLETED,
            start_date=config.start_date.isoformat(),
            end_date=config.end_date.isoformat(),
            duration_days=duration_days,
            initial_capital=str(config.initial_capital),
            final_capital=str(round(final, 2)),
            total_return_pct=round(total_return_pct, 2),
            cagr_pct=round(cagr_pct, 2),
            sharpe_ratio=round(sharpe, 3),
            sortino_ratio=round(sortino, 3),
            calmar_ratio=round(calmar, 3),
            max_drawdown_pct=round(max_dd_pct, 2),
            max_drawdown_duration_days=max_dd_days,
            profit_factor=round(trade_stats["profit_factor"], 3),
            hit_rate_pct=round(trade_stats["hit_rate_pct"], 1),
            expected_value_per_trade=str(round(trade_stats["expected_value"], 2)),
            total_trades=len(trades),
            winning_trades=trade_stats["winning"],
            losing_trades=trade_stats["losing"],
            avg_winner=str(round(trade_stats["avg_winner"], 2)),
            avg_loser=str(round(trade_stats["avg_loser"], 2)),
            avg_holding_bars=round(trade_stats["avg_holding_bars"], 1),
            total_fees=str(round(trade_stats["total_fees"], 2)),
            total_slippage=str(round(trade_stats["total_slippage"], 2)),
            strategy_breakdown=strategy_breakdown,
            regime_breakdown=regime_breakdown,
            equity_curve=[
                {
                    "timestamp": p.timestamp.isoformat(),
                    "portfolio_value": float(p.portfolio_value),
                    "drawdown_pct": p.drawdown_pct,
                }
                for p in equity_curve[:: max(len(equity_curve) // 500, 1)]  # Max 500 Punkte
            ],
            trades=[self._trade_to_dict(t) for t in trades],
            go_live_eligible=len(blockers) == 0,
            go_live_blockers=blockers,
        )

        log.info(
            "performance_analyzer.complete",
            total_return=f"{total_return_pct:.1f}%",
            sharpe=f"{sharpe:.2f}",
            max_dd=f"{max_dd_pct:.1f}%",
            trades=len(trades),
            go_live=len(blockers) == 0,
        )

        return result

    # ------------------------------------------------------------------
    # KPI Berechnungen
    # ------------------------------------------------------------------

    def _cagr(self, initial: float, final: float, days: int) -> float:
        if initial <= 0 or days <= 0:
            return 0.0
        years = days / 365.25
        return ((final / initial) ** (1 / years) - 1) * 100

    def _max_drawdown(
        self,
        equity: np.ndarray,
        curve: list[EquityCurvePoint],
    ) -> tuple[float, int]:
        """Berechnet Max Drawdown (%) und Dauer (Tage)."""
        if len(equity) < 2:
            return 0.0, 0

        peak = equity[0]
        max_dd = 0.0
        max_dd_start_idx = 0
        max_dd_end_idx = 0
        dd_start_idx = 0

        for i, val in enumerate(equity):
            if val > peak:
                peak = val
                dd_start_idx = i
            dd = (peak - val) / peak * 100
            if dd > max_dd:
                max_dd = dd
                max_dd_start_idx = dd_start_idx
                max_dd_end_idx = i

        # Dauer in Tagen
        if curve and max_dd_start_idx < len(curve) and max_dd_end_idx < len(curve):
            dd_duration = (curve[max_dd_end_idx].timestamp - curve[max_dd_start_idx].timestamp).days
        else:
            dd_duration = 0

        return max_dd, dd_duration

    def _sharpe(self, returns: np.ndarray, ann_factor: int) -> float:
        if len(returns) < 2:
            return 0.0
        mean = np.mean(returns)
        std = np.std(returns, ddof=1)
        if std == 0:
            return 0.0
        return float(mean / std * np.sqrt(ann_factor))

    def _sortino(self, returns: np.ndarray, ann_factor: int) -> float:
        if len(returns) < 2:
            return 0.0
        mean = np.mean(returns)
        downside = returns[returns < 0]
        if len(downside) == 0:
            return float("inf")
        downside_std = np.std(downside, ddof=1)
        if downside_std == 0:
            return 0.0
        return float(mean / downside_std * np.sqrt(ann_factor))

    def _trade_stats(self, trades: list[BacktestTrade]) -> dict[str, float]:
        if not trades:
            return {
                "profit_factor": 0.0,
                "hit_rate_pct": 0.0,
                "expected_value": 0.0,
                "winning": 0,
                "losing": 0,
                "avg_winner": 0.0,
                "avg_loser": 0.0,
                "avg_holding_bars": 0.0,
                "total_fees": 0.0,
                "total_slippage": 0.0,
            }

        winners = [t for t in trades if t.is_winner]
        losers = [t for t in trades if not t.is_winner]

        gross_profit = sum(float(t.net_pnl) for t in winners)
        gross_loss = abs(sum(float(t.net_pnl) for t in losers))

        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        hit_rate = len(winners) / len(trades) * 100 if trades else 0.0
        expected_value = sum(float(t.net_pnl) for t in trades) / len(trades)

        avg_winner = gross_profit / len(winners) if winners else 0.0
        avg_loser = gross_loss / len(losers) if losers else 0.0
        avg_holding = sum(t.holding_bars for t in trades) / len(trades)

        total_fees = sum(float(t.fees) for t in trades)
        total_slippage = sum(float(t.slippage) for t in trades)

        return {
            "profit_factor": profit_factor,
            "hit_rate_pct": hit_rate,
            "expected_value": expected_value,
            "winning": len(winners),
            "losing": len(losers),
            "avg_winner": avg_winner,
            "avg_loser": avg_loser,
            "avg_holding_bars": avg_holding,
            "total_fees": total_fees,
            "total_slippage": total_slippage,
        }

    def _breakdown_by(
        self,
        trades: list[BacktestTrade],
        field: str,
    ) -> dict[str, dict[str, Any]]:
        """Gruppiert Trade-Stats nach Strategie oder Regime."""
        groups: dict[str, list[BacktestTrade]] = {}
        for t in trades:
            key = getattr(t, field, "unknown")
            if hasattr(key, "value"):
                key = key.value
            groups.setdefault(str(key), []).append(t)

        result = {}
        for key, group_trades in groups.items():
            stats = self._trade_stats(group_trades)
            result[key] = {
                "total_trades": len(group_trades),
                "hit_rate_pct": round(stats["hit_rate_pct"], 1),
                "profit_factor": round(stats["profit_factor"], 3),
                "net_pnl": round(sum(float(t.net_pnl) for t in group_trades), 2),
                "avg_holding_bars": round(stats["avg_holding_bars"], 1),
            }
        return result

    def _check_go_live_gates(
        self,
        sharpe: float,
        profit_factor: float,
        max_dd_pct: float,
        hit_rate: float,
        total_trades: int,
    ) -> list[str]:
        """Prüft Go-Live Gates. Gibt Liste der Blocker zurück."""
        blockers = []
        if sharpe < 1.0:
            blockers.append(f"Sharpe {sharpe:.2f} < 1.0 (minimum)")
        if profit_factor < 1.3:
            blockers.append(f"Profit Factor {profit_factor:.2f} < 1.3")
        if max_dd_pct > 20.0:
            blockers.append(f"Max Drawdown {max_dd_pct:.1f}% > 20%")
        if hit_rate < 40.0:
            blockers.append(f"Hit Rate {hit_rate:.1f}% < 40%")
        if total_trades < 30:
            blockers.append(f"Only {total_trades} trades (min 30 for statistical significance)")
        return blockers

    def _trade_to_dict(self, t: BacktestTrade) -> dict[str, Any]:
        return {
            "id": t.id,
            "symbol": t.symbol,
            "strategy": t.strategy,
            "side": t.side,
            "entry_time": t.entry_time.isoformat(),
            "exit_time": t.exit_time.isoformat(),
            "entry_price": str(t.entry_price),
            "exit_price": str(t.exit_price),
            "quantity": str(t.quantity),
            "net_pnl": str(t.net_pnl),
            "fees": str(t.fees),
            "holding_bars": t.holding_bars,
            "regime": t.regime.value if hasattr(t.regime, "value") else str(t.regime),
            "confidence": t.entry_signal_confidence,
            "mae": str(t.max_adverse_excursion),
            "mfe": str(t.max_favorable_excursion),
        }

    def _empty_result(self, config: BacktestConfig) -> BacktestResult:
        return BacktestResult(
            config_summary={},
            status=BacktestStatus.FAILED,
            start_date=config.start_date.isoformat(),
            end_date=config.end_date.isoformat(),
            duration_days=0,
            initial_capital=str(config.initial_capital),
            final_capital=str(config.initial_capital),
            total_return_pct=0.0,
            cagr_pct=0.0,
            sharpe_ratio=0.0,
            sortino_ratio=0.0,
            calmar_ratio=0.0,
            max_drawdown_pct=0.0,
            max_drawdown_duration_days=0,
            profit_factor=0.0,
            hit_rate_pct=0.0,
            expected_value_per_trade="0",
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            avg_winner="0",
            avg_loser="0",
            avg_holding_bars=0.0,
            total_fees="0",
            total_slippage="0",
            go_live_eligible=False,
            go_live_blockers=["No trades generated"],
        )
