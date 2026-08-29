"""
SGR Risk Engine
===============
Zentrales Risikomanagementsystem. Bewertet jeden Signal vor Execution.

Verantwortlichkeiten:
    1. Portfolio-Risiko-Metriken berechnen (VaR, Drawdown, Heat)
    2. Signal gegen alle Limits prüfen (Hard + Soft)
    3. Positionsgröße bestimmen (unter Risk-Constraints)
    4. Kill Switch bei Hard-Limit-Verletzung auslösen
    5. Risk Report generieren (für Monitoring + Audit)

Datenfluss:
    Signal → [Risk Engine] → RiskAssessment (APPROVED | REJECTED | REDUCED)
                                   ↓
                            OrderRequest an Execution Engine

Limit-Hierarchie:
    HARD (Kill Switch):
        max_portfolio_drawdown  > 15% → KILL
        daily_loss_limit        > 5%  → KILL
        exchange_offline        > 30s → KILL

    SOFT (Größen-Reduktion):
        var_95_limit            > 3%   → Reduktion
        portfolio_heat          > 70%  → Reduktion / Block
        max_correlation         > 0.8  → Warnung
        slippage_estimate       > 0.3% → Limit Order erzwingen

    MONITOR (Alert only):
        rolling_sharpe_30d      < 0.5  → Alert
        hit_rate_30d            < 40%  → Alert

Designentscheidung: Fail-Safe statt Fail-Silent
    Bei Fehler in Risk-Berechnung → REJECT, nie APPROVE.
    Es ist besser einen Trade zu verpassen als unkontrolliert zu handeln.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np

from sgr.core.config import get_config
from sgr.core.logging import get_logger
from sgr.core.types import (
    OrderRequest,
    OrderType,
    Position,
    RiskAssessment,
    RiskDecision,
    RiskMetrics,
    Signal,
    TradingMode,
)
from sgr.risk.kill_switch import KillSwitch, get_kill_switch
from sgr.risk.position_sizer import PositionSizer
from sgr.risk.types import (
    LimitCheck,
    LimitStatus,
    LimitType,
    RiskReport,
)
from sgr.risk.var_calculator import VaRCalculator, VaRMethod

log = get_logger(__name__)


class RiskEngine:
    """
    Haupt-Risk-Engine. Bewertet Signale gegen Portfolio-Risiko.

    Stateful: hält Portfolio-State (Positionen, PnL, Peak Value).
    Nicht thread-safe: nur aus einem asyncio Task verwenden.

    Lifecycle:
        engine = RiskEngine(TradingMode.PAPER)
        await engine.initialize()
        assessment = await engine.evaluate(signal, positions, balance)
    """

    def __init__(self, trading_mode: TradingMode) -> None:
        self._trading_mode = trading_mode
        self._config = get_config()
        self._limits = self._config.risk_limits
        self._var_calc = VaRCalculator()
        self._sizer = PositionSizer()
        self._kill_switch: KillSwitch = get_kill_switch(trading_mode)

        # Portfolio State (wird bei jedem evaluate Update)
        self._peak_portfolio_value: Decimal = Decimal("0")
        self._daily_pnl_start: Decimal | None = None
        self._daily_pnl_date: str | None = None
        self._return_history: list[float] = []  # Rolling 30-day returns
        self._initialized = False

    async def initialize(self) -> None:
        """Initialisiert Engine mit aktuellem Portfolio-State."""
        self._initialized = True
        log.info(
            "risk_engine.initialized",
            trading_mode=self._trading_mode.value,
        )

    # ------------------------------------------------------------------
    # Main Entry Point
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        signal: Signal,
        open_positions: list[Position],
        portfolio_value: Decimal,
        available_capital: Decimal,
        current_price: Decimal,
        atr: Decimal | None = None,
        strategy_win_rate: float | None = None,
        strategy_profit_factor: float | None = None,
    ) -> RiskAssessment:
        """
        Bewertet Signal gegen alle Risk-Constraints.
        Fail-Safe: Exception → REJECTED mit Fehler-Reason.

        Returns:
            RiskAssessment mit APPROVED, REJECTED oder REDUCED
        """
        try:
            return await self._evaluate_internal(
                signal=signal,
                open_positions=open_positions,
                portfolio_value=portfolio_value,
                available_capital=available_capital,
                current_price=current_price,
                atr=atr,
                strategy_win_rate=strategy_win_rate,
                strategy_profit_factor=strategy_profit_factor,
            )
        except Exception as e:
            log.error(
                "risk_engine.evaluate.unexpected_error",
                signal_id=str(signal.id),
                error=str(e),
                exc_info=True,
            )
            # Fail-Safe: bei Fehler → REJECT
            metrics = self._empty_metrics(portfolio_value)
            return RiskAssessment(
                signal_id=signal.id,
                decision=RiskDecision.REJECTED,
                approved_quantity=Decimal("0"),
                rejection_reason=f"Risk engine error: {e}",
                risk_metrics_snapshot=metrics,
                warnings=["Risk engine encountered an error – trade rejected for safety"],
            )

    async def _evaluate_internal(
        self,
        signal: Signal,
        open_positions: list[Position],
        portfolio_value: Decimal,
        available_capital: Decimal,
        current_price: Decimal,
        atr: Decimal | None,
        strategy_win_rate: float | None,
        strategy_profit_factor: float | None,
    ) -> RiskAssessment:

        # 1. Kill Switch Check (synchron, sofort)
        if self._kill_switch.is_active:
            return self._reject(
                signal.id,
                "Kill switch is active",
                portfolio_value,
            )

        # 2. Portfolio Metriken berechnen
        metrics = self._compute_metrics(portfolio_value, open_positions)

        # 3. Alle Limit-Checks durchführen
        checks = self._run_all_checks(metrics)
        report = RiskReport(
            timestamp=datetime.now(tz=UTC),
            trading_mode=self._trading_mode,
            checks=checks,
            portfolio_value=portfolio_value,
            daily_pnl=metrics.daily_pnl,
            daily_pnl_pct=metrics.daily_pnl_pct,
            drawdown_from_peak=metrics.drawdown_from_peak,
            var_95=metrics.var_95,
            expected_shortfall=metrics.expected_shortfall,
            portfolio_heat=metrics.portfolio_heat,
            active_positions=metrics.active_positions,
            correlation_exposure=metrics.correlation_exposure,
        )

        warnings: list[str] = []

        # 4. Hard Limits → Kill Switch + REJECT
        if report.has_hard_breach:
            hard_checks = [
                c
                for c in checks
                if c.status == LimitStatus.BREACHED and c.limit_type == LimitType.HARD
            ]
            reason = "; ".join(c.message for c in hard_checks)

            # Kill Switch auslösen (async, aber State sofort gesetzt)
            asyncio.create_task(self._kill_switch.trigger(reason, triggered_by="risk_engine"))

            return self._reject(signal.id, reason, portfolio_value, metrics)

        # 5. Soft Limits → Positionsgröße reduzieren oder REJECT
        reduction_factor = report.min_reduction_factor

        # 6. Positionsgröße berechnen
        qty, reduction_reason = self._sizer.compute(
            signal=signal,
            portfolio_value=portfolio_value,
            available_capital=available_capital,
            current_price=current_price,
            atr=atr,
            portfolio_heat=metrics.portfolio_heat,
            max_position_pct=self._limits.max_single_position_pct,
            max_portfolio_heat=self._limits.portfolio_heat_limit,
            win_rate=strategy_win_rate,
            profit_factor=strategy_profit_factor,
        )

        # 7. Reduction Factor anwenden (Soft Limits)
        if reduction_factor < 1.0:
            qty = qty * Decimal(str(reduction_factor))
            warnings.append(f"Position reduced to {reduction_factor:.0%} due to soft limit breach")

        # 8. Null-Qty → REJECT
        if qty <= 0:
            reason = reduction_reason or "Position size computed as 0"
            return self._reject(signal.id, reason, portfolio_value, metrics)

        # 9. Soft Limit Warnings sammeln
        for check in checks:
            if check.status == LimitStatus.BREACHED and check.limit_type == LimitType.SOFT:
                warnings.append(f"Soft limit: {check.message}")
            elif check.status == LimitStatus.WARNING:
                warnings.append(f"Warning: {check.message}")

        if reduction_reason:
            warnings.append(f"Size reduced: {reduction_reason}")

        decision = (
            RiskDecision.REDUCED
            if (reduction_factor < 1.0 or reduction_reason)
            else RiskDecision.APPROVED
        )

        log.info(
            "risk_engine.assessment",
            signal_id=str(signal.id),
            symbol=str(signal.symbol),
            decision=decision.value,
            qty=str(qty),
            portfolio_heat=f"{metrics.portfolio_heat:.1%}",
            drawdown=f"{metrics.drawdown_from_peak:.1%}",
            warnings=len(warnings),
        )

        return RiskAssessment(
            signal_id=signal.id,
            decision=decision,
            approved_quantity=qty,
            risk_metrics_snapshot=metrics,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Metric Computation
    # ------------------------------------------------------------------

    def _compute_metrics(
        self,
        portfolio_value: Decimal,
        positions: list[Position],
    ) -> RiskMetrics:
        """Berechnet alle Portfolio-Risiko-Metriken."""
        now = datetime.now(tz=UTC)

        # Peak Value tracken (für Drawdown)
        if portfolio_value > self._peak_portfolio_value:
            self._peak_portfolio_value = portfolio_value

        drawdown = 0.0
        if self._peak_portfolio_value > 0:
            drawdown = float(
                (self._peak_portfolio_value - portfolio_value) / self._peak_portfolio_value
            )

        # Daily PnL (Reset um Mitternacht UTC)
        today_str = now.strftime("%Y-%m-%d")
        if self._daily_pnl_date != today_str:
            self._daily_pnl_date = today_str
            self._daily_pnl_start = portfolio_value

        daily_pnl = portfolio_value - (self._daily_pnl_start or portfolio_value)
        daily_pnl_pct = float(daily_pnl / portfolio_value) if portfolio_value > 0 else 0.0

        # VaR (aus Return-History)
        var_95 = 0.0
        es = 0.0
        if len(self._return_history) >= 10:
            returns = np.array(self._return_history[-30:])  # Max 30 Tage
            var_result = self._var_calc.compute(
                returns, confidence=0.95, method=VaRMethod.HISTORICAL
            )
            var_95 = var_result.var
            es = var_result.es

        # Portfolio Heat (Summe der Risk Units)
        total_notional = sum(p.notional_value for p in positions)
        heat = float(total_notional / portfolio_value) if portfolio_value > 0 else 0.0

        # Gross Leverage: Summe |Notional| aller Positionen / Portfolio Value.
        # Notional ist bereits das mit Hebel gehandelte Exposure (quantity *
        # current_price), daher approximiert total_notional / portfolio_value
        # den tatsächlichen Portfolio-weiten Leverage-Faktor unabhängig
        # davon, wie die einzelne Position selbst leveraged wurde.
        gross_leverage = float(total_notional / portfolio_value) if portfolio_value > 0 else 0.0

        # Korrelations-Exposure (vereinfacht: Anzahl Positionen × Durchschnitts-Korrelation)
        # Vollständige Implementierung: Korrelationsmatrix aus Market Data Engine
        corr_exposure = min(len(positions) * 0.1, 1.0)  # Approximation

        return RiskMetrics(
            timestamp=now,
            portfolio_value=portfolio_value,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            drawdown_from_peak=drawdown,
            var_95=var_95,
            expected_shortfall=es,
            portfolio_heat=heat,
            active_positions=len(positions),
            correlation_exposure=corr_exposure,
            gross_leverage=gross_leverage,
        )

    def update_returns(self, daily_return: float) -> None:
        """
        Aktualisiert Return-History für VaR-Berechnung.
        Täglich aufrufen mit dem realisierten Portfolio-Return.
        """
        self._return_history.append(daily_return)
        # Rolling Window: max 252 Handelstage (1 Jahr)
        if len(self._return_history) > 252:
            self._return_history = self._return_history[-252:]

    # ------------------------------------------------------------------
    # Limit Checks
    # ------------------------------------------------------------------

    def _run_all_checks(self, metrics: RiskMetrics) -> list[LimitCheck]:
        """Führt alle konfigurierten Limit-Checks aus."""
        checks: list[LimitCheck] = []

        # --- HARD LIMITS ---

        # Max Drawdown
        checks.append(
            self._check_threshold(
                name="max_drawdown",
                limit_type=LimitType.HARD,
                current=metrics.drawdown_from_peak,
                limit=self._limits.max_portfolio_drawdown,
                message_template="Portfolio drawdown {current:.1%} exceeds hard limit {limit:.1%}",
            )
        )

        # Daily Loss
        checks.append(
            self._check_threshold(
                name="daily_loss",
                limit_type=LimitType.HARD,
                current=-metrics.daily_pnl_pct if metrics.daily_pnl_pct < 0 else 0.0,
                limit=self._limits.daily_loss_limit,
                message_template="Daily loss {current:.1%} exceeds hard limit {limit:.1%}",
            )
        )

        # Max Open Positions
        checks.append(
            self._check_threshold(
                name="max_positions",
                limit_type=LimitType.HARD,
                current=float(metrics.active_positions),
                limit=float(self._limits.max_open_positions),
                message_template="Open positions {current:.0f} exceeds max {limit:.0f}",
            )
        )

        # Max Leverage (Gross Exposure / Portfolio Value)
        checks.append(
            self._check_threshold(
                name="max_leverage",
                limit_type=LimitType.HARD,
                current=metrics.gross_leverage,
                limit=float(self._limits.max_leverage),
                message_template="Gross leverage {current:.2f}x exceeds hard limit {limit:.2f}x",
            )
        )

        # --- SOFT LIMITS ---

        # VaR 95%
        checks.append(
            self._check_threshold(
                name="var_95",
                limit_type=LimitType.SOFT,
                current=metrics.var_95,
                limit=self._limits.var_95_limit,
                message_template="VaR 95% {current:.2%} exceeds soft limit {limit:.2%}",
                warning_threshold=0.8,
                reduction_factor=0.5,
            )
        )

        # Portfolio Heat
        checks.append(
            self._check_threshold(
                name="portfolio_heat",
                limit_type=LimitType.SOFT,
                current=metrics.portfolio_heat,
                limit=self._limits.portfolio_heat_limit,
                message_template="Portfolio heat {current:.1%} exceeds soft limit {limit:.1%}",
                warning_threshold=0.85,
                reduction_factor=0.25,
            )
        )

        # Correlation Exposure
        checks.append(
            self._check_threshold(
                name="correlation_exposure",
                limit_type=LimitType.SOFT,
                current=metrics.correlation_exposure,
                limit=self._limits.max_correlation_exposure,
                message_template="Correlation exposure {current:.2f} exceeds limit {limit:.2f}",
                warning_threshold=0.9,
                reduction_factor=0.5,
            )
        )

        return checks

    def _check_threshold(
        self,
        name: str,
        limit_type: LimitType,
        current: float,
        limit: float,
        message_template: str,
        warning_threshold: float = 0.9,  # Warnung bei 90% des Limits
        reduction_factor: float = 0.5,
    ) -> LimitCheck:
        """Generische Limit-Check Methode."""
        if current >= limit:
            status = LimitStatus.BREACHED
        elif current >= limit * warning_threshold:
            status = LimitStatus.WARNING
        else:
            status = LimitStatus.OK

        message = message_template.format(current=current, limit=limit)

        return LimitCheck(
            name=name,
            limit_type=limit_type,
            status=status,
            current_value=current,
            limit_value=limit,
            message=message,
            reduction_factor=reduction_factor if status == LimitStatus.BREACHED else 1.0,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _reject(
        self,
        signal_id: Any,
        reason: str,
        portfolio_value: Decimal,
        metrics: RiskMetrics | None = None,
    ) -> RiskAssessment:
        if metrics is None:
            metrics = self._empty_metrics(portfolio_value)

        log.warning(
            "risk_engine.rejected",
            signal_id=str(signal_id),
            reason=reason,
        )

        return RiskAssessment(
            signal_id=signal_id,
            decision=RiskDecision.REJECTED,
            approved_quantity=Decimal("0"),
            rejection_reason=reason,
            risk_metrics_snapshot=metrics,
        )

    def _empty_metrics(self, portfolio_value: Decimal) -> RiskMetrics:
        return RiskMetrics(
            timestamp=datetime.now(tz=UTC),
            portfolio_value=portfolio_value,
            daily_pnl=Decimal("0"),
            daily_pnl_pct=0.0,
            drawdown_from_peak=0.0,
            var_95=0.0,
            expected_shortfall=0.0,
            portfolio_heat=0.0,
            active_positions=0,
            correlation_exposure=0.0,
            gross_leverage=0.0,
        )

    def build_order_request(
        self,
        signal: Signal,
        assessment: RiskAssessment,
        current_price: Decimal | None = None,
    ) -> OrderRequest:
        """
        Erstellt OrderRequest aus genehmigtem Assessment.
        Nur aufrufen wenn assessment.decision != REJECTED.

        current_price wird als limit_price verwendet, wenn wegen hoher
        Slippage ein LIMIT statt MARKET Order erzwungen wird. Ohne
        current_price würde eine LIMIT-Order ohne Preis erzeugt, was
        Exchange-Adapter (Paper wie Live) mit Fill-Preis 0 interpretieren
        können – daher ist current_price für den LIMIT-Fall verpflichtend.
        """
        from sgr.core.types import Side

        side = Side.BUY if signal.direction.value == "long" else Side.SELL

        # Slippage-Kontrolle: hohe Slippage → Limit Order erzwingen
        order_type = OrderType.MARKET
        var = assessment.risk_metrics_snapshot.var_95
        if var > self._limits.var_95_limit * 0.8:
            order_type = OrderType.LIMIT

        limit_price: Decimal | None = None
        if order_type == OrderType.LIMIT:
            if current_price is None:
                # Fail-safe: kein Preis bekannt → nicht auf MARKET zurückfallen
                # (das würde die Slippage-Kontrolle unterlaufen), stattdessen
                # explizit signalisieren, dass der Aufrufer ohne current_price
                # keine sichere LIMIT-Order bauen kann.
                raise ValueError(
                    "build_order_request: current_price required to build a "
                    "LIMIT order (high slippage risk detected)"
                )
            limit_price = current_price

        return OrderRequest(
            signal_id=signal.id,
            symbol=signal.symbol,
            side=side,
            order_type=order_type,
            quantity=assessment.approved_quantity,
            limit_price=limit_price,
            trading_mode=self._trading_mode,
        )
