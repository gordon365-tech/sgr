"""
SGR Strategy Parameter Optimizer
====================================
Bounded, anti-overfitting Parameter-Suche fuer die bestehenden
TradingStrategy-Implementierungen (Autonomous-Strategy-Universe-Rollout,
Phase-8-Folgearbeit: "Parameter-Adaptivitaet ist der eigentliche
strukturelle Befund hinter der 99.96%-Walk-Forward-Kollaps-Rate", siehe
/tmp/sgr-718-validation-analysis.md Research-Report Punkt 8).

Architektur-Entscheidung (additiv, keine Parallelarchitektur):
    Nutzt exakt dieselbe BacktestingEngine/BacktestSimulator/
    WalkForwardAnalyzer-Pipeline wie jede andere Validierung in diesem
    Repository. Eine Parameter-Suche ist technisch nichts anderes als
    "backteste denselben Strategie-Code mehrfach mit unterschiedlichen
    Konstruktor-Argumenten" - dafuer registriert dieses Modul pro
    Kandidaten-Parametersatz eine TEMPORAERE Trial-Instanz unter einem
    synthetischen Namen in der bestehenden StrategyRegistry (siehe
    StrategyRegistry.unregister() Docstring), lässt
    BacktestingEngine.run_full_validation() unveraendert darauf laufen,
    und entfernt den Trial-Eintrag danach wieder.

KRITISCHE Anti-Overfitting-Entscheidung (der eigentliche Kern dieses
Moduls, nicht nur eine Implementierungsdetail):
    Die Parameter-SUCHE laeuft AUSSCHLIESSLICH auf einem FRUEHEN,
    separaten Zeitfenster ("optimization window" - die ersten
    OPTIMIZATION_WINDOW_FRACTION der verfuegbaren Historie). Das
    GEWAEHLTE Parameter-Set wird danach auf einem SPAETEREN, davon
    disjunkten Zeitfenster ("validation window") ganz normal durch
    BacktestingEngine.run_full_validation() geschickt - inklusive
    dessen EIGENEM internen 6-Split Walk-Forward. Ohne diese Trennung
    wuerde ein Optimierungslauf denselben Datensatz sowohl zum Waehlen
    ALS AUCH zum "Bestehen" der Validierung benutzen - genau das
    Overfitting-Risiko, das im Research-Report (Punkt 5/9) als Ursache
    fuer die 99.96%-Walk-Forward-Kollaps-Rate der bisherigen, GAR NICHT
    optimierten Strategien identifiziert wurde. Ein Optimierer, der auf
    denselben 718 Symbolen sowohl sucht als auch validiert, waere ein
    Overfitting-Problem zweiter Ordnung (siehe Research-Report Punkt 10,
    letzter Absatz) - dieses Modul existiert explizit, um genau das zu
    vermeiden.

    Symbole mit zu wenig Historie fuer eine sinnvolle Aufteilung
    (< MIN_CANDLES_FOR_OPTIMIZATION) werden NICHT optimiert - sie
    durchlaufen weiterhin die bestehende, unveraenderte Default-Parameter-
    Validierung. Das ist ein ehrliches, dokumentiertes Ergebnis
    ("optimization_skipped": "insufficient_history"), keine Luecke.

Suchraum bewusst klein und explizit (kein generisches "perturbiere jedes
Feld"): pro Strategie genau zwei tatsaechlich entry-relevante Felder
(plus min_confidence, das gattungsuebergreifend als Schwelle existiert),
je 3 Kandidatenwerte inklusive des bestehenden Default-Werts als
Mittelpunkt - 9 Kandidaten pro (Symbol, Strategie). Explizit statt
generisch, weil ein blindes Perturbieren jedes Feldes auch Boolean-Flags
(z.B. require_imbalance) oder an den Regime-Klassifizierer gekoppelte
Konstanten (z.B. VolAdjustedMomentumParams.atr_pct_reference) sinnlos
oder inkonsistent variieren wuerde.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sgr.core.logging import get_logger
from sgr.core.types import ExchangeID
from sgr.strategy.base import TradingStrategy

log = get_logger(__name__)

# Symbol braucht deutlich mehr als das reine Data-Quality-Minimum
# (sgr.backtesting.data_quality.MIN_CANDLES = 1000), weil das
# verfuegbare Fenster hier zusaetzlich in ein Optimization- UND ein
# Validation-Fenster aufgeteilt wird - jedes der beiden braucht fuer
# sich genommen noch genug Bars fuer einen aussagekraeftigen Backtest
# bzw. (im Validation-Fenster) fuer WalkForwardAnalyzer's 6 Splits.
MIN_CANDLES_FOR_OPTIMIZATION = 2200

# Anteil der verfuegbaren Historie, der fuer die Parameter-SUCHE
# verwendet wird - der Rest (spaeter, disjunkt) ist das Validation-
# Fenster. < 50%, weil das Validation-Fenster selbst noch einen
# 6-Split Walk-Forward tragen muss (braucht mehr Bars als ein simpler
# Backtest) und damit der groessere Anteil sein sollte.
OPTIMIZATION_WINDOW_FRACTION = 0.35

# Mindestanzahl Trades im Optimierungsfenster, damit ein Kandidat
# ueberhaupt in die Rangliste aufgenommen wird - ein Kandidat mit 1-2
# zufaelligen Trades und zufaellig hohem Sharpe waere kein belastbares
# Auswahlkriterium (dieselbe Denkweise wie das bestehende
# BacktestResult.is_acceptable-Trade-Count-Gate, hier bewusst niedriger
# angesetzt, weil das Optimierungsfenster selbst kuerzer ist als das
# volle Validierungsfenster).
MIN_TRADES_FOR_CANDIDATE = 10


@dataclass
class ParameterCandidate:
    overrides: dict[str, float]
    sharpe_ratio: float
    total_trades: int
    total_return_pct: float
    max_drawdown_pct: float
    disqualified_reason: str | None = None

    @property
    def qualifies(self) -> bool:
        return self.disqualified_reason is None


@dataclass
class OptimizationResult:
    strategy_name: str
    symbol: str
    performed: bool
    skip_reason: str | None = None
    best_overrides: dict[str, float] = field(default_factory=dict)
    candidates_tried: int = 0
    candidates: list[ParameterCandidate] = field(default_factory=list)
    optimization_window: tuple[str, str] | None = None
    validation_window: tuple[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "performed": self.performed,
            "skip_reason": self.skip_reason,
            "best_overrides": self.best_overrides,
            "candidates_tried": self.candidates_tried,
            "optimization_window": self.optimization_window,
            "validation_window": self.validation_window,
            "candidate_sharpes": [round(c.sharpe_ratio, 4) for c in self.candidates],
        }


# Suchraum: strategy_name -> {feld_name: [kandidat1, kandidat2(=Default), kandidat3]}
# Der jeweils MITTLERE Wert ist immer der bestehende Default aus der
# jeweiligen *Params-Dataclass (sgr/strategy/{mean_reversion,
# trend_following,breakout,momentum,volatility_adjusted_momentum}.py) -
# ein Optimierungslauf kann den Status quo dadurch nie "verpassen".
PARAMETER_SEARCH_SPACE: dict[str, dict[str, list[float]]] = {
    "mean_reversion_v1": {
        "rsi_oversold": [30.0, 35.0, 40.0],
        "min_confidence": [0.45, 0.55, 0.65],
    },
    "trend_following_v1": {
        "adx_min": [20.0, 25.0, 30.0],
        "min_confidence": [0.45, 0.55, 0.65],
    },
    "breakout_v1": {
        "bb_width_expansion_min": [0.035, 0.045, 0.055],
        "min_confidence": [0.45, 0.55, 0.65],
    },
    "momentum_v1": {
        "returns_5_min": [0.005, 0.01, 0.02],
        "min_confidence": [0.45, 0.55, 0.65],
    },
    "volatility_adjusted_momentum_v1": {
        "returns_5_min": [0.01, 0.02, 0.03],
        "min_confidence": [0.45, 0.55, 0.65],
    },
}


def _param_grid(space: dict[str, list[float]]) -> list[dict[str, float]]:
    """Kartesisches Produkt eines kleinen, expliziten Suchraums - kein
    externes Optimierungs-Paket noetig fuer <=9 Kombinationen."""
    keys = list(space.keys())
    combos: list[dict[str, float]] = [{}]
    for key in keys:
        new_combos = []
        for combo in combos:
            for value in space[key]:
                new_combos.append({**combo, key: value})
        combos = new_combos
    return combos


def build_trial_strategy(
    strategy_class: type[Any],
    params_class: type,
    overrides: dict[str, float],
    trial_name: str,
) -> TradingStrategy:
    """Konstruiert eine Trial-Instanz: Default-Params-Instanz + gezielte
    Feld-Overrides (dataclasses.replace, kein manuelles Feld-fuer-Feld-
    Kopieren) - dann name auf einen eindeutigen, synthetischen Wert
    gesetzt (siehe StrategyRegistry.unregister() Docstring).

    strategy_class: type[Any] statt type[TradingStrategy] - TradingStrategy
    ist ein Protocol (siehe sgr/strategy/base.py) und beschreibt damit nur
    die Instanz-Schnittstelle, nicht den Konstruktor. Jede der 5 echten
    Strategien folgt zwar identisch __init__(self, params: XParams | None
    = None), das ist aber kein Teil des Protocols selbst."""
    base_params = params_class()
    trial_params = replace(base_params, **overrides)
    instance = strategy_class(trial_params)
    instance.name = trial_name
    return instance  # type: ignore[no-any-return]


async def optimize_strategy_parameters(
    *,
    engine: Any,
    registry: Any,
    strategy_name: str,
    strategy_class: type[Any],
    params_class: type,
    symbol: str,
    timeframe: str,
    full_start: datetime,
    full_end: datetime,
    exchange_id: ExchangeID,
) -> OptimizationResult:
    """
    Fuehrt die Parameter-Suche fuer EIN (Symbol, Strategie)-Paar durch.

    full_start/full_end: das GESAMTE fuer diese Validierung verfuegbare
    Zeitfenster (identisch zu dem, das _validate_symbol() sonst als
    Ganzes an BacktestingEngine.run_full_validation() uebergeben wuerde).
    Wird HIER in [optimization_window] + [validation_window] aufgeteilt
    (siehe Modul-Docstring) - der Aufrufer bekommt in
    OptimizationResult.best_overrides die gewaehlten Parameter zurueck
    und ist selbst dafuer verantwortlich, die anschliessende
    run_full_validation() NUR auf dem validation_window (nicht mehr dem
    vollen full_start/full_end) laufen zu lassen.

    Rein additiv: bei jedem Fehler (z.B. zu wenig Daten, ein einzelner
    Kandidat schlaegt technisch fehl) wird ein OptimizationResult mit
    performed=False zurueckgegeben - _validate_symbol() faellt dann auf
    das bestehende Default-Parameter-Verhalten zurueck, nie ein harter
    Fehler.
    """
    total_days = (full_end - full_start).days
    space = PARAMETER_SEARCH_SPACE.get(strategy_name)
    if space is None:
        return OptimizationResult(
            strategy_name=strategy_name,
            symbol=symbol,
            performed=False,
            skip_reason="no_search_space_defined_for_strategy",
        )

    # Grobe Bar-Schaetzung (1h-Timeframe angenommen, wie ueberall sonst
    # in diesem Pipeline-Schritt - DEFAULT_TIMEFRAME in
    # symbol_validation_runner.py) um MIN_CANDLES_FOR_OPTIMIZATION vs.
    # die tatsaechliche Spanne grob abzuschaetzen, OHNE die Candles
    # selbst hier zweimal zu laden (das macht der Aufrufer bereits fuer
    # die Data-Quality-Pruefung).
    approx_candles = total_days * 24 if timeframe == "1h" else total_days
    if approx_candles < MIN_CANDLES_FOR_OPTIMIZATION:
        return OptimizationResult(
            strategy_name=strategy_name,
            symbol=symbol,
            performed=False,
            skip_reason="insufficient_history_for_optimization_split",
        )

    opt_days = int(total_days * OPTIMIZATION_WINDOW_FRACTION)
    opt_start = full_start
    opt_end = full_start + timedelta(days=opt_days)
    # Validation-Fenster beginnt NACH dem Optimierungsfenster - disjunkt,
    # keine Ueberlappung (siehe Modul-Docstring).
    validation_start = opt_end
    validation_end = full_end

    combos = _param_grid(space)
    candidates: list[ParameterCandidate] = []

    for overrides in combos:
        trial_name = f"{strategy_name}__opt_{uuid4().hex[:8]}"
        try:
            trial_instance = build_trial_strategy(
                strategy_class, params_class, overrides, trial_name
            )
            registry.register_instance(trial_instance)

            report = await engine.run_full_validation(
                strategy_names=[trial_name],
                symbols=[symbol],
                timeframe=timeframe,
                start_date=opt_start,
                end_date=opt_end,
                exchange_pool=None,
                exchange_id=exchange_id,
                run_walk_forward=False,
                run_monte_carlo=False,
            )
            bt = report.backtest
            disqualified = None
            if bt.total_trades < MIN_TRADES_FOR_CANDIDATE:
                disqualified = "too_few_trades_in_optimization_window"
            candidates.append(
                ParameterCandidate(
                    overrides=overrides,
                    sharpe_ratio=bt.sharpe_ratio,
                    total_trades=bt.total_trades,
                    total_return_pct=bt.total_return_pct,
                    max_drawdown_pct=bt.max_drawdown_pct,
                    disqualified_reason=disqualified,
                )
            )
        except Exception as e:
            log.warning(
                "parameter_optimizer.candidate_failed",
                strategy=strategy_name,
                symbol=symbol,
                overrides=overrides,
                error=str(e),
            )
            candidates.append(
                ParameterCandidate(
                    overrides=overrides,
                    sharpe_ratio=-999.0,
                    total_trades=0,
                    total_return_pct=0.0,
                    max_drawdown_pct=0.0,
                    disqualified_reason=f"technical_error: {e}"[:200],
                )
            )
        finally:
            registry.unregister(trial_name)

    qualifying = [c for c in candidates if c.qualifies]
    if not qualifying:
        return OptimizationResult(
            strategy_name=strategy_name,
            symbol=symbol,
            performed=True,
            skip_reason="no_candidate_reached_minimum_trade_count",
            candidates_tried=len(candidates),
            candidates=candidates,
            optimization_window=(opt_start.isoformat(), opt_end.isoformat()),
            validation_window=(validation_start.isoformat(), validation_end.isoformat()),
        )

    best = max(qualifying, key=lambda c: c.sharpe_ratio)

    log.info(
        "parameter_optimizer.completed",
        strategy=strategy_name,
        symbol=symbol,
        candidates_tried=len(candidates),
        best_sharpe=round(best.sharpe_ratio, 3),
        best_overrides=best.overrides,
        median_candidate_sharpe=round(statistics.median([c.sharpe_ratio for c in qualifying]), 3),
    )

    return OptimizationResult(
        strategy_name=strategy_name,
        symbol=symbol,
        performed=True,
        best_overrides=best.overrides,
        candidates_tried=len(candidates),
        candidates=candidates,
        optimization_window=(opt_start.isoformat(), opt_end.isoformat()),
        validation_window=(validation_start.isoformat(), validation_end.isoformat()),
    )
