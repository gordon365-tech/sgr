"""
SGR Strategy Scoring & Robustness
====================================
Objektive Bewertung eines (Symbol, Strategie)-Validierungsergebnisses
(Autonomous-Strategy-Universe-Rollout, Phase 8 "Strategy Scoring" +
Phase 9 "Robustness Check").

Bewusste Entwurfsentscheidung (Phase 8: "darf nicht ausschliesslich auf
Return basieren"): Return fliesst NICHT als eigener gewichteter Term
ein - Sharpe Ratio IST bereits die risikoadjustierte Rendite (Return /
Volatilitaet), ein direkter Return-Term wuerde denselben Effekt, den
die Vorgabe explizit verhindern will, durch die Hintertuer wieder
einfuehren (hohe Rendite + hohe Vola koennte sonst einen hohen Score
"erkaufen"). Drawdown, Trade-Count, Walk-Forward-Konsistenz und
Robustness sind eigene, unabhaengige Terme - eine Strategie mit hoher
Rendite und extremem Drawdown kann dadurch nicht automatisch gewinnen.

Robustness (Phase 9) nutzt bewusst NUR bereits vorhandene Backtest-
/Walk-Forward-Daten (keine Parameter-Grid-Suche, siehe Modul-Docstring
von symbol_validation_runner.py fuer die begruendete Scope-Entscheidung
dazu):
    - Walk-Forward Split-Konsistenz (bereits vorhanden:
      WalkForwardResult.consistency_score).
    - Abhaengigkeit von einem einzelnen Ausreisser-Trade: wuerde das
      Entfernen des besten Trades das Ergebnis von profitabel auf
      unprofitabel kippen?
    - Abhaengigkeit von einem kurzen Zeitfenster: sind die Trades über
      die gesamte Backtest-Dauer verteilt oder in einem kleinen
      Zeitfenster geclustert?
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sgr.backtesting.types import BacktestResult, WalkForwardResult

# Gewichte summieren sich auf 100 - siehe Modul-Docstring fuer die
# Begruendung, warum Return kein eigener Term ist.
_WEIGHT_SHARPE = 30.0
_WEIGHT_DRAWDOWN = 20.0
_WEIGHT_TRADE_COUNT = 10.0
_WEIGHT_WF_CONSISTENCY = 20.0
_WEIGHT_WF_DEGRADATION = 10.0
_WEIGHT_ROBUSTNESS = 10.0

_SHARPE_CEILING = 3.0  # ab hier volle Punktzahl im Sharpe-Term
_MAX_DD_LIMIT_PCT = 20.0  # identisch zum bestehenden Go-Live-Hard-Limit
_TRADE_COUNT_CEILING = 130  # 30 (Minimum) + 100 fuer volle Punktzahl


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


@dataclass
class ScoreResult:
    score: float  # 0-100, 0 wenn die Basis-Gates nicht bestanden sind
    robustness_score: float  # 0-1
    passes_gates: bool
    components: dict[str, float]


def score_validation(
    backtest: BacktestResult,
    walk_forward: WalkForwardResult | None,
) -> ScoreResult:
    """
    Berechnet Score + Robustness fuer EIN (Symbol, Strategie)-Ergebnis.

    Basis-Gate (score=0, wenn nicht erfuellt - keine "teilweise gute"
    Strategie ohne bestandene Mindestanforderungen):
        - backtest.is_acceptable (bereits vorhandenes Kriterium:
          Sharpe>=1.0, PF>=1.3, MaxDD<=20%, HitRate>=40%, >=30 Trades)
        - walk_forward vorhanden UND is_consistent
    """
    robustness = _robustness_score(backtest, walk_forward)

    passes_gates = (
        backtest.is_acceptable and walk_forward is not None and walk_forward.is_consistent
    )
    if not passes_gates:
        return ScoreResult(
            score=0.0, robustness_score=robustness, passes_gates=False, components={}
        )

    assert walk_forward is not None  # fuer mypy - durch passes_gates bereits sichergestellt

    sharpe_component = _clamp(backtest.sharpe_ratio / _SHARPE_CEILING) * _WEIGHT_SHARPE
    drawdown_component = (
        _clamp(1.0 - backtest.max_drawdown_pct / _MAX_DD_LIMIT_PCT) * _WEIGHT_DRAWDOWN
    )
    trade_count_component = (
        _clamp((backtest.total_trades - 30) / (_TRADE_COUNT_CEILING - 30)) * _WEIGHT_TRADE_COUNT
    )
    wf_consistency_component = _clamp(walk_forward.consistency_score) * _WEIGHT_WF_CONSISTENCY
    wf_degradation_component = _clamp(walk_forward.degradation_factor) * _WEIGHT_WF_DEGRADATION
    robustness_component = robustness * _WEIGHT_ROBUSTNESS

    components = {
        "sharpe": round(sharpe_component, 2),
        "drawdown": round(drawdown_component, 2),
        "trade_count": round(trade_count_component, 2),
        "wf_consistency": round(wf_consistency_component, 2),
        "wf_degradation": round(wf_degradation_component, 2),
        "robustness": round(robustness_component, 2),
    }
    total = sum(components.values())

    return ScoreResult(
        score=round(total, 2),
        robustness_score=round(robustness, 3),
        passes_gates=True,
        components=components,
    )


def _robustness_score(
    backtest: BacktestResult,
    walk_forward: WalkForwardResult | None,
) -> float:
    """0-1. Siehe Modul-Docstring fuer die drei Teilkriterien."""
    scores: list[float] = []

    # 1. Walk-Forward Split-Konsistenz (bereits 0-1).
    if walk_forward is not None:
        scores.append(_clamp(walk_forward.consistency_score))

    trades = backtest.trades  # list[dict], siehe PerformanceAnalyzer._trade_to_dict
    if len(trades) >= 2:
        pnls = [float(t["net_pnl"]) for t in trades]
        total_pnl = sum(pnls)

        # 2. Abhaengigkeit von einem einzelnen Ausreisser-Trade: kippt
        # das Ergebnis ohne den besten Trade von profitabel zu
        # unprofitabel?
        best_pnl = max(pnls)
        pnl_without_best = total_pnl - best_pnl
        if total_pnl > 0:
            single_trade_dependent = pnl_without_best <= 0
            scores.append(0.0 if single_trade_dependent else 1.0)
        else:
            scores.append(0.0)

        # 3. Zeitliche Konzentration: liegen die Trades ueber die
        # gesamte Backtest-Dauer verteilt, oder geclustert in einem
        # kurzen Fenster? Gemessen als Spanne der mittleren 80% der
        # Trade-Entry-Zeitpunkte relativ zur Gesamtdauer.
        entry_times = sorted(
            datetime.fromisoformat(t["entry_time"]) for t in trades
        )
        total_span = (entry_times[-1] - entry_times[0]).total_seconds()
        if total_span > 0:
            lo_idx = int(len(entry_times) * 0.1)
            hi_idx = int(len(entry_times) * 0.9)
            hi_idx = max(hi_idx, lo_idx + 1)
            mid_span = (entry_times[hi_idx - 1] - entry_times[lo_idx]).total_seconds()
            concentration = _clamp(mid_span / total_span)
            scores.append(concentration)

    if not scores:
        return 0.0
    return sum(scores) / len(scores)
