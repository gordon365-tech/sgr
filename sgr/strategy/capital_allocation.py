"""
SGR Capital Allocation Engine (v1 - regelbasiert)
====================================================
Erste Version einer Kapitalzuteilung ueber MEHRERE Strategieklassen
hinweg (Trend Following, Mean Reversion, Long/Short/Adaptive Futures
Grid, ...) - siehe Aufgabenstellung: "Trend Strategy bekommt Kapital,
Futures Long Grid bekommt Kapital, ... oder keine Strategie bekommt
zusaetzliches Kapital."

WICHTIG - was diese Klasse NICHT ist:
    Kein Reinforcement-Learning-/ML-Allokator, keine Live-Portfolio-
    Rebalancing-Engine, die automatisch Kapital zwischen laufenden
    Positionen verschiebt. Dies ist ein REGELBASIERTER, transparenter
    Scorer + Allokations-Vorschlag: er nimmt bereits vorhandene
    Performance-/Edge-Kennzahlen (StrategyPerformance fuer direktionale
    Strategien, GridEdgeMetrics fuer Grid-Strategien) entgegen und
    verteilt ein Gesamt-Kapitalbudget proportional zu einem
    risikoadjustierten Score. Die eigentliche Positionsgroessen-
    durchsetzung bleibt vollstaendig bei RiskEngine/PositionSizer/
    GridRiskEngine - dieses Modul aendert dort keine Limits.

Keine feste Praeferenz fuer Pionex/Futures Grid allein aufgrund des
Produktnamens (siehe Aufgabenstellung: "Keine feste Bevorzugung
ausschliesslich aufgrund des Produktnamens") - die Gewichtung haengt
ausschliesslich vom nachgewiesenen, kostenbereinigten Edge-Score, der
Regimepassung und dem Risiko (Drawdown/Volatilitaet) ab.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sgr.strategy.grid_edge import GridEdgeMetrics


@dataclass(frozen=True)
class AllocationCandidate:
    """
    Ein Kandidat fuer Kapitalzuteilung - entweder eine direktionale
    Strategie (directional_score gesetzt) oder eine Grid-Strategie
    (grid_metrics gesetzt). Genau eines der beiden Felder ist gesetzt.
    """

    strategy_name: str
    product_type: str  # z.B. "spot", "perpetual", "futures_grid"
    exchange: str
    is_validated: bool  # StrategyRegistry.get_entry(name).is_validated bzw. Grid-Aequivalent

    directional_sharpe: float | None = None
    directional_max_drawdown_pct: float | None = None

    grid_metrics: GridEdgeMetrics | None = None

    def edge_score(self) -> float:
        """
        Einheitlicher, vergleichbarer Score (0.0+, hoeher = besser) ueber
        BEIDE Strategieklassen hinweg - notwendig, damit
        CapitalAllocationEngine ueberhaupt fair zwischen z.B.
        trend_following_v1 (Sharpe-basiert) und futures_grid_long_v1
        (GridEdgeMetrics-basiert) vergleichen kann.
        """
        if not self.is_validated:
            return 0.0

        if self.grid_metrics is not None:
            m = self.grid_metrics
            # Sharpe als Kernkomponente (konsistent mit direktionalen
            # Strategien), skaliert mit Grid-Effizienz und -Stabilitaet -
            # ein hoher Sharpe bei niedriger Grid Efficiency (viel Ertrag
            # verpufft in Kosten) oder niedriger Edge Stability wird
            # abgewertet, nicht 1:1 uebernommen.
            base = max(m.sharpe, 0.0)
            return base * m.grid_efficiency * (0.5 + 0.5 * m.edge_stability)

        if self.directional_sharpe is not None:
            dd_penalty = 1.0
            max_dd = self.directional_max_drawdown_pct
            if max_dd is not None and max_dd > 0:
                # Je hoeher der Max Drawdown, desto staerker die
                # Abwertung - 20% Drawdown halbiert den Score.
                dd_penalty = max(0.1, 1.0 - max_dd / 40.0)
            return max(self.directional_sharpe, 0.0) * dd_penalty

        return 0.0


@dataclass(frozen=True)
class AllocationResult:
    strategy_name: str
    product_type: str
    exchange: str
    score: float
    allocated_fraction: float  # 0.0-1.0 des Gesamtbudgets
    allocated_capital: Decimal
    reason: str


class CapitalAllocationEngine:
    """Stateless - reine Berechnung aus uebergebenen Kandidaten."""

    def __init__(self, max_single_strategy_fraction: float = 0.4) -> None:
        # Hard Cap: keine einzelne Strategie/kein einzelnes Grid darf
        # mehr als diesen Anteil des Gesamtbudgets erhalten, unabhaengig
        # vom Score - Diversifikations-Untergrenze, verhindert
        # Konzentrationsrisiko auf eine einzelne (ggf. ueberoptimierte)
        # Variante.
        self._max_single_fraction = max_single_strategy_fraction

    def allocate(
        self,
        candidates: list[AllocationCandidate],
        total_capital: Decimal,
        min_score_threshold: float = 0.1,
    ) -> list[AllocationResult]:
        scored = [(c, c.edge_score()) for c in candidates]
        eligible = [(c, s) for c, s in scored if s >= min_score_threshold]

        results: list[AllocationResult] = []
        for c, s in scored:
            if s < min_score_threshold:
                results.append(
                    AllocationResult(
                        strategy_name=c.strategy_name,
                        product_type=c.product_type,
                        exchange=c.exchange,
                        score=round(s, 4),
                        allocated_fraction=0.0,
                        allocated_capital=Decimal("0"),
                        reason=(
                            "Nicht validiert"
                            if not c.is_validated
                            else f"Score {s:.3f} unter Mindestschwelle {min_score_threshold}"
                        ),
                    )
                )

        if not eligible:
            return results

        total_score = sum(s for _, s in eligible)
        for c, s in eligible:
            raw_fraction = s / total_score if total_score > 0 else 0.0
            fraction = min(raw_fraction, self._max_single_fraction)
            results.append(
                AllocationResult(
                    strategy_name=c.strategy_name,
                    product_type=c.product_type,
                    exchange=c.exchange,
                    score=round(s, 4),
                    allocated_fraction=round(fraction, 4),
                    allocated_capital=(total_capital * Decimal(str(fraction))).quantize(
                        Decimal("0.01")
                    ),
                    reason="",
                )
            )

        # Nicht neu normalisiert, wenn der Cap gegriffen hat (siehe
        # max_single_strategy_fraction) - das nicht zugeteilte Restkapital
        # bleibt bewusst UNALLOZIERT (kein automatisches Nachverteilen auf
        # die anderen Kandidaten), bis ein Operator/eine spaetere Version
        # explizit entscheidet, was mit dem Rest geschieht (z.B. Cash-
        # Reserve). Transparenter als eine stille Umverteilung.
        return results

    @property
    def max_single_strategy_fraction(self) -> float:
        return self._max_single_fraction
