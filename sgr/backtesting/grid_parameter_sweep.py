"""
Grid Parameter Sweep (Phase O, 2026-09-23)
=============================================
Bindet FuturesGridParameters-Kandidaten an den bestehenden Grid-Backtest
an (GridBacktestSimulator + PerformanceAnalyzer, siehe grid_simulator.py)
- explizit KEINE neue Optimizer-Engine (Aufgabenstellung: "Keine neue
Optimizer-Engine"). Wiederverwendet dasselbe Anti-Overfitting-Prinzip wie
sgr.strategy.parameter_optimizer (siehe dortigen Modul-Docstring): die
Suche laeuft auf einem FRUEHEN Fenster der uebergebenen Candle-Serie, das
gewaehlte (bestbewertete) Parameter-Set wird danach auf dem SPAETEREN,
disjunkten Fenster erneut bewertet - dieses zweite Ergebnis ist die
massgebliche Kennzahl, nicht das Optimierungsergebnis selbst.

Suchraum bewusst klein und explizit (identische Philosophie wie
sgr.strategy.parameter_optimizer: "explizit statt generisch"): dieser
erste Schritt deckt grid_count, leverage und Range-Breite ab (die drei
Parameter mit dem groessten Einfluss auf Kapitalbindung/Zyklen-Frequenz)
- Grid-Spacing-Mode, position_size, stop_loss/take_profit als weitere
Dimensionen sind der naheliegende, mit demselben Muster nachruestbare
naechste Schritt (siehe Abschlussbericht), bewusst NICHT in diesem
Schritt kombinatorisch mit aufgenommen, um keinen ungetesteten
Parameterraum zu erzeugen.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from sgr.backtesting.grid_simulator import GridBacktestSimulator
from sgr.backtesting.grid_types import GridBacktestConfig
from sgr.core.grid_types import FuturesGridParameters
from sgr.core.logging import get_logger
from sgr.core.types import Candle
from sgr.strategy.grid_edge import compute_grid_edge_metrics, is_edge_confirmed

log = get_logger(__name__)

OPTIMIZATION_WINDOW_FRACTION = 0.6  # identisch zur Philosophie in parameter_optimizer.py


@dataclass
class GridSweepCandidateResult:
    parameters: FuturesGridParameters
    optimization_window_edge_score: float


@dataclass
class GridSweepResult:
    chosen_parameters: FuturesGridParameters | None
    candidates_evaluated: int
    validation_window_edge_confirmed: bool
    validation_window_blockers: list[str]
    skipped_reason: str | None = None


def _edge_score(candles: list[Candle], config: GridBacktestConfig) -> float:
    sim = GridBacktestSimulator(config)
    run_result = sim.run(candles)
    backtest_result = sim.to_backtest_result(run_result, candles)
    metrics = compute_grid_edge_metrics(
        backtest_result, run_result, grid_count=config.parameters.grid_count
    )
    # Gleicher Score-Stil wie sgr.strategy.capital_allocation.
    # CapitalAllocationEngine (Grid-Zweig): sharpe x grid_efficiency x
    # Stabilitaets-Faktor - eine einzelne, vergleichbare Kennzahl pro
    # Kandidat, keine neue KPI-Definition.
    sharpe = max(backtest_result.sharpe_ratio, 0.0)
    return sharpe * metrics.grid_efficiency * (0.5 + 0.5 * metrics.edge_stability)


def sweep_grid_parameters(
    candles: list[Candle],
    base_config: GridBacktestConfig,
    grid_count_candidates: list[int] | None = None,
    leverage_candidates: list[Decimal] | None = None,
    range_width_multiplier_candidates: list[Decimal] | None = None,
    min_candles: int = 200,
) -> GridSweepResult:
    """
    Sucht auf candles[:split] nach dem Kandidaten mit dem hoechsten
    Edge-Score (siehe _edge_score()), bewertet ihn dann NOCHMAL auf
    candles[split:] (disjunkt) mit dem bestehenden, unveraenderten
    GridEdgeMetrics/is_edge_confirmed()-Gate (sgr/strategy/grid_edge.py) -
    identisches Gate wie GridValidationRunner, keine laxere Schwelle fuer
    optimierte Parameter.

    Zu wenig Historie (< min_candles) -> kein Sweep, ehrliches
    "skipped_reason", kein erzwungenes Ergebnis (identisches Prinzip wie
    parameter_optimizer.py's "optimization_skipped": "insufficient_history").
    """
    if len(candles) < min_candles:
        return GridSweepResult(
            chosen_parameters=None,
            candidates_evaluated=0,
            validation_window_edge_confirmed=False,
            validation_window_blockers=[],
            skipped_reason="insufficient_history",
        )

    split = int(len(candles) * OPTIMIZATION_WINDOW_FRACTION)
    optimization_candles = candles[:split]
    validation_candles = candles[split:]
    if len(validation_candles) < 50:
        return GridSweepResult(
            chosen_parameters=None,
            candidates_evaluated=0,
            validation_window_edge_confirmed=False,
            validation_window_blockers=[],
            skipped_reason="insufficient_validation_window",
        )

    base_params = base_config.parameters
    grid_count_candidates = grid_count_candidates or [
        max(2, base_params.grid_count - 2),
        base_params.grid_count,
        base_params.grid_count + 2,
    ]
    leverage_candidates = leverage_candidates or [
        base_params.leverage,
        base_params.leverage * Decimal("2"),
    ]
    range_width_multiplier_candidates = range_width_multiplier_candidates or [
        Decimal("0.5"),
        Decimal("1"),
        Decimal("1.5"),
    ]

    base_width = base_params.grid_upper_price - base_params.grid_lower_price
    base_mid = (base_params.grid_upper_price + base_params.grid_lower_price) / 2

    best_score = float("-inf")
    best_params: FuturesGridParameters | None = None
    evaluated = 0

    for grid_count in grid_count_candidates:
        for leverage in leverage_candidates:
            for width_mult in range_width_multiplier_candidates:
                half_width = (base_width * width_mult) / 2
                candidate_params = replace(
                    base_params,
                    grid_count=grid_count,
                    leverage=leverage,
                    grid_lower_price=base_mid - half_width,
                    grid_upper_price=base_mid + half_width,
                    grid_spacing=None,  # neu ableiten lassen (siehe compute_levels())
                )
                candidate_config = replace(base_config, parameters=candidate_params)
                try:
                    score = _edge_score(optimization_candles, candidate_config)
                except Exception as e:
                    log.warning(
                        "grid_parameter_sweep.candidate_failed",
                        grid_count=grid_count,
                        leverage=str(leverage),
                        width_multiplier=str(width_mult),
                        error=str(e),
                    )
                    continue
                evaluated += 1
                if score > best_score:
                    best_score = score
                    best_params = candidate_params

    if best_params is None:
        return GridSweepResult(
            chosen_parameters=None,
            candidates_evaluated=evaluated,
            validation_window_edge_confirmed=False,
            validation_window_blockers=["no_candidate_produced_a_valid_backtest"],
            skipped_reason=None,
        )

    validation_config = replace(base_config, parameters=best_params)
    sim = GridBacktestSimulator(validation_config)
    run_result = sim.run(validation_candles)
    backtest_result = sim.to_backtest_result(run_result, validation_candles)
    metrics = compute_grid_edge_metrics(
        backtest_result, run_result, grid_count=best_params.grid_count
    )
    confirmed, blockers = is_edge_confirmed(metrics, n_trades=backtest_result.total_trades)

    log.info(
        "grid_parameter_sweep.completed",
        candidates_evaluated=evaluated,
        chosen_grid_count=best_params.grid_count,
        chosen_leverage=str(best_params.leverage),
        optimization_window_score=best_score,
        validation_window_edge_confirmed=confirmed,
    )

    return GridSweepResult(
        chosen_parameters=best_params,
        candidates_evaluated=evaluated,
        validation_window_edge_confirmed=confirmed,
        validation_window_blockers=blockers,
    )
