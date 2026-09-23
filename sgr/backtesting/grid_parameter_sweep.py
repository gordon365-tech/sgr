"""
Grid Parameter Sweep (Phase O/10, zuletzt revidiert 2026-09-24)
=================================================================
Bindet FuturesGridParameters-Kandidaten an den bestehenden Grid-Backtest
an (GridBacktestSimulator + PerformanceAnalyzer, siehe grid_simulator.py)
- explizit KEINE neue Optimizer-Engine (Aufgabenstellung: "Keine neue
Optimizer-Engine"). Wiederverwendet dasselbe Anti-Overfitting-Prinzip wie
sgr.strategy.parameter_optimizer (siehe dortigen Modul-Docstring).

Echte DREI-Wege-Trennung (Revision 2026-09-24, explizite Anweisung:
"Backtests muessen unterscheiden zwischen in-sample / validation /
out-of-sample. Keine Go-Live-Freigabe aufgrund ausschliesslich
historischer In-Sample-Ergebnisse."):

    1. IN-SAMPLE (erste IN_SAMPLE_FRACTION der Candles): ALLE Kandidaten
       werden hier bewertet (guenstig - kleiner Suchraum, siehe unten) -
       liefert eine Rangliste, KEINE Go-Live-Aussage.
    2. VALIDATION (zweites Fenster): NUR die Top-K Kandidaten aus Schritt 1
       (Pruning, siehe TOP_K_FOR_VALIDATION) werden hier erneut bewertet.
       Der ERSTE Kandidat (in In-Sample-Rangfolge), der hier
       is_edge_confirmed() besteht, wird als chosen_parameters uebernommen -
       das ist die eigentliche SELEKTIONS-Entscheidung, nicht Schritt 1.
    3. OUT-OF-SAMPLE (drittes, disjunktes Fenster): der gewaehlte Kandidat
       wird HIER ein drittes Mal bewertet - dieses Ergebnis fliesst in
       KEINE Auswahl-Entscheidung mehr ein, es ist die berichtete,
       unverzerrte Schaetzung der tatsaechlichen Edge. Ein Kandidat, der
       Validation besteht, aber Out-of-Sample durchfaellt, wird ALS
       SOLCHES markiert (out_of_sample_edge_confirmed=False) - es gibt
       KEINE automatische Nachbesserung/Neuauswahl an dieser Stelle
       (das waere erneutes Data-Snooping).

Pruning (explizite Anweisung: "Vermeide eine sinnlose kombinatorische
Explosion. Baue sinnvolle Constraints/Pruning ein."): der volle
Kandidaten-Kreuzprodukt-Raum wird NUR auf dem guenstigen In-Sample-Fenster
vollstaendig ausgewertet; Validation/Out-of-Sample (die teureren,
zusaetzlichen Backtest-Laeufe) sehen hoechstens TOP_K_FOR_VALIDATION
Kandidaten, nicht den vollen Raum - linear statt kombinatorisch in den
teuren Schritten.

Suchraum (bewusst weiterhin klein und explizit, identische Philosophie
wie sgr.strategy.parameter_optimizer: "explizit statt generisch"): deckt
grid_count, leverage, Range-Breite UND (neu in dieser Revision) ein
gekoppeltes Stop-Loss/Take-Profit-Paar ab - Spacing-Mode und
position_size bleiben bewusst NICHT mit aufgenommen (naechster,
nachruestbarer Schritt mit demselben Muster, siehe Abschlussbericht).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from sgr.backtesting.grid_simulator import GridBacktestSimulator
from sgr.backtesting.grid_types import GridBacktestConfig
from sgr.core.grid_types import FuturesGridParameters
from sgr.core.logging import get_logger
from sgr.core.types import Candle, GridDirection
from sgr.strategy.grid_edge import compute_grid_edge_metrics, is_edge_confirmed

log = get_logger(__name__)

IN_SAMPLE_FRACTION = 0.5
VALIDATION_FRACTION = 0.25
# Restliches Fenster (0.25) ist Out-of-Sample.
TOP_K_FOR_VALIDATION = 3


@dataclass
class GridSweepCandidateScore:
    parameters: FuturesGridParameters
    in_sample_score: float


@dataclass
class GridSweepResult:
    chosen_parameters: FuturesGridParameters | None
    candidates_evaluated: int
    candidates_promoted_to_validation: int
    validation_edge_confirmed: bool
    validation_blockers: list[str]
    out_of_sample_edge_confirmed: bool
    out_of_sample_blockers: list[str]
    skipped_reason: str | None = None


def _edge_score_and_confirmation(
    candles: list[Candle], config: GridBacktestConfig
) -> tuple[float, bool, list[str]]:
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
    score = sharpe * metrics.grid_efficiency * (0.5 + 0.5 * metrics.edge_stability)
    confirmed, blockers = is_edge_confirmed(metrics, n_trades=backtest_result.total_trades)
    return score, confirmed, blockers


def sweep_grid_parameters(
    candles: list[Candle],
    base_config: GridBacktestConfig,
    grid_count_candidates: list[int] | None = None,
    leverage_candidates: list[Decimal] | None = None,
    range_width_multiplier_candidates: list[Decimal] | None = None,
    stop_loss_take_profit_pct_candidates: list[tuple[float, float]] | None = None,
    min_candles: int = 300,
    top_k_for_validation: int = TOP_K_FOR_VALIDATION,
) -> GridSweepResult:
    """
    Siehe Modul-Docstring fuer die vollstaendige Drei-Wege-Logik. Zu wenig
    Historie (< min_candles, jetzt 300 statt vorher 200 - drei Fenster
    statt zwei brauchen mehr Rohdaten fuer ein noch aussagekraeftiges
    Out-of-Sample-Fenster) -> kein Sweep, ehrliches "skipped_reason".
    """
    if len(candles) < min_candles:
        return _skip("insufficient_history")

    n = len(candles)
    i1 = int(n * IN_SAMPLE_FRACTION)
    i2 = int(n * (IN_SAMPLE_FRACTION + VALIDATION_FRACTION))
    in_sample = candles[:i1]
    validation = candles[i1:i2]
    out_of_sample = candles[i2:]
    if len(validation) < 30 or len(out_of_sample) < 30:
        return _skip("insufficient_validation_or_oos_window")

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
    # SL/TP als Prozent-Abstand vom Grid-Mittelpunkt (Paar, damit die
    # Kombinatorik nicht durch zwei unabhaengige Achsen weiter waechst -
    # bewusste Kopplung, siehe Modul-Docstring "Pruning").
    stop_loss_take_profit_pct_candidates = stop_loss_take_profit_pct_candidates or [
        (0.08, 0.0),  # nur SL, kein hartes TP (Default-Verhalten des Grids selbst)
        (0.05, 0.10),
    ]

    base_width = base_params.grid_upper_price - base_params.grid_lower_price
    base_mid = (base_params.grid_upper_price + base_params.grid_lower_price) / 2

    scored: list[GridSweepCandidateScore] = []
    for grid_count in grid_count_candidates:
        for leverage in leverage_candidates:
            for width_mult in range_width_multiplier_candidates:
                for sl_pct, tp_pct in stop_loss_take_profit_pct_candidates:
                    half_width = (base_width * width_mult) / 2
                    lower = base_mid - half_width
                    upper = base_mid + half_width
                    is_long = base_params.long_or_short == GridDirection.LONG
                    stop_loss = (
                        lower * Decimal(str(1 - sl_pct))
                        if is_long
                        else upper * Decimal(str(1 + sl_pct))
                    )
                    take_profit = None
                    if tp_pct > 0:
                        take_profit = (
                            upper * Decimal(str(1 + tp_pct))
                            if is_long
                            else lower * Decimal(str(1 - tp_pct))
                        )
                    candidate_params = replace(
                        base_params,
                        grid_count=grid_count,
                        leverage=leverage,
                        grid_lower_price=lower,
                        grid_upper_price=upper,
                        grid_spacing=None,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                    )
                    candidate_config = replace(base_config, parameters=candidate_params)
                    try:
                        score, _, _ = _edge_score_and_confirmation(in_sample, candidate_config)
                    except Exception as e:
                        log.warning(
                            "grid_parameter_sweep.in_sample_candidate_failed",
                            grid_count=grid_count,
                            leverage=str(leverage),
                            width_multiplier=str(width_mult),
                            error=str(e),
                        )
                        continue
                    scored.append(GridSweepCandidateScore(candidate_params, score))

    if not scored:
        return _skip(None, candidates_evaluated=0)

    scored.sort(key=lambda c: c.in_sample_score, reverse=True)
    promoted = scored[:top_k_for_validation]

    # Validation: erster promovierter Kandidat (in In-Sample-Rangfolge),
    # der hier ebenfalls is_edge_confirmed() besteht, gewinnt - siehe
    # Modul-Docstring: das ist die eigentliche Selektions-Entscheidung.
    chosen: FuturesGridParameters | None = None
    validation_confirmed = False
    validation_blockers: list[str] = ["no_candidate_confirmed_on_validation_window"]
    for candidate in promoted:
        candidate_config = replace(base_config, parameters=candidate.parameters)
        try:
            _, confirmed, blockers = _edge_score_and_confirmation(validation, candidate_config)
        except Exception as e:
            log.warning("grid_parameter_sweep.validation_candidate_failed", error=str(e))
            continue
        if confirmed:
            chosen = candidate.parameters
            validation_confirmed = True
            validation_blockers = []
            break
        validation_blockers = blockers

    if chosen is None:
        # Kein Kandidat besteht die Validierung - der beste In-Sample-
        # Kandidat wird trotzdem als "chosen_parameters" zurueckgegeben
        # (Transparenz - der Aufrufer sieht, was gesucht wurde), aber
        # validation_edge_confirmed=False macht unmissverstaendlich klar:
        # KEINE Go-Live-Empfehlung.
        chosen = promoted[0].parameters
        out_of_sample_confirmed = False
        out_of_sample_blockers: list[str] = ["validation_not_confirmed_oos_not_evaluated"]
    else:
        oos_config = replace(base_config, parameters=chosen)
        try:
            _, out_of_sample_confirmed, out_of_sample_blockers = _edge_score_and_confirmation(
                out_of_sample, oos_config
            )
        except Exception as e:
            log.warning("grid_parameter_sweep.oos_evaluation_failed", error=str(e))
            out_of_sample_confirmed = False
            out_of_sample_blockers = [f"oos_evaluation_error: {e}"]

    log.info(
        "grid_parameter_sweep.completed",
        candidates_evaluated=len(scored),
        candidates_promoted=len(promoted),
        chosen_grid_count=chosen.grid_count,
        chosen_leverage=str(chosen.leverage),
        validation_edge_confirmed=validation_confirmed,
        out_of_sample_edge_confirmed=out_of_sample_confirmed,
    )

    return GridSweepResult(
        chosen_parameters=chosen,
        candidates_evaluated=len(scored),
        candidates_promoted_to_validation=len(promoted),
        validation_edge_confirmed=validation_confirmed,
        validation_blockers=validation_blockers,
        out_of_sample_edge_confirmed=out_of_sample_confirmed,
        out_of_sample_blockers=out_of_sample_blockers,
    )


def _skip(reason: str | None, candidates_evaluated: int = 0) -> GridSweepResult:
    return GridSweepResult(
        chosen_parameters=None,
        candidates_evaluated=candidates_evaluated,
        candidates_promoted_to_validation=0,
        validation_edge_confirmed=False,
        validation_blockers=[],
        out_of_sample_edge_confirmed=False,
        out_of_sample_blockers=[],
        skipped_reason=reason or "no_candidate_produced_a_valid_backtest",
    )
