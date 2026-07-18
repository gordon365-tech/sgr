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
from sgr.market_data.feature_engineering import FeatureEngineer
from sgr.market_data.types import MarketContext
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

    def __init__(self, config: BacktestConfig) -> None:
        self._config = config
        self._engineer = FeatureEngineer()
        self._cash = config.initial_capital
        self._peak_value = config.initial_capital
        self._positions: dict[str, SimulatedPosition] = {}
        self._closed_trades: list[BacktestTrade] = []
        self._equity_curve: list[EquityCurvePoint] = []

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

        warmup = 200  # Bars für Indikator-Warmup
        bar_count = 0
        active_strategies = registry.get_active()

        log.info(
            "backtesting.simulation.started",
            symbol=primary_symbol,
            total_bars=len(candles),
            warmup_bars=warmup,
            strategies=[s.name for s in active_strategies],
        )

        for bar_idx in range(warmup, len(candles)):
            current_bar = candles[bar_idx]
            history = candles[: bar_idx + 1]
            bar_count += 1

            # 1. Features berechnen (nur History bis inkl. aktuellen Bar)
            try:
                features = self._engineer.compute(history)
            except Exception:
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
            await self._check_exits(bar_idx, current_bar, history)

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
        strategies: list,
        regime: MarketRegime,
    ) -> Signal | None:
        """Alle aktiven Strategien befragen, bestes Signal wählen."""
        signals = []
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

        return max(signals, key=lambda s: s.confidence)

    # ------------------------------------------------------------------
    # Position Management
    # ------------------------------------------------------------------

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

        self._cash -= total_cost

        pos = SimulatedPosition(
            symbol=symbol_str,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            entry_time=entry_bar.timestamp,
            strategy=signal.strategy_name,
            signal_confidence=signal.confidence,
            regime=signal.regime,
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
    ) -> None:
        """
        Prüft Exit-Bedingungen für alle offenen Positionen.
        Exit-Typen:
            - ATR-Stop: 2.5x ATR unter Entry (Long) / über Entry (Short)
            - Zeit-Exit: Max 20 Bars gehalten
        """
        for symbol_str, pos in list(self._positions.items()):
            if symbol_str != current_bar.symbol.ccxt_symbol:
                continue

            close = current_bar.close
            bars_held = bar_idx - pos.entry_bar_index
            exit_triggered = False
            exit_reason = ""

            # ATR-basierter Stop (aus letzten 14 Bars)
            if len(history) >= 15:
                import numpy as np

                from sgr.market_data.feature_engineering import calc_atr, candles_to_arrays

                arrays = candles_to_arrays(history[-15:])
                atr_arr = calc_atr(arrays.high, arrays.low, arrays.close, 14)
                atr = Decimal(str(atr_arr[-1])) if not np.isnan(atr_arr[-1]) else None

                if atr:
                    stop_distance = atr * Decimal("2.5")
                    if pos.side == "long" and close < pos.entry_price - stop_distance:
                        exit_triggered = True
                        exit_reason = "atr_stop"
                    elif pos.side == "short" and close > pos.entry_price + stop_distance:
                        exit_triggered = True
                        exit_reason = "atr_stop"

            # Zeit-Exit: max 20 Bars
            if bars_held >= 20:
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
        self._cash += exit_notional - exit_fee

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
        """Cash + offene Positions Notional."""
        position_value = sum(
            float(pos.quantity) * current_price for pos in self._positions.values()
        )
        return float(self._cash) + position_value

    def _record_equity(
        self,
        timestamp: datetime,
        portfolio_value: float,
        current_price: float,
    ) -> None:
        """Fügt Equity-Kurve Punkt hinzu."""
        if portfolio_value > self._peak_value:
            self._peak_value = Decimal(str(portfolio_value))  # type: ignore[assignment]

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
