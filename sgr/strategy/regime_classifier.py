"""
SGR Regime Classifier ("regime_detector_v1")
==============================================
Klassifiziert das aktuelle Marktregime aus bereits berechneten
Indikatoren (sgr/market_data/feature_engineering.py) - rein rule-based,
kein trainiertes Modell, keine Persistenz-Abhaengigkeit.

Warum rule-based statt des vorhandenen sgr/ml/regime_detector.py
(RandomForest+HMM)?
    RegimeDetector.predict() faellt bei fehlendem trainierten Modell
    bereits selbst auf einen rule-based Fallback zurueck
    (_rule_based_fallback(), siehe dortigen Docstring) - und dieser
    Fallback wird produktiv nie trainiert/persistiert verwendet (kein
    save()/load()-Aufruf irgendwo im Code, kein Produktions-Call-Site,
    empirisch per grep verifiziert). Fuer den 24/7-Autonomiebetrieb
    braucht die Live-Pipeline ein Regime-Signal, das IMMER, synchron,
    ohne Trainings-/Ladefehler-Risiko verfuegbar ist. Ein rule-based
    Klassifizierer erreicht genau das, ohne den ML-Pfad (der fuer eine
    spaetere, echte trainierte Verbesserung weiterhin offen bleibt)
    ueberhaupt anzufassen.

    Dieselbe Grund-Heuristik (ADX+DI fuer Trend, atr_pct fuer
    Volatilitaet) existiert bereits an ZWEI Stellen im Code
    (RegimeDetector._rule_based_fallback() UND
    sgr.backtesting.simulator.BacktestSimulator._detect_regime_simple())
    - dieses Modul verallgemeinert dasselbe, bereits etablierte Muster
    (keine dritte, abweichende Erfindung), erweitert es aber um
    BREAKOUT und LOW_VOLATILITY (siehe MarketRegime-Docstring in
    sgr/core/types.py - beide fehlten in den bisherigen Implementierungen
    komplett). sgr.backtesting.simulator wird bewusst NICHT auf dieses
    Modul umgestellt: eine Aenderung dort wuerde historische
    Backtest-/Walk-Forward-Ergebnisse der bereits validierten
    Bestandsstrategien (mean_reversion_v1, trend_following_v1)
    verschieben koennen - außerhalb des Auftragsumfangs, unnoetiges
    Risiko fuer "muss erhalten bleiben".

Schwellenwerte sind bewusst einfache, erklaerbare Heuristiken (kein
statistisches Fitting) - auf demselben Praezisionsniveau wie die
bereits bestehenden rule-based Implementierungen im Code (z.B.
atr_pct > 0.05 fuer HIGH_VOLATILITY, identisch zu
BacktestSimulator._detect_regime_simple()). Keine empirische
Validierung dieser konkreten Schwellen wurde fuer dieses Modul
durchgefuehrt - die Klassifikation ist ein Eingabesignal fuer die
Strategieauswahl, keine eigene handelnde Strategie, und wird deshalb
nicht durch StrategyValidationRunner backtestet.
"""

from __future__ import annotations

from sgr.core.types import MarketRegime
from sgr.market_data.types import IndicatorValues

REGIME_DETECTOR_VERSION = "regime_detector_v1"

# Numerischer Rang fuer Grafana-Sortierung/Heatmaps (siehe
# sgr.monitoring.metrics.record_market_regime) - keine inhaltliche
# Ordnung (ein Regime ist nicht "besser" als ein anderes), nur ein
# stabiler, eindeutiger Wert pro Regime fuer den Gauge.
REGIME_RANK: dict[MarketRegime, int] = {
    MarketRegime.UNKNOWN: 0,
    MarketRegime.LOW_VOLATILITY: 1,
    MarketRegime.RANGING: 2,
    MarketRegime.TRENDING_UP: 3,
    MarketRegime.TRENDING_DOWN: 4,
    MarketRegime.BREAKOUT: 5,
    MarketRegime.HIGH_VOLATILITY: 6,
    MarketRegime.CRISIS: 7,
}

# Volatilitaets-Schwellen (atr_pct = ATR als Anteil des Preises).
# HIGH_VOLATILITY-Schwelle identisch zu BacktestSimulator.
HIGH_VOLATILITY_ATR_PCT = 0.05
_LOW_VOLATILITY_ATR_PCT = 0.012

# Trendstaerke (ADX): > 25 = klarer Trend, < 20 = kein Trend (Range),
# dazwischen = Uebergangszone -> nicht eindeutig genug fuer TRENDING.
_ADX_TREND_THRESHOLD = 25.0
_ADX_RANGE_THRESHOLD = 20.0

# Breakout: Baender expandiert (Gegenteil der "Squeeze"-Schwelle 0.04,
# die mean_reversion_v1 fuer RANGING nutzt) UND Preis nahe am/ueber dem
# Band-Rand.
_BREAKOUT_BB_WIDTH = 0.045
_BREAKOUT_BB_POSITION_LOW = 0.05
_BREAKOUT_BB_POSITION_HIGH = 0.95


def classify_regime(indicators: IndicatorValues) -> tuple[MarketRegime, float]:
    """
    Klassifiziert das Marktregime aus einem einzelnen Indikator-Snapshot.

    Reine Funktion, kein I/O, kein State - sicher aus jedem synchronen
    Kontext aufrufbar (siehe StrategyEngine.process()).

    Returns:
        (regime, confidence). confidence in [0, 1] - grob, kein
        kalibriertes Wahrscheinlichkeitsmass (siehe Modul-Docstring:
        keine ML-Kalibrierung fuer diesen rule-based Pfad).
        MarketRegime.UNKNOWN bei fehlenden Pflicht-Indikatoren
        (adx_14/atr_pct) - z.B. zu wenig Candle-History.
    """
    adx = indicators.adx_14
    atr_pct = indicators.atr_pct
    di_plus = indicators.di_plus
    di_minus = indicators.di_minus
    bb_width = indicators.bb_width
    bb_position = indicators.bb_position

    if adx is None or atr_pct is None:
        return MarketRegime.UNKNOWN, 0.0

    # 1. Extremvolatilitaet zuerst - unabhaengig vom Trendstatus
    # sicherheitsrelevant genug (HIGH_VOLATILITY -> reduzierte
    # Positionsgroesse oder kein Trade, siehe Task-Vorgabe), um Vorrang
    # vor einer Trend-/Range-Einordnung zu haben.
    if atr_pct > HIGH_VOLATILITY_ATR_PCT:
        return MarketRegime.HIGH_VOLATILITY, min(0.5 + (atr_pct - HIGH_VOLATILITY_ATR_PCT), 0.9)

    # 2. Breakout: Baender expandiert UND Preis am/ueber dem Rand -
    # unterscheidet sich von TRENDING dadurch, dass der Ausbruch selbst
    # (nicht ein bereits etablierter Trend) das Signal ist.
    if (
        bb_width is not None
        and bb_position is not None
        and bb_width > _BREAKOUT_BB_WIDTH
        and (bb_position <= _BREAKOUT_BB_POSITION_LOW or bb_position >= _BREAKOUT_BB_POSITION_HIGH)
    ):
        return MarketRegime.BREAKOUT, 0.6

    # 3. Trend (ADX + Richtungsindikatoren)
    if adx > _ADX_TREND_THRESHOLD and di_plus is not None and di_minus is not None:
        confidence = min(0.5 + (adx - _ADX_TREND_THRESHOLD) / 50.0, 0.9)
        if di_plus > di_minus:
            return MarketRegime.TRENDING_UP, confidence
        if di_minus > di_plus:
            return MarketRegime.TRENDING_DOWN, confidence

    # 4. Kein Trend (ADX niedrig): Range vs. Low-Volatility
    if adx < _ADX_RANGE_THRESHOLD:
        if atr_pct < _LOW_VOLATILITY_ATR_PCT:
            return MarketRegime.LOW_VOLATILITY, 0.55
        return MarketRegime.RANGING, 0.6

    # 5. Uebergangszone (ADX zwischen den Schwellen, keine der obigen
    # Bedingungen eindeutig) - ehrlich als nicht sicher klassifizierbar
    # melden statt zu raten.
    return MarketRegime.UNKNOWN, 0.3
