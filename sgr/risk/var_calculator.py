"""
SGR VaR Calculator
==================
Value at Risk (VaR) und Expected Shortfall (ES) Berechnungen.

Methoden:
    Historical Simulation: nutzt tatsächliche Return-Verteilung
    Parametric (Normal): schnell, bei normalverteilten Returns
    Parametric (Cornish-Fisher): korrigiert für Schiefe und Kurtosis

Wahl der Methode:
    - Historical: Default (kein Normalverteilungs-Annahme)
    - Parametric Normal: für sehr kurze History (< 30 Tage)
    - Cornish-Fisher: wenn starke Nicht-Normalität bekannt

VaR gibt die maximale Verlust mit Konfidenz X an (täglich, 1-tägig).
ES (= CVaR) gibt den erwarteten Verlust über VaR hinaus an.

Konvention in SGR: VaR und ES als positive Zahlen (Verlust als +).
VaR 95%, 1-tägig, 3% → „95% Wahrscheinlichkeit: max. 3% Tagesverlust"

Limitierungen:
    - Historical VaR unterschätzt Tail-Risk bei kurzer History
    - Normalverteilungs-Annahme gilt für Crypto NICHT
    - Immer mit Stress-Test kombinieren
"""

from __future__ import annotations

from enum import StrEnum

import numpy as np

from sgr.core.logging import get_logger

log = get_logger(__name__)


class VaRMethod(StrEnum):
    HISTORICAL = "historical"
    PARAMETRIC_NORMAL = "parametric_normal"
    CORNISH_FISHER = "cornish_fisher"


class VaRResult:
    """Ergebnis einer VaR-Berechnung."""

    __slots__ = ("var", "es", "confidence", "method", "observations")

    def __init__(
        self,
        var: float,
        es: float,
        confidence: float,
        method: VaRMethod,
        observations: int,
    ) -> None:
        self.var = var  # VaR als positiver Dezimalwert (z.B. 0.03 = 3%)
        self.es = es  # Expected Shortfall
        self.confidence = confidence
        self.method = method
        self.observations = observations

    def __repr__(self) -> str:
        return (
            f"VaRResult(var={self.var:.4%}, es={self.es:.4%}, "
            f"conf={self.confidence:.0%}, method={self.method.value})"
        )


class VaRCalculator:
    """
    Berechnet VaR und Expected Shortfall für ein Portfolio.

    Usage:
        calc = VaRCalculator()
        returns = np.array([0.01, -0.02, 0.005, ...])  # Daily returns
        result = calc.compute(returns, confidence=0.95)
        print(f"VaR 95%: {result.var:.2%}")
    """

    def compute(
        self,
        returns: np.ndarray,
        confidence: float = 0.95,
        method: VaRMethod = VaRMethod.HISTORICAL,
    ) -> VaRResult:
        """
        Berechnet VaR und ES.

        Args:
            returns: Array von täglichen Returns (z.B. [-0.02, 0.01, ...])
                     Positiv = Gewinn, Negativ = Verlust
            confidence: Konfidenz-Level (0.95 = 95% VaR)
            method: Berechnungsmethode

        Returns:
            VaRResult mit VaR und ES als positive Werte (Verlustgröße)
        """
        if len(returns) < 10:
            log.warning(
                "var.insufficient_data",
                observations=len(returns),
                minimum=10,
            )
            return VaRResult(
                var=0.05,  # Konservative Schätzung bei wenig Daten
                es=0.08,
                confidence=confidence,
                method=method,
                observations=len(returns),
            )

        if method == VaRMethod.HISTORICAL:
            return self._historical(returns, confidence)
        elif method == VaRMethod.PARAMETRIC_NORMAL:
            return self._parametric_normal(returns, confidence)
        elif method == VaRMethod.CORNISH_FISHER:
            return self._cornish_fisher(returns, confidence)
        else:
            raise ValueError(f"Unknown VaR method: {method}")

    def _historical(self, returns: np.ndarray, confidence: float) -> VaRResult:
        """
        Historical Simulation VaR.
        Sortiert Returns und schneidet am Quantil ab.
        Kein Verteilungs-Annahme – nutzt tatsächliche Return-Verteilung.
        """
        losses = -returns  # Verluste als positive Zahlen
        var = float(np.percentile(losses, confidence * 100))

        # ES = Durchschnitt der Verluste über VaR hinaus
        tail_losses = losses[losses >= var]
        es = float(np.mean(tail_losses)) if len(tail_losses) > 0 else var

        return VaRResult(
            var=max(var, 0.0),
            es=max(es, var),
            confidence=confidence,
            method=VaRMethod.HISTORICAL,
            observations=len(returns),
        )

    def _parametric_normal(self, returns: np.ndarray, confidence: float) -> VaRResult:
        """
        Parametric VaR unter Normalverteilungs-Annahme.
        VaR = μ - z * σ (wo z = z-score für Konfidenz)
        Schnell aber unzuverlässig bei Fat Tails (Crypto!).
        """
        from scipy import stats  # type: ignore[import-untyped]

        mu = float(np.mean(returns))
        sigma = float(np.std(returns, ddof=1))
        z = stats.norm.ppf(1 - confidence)

        var = float(-(mu + z * sigma))
        # ES für Normalverteilung: ES = μ + σ * φ(z)/(1-α)
        phi_z = float(stats.norm.pdf(z))
        es = float(-(mu - sigma * phi_z / (1 - confidence)))

        return VaRResult(
            var=max(var, 0.0),
            es=max(es, var),
            confidence=confidence,
            method=VaRMethod.PARAMETRIC_NORMAL,
            observations=len(returns),
        )

    def _cornish_fisher(self, returns: np.ndarray, confidence: float) -> VaRResult:
        """
        Cornish-Fisher VaR: korrigiert Normalverteilung für Schiefe (S) und Kurtosis (K).
        Besser als Normal für nicht-normale Verteilungen.

        z_cf = z + (z²-1)·S/6 + (z³-3z)·(K-3)/24 - (2z³-5z)·S²/36
        """
        from scipy import stats  # type: ignore[import-untyped]

        mu = float(np.mean(returns))
        sigma = float(np.std(returns, ddof=1))
        skew = float(stats.skew(returns))
        kurt = float(stats.kurtosis(returns))  # Excess kurtosis

        z = stats.norm.ppf(1 - confidence)

        # Cornish-Fisher Expansion
        z_cf = (
            z
            + (z**2 - 1) * skew / 6
            + (z**3 - 3 * z) * kurt / 24
            - (2 * z**3 - 5 * z) * skew**2 / 36
        )

        var = float(-(mu + z_cf * sigma))

        # ES via Historical als Fallback (CF expansion für ES komplex)
        hist_result = self._historical(returns, confidence)

        return VaRResult(
            var=max(var, 0.0),
            es=hist_result.es,
            confidence=confidence,
            method=VaRMethod.CORNISH_FISHER,
            observations=len(returns),
        )

    def compute_portfolio_var(
        self,
        position_returns: dict[str, np.ndarray],
        weights: dict[str, float],
        confidence: float = 0.95,
    ) -> VaRResult:
        """
        Portfolio VaR unter Berücksichtigung von Korrelationen.
        Nutzt gewichtete Return-Serie.

        Args:
            position_returns: {symbol: returns_array}
            weights: {symbol: portfolio_weight} (sum ≈ 1.0)
        """
        symbols = list(position_returns.keys())
        if not symbols:
            return VaRResult(
                var=0.0,
                es=0.0,
                confidence=confidence,
                method=VaRMethod.HISTORICAL,
                observations=0,
            )

        # Mindestlänge über alle Serien
        min_len = min(len(r) for r in position_returns.values())
        if min_len < 10:
            return self.compute(np.array([]), confidence)

        # Gewichtete Portfolio-Returns
        portfolio_returns = np.zeros(min_len)
        for symbol in symbols:
            weight = weights.get(symbol, 0.0)
            returns = position_returns[symbol][-min_len:]
            portfolio_returns += weight * returns

        return self.compute(portfolio_returns, confidence, VaRMethod.HISTORICAL)


class MonteCarloVaR:
    """
    Monte Carlo VaR Simulation.
    Verwendet für Stress-Tests und Go-Live Gates.
    Nicht für Echtzeit-Berechnung (zu langsam).
    """

    def simulate(
        self,
        returns: np.ndarray,
        portfolio_value: float,
        simulations: int = 1000,
        horizon_days: int = 1,
        confidence: float = 0.95,
        seed: int | None = 42,
    ) -> dict[str, float]:
        """
        Monte Carlo Simulation der Portfolio-Returns.

        Returns:
            dict mit:
                var_95: VaR 95%
                var_99: VaR 99%
                es_95: Expected Shortfall 95%
                worst_case: schlechtestes Szenario
                percentile_5: 5th Percentile des Portfolio-Werts
        """
        if seed is not None:
            np.random.seed(seed)

        mu = float(np.mean(returns))
        sigma = float(np.std(returns, ddof=1))

        # Simuliere `simulations` Szenarien über `horizon_days`
        sim_returns = np.random.normal(
            mu * horizon_days,
            sigma * np.sqrt(horizon_days),
            simulations,
        )

        sim_losses = -sim_returns * portfolio_value
        sim_losses_sorted = np.sort(sim_losses)

        return {
            "var_95": float(np.percentile(sim_losses_sorted, 95)),
            "var_99": float(np.percentile(sim_losses_sorted, 99)),
            "es_95": float(
                np.mean(
                    sim_losses_sorted[sim_losses_sorted >= np.percentile(sim_losses_sorted, 95)]
                )
            ),
            "worst_case": float(sim_losses_sorted[-1]),
            "percentile_5": float(portfolio_value - np.percentile(sim_losses_sorted, 95)),
        }
