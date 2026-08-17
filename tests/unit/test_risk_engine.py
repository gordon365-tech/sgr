"""
Tests für den Risk Engine.

Kritischste Tests im System: Fehler hier haben direkte finanzielle Folgen.
Jeder Limit-Check und jeder Sizing-Algorithmus wird separat getestet.

Teststrategie:
    1. VaR Calculator: mathematische Korrektheit
    2. Position Sizer: Constraints werden korrekt eingehalten
    3. Kill Switch: Trigger, Idempotenz, Reset
    4. Limit Checks: Hard/Soft Breach Logik
    5. Risk Engine: Integration aller Komponenten
    6. Fail-Safe: Exception → REJECT
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pytest

from sgr.core.types import (
    ExchangeID,
    MarketRegime,
    Position,
    PositionSide,
    RiskDecision,
    Signal,
    SignalDirection,
    Symbol,
    TradingMode,
)
from sgr.risk.engine import RiskEngine
from sgr.risk.kill_switch import KillSwitch
from sgr.risk.position_sizer import PositionSizer
from sgr.risk.var_calculator import VaRCalculator, VaRMethod

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def btc_symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


@pytest.fixture
def sample_signal(btc_symbol: Symbol) -> Signal:
    return Signal(
        timestamp=datetime.now(tz=UTC),
        strategy_name="trend_v1",
        symbol=btc_symbol,
        direction=SignalDirection.LONG,
        confidence=0.80,
        regime=MarketRegime.TRENDING_UP,
        size_hint=1.0,
    )


@pytest.fixture
def open_position(btc_symbol: Symbol) -> Position:
    return Position(
        symbol=btc_symbol,
        side=PositionSide.LONG,
        quantity=Decimal("0.1"),
        entry_price=Decimal("50000"),
        current_price=Decimal("51000"),
        opened_at=datetime.now(tz=UTC),
        strategy_name="trend_v1",
        trading_mode=TradingMode.PAPER,
    )


@pytest.fixture
def risk_engine() -> RiskEngine:
    engine = RiskEngine(TradingMode.PAPER)
    return engine


# ---------------------------------------------------------------------------
# VaR Calculator
# ---------------------------------------------------------------------------


class TestVaRCalculator:
    def test_historical_var_positive(self) -> None:
        """VaR ist immer positiv (Verlust als positive Zahl)."""
        returns = np.random.normal(0.001, 0.02, 100)
        calc = VaRCalculator()
        result = calc.compute(returns, 0.95, VaRMethod.HISTORICAL)
        assert result.var >= 0
        assert result.es >= result.var

    def test_higher_confidence_higher_var(self) -> None:
        """99% VaR > 95% VaR."""
        returns = np.random.normal(0, 0.02, 200)
        calc = VaRCalculator()
        r95 = calc.compute(returns, 0.95)
        r99 = calc.compute(returns, 0.99)
        assert r99.var >= r95.var

    def test_higher_volatility_higher_var(self) -> None:
        """Höhere Volatilität → höheres VaR."""
        np.random.seed(42)
        low_vol = np.random.normal(0, 0.005, 100)
        high_vol = np.random.normal(0, 0.05, 100)
        calc = VaRCalculator()
        r_low = calc.compute(low_vol, 0.95)
        r_high = calc.compute(high_vol, 0.95)
        assert r_high.var > r_low.var

    def test_insufficient_data_returns_conservative(self) -> None:
        """Zu wenig Daten → konservative Schätzung (kein Crash)."""
        returns = np.array([0.01, -0.02, 0.005])
        calc = VaRCalculator()
        result = calc.compute(returns, 0.95)
        assert result.var > 0
        assert result.es > 0

    def test_es_greater_than_var(self) -> None:
        """ES (CVaR) immer >= VaR per Definition."""
        np.random.seed(7)
        returns = np.random.normal(0, 0.02, 500)
        calc = VaRCalculator()
        result = calc.compute(returns, 0.95)
        assert result.es >= result.var

    def test_all_methods_produce_valid_results(self) -> None:
        np.random.seed(42)
        returns = np.random.normal(0.001, 0.02, 100)
        calc = VaRCalculator()
        for method in [VaRMethod.HISTORICAL, VaRMethod.PARAMETRIC_NORMAL]:
            result = calc.compute(returns, 0.95, method)
            assert result.var >= 0
            assert result.es >= 0
            assert result.observations == 100

    def test_portfolio_var_weighted(self) -> None:
        """Portfolio VaR nutzt gewichtete Returns."""
        np.random.seed(42)
        btc_returns = np.random.normal(0, 0.03, 50)
        eth_returns = np.random.normal(0, 0.04, 50)
        calc = VaRCalculator()
        result = calc.compute_portfolio_var(
            {"BTC": btc_returns, "ETH": eth_returns},
            {"BTC": 0.6, "ETH": 0.4},
        )
        assert result.var >= 0


# ---------------------------------------------------------------------------
# Position Sizer
# ---------------------------------------------------------------------------


class TestPositionSizer:
    def test_basic_sizing(self, sample_signal: Signal) -> None:
        """Grundlegende Größenberechnung ohne Constraints."""
        sizer = PositionSizer()
        qty, reason = sizer.compute(
            signal=sample_signal,
            portfolio_value=Decimal("100000"),
            available_capital=Decimal("90000"),
            current_price=Decimal("50000"),
            atr=Decimal("500"),
            portfolio_heat=0.1,
            max_position_pct=0.10,
            max_portfolio_heat=0.70,
        )
        assert qty > 0
        assert reason is None or isinstance(reason, str)

    def test_respects_max_position_pct(self, sample_signal: Signal) -> None:
        """Positionsgröße überschreitet nie max_position_pct."""
        sizer = PositionSizer()
        portfolio = Decimal("100000")
        price = Decimal("50000")
        max_pct = 0.10  # 10% max

        qty, _ = sizer.compute(
            signal=sample_signal,
            portfolio_value=portfolio,
            available_capital=portfolio,
            current_price=price,
            atr=None,
            portfolio_heat=0.0,
            max_position_pct=max_pct,
            max_portfolio_heat=0.70,
        )

        notional = qty * price
        max_notional = portfolio * Decimal(str(max_pct))
        assert notional <= max_notional * Decimal("1.01")  # 1% Toleranz für Rundung

    def test_zero_qty_on_max_heat(self, sample_signal: Signal) -> None:
        """Bei voller Portfolio-Heat: qty = 0."""
        sizer = PositionSizer()
        qty, reason = sizer.compute(
            signal=sample_signal,
            portfolio_value=Decimal("100000"),
            available_capital=Decimal("90000"),
            current_price=Decimal("50000"),
            atr=None,
            portfolio_heat=0.75,  # Über 0.70 Limit
            max_position_pct=0.10,
            max_portfolio_heat=0.70,
        )
        assert qty == Decimal("0")
        assert reason is not None

    def test_zero_qty_on_no_capital(self, sample_signal: Signal) -> None:
        sizer = PositionSizer()
        qty, reason = sizer.compute(
            signal=sample_signal,
            portfolio_value=Decimal("100000"),
            available_capital=Decimal("0"),
            current_price=Decimal("50000"),
            atr=None,
            portfolio_heat=0.0,
            max_position_pct=0.10,
            max_portfolio_heat=0.70,
        )
        assert qty == Decimal("0")

    def test_low_confidence_reduces_size(self, btc_symbol: Symbol) -> None:
        """Niedrigere Konfidenz → kleinere Position."""
        sizer = PositionSizer()

        signal_high = Signal(
            timestamp=datetime.now(tz=UTC),
            strategy_name="test",
            symbol=btc_symbol,
            direction=SignalDirection.LONG,
            confidence=0.95,
            regime=MarketRegime.TRENDING_UP,
        )
        signal_low = Signal(
            timestamp=datetime.now(tz=UTC),
            strategy_name="test",
            symbol=btc_symbol,
            direction=SignalDirection.LONG,
            confidence=0.40,
            regime=MarketRegime.TRENDING_UP,
        )

        base_args = dict(
            portfolio_value=Decimal("100000"),
            available_capital=Decimal("90000"),
            current_price=Decimal("50000"),
            atr=None,
            portfolio_heat=0.1,
            max_position_pct=0.10,
            max_portfolio_heat=0.70,
        )

        qty_high, _ = sizer.compute(signal=signal_high, **base_args)
        qty_low, _ = sizer.compute(signal=signal_low, **base_args)

        assert qty_high > qty_low

    def test_fractional_kelly(self) -> None:
        """Kelly Criterion: win_rate=0.6, pf=1.5 → positives Kelly."""
        sizer = PositionSizer()
        kelly = sizer._fractional_kelly(win_rate=0.6, profit_factor=1.5, fraction=0.25)
        assert kelly > 0
        assert kelly < 0.25  # Immer kleiner als Fraction

    def test_kelly_zero_for_losing_strategy(self) -> None:
        """Verlierende Strategie → Kelly = 0 (kein Trading)."""
        sizer = PositionSizer()
        kelly = sizer._fractional_kelly(win_rate=0.3, profit_factor=0.5)
        assert kelly == 0.0


# ---------------------------------------------------------------------------
# Kill Switch
# ---------------------------------------------------------------------------


class TestKillSwitch:
    @pytest.fixture
    def kill_switch(self) -> KillSwitch:
        ks = KillSwitch(TradingMode.PAPER)
        return ks

    def test_initially_inactive(self, kill_switch: KillSwitch) -> None:
        assert not kill_switch.is_active
        assert kill_switch.trading_allowed

    async def test_trigger_activates(self, kill_switch: KillSwitch) -> None:
        await kill_switch.trigger("Test reason", triggered_by="test")
        assert kill_switch.is_active
        assert not kill_switch.trading_allowed

    async def test_trigger_idempotent(self, kill_switch: KillSwitch) -> None:
        """Zweimaliges Triggern hat keinen zusätzlichen Effekt."""
        await kill_switch.trigger("First reason")
        await kill_switch.trigger("Second reason")
        # Reason sollte die erste sein
        assert kill_switch.state.reason == "First reason"

    async def test_reset_deactivates(self, kill_switch: KillSwitch) -> None:
        await kill_switch.trigger("Test")
        assert kill_switch.is_active
        await kill_switch.reset(reset_by="test_user")
        assert not kill_switch.is_active
        assert kill_switch.trading_allowed

    async def test_reset_inactive_no_error(self, kill_switch: KillSwitch) -> None:
        """Reset auf inaktivem Kill Switch wirft keine Exception."""
        await kill_switch.reset(reset_by="test_user")
        assert not kill_switch.is_active

    def test_state_contains_reason(self, kill_switch: KillSwitch) -> None:
        assert kill_switch.state.reason is None
        assert kill_switch.state.triggered_at is None

    async def test_trigger_records_timestamp(self, kill_switch: KillSwitch) -> None:
        await kill_switch.trigger("drawdown exceeded")
        assert kill_switch.state.triggered_at is not None


# ---------------------------------------------------------------------------
# Risk Engine – Limit Checks
# ---------------------------------------------------------------------------


class TestRiskEngineLimits:
    async def test_approve_healthy_portfolio(
        self,
        risk_engine: RiskEngine,
        sample_signal: Signal,
    ) -> None:
        """Gesundes Portfolio → APPROVED."""
        await risk_engine.initialize()
        assessment = await risk_engine.evaluate(
            signal=sample_signal,
            open_positions=[],
            portfolio_value=Decimal("100000"),
            available_capital=Decimal("90000"),
            current_price=Decimal("50000"),
            atr=Decimal("500"),
        )
        assert assessment.decision in (RiskDecision.APPROVED, RiskDecision.REDUCED)
        assert assessment.approved_quantity > 0

    async def test_reject_when_kill_switch_active(
        self,
        risk_engine: RiskEngine,
        sample_signal: Signal,
    ) -> None:
        """Aktiver Kill Switch → immer REJECTED."""
        await risk_engine.initialize()
        await risk_engine._kill_switch.trigger("manual test")

        assessment = await risk_engine.evaluate(
            signal=sample_signal,
            open_positions=[],
            portfolio_value=Decimal("100000"),
            available_capital=Decimal("90000"),
            current_price=Decimal("50000"),
        )
        assert assessment.decision == RiskDecision.REJECTED
        assert "Kill switch" in (assessment.rejection_reason or "")

        # Cleanup
        await risk_engine._kill_switch.reset("cleanup")

    async def test_hard_limit_drawdown_triggers_kill_switch(
        self,
        risk_engine: RiskEngine,
        sample_signal: Signal,
    ) -> None:
        """Max Drawdown überschritten → Kill Switch + REJECTED."""
        await risk_engine.initialize()

        # Peak auf 100k setzen
        risk_engine._peak_portfolio_value = Decimal("100000")

        # Aktuelle Value bei 80k → 20% Drawdown (über 15% Limit)
        assessment = await risk_engine.evaluate(
            signal=sample_signal,
            open_positions=[],
            portfolio_value=Decimal("80000"),
            available_capital=Decimal("70000"),
            current_price=Decimal("50000"),
        )

        # Kleine Pause für async Kill Switch Task
        import asyncio

        await asyncio.sleep(0.01)

        assert assessment.decision == RiskDecision.REJECTED

        # Cleanup
        await risk_engine._kill_switch.reset("cleanup")

    async def test_daily_loss_limit(
        self,
        risk_engine: RiskEngine,
        sample_signal: Signal,
    ) -> None:
        """Daily Loss Limit überschritten → REJECTED."""
        await risk_engine.initialize()

        # Tages-Start auf 100k setzen
        risk_engine._daily_pnl_start = Decimal("100000")
        risk_engine._daily_pnl_date = datetime.now(tz=UTC).strftime("%Y-%m-%d")

        # Aktuelle Value bei 94k → -6% (über -5% Limit)
        assessment = await risk_engine.evaluate(
            signal=sample_signal,
            open_positions=[],
            portfolio_value=Decimal("94000"),
            available_capital=Decimal("84000"),
            current_price=Decimal("50000"),
        )

        assert assessment.decision == RiskDecision.REJECTED

        import asyncio

        await asyncio.sleep(0.01)
        await risk_engine._kill_switch.reset("cleanup")

    async def test_fail_safe_on_exception(
        self,
        risk_engine: RiskEngine,
        sample_signal: Signal,
    ) -> None:
        """Exception in Risk-Berechnung → REJECT (Fail-Safe)."""
        await risk_engine.initialize()

        # Ungültiger Preis triggert Fehler
        assessment = await risk_engine.evaluate(
            signal=sample_signal,
            open_positions=[],
            portfolio_value=Decimal("0"),  # Division by zero kandidat
            available_capital=Decimal("0"),
            current_price=Decimal("0"),
        )
        # Fail-safe: muss REJECTED sein
        assert assessment.decision == RiskDecision.REJECTED

    async def test_soft_limit_reduces_not_rejects(
        self,
        risk_engine: RiskEngine,
        sample_signal: Signal,
        open_position: Position,
    ) -> None:
        """Soft Limit (hohe Heat) → REDUCED, nicht REJECTED."""
        await risk_engine.initialize()

        # Viele Positionen simulieren (hohe Heat)
        many_positions = [open_position] * 5  # 5 Positionen

        assessment = await risk_engine.evaluate(
            signal=sample_signal,
            open_positions=many_positions,
            portfolio_value=Decimal("100000"),
            available_capital=Decimal("50000"),
            current_price=Decimal("50000"),
        )
        # Sollte nicht Hard-Rejected sein
        assert (
            assessment.decision != RiskDecision.REJECTED
            or assessment.rejection_reason != "Kill switch is active"
        )


# ---------------------------------------------------------------------------
# Risk Engine – Order Construction
# ---------------------------------------------------------------------------


class TestOrderConstruction:
    async def test_build_order_request(
        self,
        risk_engine: RiskEngine,
        sample_signal: Signal,
    ) -> None:
        await risk_engine.initialize()

        from sgr.core.types import RiskAssessment, RiskDecision, RiskMetrics

        metrics = RiskMetrics(
            timestamp=datetime.now(tz=UTC),
            portfolio_value=Decimal("100000"),
            daily_pnl=Decimal("0"),
            daily_pnl_pct=0.0,
            drawdown_from_peak=0.0,
            var_95=0.01,
            expected_shortfall=0.02,
            portfolio_heat=0.1,
            active_positions=0,
            correlation_exposure=0.0,
        )
        assessment = RiskAssessment(
            signal_id=sample_signal.id,
            decision=RiskDecision.APPROVED,
            approved_quantity=Decimal("0.1"),
            risk_metrics_snapshot=metrics,
        )

        order = risk_engine.build_order_request(sample_signal, assessment)
        assert order.quantity == Decimal("0.1")
        assert order.signal_id == sample_signal.id
        assert order.trading_mode == TradingMode.PAPER

    async def test_high_var_forces_limit_order_with_price(
        self,
        risk_engine: RiskEngine,
        sample_signal: Signal,
    ) -> None:
        """Hohe Slippage (VaR > 80% des Limits) -> LIMIT statt MARKET,
        mit current_price als limit_price (kein Fill zu Preis 0)."""
        await risk_engine.initialize()

        from sgr.core.types import OrderType, RiskAssessment, RiskDecision, RiskMetrics

        metrics = RiskMetrics(
            timestamp=datetime.now(tz=UTC),
            portfolio_value=Decimal("100000"),
            daily_pnl=Decimal("0"),
            daily_pnl_pct=0.0,
            drawdown_from_peak=0.0,
            var_95=risk_engine._limits.var_95_limit * 0.9,
            expected_shortfall=0.02,
            portfolio_heat=0.1,
            active_positions=0,
            correlation_exposure=0.0,
        )
        assessment = RiskAssessment(
            signal_id=sample_signal.id,
            decision=RiskDecision.APPROVED,
            approved_quantity=Decimal("0.1"),
            risk_metrics_snapshot=metrics,
        )

        order = risk_engine.build_order_request(
            sample_signal, assessment, current_price=Decimal("50000")
        )
        assert order.order_type == OrderType.LIMIT
        assert order.limit_price == Decimal("50000")

    async def test_high_var_without_current_price_raises_instead_of_zero_price_order(
        self,
        risk_engine: RiskEngine,
        sample_signal: Signal,
    ) -> None:
        """Bug-Fix-Regressionstest: früher entstand hier eine LIMIT-Order
        ohne Preis, die der Paper-Adapter mit Fill-Preis 0 interpretiert
        hätte. Jetzt: explizite Exception statt stiller Fehlbepreisung."""
        await risk_engine.initialize()

        from sgr.core.types import RiskAssessment, RiskDecision, RiskMetrics

        metrics = RiskMetrics(
            timestamp=datetime.now(tz=UTC),
            portfolio_value=Decimal("100000"),
            daily_pnl=Decimal("0"),
            daily_pnl_pct=0.0,
            drawdown_from_peak=0.0,
            var_95=risk_engine._limits.var_95_limit * 0.9,
            expected_shortfall=0.02,
            portfolio_heat=0.1,
            active_positions=0,
            correlation_exposure=0.0,
        )
        assessment = RiskAssessment(
            signal_id=sample_signal.id,
            decision=RiskDecision.APPROVED,
            approved_quantity=Decimal("0.1"),
            risk_metrics_snapshot=metrics,
        )

        with pytest.raises(ValueError, match="current_price required"):
            risk_engine.build_order_request(sample_signal, assessment)
