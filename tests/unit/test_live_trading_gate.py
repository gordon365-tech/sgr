"""
Tests für den Live Trading Safety Gate (sgr/risk/live_trading_gate.py).

Kernfrage: kann STRATEGY_FORCE_ACTIVATE (Paper-Trading-Operator-Override,
siehe sgr.api.main.apply_strategy_force_activate_override()) unter
IRGENDEINEM Umstand Live Trading freischalten? Diese Suite beweist: nein,
in keiner der geprüften Kombinationen (live+override, paper+override,
fehlendes live_approved, fehlendes is_validated/is_active, fehlende
Strategie-Zuordnung, fehlende Safety-Infrastruktur).
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from sgr.core.types import ExchangeID, OrderRequest, OrderType, Side, Symbol, TradingMode
from sgr.risk.live_trading_gate import check_live_trading_allowed
from sgr.strategy.base import ValidationStatus
from sgr.strategy.registry import StrategyRegistry


def _make_order(trading_mode: TradingMode, strategy: str | None = "test_strategy") -> OrderRequest:
    sym = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)
    metadata = {"strategy": strategy} if strategy is not None else {}
    return OrderRequest(
        signal_id=uuid4(),
        symbol=sym,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.01"),
        trading_mode=trading_mode,
        metadata=metadata,
    )


class _FakeStrategy:
    name = "test_strategy"
    version = "1.0.0"
    supported_regimes: list = []

    def generate_signal(self, context):  # pragma: no cover - not exercised here
        return None


@pytest.fixture(autouse=True)
def reset_registry():
    StrategyRegistry.get().clear()
    yield
    StrategyRegistry.get().clear()


@pytest.fixture
def registry() -> StrategyRegistry:
    return StrategyRegistry.get()


class TestPaperModeAlwaysAllowed:
    """Paper Trading darf durch dieses Gate NIEMALS blockiert werden - es
    betrifft ausschliesslich TradingMode.LIVE."""

    def test_paper_order_allowed_with_no_registry_at_all(self) -> None:
        order = _make_order(TradingMode.PAPER)
        result = check_live_trading_allowed(order, registry=None, kill_switch=None)
        assert result.allowed is True
        assert result.reason is None

    async def test_paper_order_allowed_with_operator_override_active(
        self, registry: StrategyRegistry
    ) -> None:
        """Der explizite Kernfall aus Punkt 6: paper mode + override MUSS
        weiterhin funktionieren."""
        registry.register_instance(_FakeStrategy())
        registry.mark_validated(
            "test_strategy",
            ValidationStatus(
                backtest_passed=True,
                walk_forward_passed=True,
                paper_trading_passed=True,
                live_approved=False,
                is_operator_override=True,
                notes="MANUAL OVERRIDE",
            ),
        )
        await registry.activate("test_strategy")

        order = _make_order(TradingMode.PAPER)
        result = check_live_trading_allowed(order, registry=registry, kill_switch=MagicMock())
        assert result.allowed is True


class TestLiveModeWithOperatorOverride:
    """Der explizite Kernfall aus Punkt 6: live mode + override MUSS
    verweigert werden, unabhängig von is_validated/is_active."""

    async def test_live_order_blocked_when_strategy_is_operator_override(
        self, registry: StrategyRegistry
    ) -> None:
        registry.register_instance(_FakeStrategy())
        registry.mark_validated(
            "test_strategy",
            ValidationStatus(
                backtest_passed=True,
                walk_forward_passed=True,
                paper_trading_passed=True,
                live_approved=False,
                is_operator_override=True,
            ),
        )
        await registry.activate("test_strategy")
        assert registry.get_entry("test_strategy").is_validated is True
        assert registry.get_entry("test_strategy").is_active is True

        order = _make_order(TradingMode.LIVE)
        result = check_live_trading_allowed(order, registry=registry, kill_switch=MagicMock())

        assert result.allowed is False
        assert "override" in (result.reason or "").lower()

    async def test_live_order_blocked_even_if_live_approved_is_incorrectly_true(
        self, registry: StrategyRegistry
    ) -> None:
        """Verteidigung gegen einen zukuenftigen Bug: selbst wenn
        live_approved fälschlich True wäre, muss is_operator_override
        weiterhin als unabhängige zweite Sperre greifen."""
        registry.register_instance(_FakeStrategy())
        registry.mark_validated(
            "test_strategy",
            ValidationStatus(
                backtest_passed=True,
                walk_forward_passed=True,
                paper_trading_passed=True,
                live_approved=True,  # hypothetischer Bug
                is_operator_override=True,
            ),
        )
        await registry.activate("test_strategy")

        order = _make_order(TradingMode.LIVE)
        result = check_live_trading_allowed(order, registry=registry, kill_switch=MagicMock())

        assert result.allowed is False
        assert "override" in (result.reason or "").lower()


class TestLiveModeRequiresGenuineApproval:
    async def test_live_order_blocked_without_live_approved(
        self, registry: StrategyRegistry
    ) -> None:
        """is_validated=True + is_active=True (z.B. echte Backtest-
        Validierung) reichen allein NICHT fuer Live - live_approved ist
        ein separates, strengeres Feld."""
        registry.register_instance(_FakeStrategy())
        registry.mark_validated(
            "test_strategy",
            ValidationStatus(
                backtest_passed=True,
                walk_forward_passed=True,
                paper_trading_passed=True,
                live_approved=False,
                is_operator_override=False,
            ),
        )
        await registry.activate("test_strategy")

        order = _make_order(TradingMode.LIVE)
        result = check_live_trading_allowed(order, registry=registry, kill_switch=MagicMock())

        assert result.allowed is False
        assert "live_approved" in (result.reason or "")

    async def test_live_order_blocked_when_not_validated(self, registry: StrategyRegistry) -> None:
        registry.register_instance(_FakeStrategy())
        registry.mark_validated(
            "test_strategy",
            ValidationStatus(
                backtest_passed=True,
                walk_forward_passed=True,
                paper_trading_passed=True,
                live_approved=True,
            ),
        )
        # is_active NICHT gesetzt (kein registry.activate() Aufruf) -
        # simuliert live_approved=True aber (noch) nicht aktiv.
        order = _make_order(TradingMode.LIVE)
        result = check_live_trading_allowed(order, registry=registry, kill_switch=MagicMock())

        assert result.allowed is False
        assert "is_validated and is_active" in (result.reason or "")

    def test_live_order_blocked_when_strategy_not_registered(
        self, registry: StrategyRegistry
    ) -> None:
        order = _make_order(TradingMode.LIVE, strategy="ghost_strategy")
        result = check_live_trading_allowed(order, registry=registry, kill_switch=MagicMock())

        assert result.allowed is False
        assert "not registered" in (result.reason or "")

    def test_live_order_blocked_when_no_strategy_attributed(
        self, registry: StrategyRegistry
    ) -> None:
        order = _make_order(TradingMode.LIVE, strategy=None)
        result = check_live_trading_allowed(order, registry=registry, kill_switch=MagicMock())

        assert result.allowed is False
        assert "no attributable strategy_name" in (result.reason or "")

    def test_live_order_blocked_when_strategy_unknown_literal(
        self, registry: StrategyRegistry
    ) -> None:
        order = _make_order(TradingMode.LIVE, strategy="unknown")
        result = check_live_trading_allowed(order, registry=registry, kill_switch=MagicMock())

        assert result.allowed is False


class TestFailClosedOnMissingSafetyInfra:
    def test_live_order_blocked_without_registry(self) -> None:
        order = _make_order(TradingMode.LIVE)
        result = check_live_trading_allowed(order, registry=None, kill_switch=MagicMock())
        assert result.allowed is False
        assert "StrategyRegistry" in (result.reason or "")

    def test_live_order_blocked_without_kill_switch(self, registry: StrategyRegistry) -> None:
        order = _make_order(TradingMode.LIVE)
        result = check_live_trading_allowed(order, registry=registry, kill_switch=None)
        assert result.allowed is False
        assert "KillSwitch" in (result.reason or "")


class TestFullyApprovedLiveStrategyPasses:
    """Positivfall: eine Strategie, die alle Bedingungen echt erfüllt,
    darf durch - das Gate ist kein Totalblock, sondern ein echtes Gate."""

    async def test_live_order_allowed_when_genuinely_approved(
        self, registry: StrategyRegistry
    ) -> None:
        registry.register_instance(_FakeStrategy())
        registry.mark_validated(
            "test_strategy",
            ValidationStatus(
                backtest_passed=True,
                walk_forward_passed=True,
                paper_trading_passed=True,
                live_approved=True,
                is_operator_override=False,
            ),
        )
        await registry.activate("test_strategy")

        order = _make_order(TradingMode.LIVE)
        result = check_live_trading_allowed(order, registry=registry, kill_switch=MagicMock())

        assert result.allowed is True
        assert result.reason is None
