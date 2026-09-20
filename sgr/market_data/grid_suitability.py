"""
SGR Grid Suitability Scoring (Market State Engine Erweiterung)
==================================================================
Beantwortet: "ist ein Futures Grid fuer dieses Symbol JETZT ueberhaupt
eine plausible Strategieklasse?" - AUSDRUECKLICH keine Kauf-/
Verkaufsempfehlung und kein Gewinnversprechen (siehe Aufgabenstellung:
"Dieser Score ist kein direkter Buy/Sell Signal Ersatz").

Das Ergebnis (GridSuitabilityScore) ist ein Vorfilter: es entscheidet nur,
ob GridTradingStrategy.evaluate() (siehe sgr/strategy/futures_grid.py)
ueberhaupt ein Grid mit Kapital vorschlagen darf - die eigentliche
Positionsgroesse/Freigabe bleibt weiterhin Aufgabe von
sgr.risk.grid_risk.GridRiskEngine.

Verwendet ausschliesslich bereits vorhandene, in MarketContext/FeatureSet
berechnete Indikatoren (sgr/market_data/types.py) - keine neuen
Marktdaten-Abrufe, kein zusaetzlicher Netzwerk-Call.

Komponenten (siehe Aufgabenstellung):
    Range Quality      - wie "eingesperrt" bewegt sich der Preis (BB-Width,
                         ADX niedrig = eher Range als Trend)
    Volatility Quality - weder zu ruhig (kein Grid-Capture-Potential) noch
                         zu wild (Liquidationsrisiko)
    Liquidity          - ausreichendes Handelsvolumen fuer Fills ohne
                         extreme Slippage
    Funding Cost       - Funding-Rate darf die erwartete Grid-Marge nicht
                         auffressen
    Trend Risk         - starker gerichteter Trend gegen die Grid-Range
                         (Breakout-Gefahr, insbesondere fuer symmetrische
                         Grids)
    Breakout Risk       - Wahrscheinlichkeit, dass der Preis die Range
                         verlaesst (ADX-Beschleunigung, DI-Divergenz)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sgr.core.types import GridDirection
from sgr.market_data.types import MarketContext

# ---------------------------------------------------------------------------
# Konfigurierbare Schwellenwerte (bewusst als Modul-Konstanten, nicht
# hartkodiert in der Scoring-Logik - Strategy Evolution kann diese
# spaeter pro Genome-Variante ueberschreiben, siehe evaluate() Parameter)
# ---------------------------------------------------------------------------

_DEFAULT_MIN_LIQUIDITY_VOLUME = 100_000  # Quote-Currency, 24h
_DEFAULT_MAX_FUNDING_ANNUALIZED_PCT = 50.0  # |funding_rate_annualized| in %
_ADX_RANGE_THRESHOLD = 20.0  # unterhalb: eher Range
_ADX_TREND_THRESHOLD = 30.0  # oberhalb: klarer Trend, Grid-Risiko steigt
_MIN_VOLATILITY_ATR_PCT = 0.003  # zu ruhig -> kein Grid-Capture-Potential
_MAX_VOLATILITY_ATR_PCT = 0.08  # zu wild -> Liquidationsrisiko/Range-Bruch


@dataclass(frozen=True)
class GridSuitabilityScore:
    """
    Zusammengesetzter Score (0.0-1.0 je Komponente, hoeher = guenstiger
    fuer ein Grid) plus eine binaere is_suitable-Empfehlung. Alle
    Komponenten sind einzeln nachvollziehbar (siehe reasons) - kein
    Black-Box-Gesamtwert ohne Begruendung.
    """

    range_quality: float
    volatility_quality: float
    liquidity_score: float
    funding_cost_score: float
    trend_risk: float  # hoeher = risikoreicher (invertiert ggue. den anderen Scores)
    breakout_risk: float  # hoeher = risikoreicher
    composite: float
    is_suitable: bool
    suggested_direction: GridDirection
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, float | bool | str | list[str]]:
        return {
            "range_quality": self.range_quality,
            "volatility_quality": self.volatility_quality,
            "liquidity_score": self.liquidity_score,
            "funding_cost_score": self.funding_cost_score,
            "trend_risk": self.trend_risk,
            "breakout_risk": self.breakout_risk,
            "composite": self.composite,
            "is_suitable": self.is_suitable,
            "suggested_direction": self.suggested_direction.value,
            "reasons": list(self.reasons),
        }


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def compute_grid_suitability(
    context: MarketContext,
    *,
    min_liquidity_volume: float = _DEFAULT_MIN_LIQUIDITY_VOLUME,
    max_funding_annualized_pct: float = _DEFAULT_MAX_FUNDING_ANNUALIZED_PCT,
    suitability_threshold: float = 0.55,
) -> GridSuitabilityScore:
    """
    Reine, synchrone Funktion (keine I/O, kein State) - siehe
    sgr.strategy.regime_profile.build_regime_profile fuer das gleiche
    Architekturprinzip. Fehlende Indikatoren (None) fuehren zu einem
    konservativen (niedrigen) Teil-Score statt einer Exception - ein
    Symbol ohne ausreichende Feature-Historie ist damit automatisch
    "nicht geeignet", nicht ein Fehlerzustand.
    """
    ind = context.primary.indicators
    reasons: list[str] = []

    # --- Range Quality: niedriger ADX + kompakte Bollinger-Band-Breite ---
    adx = ind.adx_14
    if adx is None:
        # Bewusst NICHT mit bb_component blenden, wenn ADX fehlt: ohne
        # Trendstaerke-Signal kann die Range-Eignung nicht verlaesslich
        # beurteilt werden - ein isoliert schmales Bollinger-Band allein
        # waere kein ausreichender Ersatz (siehe Aufgabenstellung: "darf
        # nicht als feste Gewinnversprechen" - im Zweifel konservativ).
        range_quality = 0.0
        reasons.append("ADX nicht verfuegbar - Range-Qualitaet konservativ auf 0 gesetzt")
    else:
        # ADX 0..15 => 1.0 (klare Range), ADX >= _ADX_TREND_THRESHOLD => 0.0
        range_quality = _clamp(1.0 - (adx - 10.0) / (_ADX_TREND_THRESHOLD - 10.0))
        if adx >= _ADX_TREND_THRESHOLD:
            reasons.append(f"ADX {adx:.1f} zeigt starken Trend, ungeeignet fuer Range-Grid")

        bb_width = ind.bb_width
        if bb_width is not None:
            # Sehr breite Baender (>0.15 relative Breite) sprechen gegen
            # eine stabile Range - Score reduziert sich mit wachsender
            # Breite. Nur relevant, wenn ADX bereits ein Range-Signal
            # liefert (siehe oben) - reine Verfeinerung, kein Ersatz.
            bb_component = _clamp(1.0 - bb_width / 0.15)
            range_quality = _clamp((range_quality + bb_component) / 2)

    # --- Volatility Quality: weder zu ruhig noch zu wild ---
    atr_pct = ind.atr_pct
    if atr_pct is None:
        volatility_quality = 0.0
        reasons.append("ATR% nicht verfuegbar - Volatility-Qualitaet konservativ auf 0 gesetzt")
    elif atr_pct < _MIN_VOLATILITY_ATR_PCT:
        volatility_quality = _clamp(atr_pct / _MIN_VOLATILITY_ATR_PCT)
        reasons.append(f"ATR% {atr_pct:.2%} sehr niedrig - kaum Grid-Capture-Potential")
    elif atr_pct > _MAX_VOLATILITY_ATR_PCT:
        volatility_quality = 0.0
        reasons.append(f"ATR% {atr_pct:.2%} extrem hoch - Liquidationsrisiko")
    else:
        # Sweet Spot linear zwischen min und max, Peak in der Mitte.
        mid = (_MIN_VOLATILITY_ATR_PCT + _MAX_VOLATILITY_ATR_PCT) / 2
        span = (_MAX_VOLATILITY_ATR_PCT - _MIN_VOLATILITY_ATR_PCT) / 2
        volatility_quality = _clamp(1.0 - abs(atr_pct - mid) / span)

    # --- Liquidity: 24h Volumen (Quote-Currency, approximiert ueber
    # close * volume, da FeatureSet keinen separaten Quote-Volume-Wert
    # fuehrt) ---
    liquidity_score = 0.0
    try:
        quote_volume = float(context.primary.close) * float(context.primary.volume)
        if quote_volume >= min_liquidity_volume:
            liquidity_score = 1.0
        else:
            liquidity_score = _clamp(quote_volume / min_liquidity_volume)
            reasons.append(
                f"Geschaetztes Quote-Volumen {quote_volume:,.0f} unter Mindestschwelle "
                f"{min_liquidity_volume:,.0f}"
            )
    except (TypeError, ValueError, ArithmeticError):
        reasons.append("Volumen nicht auswertbar - Liquidity-Score konservativ auf 0 gesetzt")

    # --- Funding Cost: nur relevant fuer Futures-Kontext ---
    funding_cost_score = 1.0  # kein Futures-Kontext -> kein Funding-Risiko
    if context.primary.futures is not None:
        annualized_pct = abs(context.primary.futures.funding_rate_annualized)
        if annualized_pct >= max_funding_annualized_pct:
            funding_cost_score = 0.0
            reasons.append(
                f"Annualisierte Funding Rate {annualized_pct:.1f}% ueber Limit "
                f"{max_funding_annualized_pct:.1f}%"
            )
        else:
            funding_cost_score = _clamp(1.0 - annualized_pct / max_funding_annualized_pct)

    # --- Trend Risk (invertiert: hoeher = riskanter) ---
    trend_risk = _clamp(1.0 - range_quality)

    # --- Breakout Risk: DI-Divergenz + ADX-Beschleunigung als Proxy ---
    breakout_risk = 0.3  # Basis-Unsicherheit ohne weitere Signale
    if ind.di_plus is not None and ind.di_minus is not None:
        di_gap = abs(ind.di_plus - ind.di_minus)
        breakout_risk = _clamp(di_gap / 40.0)
        if breakout_risk > 0.6:
            reasons.append(
                f"DI+/DI- Divergenz {di_gap:.1f} deutet auf beginnenden gerichteten Ausbruch hin"
            )

    # --- Composite: gewichtetes Mittel, konservativ (schwaechstes Glied
    # zaehlt ueberproportional) ---
    positive_components = [range_quality, volatility_quality, liquidity_score, funding_cost_score]
    risk_components = [trend_risk, breakout_risk]
    avg_positive = sum(positive_components) / len(positive_components)
    avg_risk = sum(risk_components) / len(risk_components)
    weakest_positive = min(positive_components)

    composite = _clamp(0.5 * avg_positive + 0.3 * weakest_positive - 0.2 * avg_risk)

    is_suitable = composite >= suitability_threshold

    # --- Vorschlag fuer Richtung (nur relevant fuer Adaptive Futures Grid,
    # siehe sgr/strategy/futures_grid.py::AdaptiveFuturesGridStrategy) ---
    suggested_direction = GridDirection.NEUTRAL
    if is_suitable:
        if adx is not None and adx >= _ADX_RANGE_THRESHOLD and ind.di_plus and ind.di_minus:
            if ind.di_plus > ind.di_minus:
                suggested_direction = GridDirection.LONG
                reasons.append("Leichter Aufwaertsbias (DI+ > DI-) -> Long-Grid-Kandidat")
            else:
                suggested_direction = GridDirection.SHORT
                reasons.append("Leichter Abwaertsbias (DI- > DI+) -> Short-Grid-Kandidat")
        else:
            # Reiner Seitwaertsmarkt ohne klaren Bias -> Long-Grid als
            # konservativer Default (Spot-Aequivalent, keine Short-
            # Finanzierungskosten) - NICHT als Gewinnversprechen, nur als
            # neutralste verfuegbare Ausrichtung.
            suggested_direction = GridDirection.LONG

    if not is_suitable:
        reasons.append(
            f"Composite-Score {composite:.2f} unter Schwelle {suitability_threshold:.2f} "
            "- kein Grid-Kandidat"
        )

    return GridSuitabilityScore(
        range_quality=round(range_quality, 4),
        volatility_quality=round(volatility_quality, 4),
        liquidity_score=round(liquidity_score, 4),
        funding_cost_score=round(funding_cost_score, 4),
        trend_risk=round(trend_risk, 4),
        breakout_risk=round(breakout_risk, 4),
        composite=round(composite, 4),
        is_suitable=is_suitable,
        suggested_direction=suggested_direction,
        reasons=reasons,
    )
