"""
Unit-Tests für PositionLiquidator.

Deckt die im Rahmen der operativen Validierung gefundene Lücke ab:
KillSwitch.trigger(close_positions=True) hatte vorher keine Wirkung, weil
niemand das publizierte KillSwitchEvent konsumierte. Diese Tests
verifizieren, dass der Liquidator:
    1. close_positions=False ignoriert (kein Order-Versand)
    2. Events fremder Tenants ignoriert (Cross-Tenant-Schutz)
    3. für jede offene Position eine gegenläufige Market Order sendet
    4. LONG -> SELL, SHORT -> BUY korrekt ableitet
    5. den Kill-Switch-Bypass beim Order-Versand setzt
    6. bei FILLED das Portfolio direkt aktualisiert (kein Event-Bus-Pfad)
    7. ein fehlgeschlagener Close eine andere Position nicht blockiert
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from sgr.core.types import (
    ExchangeID,
    KillSwitchEvent,
    OrderResult,
    OrderStatus,
    Position,
    PositionSide,
    Side,
    Symbol,
    TradingMode,
)
from sgr.risk.position_liquidator import PositionLiquidator

GORDON = "a47d994d-35cc-4619-83bb-86fd0cb48447"
SUMO = "144f7bb0-113d-4c66-b5d3-7a34ab1d551a"


def _symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def _position(side: PositionSide, quantity: str = "0.5") -> Position:
    return Position(
        symbol=_symbol(),
        side=side,
        quantity=Decimal(quantity),
        entry_price=Decimal("50000"),
        current_price=Decimal("51000"),
        opened_at=datetime.now(tz=UTC),
        strategy_name="mean_reversion_v1",
        trading_mode=TradingMode.PAPER,
    )


def _event(
    close_positions: bool = True,
    tenant_id: str | None = GORDON,
    reason: str = "max_drawdown_exceeded",
) -> KillSwitchEvent:
    return KillSwitchEvent(
        timestamp=datetime.now(tz=UTC),
        reason=reason,
        trading_mode=TradingMode.PAPER,
        tenant_id=tenant_id,
        close_positions=close_positions,
    )


def _fill_result(order, status: OrderStatus = OrderStatus.FILLED) -> OrderResult:
    return OrderResult(
        request_id=order.id,
        exchange_order_id="PAPER-1",
        symbol=order.symbol,
        status=status,
        filled_quantity=order.quantity if status == OrderStatus.FILLED else Decimal("0"),
        average_fill_price=Decimal("51000") if status == OrderStatus.FILLED else None,
        fees=Decimal("1"),
        submitted_at=datetime.now(tz=UTC),
        trading_mode=order.trading_mode,
    )


@pytest.fixture
def portfolio() -> MagicMock:
    p = MagicMock()
    p.positions = []
    p.on_order_filled = AsyncMock()
    return p


@pytest.fixture
def execution() -> MagicMock:
    e = MagicMock()
    e.execute = AsyncMock(side_effect=lambda order, **kw: _fill_result(order))
    return e


@pytest.fixture
def liquidator(portfolio: MagicMock, execution: MagicMock) -> PositionLiquidator:
    return PositionLiquidator(
        portfolio_engine=portfolio, execution_engine=execution, tenant_id=GORDON
    )


class TestIgnoredEvents:
    async def test_close_positions_false_is_ignored(
        self, liquidator: PositionLiquidator, portfolio: MagicMock, execution: MagicMock
    ) -> None:
        portfolio.positions = [_position(PositionSide.LONG)]
        await liquidator.on_kill_switch_event(_event(close_positions=False))
        execution.execute.assert_not_awaited()

    async def test_foreign_tenant_event_is_ignored(
        self, liquidator: PositionLiquidator, portfolio: MagicMock, execution: MagicMock
    ) -> None:
        """Gordons Liquidator darf auf ein Sumo-Event nicht reagieren -
        sonst wuerde ein fremder Kill Switch Gordons Positionen schliessen."""
        portfolio.positions = [_position(PositionSide.LONG)]
        await liquidator.on_kill_switch_event(_event(tenant_id=SUMO))
        execution.execute.assert_not_awaited()

    async def test_no_open_positions_is_noop(
        self, liquidator: PositionLiquidator, portfolio: MagicMock, execution: MagicMock
    ) -> None:
        portfolio.positions = []
        await liquidator.on_kill_switch_event(_event())
        execution.execute.assert_not_awaited()

    async def test_single_tenant_deployment_none_matches_none(
        self, portfolio: MagicMock, execution: MagicMock
    ) -> None:
        """tenant_id=None (Single-Tenant) muss mit tenant_id=None im Event matchen."""
        liquidator = PositionLiquidator(portfolio, execution, tenant_id=None)
        portfolio.positions = [_position(PositionSide.LONG)]
        await liquidator.on_kill_switch_event(_event(tenant_id=None))
        execution.execute.assert_awaited_once()


class TestClosingOrders:
    async def test_long_position_closed_with_sell(
        self, liquidator: PositionLiquidator, portfolio: MagicMock, execution: MagicMock
    ) -> None:
        portfolio.positions = [_position(PositionSide.LONG, "0.5")]
        await liquidator.on_kill_switch_event(_event())

        order = execution.execute.await_args.args[0]
        assert order.side == Side.SELL
        assert order.quantity == Decimal("0.5")
        assert order.reduce_only is True

    async def test_short_position_closed_with_buy(
        self, liquidator: PositionLiquidator, portfolio: MagicMock, execution: MagicMock
    ) -> None:
        portfolio.positions = [_position(PositionSide.SHORT, "0.3")]
        await liquidator.on_kill_switch_event(_event())

        order = execution.execute.await_args.args[0]
        assert order.side == Side.BUY

    async def test_bypasses_kill_switch_on_execute(
        self, liquidator: PositionLiquidator, portfolio: MagicMock, execution: MagicMock
    ) -> None:
        """Die Closing-Order muss trotz aktivem Kill Switch durchkommen -
        sonst wuerde sie sich selbst blockieren."""
        portfolio.positions = [_position(PositionSide.LONG)]
        await liquidator.on_kill_switch_event(_event())

        assert execution.execute.await_args.kwargs.get("bypass_kill_switch") is True

    async def test_multiple_positions_all_closed(
        self, liquidator: PositionLiquidator, portfolio: MagicMock, execution: MagicMock
    ) -> None:
        portfolio.positions = [_position(PositionSide.LONG), _position(PositionSide.SHORT)]
        await liquidator.on_kill_switch_event(_event())
        assert execution.execute.await_count == 2

    async def test_filled_result_updates_portfolio_directly(
        self, liquidator: PositionLiquidator, portfolio: MagicMock, execution: MagicMock
    ) -> None:
        """Kein Event-Bus-Pfad - direkter Aufruf wie im TradingOrchestrator,
        um Doppelverarbeitung/Race auszuschliessen (siehe Modul-Docstring)."""
        portfolio.positions = [_position(PositionSide.LONG)]
        await liquidator.on_kill_switch_event(_event())
        portfolio.on_order_filled.assert_awaited_once()

    async def test_non_filled_result_does_not_update_portfolio(
        self, liquidator: PositionLiquidator, portfolio: MagicMock, execution: MagicMock
    ) -> None:
        execution.execute = AsyncMock(
            side_effect=lambda order, **kw: _fill_result(order, status=OrderStatus.REJECTED)
        )
        portfolio.positions = [_position(PositionSide.LONG)]
        await liquidator.on_kill_switch_event(_event())
        portfolio.on_order_filled.assert_not_awaited()

    async def test_one_close_failure_does_not_block_others(
        self, liquidator: PositionLiquidator, portfolio: MagicMock, execution: MagicMock
    ) -> None:
        """Ein Exchange-Fehler beim Schliessen einer Position darf die
        anderen nicht verhindern (fail-safe, analog zu KillSwitch._cancel_all_orders)."""
        calls = {"n": 0}

        async def _side_effect(order, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("exchange down")
            return _fill_result(order)

        execution.execute = AsyncMock(side_effect=_side_effect)
        portfolio.positions = [_position(PositionSide.LONG), _position(PositionSide.SHORT)]

        await liquidator.on_kill_switch_event(_event())

        assert execution.execute.await_count == 2
        portfolio.on_order_filled.assert_awaited_once()


class TestAggregateFlatnessProof:
    """Explizite Anweisung: 'Nach Emergency Close muss NET EXPOSURE = 0
    beweisbar sein... Wenn Flatness nicht eindeutig festgestellt werden
    kann: FAIL CLOSED / Operator Attention Required.' Verifiziert den
    aggregierten Flatness-Nachweis am Ende von on_kill_switch_event()
    (CRITICAL-Log bei jedem nicht bewiesenen Flatness-Fall), nicht nur
    die bereits bestehenden Per-Item-Logs."""

    @staticmethod
    def _critical_events(mock_log: MagicMock) -> list[str]:
        return [call.args[0] for call in mock_log.critical.call_args_list]

    async def test_all_positions_filled_logs_complete_not_critical(
        self, liquidator: PositionLiquidator, portfolio: MagicMock, mocker
    ) -> None:
        mock_log = mocker.patch("sgr.risk.position_liquidator.log")
        portfolio.positions = [_position(PositionSide.LONG)]

        await liquidator.on_kill_switch_event(_event())

        assert self._critical_events(mock_log) == []
        assert mock_log.info.call_args_list[-1].args[0] == (
            "position_liquidator.emergency_close_complete"
        )

    async def test_rejected_close_triggers_critical_escalation(
        self, liquidator: PositionLiquidator, portfolio: MagicMock, execution: MagicMock, mocker
    ) -> None:
        mock_log = mocker.patch("sgr.risk.position_liquidator.log")
        execution.execute = AsyncMock(
            side_effect=lambda order, **kw: _fill_result(order, status=OrderStatus.REJECTED)
        )
        portfolio.positions = [_position(PositionSide.LONG)]

        await liquidator.on_kill_switch_event(_event())

        events = self._critical_events(mock_log)
        assert "position_liquidator.close_order_not_filled" in events
        assert "position_liquidator.emergency_close_incomplete" in events

    async def test_exception_during_close_triggers_critical_escalation(
        self, liquidator: PositionLiquidator, portfolio: MagicMock, execution: MagicMock, mocker
    ) -> None:
        mock_log = mocker.patch("sgr.risk.position_liquidator.log")
        execution.execute = AsyncMock(side_effect=RuntimeError("exchange unreachable"))
        portfolio.positions = [_position(PositionSide.LONG)]

        await liquidator.on_kill_switch_event(_event())

        events = self._critical_events(mock_log)
        assert "position_liquidator.close_order_failed" in events
        assert "position_liquidator.emergency_close_incomplete" in events

    async def test_all_filled_and_no_grids_reports_no_critical(
        self, liquidator: PositionLiquidator, portfolio: MagicMock, mocker
    ) -> None:
        mock_log = mocker.patch("sgr.risk.position_liquidator.log")
        portfolio.positions = [_position(PositionSide.LONG), _position(PositionSide.SHORT)]

        await liquidator.on_kill_switch_event(_event())

        assert self._critical_events(mock_log) == []


# ---------------------------------------------------------------------------
# Phase H (GATE 3): Grid-Awareness - echter GridController, kein Mock
# ---------------------------------------------------------------------------


class TestGridAwareLiquidation:
    """Echter Integrationstest: oeffnet ein reales Grid ueber einen echten
    GridController (ExecutionEngine + MockExchangeAdapter), fuellt ein
    Level, loest dann einen Kill Switch mit close_positions=True aus und
    verifiziert, dass PositionLiquidator das Grid VOLLSTAENDIG schliesst
    und Flatness erreicht - GATE 3."""

    async def _setup(self):
        from sgr.compliance.engine import ComplianceEngine
        from sgr.compliance.types import AccountEligibility, ProductAvailabilityRule
        from sgr.core.grid_types import FuturesGridParameters, GridDecision
        from sgr.core.types import ExchangeID as EID, GridDirection, ProductType
        from sgr.exchanges.factory import ExchangePool
        from sgr.execution.engine import ExecutionEngine
        from sgr.execution.grid_controller import GridController
        from sgr.risk.grid_risk import GridPortfolioSnapshot
        from tests.mocks.mock_exchange import MockExchangeAdapter

        adapter = MockExchangeAdapter(trading_mode=TradingMode.PAPER)
        await adapter.connect()
        pool = ExchangePool()
        pool._adapters[(EID.BINANCE, TradingMode.PAPER)] = adapter

        compliance = ComplianceEngine()
        compliance.register_rule(
            ProductAvailabilityRule(
                exchange=EID.BINANCE, product_type=ProductType.FUTURES_GRID,
                jurisdictions_allowed=["DE"],
            )
        )
        account = AccountEligibility(
            tenant_id=GORDON,
            jurisdiction="DE",
            kyc_verified=True,
            futures_trading_enabled=True,
            risk_disclosure_acknowledged=True,
            enabled_product_types=["futures_grid"],
        )

        execution = ExecutionEngine(pool, TradingMode.PAPER)
        grid_controller = GridController(
            execution, TradingMode.PAPER, tenant_id=GORDON, compliance_engine=compliance
        )

        params = FuturesGridParameters(
            grid_lower_price=Decimal("49000"),
            grid_upper_price=Decimal("51000"),
            grid_count=5,
            long_or_short=GridDirection.LONG,
            leverage=Decimal("2"),
            position_size=Decimal("50"),
            max_notional=Decimal("250"),
        )
        decision = GridDecision(
            direction=GridDirection.LONG, parameters=params, confidence=0.7, reasons=["test"]
        )
        symbol = _symbol()
        open_result = await grid_controller.open_grid(
            decision, symbol, "futures_grid_long_v1", account,
            GridPortfolioSnapshot(open_grids=[], portfolio_value=Decimal("10000")),
            current_price=Decimal("50000"),
        )
        assert open_result.approved is True

        # Level bei 49500 fuellen (Preisabfall).
        adapter.ticker_price = Decimal("49500")
        grid = await grid_controller.on_price_tick(str(open_result.grid.id), Decimal("49500"))
        assert any(lv.is_filled for lv in grid.levels)

        return grid_controller, grid, execution

    async def test_kill_switch_with_close_positions_closes_active_grid_flat(self) -> None:
        grid_controller, grid, execution = await self._setup()

        portfolio = MagicMock()
        portfolio.positions = []  # keine normalen Positionen betroffen
        liquidator = PositionLiquidator(
            portfolio_engine=portfolio,
            execution_engine=execution,
            tenant_id=GORDON,
            grid_controller=grid_controller,
        )

        await liquidator.on_kill_switch_event(_event(tenant_id=GORDON))

        closed = grid_controller.get_grid(str(grid.id))
        assert closed.status.value == "closed"
        assert abs(closed.net_position_qty) < Decimal("0.00000001")  # Flatness verifiziert

    async def test_grid_controller_not_injected_is_safe_noop(self) -> None:
        """Ohne set_grid_controller() (Default None) bleibt das exakte
        Vor-Aenderungs-Verhalten erhalten - kein Fehler, kein Effekt."""
        portfolio = MagicMock()
        portfolio.positions = []
        execution = MagicMock()
        liquidator = PositionLiquidator(
            portfolio_engine=portfolio, execution_engine=execution, tenant_id=GORDON
        )

        await liquidator.on_kill_switch_event(_event(tenant_id=GORDON))  # darf nicht crashen

    async def test_late_injection_via_set_grid_controller(self) -> None:
        """set_grid_controller() (die spaete Injektion, wie in
        sgr/api/main.py verwendet) wirkt korrekt."""
        grid_controller, grid, execution = await self._setup()

        portfolio = MagicMock()
        portfolio.positions = []
        liquidator = PositionLiquidator(
            portfolio_engine=portfolio, execution_engine=execution, tenant_id=GORDON
        )
        liquidator.set_grid_controller(grid_controller)

        await liquidator.on_kill_switch_event(_event(tenant_id=GORDON))

        assert grid_controller.get_grid(str(grid.id)).status.value == "closed"

    async def test_close_positions_false_leaves_grid_untouched(self) -> None:
        grid_controller, grid, execution = await self._setup()

        portfolio = MagicMock()
        portfolio.positions = []
        liquidator = PositionLiquidator(
            portfolio_engine=portfolio,
            execution_engine=execution,
            tenant_id=GORDON,
            grid_controller=grid_controller,
        )

        await liquidator.on_kill_switch_event(_event(tenant_id=GORDON, close_positions=False))

        assert grid_controller.get_grid(str(grid.id)).status.value == "active"

    async def test_foreign_tenant_event_leaves_grid_untouched(self) -> None:
        grid_controller, grid, execution = await self._setup()

        portfolio = MagicMock()
        portfolio.positions = []
        liquidator = PositionLiquidator(
            portfolio_engine=portfolio,
            execution_engine=execution,
            tenant_id=GORDON,
            grid_controller=grid_controller,
        )

        await liquidator.on_kill_switch_event(_event(tenant_id=SUMO))

        assert grid_controller.get_grid(str(grid.id)).status.value == "active"

    async def test_no_active_grids_is_safe_noop(self) -> None:
        from sgr.exchanges.factory import ExchangePool
        from sgr.execution.engine import ExecutionEngine
        from sgr.execution.grid_controller import GridController

        pool = ExchangePool()
        execution = ExecutionEngine(pool, TradingMode.PAPER)
        grid_controller = GridController(execution, TradingMode.PAPER, tenant_id=GORDON)

        portfolio = MagicMock()
        portfolio.positions = []
        liquidator = PositionLiquidator(
            portfolio_engine=portfolio,
            execution_engine=execution,
            tenant_id=GORDON,
            grid_controller=grid_controller,
        )

        await liquidator.on_kill_switch_event(_event(tenant_id=GORDON))  # darf nicht crashen
