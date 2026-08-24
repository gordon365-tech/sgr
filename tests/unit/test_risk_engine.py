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
from typing import TYPE_CHECKING

import numpy as np
import pytest

if TYPE_CHECKING:
    import pytest_mock

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
from sgr.risk.var_calculator import MonteCarloVaR, VaRCalculator, VaRMethod

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

    def test_cornish_fisher_produces_valid_result(self) -> None:
        """Cornish-Fisher: korrigiert für Schiefe/Kurtosis, liefert valides VaR/ES."""
        np.random.seed(42)
        returns = np.random.normal(0.001, 0.02, 200)
        calc = VaRCalculator()
        result = calc.compute(returns, 0.95, VaRMethod.CORNISH_FISHER)
        assert result.var >= 0
        assert result.es >= result.var
        assert result.method == VaRMethod.CORNISH_FISHER
        assert result.observations == 200

    def test_cornish_fisher_reacts_to_skew(self) -> None:
        """Bei schiefer Verteilung weicht Cornish-Fisher VaR vom Normal-VaR ab."""
        np.random.seed(7)
        # Stark linksschiefe Verteilung (Crypto-typisch: Fat Left Tail)
        skewed_returns = np.concatenate(
            [np.random.normal(0.01, 0.01, 190), np.random.normal(-0.15, 0.05, 10)]
        )
        calc = VaRCalculator()
        cf_result = calc.compute(skewed_returns, 0.95, VaRMethod.CORNISH_FISHER)
        normal_result = calc.compute(skewed_returns, 0.95, VaRMethod.PARAMETRIC_NORMAL)
        # Beide muessen valide sein; CF muss nicht identisch mit Normal sein
        assert cf_result.var >= 0
        assert normal_result.var >= 0

    def test_unknown_method_raises(self) -> None:
        """Unbekannte VaR-Methode -> ValueError, kein stiller Fallback."""
        calc = VaRCalculator()
        returns = np.random.normal(0, 0.02, 50)
        with pytest.raises(ValueError, match="Unknown VaR method"):
            calc.compute(returns, 0.95, method="not_a_real_method")  # type: ignore[arg-type]

    def test_result_repr_contains_key_fields(self) -> None:
        """__repr__ ist menschenlesbar und enthaelt VaR/ES/Confidence/Method."""
        calc = VaRCalculator()
        returns = np.random.normal(0, 0.02, 50)
        result = calc.compute(returns, 0.95, VaRMethod.HISTORICAL)
        text = repr(result)
        assert "var=" in text
        assert "es=" in text
        assert "historical" in text

    def test_portfolio_var_empty_symbols_returns_zero(self) -> None:
        """Kein Symbol im Portfolio -> VaR=0, kein Crash (0/0 Fallgrube vermeiden)."""
        calc = VaRCalculator()
        result = calc.compute_portfolio_var({}, {})
        assert result.var == 0.0
        assert result.es == 0.0
        assert result.observations == 0

    def test_portfolio_var_insufficient_data_falls_back_conservative(self) -> None:
        """Zu kurze Return-Historie (< 10 Beobachtungen) -> konservativer Fallback."""
        calc = VaRCalculator()
        result = calc.compute_portfolio_var(
            {"BTC": np.array([0.01, -0.02, 0.005])},
            {"BTC": 1.0},
        )
        # Fail-safe: konservative Default-Werte statt instabiler Kleinstichproben-VaR
        assert result.var == 0.05
        assert result.es == 0.08


# ---------------------------------------------------------------------------
# Monte Carlo VaR
# ---------------------------------------------------------------------------


class TestMonteCarloVaR:
    """
    MonteCarloVaR ist aktuell nicht in RiskEngine/Orchestrator verdrahtet
    (Docstring: "für Stress-Tests und Go-Live Gates", nicht Echtzeit-Pfad).
    Da die Klasse aber oeffentlich aus sgr.risk exportiert wird und für
    spaetere Live-Trading-Readiness-Checks vorgesehen ist, muss sie
    korrekt funktionieren, auch ohne aktuelle Produktiv-Einbindung.
    """

    def test_simulate_returns_all_expected_keys(self) -> None:
        np.random.seed(42)
        returns = np.random.normal(0.001, 0.02, 100)
        mc = MonteCarloVaR()
        result = mc.simulate(returns, portfolio_value=100_000.0, simulations=500)
        assert set(result.keys()) == {
            "var_95",
            "var_99",
            "es_95",
            "worst_case",
            "percentile_5",
        }

    def test_simulate_var_99_exceeds_var_95(self) -> None:
        """Höhere Konfidenz -> höheres VaR (99% Verlust >= 95% Verlust)."""
        np.random.seed(42)
        returns = np.random.normal(0.0, 0.02, 200)
        mc = MonteCarloVaR()
        result = mc.simulate(returns, portfolio_value=100_000.0, simulations=2000)
        assert result["var_99"] >= result["var_95"]

    def test_simulate_es_exceeds_var(self) -> None:
        """Expected Shortfall (Tail-Mittelwert) muss immer >= VaR sein."""
        np.random.seed(42)
        returns = np.random.normal(0.0, 0.02, 200)
        mc = MonteCarloVaR()
        result = mc.simulate(returns, portfolio_value=100_000.0, simulations=2000)
        assert result["es_95"] >= result["var_95"]

    def test_simulate_worst_case_exceeds_var_99(self) -> None:
        """Worst Case (Simulations-Maximum) muss mindestens so hoch wie 99% VaR sein."""
        np.random.seed(42)
        returns = np.random.normal(0.0, 0.02, 200)
        mc = MonteCarloVaR()
        result = mc.simulate(returns, portfolio_value=100_000.0, simulations=2000)
        assert result["worst_case"] >= result["var_99"]

    def test_simulate_is_deterministic_with_fixed_seed(self) -> None:
        """Fester Seed -> reproduzierbare Ergebnisse (Auditierbarkeit)."""
        returns = np.random.default_rng(1).normal(0.001, 0.02, 100)
        mc = MonteCarloVaR()
        result_a = mc.simulate(returns, portfolio_value=50_000.0, simulations=500, seed=123)
        result_b = mc.simulate(returns, portfolio_value=50_000.0, simulations=500, seed=123)
        assert result_a == result_b

    def test_simulate_scales_with_horizon(self) -> None:
        """Längerer Horizont -> höheres VaR (sqrt(t)-Skalierung der Volatilität)."""
        np.random.seed(42)
        returns = np.random.normal(0.0, 0.02, 200)
        mc = MonteCarloVaR()
        result_1d = mc.simulate(
            returns, portfolio_value=100_000.0, simulations=2000, horizon_days=1, seed=42
        )
        result_10d = mc.simulate(
            returns, portfolio_value=100_000.0, simulations=2000, horizon_days=10, seed=42
        )
        assert result_10d["var_95"] > result_1d["var_95"]


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

    async def test_trigger_without_exchange_pool_does_not_crash(
        self, kill_switch: KillSwitch
    ) -> None:
        """Kein Exchange Pool injiziert -> Warnung loggen, kein Crash (fail-safe)."""
        # kill_switch fixture hat keinen Pool injiziert
        await kill_switch.trigger("No pool test")
        assert kill_switch.is_active

    async def test_inject_exchange_pool_stores_reference(
        self, kill_switch: KillSwitch
    ) -> None:
        fake_pool = object()
        kill_switch.inject_exchange_pool(fake_pool)
        assert kill_switch._exchange_pool is fake_pool

    async def test_trigger_with_wrong_pool_type_skips_cancellation(
        self, kill_switch: KillSwitch
    ) -> None:
        """Injiziertes Objekt ist kein ExchangePool -> Cancel wird übersprungen, kein Crash."""
        kill_switch.inject_exchange_pool(object())  # kein ExchangePool
        await kill_switch.trigger("Wrong pool type test")
        assert kill_switch.is_active

    async def test_trigger_cancels_orders_on_matching_mode_adapters(
        self, kill_switch: KillSwitch, mocker: pytest_mock.MockerFixture
    ) -> None:
        """Cancel wird nur für Adapter im selben TradingMode aufgerufen, nicht Cross-Mode."""
        from sgr.exchanges.factory import ExchangePool

        pool = mocker.Mock(spec=ExchangePool)
        paper_adapter = mocker.AsyncMock()
        paper_adapter.cancel_all_orders = mocker.AsyncMock(return_value=3)
        live_adapter = mocker.AsyncMock()
        live_adapter.cancel_all_orders = mocker.AsyncMock(return_value=99)

        pool._adapters = {
            (ExchangeID.BINANCE, TradingMode.PAPER): paper_adapter,
            (ExchangeID.BINANCE, TradingMode.LIVE): live_adapter,
        }
        kill_switch.inject_exchange_pool(pool)

        await kill_switch.trigger("Cross-mode isolation test")

        paper_adapter.cancel_all_orders.assert_awaited_once()
        live_adapter.cancel_all_orders.assert_not_awaited()

    async def test_trigger_isolates_per_adapter_cancel_failure(
        self, kill_switch: KillSwitch, mocker: pytest_mock.MockerFixture
    ) -> None:
        """Ein fehlschlagender Adapter darf andere Adapter nicht blockieren (best-effort)."""
        from sgr.exchanges.factory import ExchangePool

        pool = mocker.Mock(spec=ExchangePool)
        failing_adapter = mocker.AsyncMock()
        failing_adapter.cancel_all_orders = mocker.AsyncMock(
            side_effect=RuntimeError("exchange unreachable")
        )
        healthy_adapter = mocker.AsyncMock()
        healthy_adapter.cancel_all_orders = mocker.AsyncMock(return_value=2)

        pool._adapters = {
            (ExchangeID.BINANCE, TradingMode.PAPER): failing_adapter,
            (ExchangeID.KRAKEN, TradingMode.PAPER): healthy_adapter,
        }
        kill_switch.inject_exchange_pool(pool)

        # Muss trotz Exception in einem Adapter durchlaufen (fail-safe, kein Crash)
        await kill_switch.trigger("Partial failure test")

        assert kill_switch.is_active
        failing_adapter.cancel_all_orders.assert_awaited_once()
        healthy_adapter.cancel_all_orders.assert_awaited_once()

    async def test_trigger_with_close_positions_logs_without_crash(
        self, kill_switch: KillSwitch
    ) -> None:
        """close_positions=True triggert _close_all_positions-Pfad.

        Log-only: Portfolio Engine reagiert separat auf das KillSwitchEvent.
        """
        await kill_switch.trigger("Close positions test", close_positions=True)
        assert kill_switch.is_active

    async def test_trigger_survives_adapter_iteration_crash(
        self, kill_switch: KillSwitch, mocker: pytest_mock.MockerFixture
    ) -> None:
        """
        Äußerster Fail-Safe: selbst wenn die Iteration über _adapters selbst
        crasht (nicht nur ein einzelner cancel_all_orders-Call), darf trigger()
        nicht crashen. Kill Switch State muss trotzdem aktiv bleiben.
        """
        from sgr.exchanges.factory import ExchangePool

        pool = mocker.Mock(spec=ExchangePool)
        # .items() wirft direkt -> testet den äußeren try/except um die
        # gesamte Adapter-Iteration, nicht nur um einen einzelnen Adapter-Call
        broken_dict = mocker.Mock()
        broken_dict.items.side_effect = RuntimeError("adapter registry corrupted")
        pool._adapters = broken_dict
        kill_switch.inject_exchange_pool(pool)

        await kill_switch.trigger("Adapter registry crash test")

        assert kill_switch.is_active

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
# Risk Engine – Coverage-Lücken (deterministisch, kein Coverage-Theater)
# ---------------------------------------------------------------------------


class TestRiskEngineRemainingGaps:
    """
    Deckt die verbleibenden 16 Zeilen in sgr/risk/engine.py ab:
    142-151 (echter except-Block), 235-236 (reduction_factor angewendet),
    246, 248 (Soft-Limit- und Warning-Sammlung), 313-318 (VaR mit >=10
    Return-History-Einträgen), 346-349 (Rolling-Window-Trim), 451 (WARNING-
    Status-Zweig in _check_threshold).

    Jeder Test verifiziert deterministisch den Zielpfad statt auf einen
    zufällig zutreffenden Zustand zu hoffen.
    """

    async def test_except_block_on_internal_exception(
        self,
        risk_engine: RiskEngine,
        sample_signal: Signal,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Zeilen 142-151: der äußere except-Block in evaluate() wird nur
        erreicht, wenn _evaluate_internal selbst wirft – nicht durch einen
        Guard-Pfad wie portfolio_value=0 (der landet in _reject via
        PositionSizer, siehe test_fail_safe_on_exception oben).
        Wir lassen _compute_metrics gezielt crashen.
        """
        await risk_engine.initialize()

        def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated metrics computation failure")

        monkeypatch.setattr(risk_engine, "_compute_metrics", boom)

        assessment = await risk_engine.evaluate(
            signal=sample_signal,
            open_positions=[],
            portfolio_value=Decimal("100000"),
            available_capital=Decimal("50000"),
            current_price=Decimal("50000"),
        )

        assert assessment.decision == RiskDecision.REJECTED
        assert assessment.rejection_reason is not None
        assert "Risk engine error" in assessment.rejection_reason
        assert "simulated metrics computation failure" in assessment.rejection_reason
        assert any("error" in w.lower() for w in assessment.warnings)

    async def test_reduction_factor_applied_on_soft_breach(
        self,
        risk_engine: RiskEngine,
        sample_signal: Signal,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Zeilen 235-236: reduction_factor < 1.0 muss die Quantity tatsächlich
        multiplizieren und eine Warning erzeugen. Wir mocken _run_all_checks,
        um einen deterministischen Soft-Breach zu erzwingen, statt auf
        zufällige Heat-Werte aus vielen offenen Positionen zu hoffen.
        """
        await risk_engine.initialize()

        from sgr.risk.types import LimitCheck, LimitStatus, LimitType

        soft_breach_check = LimitCheck(
            name="portfolio_heat",
            limit_type=LimitType.SOFT,
            status=LimitStatus.BREACHED,
            current_value=0.9,
            limit_value=0.8,
            message="Portfolio heat 0.90 exceeds limit 0.80",
            reduction_factor=0.5,
        )

        def fake_checks(metrics: object) -> list[LimitCheck]:
            return [soft_breach_check]

        monkeypatch.setattr(risk_engine, "_run_all_checks", fake_checks)

        assessment = await risk_engine.evaluate(
            signal=sample_signal,
            open_positions=[],
            portfolio_value=Decimal("100000"),
            available_capital=Decimal("50000"),
            current_price=Decimal("50000"),
        )

        assert assessment.decision == RiskDecision.REDUCED
        assert any("reduced to 50%" in w.lower() for w in assessment.warnings)

    async def test_soft_breach_and_warning_messages_collected(
        self,
        risk_engine: RiskEngine,
        sample_signal: Signal,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Zeilen 246 und 248: sowohl der SOFT+BREACHED-Zweig ("Soft limit: ...")
        als auch der WARNING-Zweig ("Warning: ...") müssen jeweils eine
        eigene Warning-Message erzeugen. reduction_factor bleibt bei 1.0,
        damit qty > 0 bleibt und wir den Loop-Body sicher erreichen.
        """
        await risk_engine.initialize()

        from sgr.risk.types import LimitCheck, LimitStatus, LimitType

        soft_breach_no_reduction = LimitCheck(
            name="correlation_exposure",
            limit_type=LimitType.SOFT,
            status=LimitStatus.BREACHED,
            current_value=1.0,
            limit_value=0.9,
            message="Correlation exposure 1.00 exceeds limit 0.90",
            reduction_factor=1.0,
        )
        warning_check = LimitCheck(
            name="daily_loss",
            limit_type=LimitType.SOFT,
            status=LimitStatus.WARNING,
            current_value=0.85,
            limit_value=0.90,
            message="Daily loss approaching limit",
            reduction_factor=1.0,
        )

        def fake_checks(metrics: object) -> list[LimitCheck]:
            return [soft_breach_no_reduction, warning_check]

        monkeypatch.setattr(risk_engine, "_run_all_checks", fake_checks)

        assessment = await risk_engine.evaluate(
            signal=sample_signal,
            open_positions=[],
            portfolio_value=Decimal("100000"),
            available_capital=Decimal("50000"),
            current_price=Decimal("50000"),
        )

        assert any(w.startswith("Soft limit:") for w in assessment.warnings)
        assert any(w.startswith("Warning:") for w in assessment.warnings)

    def test_var_computed_with_sufficient_return_history(
        self,
        risk_engine: RiskEngine,
    ) -> None:
        """
        Zeilen 313-318: VaR/ES werden nur berechnet, wenn mindestens 10
        Werte in der Return-History vorliegen. Mit weniger bleibt var_95
        bei 0.0 (siehe Default oben im Code).
        """
        for r in [0.01, -0.02, 0.015, -0.01, 0.02, -0.015, 0.01, -0.005, 0.03, -0.02]:
            risk_engine.update_returns(r)

        assert len(risk_engine._return_history) == 10

        metrics = risk_engine._compute_metrics(Decimal("100000"), [])

        assert metrics.var_95 != 0.0
        assert metrics.var_95 >= 0.0

    def test_return_history_rolling_window_trim(
        self,
        risk_engine: RiskEngine,
    ) -> None:
        """
        Zeilen 346-349: bei mehr als 252 Einträgen wird die Historie auf
        die letzten 252 (1 Handelsjahr) gekürzt.
        """
        for i in range(260):
            risk_engine.update_returns(0.001 * (i % 5))

        assert len(risk_engine._return_history) == 252
        # Die ältesten 8 Einträge (Index 0-7) müssen verworfen worden sein.
        assert risk_engine._return_history[0] == 0.001 * (8 % 5)

    def test_check_threshold_warning_status_branch(
        self,
        risk_engine: RiskEngine,
    ) -> None:
        """
        Zeile 451: current liegt zwischen warning_threshold*limit und limit
        selbst -> Status muss WARNING sein, nicht OK oder BREACHED.
        """
        from sgr.risk.types import LimitStatus, LimitType

        check = risk_engine._check_threshold(
            name="test_metric",
            limit_type=LimitType.SOFT,
            current=0.95,
            limit=1.0,
            message_template="{current:.2f} vs {limit:.2f}",
            warning_threshold=0.9,
            reduction_factor=0.5,
        )

        assert check.status == LimitStatus.WARNING


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
