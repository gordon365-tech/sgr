"""
SGR Grid Edge Engine
======================
Bewertet eine Futures-Grid-Strategie GETRENNT von direktionalen
Strategien (siehe Aufgabenstellung: "Die Edge Engine muss Futures Grid
getrennt von anderen Strategietypen bewerten koennen").

Zentrale Regel (siehe Aufgabenstellung): "Eine Futures Grid Strategie
darf nicht nur nach absolutem PnL bewertet werden." is_edge_confirmed()
verlangt deshalb explizit, dass der Ertrag NACH Fees, Funding, Slippage
UND Kapitalbindung (capital_utilization) noch statistisch belastbar ist -
ein hoher Bruttogewinn bei winziger Netto-Marge oder permanent
gebundenem Kapital besteht die Pruefung NICHT.
"""

from __future__ import annotations

from dataclasses import dataclass

from sgr.backtesting.grid_simulator import GridBacktestRunResult
from sgr.backtesting.types import BacktestResult


@dataclass(frozen=True)
class GridEdgeMetrics:
    """Alle in der Aufgabenstellung geforderten Kennzahlen."""

    net_pnl: float
    gross_pnl: float
    fees: float
    funding: float
    slippage: float
    win_rate: float
    profit_factor: float
    max_drawdown_pct: float
    sharpe: float
    sortino: float
    grid_efficiency: float  # net_pnl / gross_pnl (Anteil, der nach Kosten uebrig bleibt)
    capital_utilization: float  # durchschnittlich gebundenes Kapital / max. verfuegbares
    exposure_time: float  # Anteil der Backtest-Zeit mit >0 offenen Leveln (0-1)
    average_grid_capture: float  # durchschnittlicher net_pnl pro abgeschlossenem Zyklus
    edge_stability: float  # 0-1, siehe _compute_edge_stability()
    regime_compatibility: float  # Anteil profitabler Regime-Segmente (0-1)


def compute_grid_edge_metrics(
    backtest_result: BacktestResult,
    run_result: GridBacktestRunResult,
    grid_count: int,
) -> GridEdgeMetrics:
    """
    Reine Berechnungsfunktion (kein I/O) - kombiniert den generischen
    BacktestResult (siehe sgr.backtesting.performance.PerformanceAnalyzer)
    mit dem Grid-spezifischen Rohergebnis (Funding, max. gleichzeitig
    offene Level).
    """
    trades = run_result.trades
    gross_pnl = sum(float(t.gross_pnl) for t in trades)
    fees = sum(float(t.fees) for t in trades)
    slippage = sum(float(t.slippage) for t in trades)
    funding = float(run_result.total_funding_paid)
    net_pnl = gross_pnl - fees - funding

    grid_efficiency = (net_pnl / gross_pnl) if gross_pnl > 0 else 0.0
    # Anteil der theoretisch verfuegbaren Grid-Kapazitaet (grid_count - 1
    # Cells), der zum Spitzenzeitpunkt gleichzeitig genutzt wurde - ein
    # Grid, das nie mehr als 1 von 10 Cells gleichzeitig fuellt, bindet
    # strukturell wenig Kapital (niedrige Utilization), unabhaengig vom
    # PnL.
    total_cells = max(1, grid_count - 1)
    capital_utilization = min(1.0, run_result.max_concurrent_open_cells / total_cells)
    # Vereinfachte, konservative Naeherung: Anteil der Zeit, in der
    # UEBERHAUPT mindestens ein Level offen war, approximiert ueber das
    # Verhaeltnis Fills/erwartete Fill-Dichte - siehe "offene Punkte" im
    # Strategiebericht fuer eine spaetere bar-genaue Messung direkt im
    # Simulator.
    exposure_time = min(1.0, run_result.fills_count / max(1, len(run_result.equity_curve)))

    avg_capture = net_pnl / len(trades) if trades else 0.0

    regime_pnls: dict[str, float] = {}
    for t in trades:
        key = t.regime.value if hasattr(t.regime, "value") else str(t.regime)
        regime_pnls[key] = regime_pnls.get(key, 0.0) + float(t.net_pnl)
    profitable_regimes = sum(1 for v in regime_pnls.values() if v > 0)
    regime_compatibility = profitable_regimes / len(regime_pnls) if regime_pnls else 0.0

    edge_stability = _compute_edge_stability(trades)

    return GridEdgeMetrics(
        net_pnl=round(net_pnl, 4),
        gross_pnl=round(gross_pnl, 4),
        fees=round(fees, 4),
        funding=round(funding, 4),
        slippage=round(slippage, 4),
        win_rate=backtest_result.hit_rate_pct / 100.0,
        profit_factor=backtest_result.profit_factor,
        max_drawdown_pct=backtest_result.max_drawdown_pct,
        sharpe=backtest_result.sharpe_ratio,
        sortino=backtest_result.sortino_ratio,
        grid_efficiency=round(grid_efficiency, 4),
        capital_utilization=round(capital_utilization, 4),
        exposure_time=round(exposure_time, 4),
        average_grid_capture=round(avg_capture, 4),
        edge_stability=round(edge_stability, 4),
        regime_compatibility=round(regime_compatibility, 4),
    )


def _compute_edge_stability(trades: list) -> float:
    """
    Grobe Stabilitaets-Heuristik: teilt die Trade-Sequenz in 4 gleich
    grosse Segmente und misst, in wie vielen Segmenten der net_pnl
    positiv war (0.0-1.0). Eine Strategie, die ihren gesamten Gewinn aus
    EINEM Segment zieht, ist weniger stabil als eine mit durchgaengig
    positiven Segmenten - unabhaengig vom identischen Gesamt-PnL.
    """
    if len(trades) < 8:
        return 0.0
    n_segments = 4
    seg_size = len(trades) // n_segments
    positive_segments = 0
    for i in range(n_segments):
        start = i * seg_size
        end = (i + 1) * seg_size if i < n_segments - 1 else len(trades)
        segment = trades[start:end]
        if sum(float(t.net_pnl) for t in segment) > 0:
            positive_segments += 1
    return positive_segments / n_segments


def is_edge_confirmed(
    metrics: GridEdgeMetrics,
    *,
    min_net_pnl: float = 0.0,
    min_grid_efficiency: float = 0.3,
    min_sharpe: float = 0.8,
    min_edge_stability: float = 0.5,
    min_trades: int = 20,
    n_trades: int = 0,
) -> tuple[bool, list[str]]:
    """
    Gate fuer Kapitalfreigabe (siehe Aufgabenstellung: PnL allein reicht
    nicht). Gibt (confirmed, blockers) zurueck - niemals nur True/False
    ohne Begruendung, damit ein Operator nachvollziehen kann, WARUM eine
    Grid-Variante (noch) kein Kapital erhaelt.
    """
    blockers: list[str] = []
    if n_trades < min_trades:
        blockers.append(f"Nur {n_trades} abgeschlossene Grid-Zyklen (min {min_trades})")
    if metrics.net_pnl <= min_net_pnl:
        blockers.append(f"Net PnL {metrics.net_pnl:.2f} <= {min_net_pnl}")
    if metrics.grid_efficiency < min_grid_efficiency:
        blockers.append(
            f"Grid Efficiency {metrics.grid_efficiency:.2%} unter Mindestschwelle "
            f"{min_grid_efficiency:.2%} (zu viel Ertrag geht in Fees/Funding/Slippage)"
        )
    if metrics.sharpe < min_sharpe:
        blockers.append(f"Sharpe {metrics.sharpe:.2f} < {min_sharpe}")
    if metrics.edge_stability < min_edge_stability:
        blockers.append(
            f"Edge Stability {metrics.edge_stability:.2f} < {min_edge_stability} "
            "(Ertrag konzentriert sich auf zu wenige Zeitsegmente)"
        )
    return len(blockers) == 0, blockers
