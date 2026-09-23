"""
SGR Backtesting Simulator
=========================
Event-driven Markt-Simulation für Backtesting.

Kernkonzept:
    Für jeden Bar (von ältestem zu neuestem):
        1. Features berechnen (nur mit History bis zu diesem Bar)
        2. MarketContext aufbauen
        3. Aktive Strategien auswerten → Signal
        4. Risk Check → approved_qty
        5. Order simulieren (Slippage, Fees)
        6. Portfolio State aktualisieren
        7. Equity-Kurve Punkt aufzeichnen

Look-Ahead-Prävention:
    - `history` enthält immer nur Bars bis zum aktuellen Bar
    - Entry-Preis = Open des NÄCHSTEN Bars (realistisch)
      (nicht Close des Signal-Bars – das wäre Look-Ahead!)
    - Exit-Preis beim gleichen Prinzip

Realismus:
    - Slippage: entry_price * (1 + slippage_pct) für BUY
    - Fees: Taker-Fee auf Notional
    - Keine partiellen Fills (vereinfachend – genug für MVP)
    - Funding Rates für Futures (falls vorhanden)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

import numpy as np

from sgr.backtesting.types import (
    BacktestConfig,
    BacktestTrade,
    EquityCurvePoint,
)
from sgr.core.logging import get_logger
from sgr.core.types import (
    Candle,
    MarketRegime,
    Signal,
    SignalDirection,
)
from sgr.market_data import feature_engineering as fe
from sgr.market_data.feature_engineering import (
    FeatureEngineer,
    calc_adx,
    calc_atr,
    calc_bollinger_bands,
    calc_keltner_channels,
    calc_macd,
    calc_obv,
    calc_rsi,
    calc_vwap,
    candles_to_arrays,
)
from sgr.market_data.types import FeatureSet, IndicatorValues, MarketContext
from sgr.strategy.base import TradingStrategy
from sgr.strategy.registry import StrategyRegistry

log = get_logger(__name__)


class SimulatedPosition:
    """Offene Position während Backtest-Simulation."""

    def __init__(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        entry_price: Decimal,
        entry_time: datetime,
        strategy: str,
        signal_confidence: float,
        regime: MarketRegime,
        target_price: Decimal | None = None,
    ) -> None:
        self.id = str(uuid.uuid4())
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.strategy = strategy
        self.signal_confidence = signal_confidence
        self.regime = regime
        self.entry_bar_index = 0

        # Strategie-spezifisches Exit-Ziel aus signal.metadata["target_price"]
        # (siehe z.B. MeanReversionStrategy: BB Middle als Mean-Reversion-
        # Ziel). Optional - Strategien ohne eigenes Ziel (z.B.
        # TrendFollowingStrategy) liefern kein target_price, die Position
        # verlaesst sich dann ausschliesslich auf den generischen ATR-
        # Stop/Zeit-Exit in _check_exits(), unveraendertes Verhalten.
        #
        # Hintergrund (Schritt 10, Teil A -> B): ein Server-Backtest zeigte
        # fuer mean_reversion_v1 eine exit_reason-Verteilung von
        # time_exit=77, atr_stop=41, backtest_end=1 (119 Trades gesamt) -
        # KEIN einziger Trade wurde je durch das eigentliche Mean-
        # Reversion-Ziel geschlossen, weil dieser Exit-Pfad bis hierhin
        # nicht existierte. target_price lag zwar im Signal, wurde aber
        # nie bis zur SimulatedPosition durchgereicht.
        self.target_price = target_price

        # MAE/MFE tracking
        self.max_adverse_excursion = Decimal("0")
        self.max_favorable_excursion = Decimal("0")

    def update_excursions(self, current_price: Decimal) -> None:
        """Aktualisiert MAE/MFE für jeden Bar."""
        if self.side == "long":
            pnl = current_price - self.entry_price
        else:
            pnl = self.entry_price - current_price

        if pnl < -self.max_adverse_excursion:
            self.max_adverse_excursion = abs(pnl)
        if pnl > self.max_favorable_excursion:
            self.max_favorable_excursion = pnl

    @property
    def unrealized_pnl(self) -> Decimal:
        return Decimal("0")  # Wird extern berechnet


class BacktestSimulator:
    """
    Event-driven Backtesting Simulator.

    Usage:
        sim = BacktestSimulator(config)
        trades, equity = await sim.run(candles_by_symbol, registry)
    """

    # Bars für Indikator-Warmup, bevor die Haupt-Loop in run() ueberhaupt
    # zu iterieren beginnt (siehe dort: `for bar_idx in range(warmup, ...)`).
    # Als Klassenkonstante exponiert (statt nur als lokale Variable in
    # run()), damit Aufrufer, die eigene Kerzen-Slices bemessen muessen -
    # insbesondere WalkForwardAnalyzer beim Zuschnitt von IS/OOS-Fenstern -
    # nicht denselben Wert redundant und potenziell abweichend hartcodieren.
    # Genau diese Redundanz war die Ursache des Schritt-18-Befunds: die
    # OOS-Fenstergroesse dort kannte diese Zahl nicht und fiel regelmaessig
    # kleiner aus, wodurch die Haupt-Loop nie iterierte (0 Trades in jedem
    # Split) - siehe validation.py.
    WARMUP_BARS: int = 200

    def __init__(self, config: BacktestConfig) -> None:
        self._config = config
        self._engineer = FeatureEngineer()
        self._cash = config.initial_capital
        self._peak_value = config.initial_capital
        self._positions: dict[str, SimulatedPosition] = {}
        self._closed_trades: list[BacktestTrade] = []
        self._equity_curve: list[EquityCurvePoint] = []

    def _precompute_all_features(self, candles: list[Candle]) -> list[FeatureSet | None]:
        """
        Berechnet alle Indikatoren EINMAL über die komplette Candle-Serie
        und liefert pro Bar-Index das fertige FeatureSet (oder None, wenn
        an diesem Index nicht genug History für FeatureEngineer.MIN_CANDLES
        vorliegt) - Performance-Fix für die Simulation-Hauptschleife.

        Hintergrund: run() rief vorher FeatureEngineer.compute() bei JEDEM
        Bar mit candles[:bar_idx+1] auf - einer wachsenden Liste. compute()
        selbst ruft calc_rsi/calc_atr/calc_adx/_ema/_sma/etc. jeweils über
        die GESAMTE übergebene Serie auf und nimmt am Ende nur den letzten
        Wert (siehe FeatureEngineer._compute_indicators). Bei 4320 Bars
        (180 Tage, 1h) bedeutete das: der RSI/ATR/ADX/etc. wurde bei Bar
        4320 über alle 4320 vorherigen Punkte neu berechnet, obwohl nur
        der letzte Wert gebraucht wurde - macht die Gesamtschleife O(n^2)
        (beobachtet auf dem Produktivserver: Backtest mit 4320 Bars ließ
        den Worker >10 Minuten auf 100% CPU haengen, "unhealthy").

        Alle hier verwendeten calc_*/_ema/_sma-Funktionen sind kausale
        Rekursionen (result[i] haengt nur von result[i-1] und values[i]
        ab, nie von zukünftigen Werten) - eine einmalige Berechnung über
        die volle Serie liefert an Index i denselben Wert wie eine
        Berechnung nur über candles[:i+1]. Kein Look-Ahead-Risiko, siehe
        Modul-Docstring "Look-Ahead-Prävention". FeatureEngineer.compute()
        selbst bleibt unverändert und stateless für alle anderen Aufrufer
        (Live-Trading, Recovery, API) - diese Methode ist ein simulator-
        interner Fast-Path, keine Ersatzimplementierung der fachlichen
        Indikator-Logik (nutzt exakt dieselben calc_*-Bausteine).
        """
        n = len(candles)
        if n == 0:
            return []

        arrays = candles_to_arrays(candles)
        c, h, lo, v = arrays.close, arrays.high, arrays.low, arrays.volume

        rsi_14_arr = calc_rsi(c, 14)
        rsi_7_arr = calc_rsi(c, 7)
        macd_line_arr, macd_signal_arr, macd_hist_arr = calc_macd(c)
        atr_arr = calc_atr(h, lo, c, 14)
        adx_arr, dip_arr, dim_arr = calc_adx(h, lo, c, 14)
        bb_upper_arr, bb_mid_arr, bb_lower_arr = calc_bollinger_bands(c, 20, 2.0)
        kc_u_arr, kc_l_arr = calc_keltner_channels(h, lo, c)
        ema_9_arr = fe._ema(c, 9)
        ema_21_arr = fe._ema(c, 21)
        ema_50_arr = fe._ema(c, 50) if n >= 50 else np.full(n, np.nan)
        ema_200_arr = fe._ema(c, 200) if n >= 200 else np.full(n, np.nan)
        sma_20_arr = fe._sma(c, 20)
        vwap_arr = calc_vwap(h, lo, c, v)
        vol_sma_arr = fe._sma(v, 20)
        obv_arr = calc_obv(c, v)
        obv_sma_arr = fe._sma(obv_arr, 20)

        def _at(arr: np.ndarray, i: int) -> float | None:
            val = arr[i]
            return None if np.isnan(val) else float(val)

        def _at_dec(arr: np.ndarray, i: int) -> Decimal | None:
            val = arr[i]
            return None if np.isnan(val) else Decimal(str(round(val, 8)))

        results: list[FeatureSet | None] = [None] * n
        for i in range(n):
            if i + 1 < self._engineer.MIN_CANDLES:
                continue

            atr_val = _at_dec(atr_arr, i)
            atr_pct = float(atr_val / Decimal(str(c[i]))) if atr_val and c[i] > 0 else None

            bb_u, bb_m, bb_l = _at(bb_upper_arr, i), _at(bb_mid_arr, i), _at(bb_lower_arr, i)
            bb_width: float | None = None
            bb_position: float | None = None
            if bb_u and bb_m and bb_l and bb_m > 0:
                bb_width = (bb_u - bb_l) / bb_m
                if bb_u != bb_l:
                    bb_position = (c[i] - bb_l) / (bb_u - bb_l)

            vol_ratio: float | None = None
            if not np.isnan(vol_sma_arr[i]) and vol_sma_arr[i] > 0:
                vol_ratio = float(v[i] / vol_sma_arr[i])

            obv_val: float | None = None
            if not np.isnan(obv_sma_arr[i]) and obv_sma_arr[i] != 0:
                obv_val = float((obv_arr[i] - obv_sma_arr[i]) / abs(obv_sma_arr[i]))

            indicators = IndicatorValues(
                rsi_14=_at(rsi_14_arr, i),
                rsi_7=_at(rsi_7_arr, i),
                macd_line=_at(macd_line_arr, i),
                macd_signal=_at(macd_signal_arr, i),
                macd_histogram=_at(macd_hist_arr, i),
                adx_14=_at(adx_arr, i),
                di_plus=_at(dip_arr, i),
                di_minus=_at(dim_arr, i),
                atr_14=atr_val,
                atr_pct=atr_pct,
                bb_upper=_at_dec(bb_upper_arr, i),
                bb_middle=_at_dec(bb_mid_arr, i),
                bb_lower=_at_dec(bb_lower_arr, i),
                bb_width=bb_width,
                bb_position=bb_position,
                kc_upper=_at_dec(kc_u_arr, i),
                kc_lower=_at_dec(kc_l_arr, i),
                ema_9=_at_dec(ema_9_arr, i),
                ema_21=_at_dec(ema_21_arr, i),
                ema_50=_at_dec(ema_50_arr, i),
                ema_200=_at_dec(ema_200_arr, i),
                sma_20=_at_dec(sma_20_arr, i),
                vwap=_at_dec(vwap_arr, i),
                volume_sma_20=_at_dec(vol_sma_arr, i),
                volume_ratio=vol_ratio,
                obv=obv_val,
            )

            returns_1 = float((c[i] - c[i - 1]) / c[i - 1]) if i >= 1 else None
            returns_5 = float((c[i] - c[i - 5]) / c[i - 5]) if i >= 5 else None
            returns_10 = float((c[i] - c[i - 10]) / c[i - 10]) if i >= 10 else None
            returns_20 = float((c[i] - c[i - 20]) / c[i - 20]) if i >= 20 else None

            candle = candles[i]
            results[i] = FeatureSet(
                symbol=candle.symbol,
                timestamp=candle.timestamp,
                timeframe=candle.timeframe,
                close=candle.close,
                volume=candle.volume,
                indicators=indicators,
                orderbook=None,
                returns_1=returns_1,
                returns_5=returns_5,
                returns_10=returns_10,
                returns_20=returns_20,
                regime=MarketRegime.UNKNOWN,
            )

        return results

    async def run(
        self,
        candles_by_symbol: dict[str, list[Candle]],
        registry: StrategyRegistry,
    ) -> tuple[list[BacktestTrade], list[EquityCurvePoint]]:
        """
        Hauptmethode: führt vollständigen Backtest durch.

        Args:
            candles_by_symbol: {"BTC/USDT": [Candle, ...], ...}
            registry: Strategy Registry mit aktivierten Strategien

        Returns:
            (closed_trades, equity_curve)
        """
        # Reset State
        self._cash = self._config.initial_capital
        self._peak_value = self._config.initial_capital
        self._positions.clear()
        self._closed_trades.clear()
        self._equity_curve.clear()

        # Primäres Symbol (für einfachen Single-Symbol Backtest)
        # Multi-Symbol: timestamps alignment nötig (für MVP: erstes Symbol)
        primary_symbol = self._config.symbols[0]
        candles = candles_by_symbol.get(primary_symbol, [])

        if len(candles) < 200:
            log.warning(
                "backtesting.insufficient_data",
                symbol=primary_symbol,
                count=len(candles),
            )

        warmup = self.WARMUP_BARS  # Bars für Indikator-Warmup (Klassenkonstante)
        bar_count = 0
        active_strategies = registry.get_active()

        log.info(
            "backtesting.simulation.started",
            symbol=primary_symbol,
            total_bars=len(candles),
            warmup_bars=warmup,
            strategies=[s.name for s in active_strategies],
        )

        # Performance: alle Indikatoren EINMAL über die komplette Serie
        # vorausberechnen statt bei jedem Bar mit einer wachsenden History
        # neu (siehe _precompute_all_features() Docstring für die
        # vollständige Begründung - vorher O(n^2), jetzt O(n)).
        precomputed_features = self._precompute_all_features(candles)

        for bar_idx in range(warmup, len(candles)):
            current_bar = candles[bar_idx]
            bar_count += 1

            # 1. Features (vorausberechnet, siehe oben - kein compute()-Call
            # mehr im Hot-Loop). None nur möglich wenn bar_idx+1 < MIN_CANDLES,
            # was wegen warmup=200 > MIN_CANDLES=50 hier nie eintritt, defensiv
            # trotzdem behandelt.
            features = precomputed_features[bar_idx]
            if features is None:
                continue

            # Regime (vereinfacht: aus ADX/RSI ableiten ohne ML)
            regime = self._detect_regime_simple(features)
            features_with_regime = features.model_copy(update={"regime": regime})

            context = MarketContext(
                symbol=current_bar.symbol,
                timestamp=current_bar.timestamp,
                primary=features_with_regime,
                regime=regime,
            )

            # 2. Bestehende Positionen aktualisieren (MAE/MFE, Drawdown)
            self._update_positions(float(current_bar.close))

            # 3. Exit-Logic: einfacher ATR-basierter Stop
            # _check_exits braucht nur die letzten 15 Bars (ATR-Fenster),
            # nicht die komplette History - vermeidet den O(bar_idx)
            # Listen-Slice candles[:bar_idx+1], der bei jedem Bar erneut
            # die komplett wachsende Liste kopiert hätte (ebenfalls O(n^2)
            # über die gesamte Schleife, unabhängig von der
            # Feature-Berechnung selbst).
            recent_window = candles[max(0, bar_idx - 14) : bar_idx + 1]
            await self._check_exits(bar_idx, current_bar, recent_window, current_regime=regime)

            # 4. Entry-Logic: Signal generieren
            if not self._positions:  # Nur neue Position wenn keine offen
                signal = self._generate_signal(context, active_strategies, regime)

                if signal and bar_idx + 1 < len(candles):
                    # Entry auf Open des NÄCHSTEN Bars (realistisch, kein Look-Ahead)
                    next_bar = candles[bar_idx + 1]
                    await self._open_position(signal, next_bar, bar_idx)

            # 5. Equity-Kurve Punkt
            portfolio_val = self._compute_portfolio_value(float(current_bar.close))
            self._record_equity(current_bar.timestamp, portfolio_val, float(current_bar.close))

            # Progress-Log alle 500 Bars
            if bar_count % 500 == 0:
                log.debug(
                    "backtesting.progress",
                    bar=bar_count,
                    total=len(candles) - warmup,
                    trades=len(self._closed_trades),
                    portfolio_value=f"{portfolio_val:.2f}",
                )

        # Alle offenen Positionen am Ende schließen
        if candles:
            last_bar = candles[-1]
            for _sym, pos in list(self._positions.items()):
                self._close_position(
                    pos=pos,
                    exit_price=last_bar.close,
                    exit_time=last_bar.timestamp,
                    bar_index=len(candles) - 1,
                    reason="backtest_end",
                )

        log.info(
            "backtesting.simulation.completed",
            total_bars=bar_count,
            total_trades=len(self._closed_trades),
            final_capital=str(self._cash),
        )

        return self._closed_trades, self._equity_curve

    # ------------------------------------------------------------------
    # Signal Generation
    # ------------------------------------------------------------------

    def _generate_signal(
        self,
        context: MarketContext,
        strategies: list[TradingStrategy],
        regime: MarketRegime,
    ) -> Signal | None:
        """Alle aktiven Strategien befragen, bestes Signal wählen."""
        signals: list[Signal] = []
        for strategy in strategies:
            if regime not in strategy.supported_regimes:
                continue
            try:
                sig = strategy.generate_signal(context)
                if sig and sig.confidence >= 0.55:
                    signals.append(sig)
            except Exception as e:
                log.debug("backtesting.strategy_error", error=str(e))

        if not signals:
            return None

        # Konflikt-Check
        has_long = any(s.direction == SignalDirection.LONG for s in signals)
        has_short = any(s.direction == SignalDirection.SHORT for s in signals)
        if has_long and has_short:
            return None

        best_signal: Signal = max(signals, key=lambda s: s.confidence)
        return best_signal

    # ------------------------------------------------------------------
    # Position Management
    # ------------------------------------------------------------------

    def _extract_target_price(self, signal: Signal) -> Decimal | None:
        """
        Liest signal.metadata["target_price"] fail-safe aus. Strategien wie
        MeanReversionStrategy setzen dies als float (siehe
        sgr/strategy/mean_reversion.py: "target_price": round(target_price, 2)
        wenn ind.bb_middle vorhanden). Ein fehlendes, None- oder nicht
        numerisch konvertierbares target_price darf den Backtest nie zum
        Absturz bringen - liefert dann einfach None, die Position verhaelt
        sich wie zuvor (nur generischer Stop/Zeit-Exit).
        """
        raw = signal.metadata.get("target_price")
        if raw is None:
            return None
        try:
            return Decimal(str(raw))
        except (ValueError, ArithmeticError):
            log.warning(
                "backtesting.invalid_target_price",
                strategy=signal.strategy_name,
                raw_value=raw,
            )
            return None

    async def _open_position(
        self,
        signal: Signal,
        entry_bar: Candle,
        bar_index: int,
    ) -> None:
        """Öffnet Position zum Open des nächsten Bars + Slippage."""
        symbol_str = signal.symbol.ccxt_symbol

        # Slippage
        if signal.direction == SignalDirection.LONG:
            entry_price = entry_bar.open * (1 + self._config.slippage_pct)
            side = "long"
        else:
            entry_price = entry_bar.open * (1 - self._config.slippage_pct)
            side = "short"

        # Position Sizing: max_position_pct des Portfolios
        portfolio_val = self._compute_portfolio_value(float(entry_bar.open))
        max_notional = Decimal(str(portfolio_val)) * Decimal(str(self._config.max_position_pct))
        max_notional = min(max_notional, self._cash * Decimal("0.95"))  # Max 95% Cash

        if max_notional <= 0:
            return

        # Konfidenz-Gewichtung
        notional = max_notional * Decimal(str(signal.size_hint)) * Decimal(str(signal.confidence))
        quantity = notional / entry_price

        if quantity <= Decimal("0.00001"):
            return

        # Fees
        fee = notional * self._config.taker_fee
        total_cost = notional + fee

        if total_cost > self._cash:
            return

        # Cash-Buchung: fuer LONG wird das Notional ausgegeben (Kauf) -
        # fuer SHORT wird das Notional (abzueglich Fee) als Verkaufserloes
        # gutgeschrieben (verkaufen zuerst, zurueckkaufen beim Close).
        # Vorher wurde hier fuer beide Seiten identisch abgebucht
        # (self._cash -= total_cost), was fuer Short-Positionen
        # wirtschaftlich falsch war und die Cash-/Equity-Kurve korrumpierte
        # - verifiziert per Cash-Delta-vs-net_pnl-Instrumentierung: 84/84
        # Long-Trades stimmten exakt, nur 5/77 Short-Trades (siehe
        # docs/ANALYSIS-mean-reversion-v1-schritt16-fundamental-suitability.md).
        # total_cost bleibt unveraendert die Affordability-Guard-Groesse
        # fuer beide Seiten (Risk-Sizing-Verhalten unveraendert) - nur die
        # tatsaechliche Cash-Bewegung wird jetzt seitenabhaengig korrekt
        # gebucht, symmetrisch zu _close_position() unten.
        if side == "long":
            self._cash -= total_cost
        else:
            self._cash += notional - fee

        pos = SimulatedPosition(
            symbol=symbol_str,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            entry_time=entry_bar.timestamp,
            strategy=signal.strategy_name,
            signal_confidence=signal.confidence,
            regime=signal.regime,
            target_price=self._extract_target_price(signal),
        )
        pos.entry_bar_index = bar_index
        self._positions[symbol_str] = pos

        log.debug(
            "backtesting.position_opened",
            symbol=symbol_str,
            side=side,
            price=str(entry_price),
            qty=str(quantity),
        )

    async def _check_exits(
        self,
        bar_idx: int,
        current_bar: Candle,
        history: list[Candle],
        current_regime: MarketRegime | None = None,
    ) -> None:
        """
        Prüft Exit-Bedingungen für alle offenen Positionen.
        Exit-Typen, in Prioritätsreihenfolge (erster Treffer gewinnt):
            1. Regime-Change: Position wurde im RANGING-Regime eröffnet
               (pos.regime == RANGING), aktuelles Regime ist nicht mehr
               RANGING -> die Mean-Reversion-These der Position ist nicht
               mehr gültig, unabhängig vom aktuellen PnL. Nur relevant für
               Positionen mit RANGING-Entry (betrifft aktuell nur
               mean_reversion_v1; trend_following_v1 eröffnet Positionen
               ausschließlich in TRENDING_UP/DOWN, für die dieser Check nie
               greift - siehe SimulatedPosition.regime, gesetzt bei
               Eröffnung, unveränderlich über die Positionslebensdauer).
               Vor dem ATR-Stop geprüft: ein ungültig gewordenes Setup soll
               nicht erst warten, bis der Preis so weit gelaufen ist, dass
               der Stop greift (siehe Schritt 10 Diagnose: 70.6% der
               ADX-Grauzone-Stop-Trades zeigten einen deutlichen
               ADX-Anstieg in den ersten 10 Bars nach Entry).
            2. ATR-Stop: config.atr_stop_multiplier x ATR unter Entry (Long)
               / über Entry (Short) - Risikoschutz geht vor Gewinnmitnahme
               oder Zeitablauf. Multiplikator konfigurierbar (Default 2.5,
               siehe BacktestConfig.atr_stop_multiplier), vormals hart
               codiert.
            3. Target erreicht: strategie-eigenes target_price aus
               signal.metadata (siehe SimulatedPosition.target_price
               Docstring) - nur falls die Strategie eines geliefert hat.
            4. Zeit-Exit: Max config.max_holding_bars Bars gehalten
               (passiver Fallback, wenn weder Stop noch Ziel erreicht
               wurden). Konfigurierbar (Default 20, siehe
               BacktestConfig.max_holding_bars), vormals hart codiert.
        """
        for symbol_str, pos in list(self._positions.items()):
            if symbol_str != current_bar.symbol.ccxt_symbol:
                continue

            close = current_bar.close
            bars_held = bar_idx - pos.entry_bar_index
            exit_triggered = False
            exit_reason = ""

            # 1. Regime-Change: nur fuer Positionen, die im RANGING-Regime
            # eroeffnet wurden (siehe Docstring oben fuer die Begruendung,
            # warum das automatisch strategiespezifisch bleibt, ohne den
            # Strategienamen hart zu verdrahten).
            if (
                current_regime is not None
                and pos.regime == MarketRegime.RANGING
                and current_regime != MarketRegime.RANGING
            ):
                exit_triggered = True
                exit_reason = "regime_change"

            # 2. ATR-basierter Stop (aus letzten 14 Bars)
            if not exit_triggered and len(history) >= 15:
                import numpy as np

                from sgr.market_data.feature_engineering import calc_atr, candles_to_arrays

                arrays = candles_to_arrays(history[-15:])
                atr_arr = calc_atr(arrays.high, arrays.low, arrays.close, 14)
                atr = Decimal(str(atr_arr[-1])) if not np.isnan(atr_arr[-1]) else None

                if atr:
                    stop_distance = atr * self._config.atr_stop_multiplier
                    if pos.side == "long" and close < pos.entry_price - stop_distance:
                        exit_triggered = True
                        exit_reason = "atr_stop"
                    elif pos.side == "short" and close > pos.entry_price + stop_distance:
                        exit_triggered = True
                        exit_reason = "atr_stop"

            # 3. Target erreicht (nur falls Regime-Change/Stop nicht schon
            # getriggert haben - Risikoschutz/Regime-Guelitgkeit haben
            # Vorrang, siehe Docstring oben)
            if not exit_triggered and pos.target_price is not None:
                if pos.side == "long" and close >= pos.target_price:
                    exit_triggered = True
                    exit_reason = "target_reached"
                elif pos.side == "short" and close <= pos.target_price:
                    exit_triggered = True
                    exit_reason = "target_reached"

            # 4. Zeit-Exit: max self._config.max_holding_bars Bars (nur
            # falls nichts von oben bereits getriggert hat)
            if not exit_triggered and bars_held >= self._config.max_holding_bars:
                exit_triggered = True
                exit_reason = "time_exit"

            if exit_triggered:
                # Exit auf Open des nächsten Bars wenn nicht letzter
                exit_price = close  # Vereinfachung: Exit auf Close
                self._close_position(
                    pos=pos,
                    exit_price=exit_price,
                    exit_time=current_bar.timestamp,
                    bar_index=bar_idx,
                    reason=exit_reason,
                )

    def _close_position(
        self,
        pos: SimulatedPosition,
        exit_price: Decimal,
        exit_time: datetime,
        bar_index: int,
        reason: str,
    ) -> None:
        """Schließt Position und berechnet PnL."""
        symbol_str = pos.symbol

        if pos.side == "long":
            exit_price_with_slippage = exit_price * (1 - self._config.slippage_pct)
            gross_pnl = (exit_price_with_slippage - pos.entry_price) * pos.quantity
        else:
            exit_price_with_slippage = exit_price * (1 + self._config.slippage_pct)
            gross_pnl = (pos.entry_price - exit_price_with_slippage) * pos.quantity

        exit_notional = pos.quantity * exit_price_with_slippage
        entry_notional = pos.quantity * pos.entry_price

        entry_fee = entry_notional * self._config.taker_fee
        exit_fee = exit_notional * self._config.taker_fee
        total_fees = entry_fee + exit_fee

        slippage_cost = abs(
            (exit_price_with_slippage - exit_price) * pos.quantity
            + (pos.entry_price - pos.entry_price / (1 + self._config.slippage_pct)) * pos.quantity
        )

        net_pnl = gross_pnl - total_fees
        # Symmetrisch zu _open_position(): LONG erhaelt beim Verkauf den
        # Exit-Erloes zurueck (Cash steigt); SHORT muss zum Exit-Preis
        # zurueckkaufen, um die beim Open erhaltenen Verkaufserloese
        # abzuloesen (Cash sinkt). Mit dem Fix in _open_position() ergibt
        # open_delta + close_delta fuer beide Seiten exakt net_pnl (siehe
        # Schritt-17-Regressionstests).
        if pos.side == "long":
            self._cash += exit_notional - exit_fee
        else:
            self._cash -= exit_notional + exit_fee

        trade = BacktestTrade(
            id=pos.id,
            symbol=symbol_str,
            strategy=pos.strategy,
            side=pos.side,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            entry_price=pos.entry_price,
            exit_price=exit_price_with_slippage,
            quantity=pos.quantity,
            gross_pnl=gross_pnl,
            fees=total_fees,
            slippage=slippage_cost,
            net_pnl=net_pnl,
            holding_bars=bar_index - pos.entry_bar_index,
            regime=pos.regime,
            max_adverse_excursion=pos.max_adverse_excursion,
            max_favorable_excursion=pos.max_favorable_excursion,
            entry_signal_confidence=pos.signal_confidence,
            metadata={"exit_reason": reason},
        )
        self._closed_trades.append(trade)
        del self._positions[symbol_str]

        log.debug(
            "backtesting.position_closed",
            symbol=symbol_str,
            reason=reason,
            net_pnl=str(net_pnl),
            bars_held=trade.holding_bars,
        )

    def _update_positions(self, current_price: float) -> None:
        """MAE/MFE update für alle offenen Positionen."""
        for pos in self._positions.values():
            pos.update_excursions(Decimal(str(current_price)))

    def _compute_portfolio_value(self, current_price: float) -> float:
        """
        Cash + Marktwert offener Positionen.

        LONG: der Marktwert ist ein Aktivum (du haeltst quantity Einheiten) -
        wird addiert. SHORT: der Marktwert ist eine Verbindlichkeit (du
        musst quantity Einheiten zum aktuellen Preis zurueckkaufen, um die
        Position zu schliessen) - wird abgezogen. Symmetrisch zum Cash-Fix
        in _open_position()/_close_position(): mit dem dort bereits beim
        Open gutgeschriebenen Verkaufserloes (Cash steigt) muss der noch
        offene Rueckkaufbedarf hier als Minus gefuehrt werden, sonst wuerde
        eine offene Short-Position faelschlich doppelt als Vermoegen
        gezaehlt (Cash-Gutschrift UND positiver Positionswert gleichzeitig).
        """
        position_value = 0.0
        for pos in self._positions.values():
            if pos.side == "long":
                position_value += float(pos.quantity) * current_price
            else:
                position_value -= float(pos.quantity) * current_price
        return float(self._cash) + position_value

    def _record_equity(
        self,
        timestamp: datetime,
        portfolio_value: float,
        current_price: float,
    ) -> None:
        """Fügt Equity-Kurve Punkt hinzu."""
        if portfolio_value > self._peak_value:
            self._peak_value = Decimal(str(portfolio_value))

        drawdown = 0.0
        if float(self._peak_value) > 0:
            drawdown = (float(self._peak_value) - portfolio_value) / float(self._peak_value) * 100

        position_value = portfolio_value - float(self._cash)

        self._equity_curve.append(
            EquityCurvePoint(
                timestamp=timestamp,
                portfolio_value=Decimal(str(round(portfolio_value, 2))),
                cash=self._cash,
                open_positions_value=Decimal(str(round(position_value, 2))),
                drawdown_pct=round(drawdown, 4),
                daily_return=0.0,  # Wird in Analyse berechnet
            )
        )

    def _detect_regime_simple(self, features: object) -> MarketRegime:
        """
        Vereinfachte Regime-Detection aus Features (ohne ML).
        Wird durch echten ML-Regime-Detector ersetzt wenn verfügbar.
        """
        from sgr.market_data.types import FeatureSet

        assert isinstance(features, FeatureSet)
        ind = features.indicators

        if ind.adx_14 is None or ind.rsi_14 is None:
            return MarketRegime.UNKNOWN

        if ind.adx_14 > 25:
            if ind.rsi_14 > 55 and ind.di_plus and ind.di_minus and ind.di_plus > ind.di_minus:
                return MarketRegime.TRENDING_UP
            elif ind.rsi_14 < 45 and ind.di_plus and ind.di_minus and ind.di_minus > ind.di_plus:
                return MarketRegime.TRENDING_DOWN
        elif ind.adx_14 < 20:
            return MarketRegime.RANGING

        if ind.atr_pct and ind.atr_pct > 0.05:
            return MarketRegime.HIGH_VOLATILITY

        return MarketRegime.RANGING
