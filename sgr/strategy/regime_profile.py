"""
SGR Symbol Regime Profile
===========================
Baut ein Marktprofil fuer ein Symbol aus historischen Candles
(Autonomous-Strategy-Universe-Rollout, Phase 5 "Market Regime
Analysis").

Zweck: den Suchraum fuer die Strategie-Validierung intelligent
reduzieren - fuer ein Symbol, das ueber seine gesamte Historie nie
BREAKOUT-Verhalten gezeigt hat, macht ein Test von breakout_v1 wenig
Sinn. Das Profil bevorzugt dabei KEINE Strategie inhaltlich (siehe
Vorgabe "darf keine Strategie künstlich bevorzugen") - es liefert nur
die beobachtete Regime-Verteilung; welche Strategien zu welchen
Regimen passen, ist bereits durch TradingStrategy.supported_regimes
jeder einzelnen Strategie selbst deklariert (sgr/strategy/base.py) -
dieses Modul mappt nur "welche Regime wurden beobachtet" auf "welche
bereits registrierten Strategien haben ueberhaupt eine Chance,
jemals einen Trade zu generieren".

Wiederverwendet bewusst:
    - sgr.market_data.feature_engineering.FeatureEngineer (identische
      Indikatoren wie im Live-Betrieb, keine Parallel-Berechnung).
    - sgr.strategy.regime_classifier.classify_regime() ("regime_detector_v1",
      dieselbe Klassifikation wie live in StrategyEngine.process()).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sgr.core.types import Candle, MarketRegime
from sgr.market_data.feature_engineering import FeatureEngineer
from sgr.strategy.regime_classifier import classify_regime

# Alle warmup Bars ueberspringen (Indikatoren wie ADX/EMA200 brauchen
# Vorlauf, siehe FeatureEngineer.FULL_CANDLES) und danach in festen
# Abstaenden samplen statt bei jedem Bar - eine Regime-Verteilung muss
# nicht bar-genau sein, um den Suchraum sinnvoll einzuschraenken, und
# ein zu feines Sampling waere unnoetig teuer bei 900+ Symbolen.
_WARMUP_BARS = 200
_SAMPLE_INTERVAL_BARS = 48  # bei "1h": alle 2 Tage ein Sample


@dataclass
class RegimeProfile:
    dominant_regime: MarketRegime
    distribution: dict[str, int] = field(default_factory=dict)
    diversity_count: int = 0
    n_samples: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "dominant_regime": self.dominant_regime.value,
            "distribution": self.distribution,
            "diversity_count": self.diversity_count,
            "n_samples": self.n_samples,
        }

    def candidate_regimes(self) -> set[MarketRegime]:
        """Alle jemals beobachteten Regime (fuer die Strategie-
        Vorauswahl - siehe select_candidate_strategies())."""
        return {MarketRegime(r) for r in self.distribution}


def build_regime_profile(
    candles: list[Candle],
    sample_interval_bars: int = _SAMPLE_INTERVAL_BARS,
) -> RegimeProfile:
    """
    Reine, synchrone Funktion (kein I/O) - Candles muessen bereits
    geladen und Data-Quality-geprueft sein (siehe
    sgr.backtesting.data_quality.assess_data_quality(), IMMER zuerst
    aufrufen).
    """
    engineer = FeatureEngineer()
    counts: Counter[str] = Counter()

    n = len(candles)
    if n <= _WARMUP_BARS:
        return RegimeProfile(dominant_regime=MarketRegime.UNKNOWN, distribution={}, n_samples=0)

    for i in range(_WARMUP_BARS, n, sample_interval_bars):
        window = candles[: i + 1]
        try:
            features = engineer.compute(window)
        except Exception:
            # Ein einzelner fehlerhafter Sample-Punkt darf das gesamte
            # Profil nicht zum Scheitern bringen (analog zum Fail-Safe-
            # Prinzip von StrategyEngine.process() bei Feature-Fehlern).
            continue
        regime, _confidence = classify_regime(features.indicators)
        counts[regime.value] += 1

    if not counts:
        return RegimeProfile(dominant_regime=MarketRegime.UNKNOWN, distribution={}, n_samples=0)

    dominant = MarketRegime(counts.most_common(1)[0][0])
    return RegimeProfile(
        dominant_regime=dominant,
        distribution=dict(counts),
        diversity_count=len(counts),
        n_samples=sum(counts.values()),
    )


def select_candidate_strategies(
    profile: RegimeProfile,
    strategies: list[Any],  # list[TradingStrategy] - Protocol-Import würde Zyklus riskieren
) -> list[Any]:
    """
    Filtert eine Strategie-Liste auf die, deren supported_regimes
    mindestens ein im Profil beobachtetes Regime enthalten. Reduziert
    den Suchraum (Phase 5) - trifft KEINE Qualitätsaussage, nur eine
    Plausibilitäts-Vorauswahl. Ein Symbol ganz ohne beobachtetes
    passendes Regime (z.B. nur UNKNOWN) liefert eine leere Liste - das
    ist ein legitimes, ehrliches Ergebnis (spaeter NO_VALID_STRATEGY),
    kein Fehler.
    """
    candidate_regimes = profile.candidate_regimes()
    return [s for s in strategies if candidate_regimes & set(s.supported_regimes)]
