"""
Tests für sgr.execution.grid_controller.GridController.

Deckt (siehe Aufgabenstellung "TESTS"):
    - Futures Grid Creation (Long/Short)
    - Grid Order Generation
    - Grid Order Cancellation (close_grid)
    - Grid Rebalancing (Level wird nach einem Zyklus wieder frei)
    - Position Accounting (net_position_qty kehrt nach Rundtrip auf 0 zurueck)
    - Compliance blocking / Unsupported Product blocking / Exchange
      capability blocking
    - Kill Switch blockiert neue Grid-Fills
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sgr.compliance.engine import ComplianceEngine
from sgr.compliance.types import AccountEligibility, ProductAvailabilityRule
from sgr.core.grid_types import FuturesGridParameters, GridDecision
from sgr.core.types import ExchangeID, GridDirection, GridStatus, ProductType, Symbol, TradingMode
from sgr.exchanges.factory import ExchangePool
from sgr.execution.engine import ExecutionEngine
from sgr.execution.grid_controller import GridController
from sgr.risk.grid_risk import GridPortfolioSnapshot
from sgr.risk.kill_switch import get_kill_switch
from tests.mocks.mock_exchange import MockExchangeAdapter


@pytest.fixture
async def adapter() -> MockExchangeAdapter:
    a = MockExchangeAdapter(trading_mode=TradingMode.PAPER)
    await a.connect()
    return a


@pytest.fixture
def pool(adapter: MockExchangeAdapter) -> ExchangePool:
    p = ExchangePool()
    p._adapters[(ExchangeID.BINANCE, TradingMode.PAPER)] = adapter
    return p


@pytest.fixture
def compliance() -> ComplianceEngine:
    engine = ComplianceEngine()
    engine.register_rule(
        ProductAvailabilityRule(
            exchange=ExchangeID.BINANCE,
            product_type=ProductType.FUTURES_GRID,
            jurisdictions_allowed=["DE"],
        )
    )
    return engine


@pytest.fixture
def account() -> AccountEligibility:
    return AccountEligibility(
        tenant_id="gordon",
        jurisdiction="DE",
        kyc_verified=True,
        futures_trading_enabled=True,
        risk_disclosure_acknowledged=True,
        enabled_product_types=["futures_grid"],
    )


@pytest.fixture
def controller(pool: ExchangePool, compliance: ComplianceEngine) -> GridController:
    engine = ExecutionEngine(pool, TradingMode.PAPER)
    return GridController(
        engine, TradingMode.PAPER, tenant_id="gordon", compliance_engine=compliance
    )


def _symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def _params(direction: GridDirection = GridDirection.LONG) -> FuturesGridParameters:
    return FuturesGridParameters(
        grid_lower_price=Decimal("49000"),
        grid_upper_price=Decimal("51000"),
        grid_count=5,
        long_or_short=direction,
        leverage=Decimal("2"),
        position_size=Decimal("50"),
        max_notional=Decimal("250"),
    )


def _decision(direction: GridDirection = GridDirection.LONG) -> GridDecision:
    return GridDecision(
        direction=direction, parameters=_params(direction), confidence=0.7, reasons=["test"]
    )


def _snapshot() -> GridPortfolioSnapshot:
    return GridPortfolioSnapshot(open_grids=[], portfolio_value=Decimal("10000"))


class TestGridCreation:
    async def test_open_long_grid_succeeds(self, controller: GridController, account) -> None:
        result = await controller.open_grid(
            _decision(GridDirection.LONG),
            _symbol(),
            "futures_grid_long_v1",
            account,
            _snapshot(),
            current_price=Decimal("50000"),
        )

        assert result.approved is True
        assert result.grid is not None
        assert result.grid.status == GridStatus.ACTIVE
        assert result.grid.direction == GridDirection.LONG
        assert len(result.grid.levels) == 5

    async def test_open_short_grid_succeeds(self, controller: GridController, account) -> None:
        result = await controller.open_grid(
            _decision(GridDirection.SHORT),
            _symbol(),
            "futures_grid_short_v1",
            account,
            _snapshot(),
            current_price=Decimal("50000"),
        )

        assert result.approved is True
        assert result.grid.direction == GridDirection.SHORT

    async def test_neutral_decision_is_rejected_without_side_effects(
        self, controller: GridController, account
    ) -> None:
        decision = GridDecision(
            direction=GridDirection.NEUTRAL, parameters=None, confidence=0.1, reasons=[]
        )
        result = await controller.open_grid(
            decision,
            _symbol(),
            "futures_grid_adaptive_v1",
            account,
            _snapshot(),
            current_price=Decimal("50000"),
        )

        assert result.approved is False
        assert result.grid is None


class TestExchangeCapabilityBlocking:
    async def test_unsupported_exchange_product_combination_is_blocked(
        self, controller: GridController, account
    ) -> None:
        from sgr.core.types import ExchangeID as EID

        symbol = Symbol(base="BTC", quote="USDT", exchange=EID.KRAKEN)
        result = await controller.open_grid(
            _decision(),
            symbol,
            "futures_grid_long_v1",
            account,
            _snapshot(),
            current_price=Decimal("50000"),
        )

        assert result.approved is False
        assert result.compliance_status == "exchange_capability_missing"


class TestComplianceBlocking:
    async def test_blocked_without_registered_rule(self, pool: ExchangePool, account) -> None:
        engine = ExecutionEngine(pool, TradingMode.PAPER)
        controller = GridController(
            engine, TradingMode.PAPER, tenant_id="gordon"
        )  # kein Rule registriert

        result = await controller.open_grid(
            _decision(),
            _symbol(),
            "futures_grid_long_v1",
            account,
            _snapshot(),
            current_price=Decimal("50000"),
        )

        assert result.approved is False
        assert result.compliance_status == "product_not_available"

    async def test_blocked_for_unverified_kyc(self, controller: GridController) -> None:
        account = AccountEligibility(
            tenant_id="gordon",
            jurisdiction="DE",
            kyc_verified=False,
            futures_trading_enabled=True,
            risk_disclosure_acknowledged=True,
            enabled_product_types=["futures_grid"],
        )
        result = await controller.open_grid(
            _decision(),
            _symbol(),
            "futures_grid_long_v1",
            account,
            _snapshot(),
            current_price=Decimal("50000"),
        )

        assert result.approved is False
        assert result.compliance_status == "account_not_eligible"


class TestGridOrderGenerationAndFills:
    async def test_price_drop_opens_level(
        self, controller: GridController, account, adapter: MockExchangeAdapter
    ) -> None:
        result = await controller.open_grid(
            _decision(),
            _symbol(),
            "futures_grid_long_v1",
            account,
            _snapshot(),
            current_price=Decimal("50000"),
        )
        grid = result.grid

        adapter.ticker_price = Decimal("49500")
        grid = await controller.on_price_tick(str(grid.id), Decimal("49500"))

        filled = [lv for lv in grid.levels if lv.is_filled]
        assert len(filled) == 1
        assert filled[0].price == Decimal("49500.00000000")
        assert grid.fills_count == 1
        assert adapter.call_count("place_order") == 1

    async def test_full_cycle_returns_net_position_to_zero(
        self, controller: GridController, account, adapter: MockExchangeAdapter
    ) -> None:
        result = await controller.open_grid(
            _decision(),
            _symbol(),
            "futures_grid_long_v1",
            account,
            _snapshot(),
            current_price=Decimal("50000"),
        )
        grid = result.grid

        for p in ["49500", "49000", "49750", "50250"]:
            adapter.ticker_price = Decimal(p)
            grid = await controller.on_price_tick(str(grid.id), Decimal(p))

        assert grid.net_position_qty == Decimal("0") or abs(grid.net_position_qty) < Decimal(
            "0.0000001"
        )
        assert grid.realized_pnl > Decimal("0")  # Grid Capture nach zwei Zyklen


class TestGridRebalancing:
    async def test_level_reopens_after_a_full_cycle(
        self, controller: GridController, account, adapter: MockExchangeAdapter
    ) -> None:
        """Nach Open+Close eines Levels muss dasselbe Level erneut
        oeffnen koennen, wenn der Preis wieder dorthin zurueckfaellt
        (Grid-Rebalancing/Level-Wechsel, siehe Aufgabenstellung)."""
        result = await controller.open_grid(
            _decision(),
            _symbol(),
            "futures_grid_long_v1",
            account,
            _snapshot(),
            current_price=Decimal("50000"),
        )
        grid = result.grid

        for p in ["49500", "50000"]:  # open + close cell (49500,50000)
            adapter.ticker_price = Decimal(p)
            grid = await controller.on_price_tick(str(grid.id), Decimal(p))

        level_49500 = next(lv for lv in grid.levels if lv.price == Decimal("49500.00000000"))
        assert level_49500.is_filled is False
        assert level_49500.cycle_count == 1

        # Preis faellt erneut auf 49500 -> Level oeffnet ein ZWEITES Mal.
        adapter.ticker_price = Decimal("49500")
        grid = await controller.on_price_tick(str(grid.id), Decimal("49500"))

        level_49500 = next(lv for lv in grid.levels if lv.price == Decimal("49500.00000000"))
        assert level_49500.is_filled is True
        assert level_49500.cycle_count == 2


class TestGridCancellationAndClose:
    async def test_close_grid_closes_all_open_levels(
        self, controller: GridController, account, adapter: MockExchangeAdapter
    ) -> None:
        result = await controller.open_grid(
            _decision(),
            _symbol(),
            "futures_grid_long_v1",
            account,
            _snapshot(),
            current_price=Decimal("50000"),
        )
        grid = result.grid
        adapter.ticker_price = Decimal("49000")
        grid = await controller.on_price_tick(str(grid.id), Decimal("49000"))
        assert any(lv.is_filled for lv in grid.levels)

        closed = await controller.close_grid(str(grid.id), "manual_close", Decimal("49000"))

        assert closed.status == GridStatus.CLOSED
        assert closed.close_reason == "manual_close"
        assert all(not lv.is_filled for lv in closed.levels)

    async def test_close_grid_is_idempotent_when_already_closed(
        self, controller: GridController, account
    ) -> None:
        result = await controller.open_grid(
            _decision(),
            _symbol(),
            "futures_grid_long_v1",
            account,
            _snapshot(),
            current_price=Decimal("50000"),
        )
        grid_id = str(result.grid.id)
        await controller.close_grid(grid_id, "first_close", Decimal("50000"))

        # Zweiter close_grid()-Aufruf auf ein bereits geschlossenes Grid
        # darf nicht erneut Orders auslösen oder den close_reason
        # ueberschreiben.
        second = await controller.close_grid(grid_id, "second_close", Decimal("50000"))

        assert second.close_reason == "first_close"


class TestMonitorGrids:
    async def test_monitor_closes_grid_on_hard_violation(
        self, controller: GridController, account, adapter: MockExchangeAdapter
    ) -> None:
        result = await controller.open_grid(
            _decision(),
            _symbol(),
            "futures_grid_long_v1",
            account,
            _snapshot(),
            current_price=Decimal("50000"),
        )
        grid = result.grid
        grid.realized_pnl = Decimal("-10000")  # erzwingt max_grid_loss_exceeded

        closed = await controller.monitor_grids({grid.symbol.ccxt_symbol: Decimal("50000")})

        assert len(closed) == 1
        assert closed[0].status == GridStatus.CLOSED

    async def test_monitor_ticks_healthy_grid_without_closing(
        self, controller: GridController, account, adapter: MockExchangeAdapter
    ) -> None:
        result = await controller.open_grid(
            _decision(),
            _symbol(),
            "futures_grid_long_v1",
            account,
            _snapshot(),
            current_price=Decimal("50000"),
        )
        grid = result.grid

        adapter.ticker_price = Decimal("49500")
        closed = await controller.monitor_grids({grid.symbol.ccxt_symbol: Decimal("49500")})

        assert closed == []
        updated = controller.get_grid(str(grid.id))
        assert updated.fills_count == 1  # on_price_tick wurde ausgefuehrt

    async def test_monitor_skips_grids_without_a_price(
        self, controller: GridController, account
    ) -> None:
        result = await controller.open_grid(
            _decision(),
            _symbol(),
            "futures_grid_long_v1",
            account,
            _snapshot(),
            current_price=Decimal("50000"),
        )
        closed = await controller.monitor_grids({})  # keine Preise geliefert

        assert closed == []
        assert controller.get_grid(str(result.grid.id)).fills_count == 0

    async def test_monitor_ignores_inactive_grids(
        self, controller: GridController, account
    ) -> None:
        result = await controller.open_grid(
            _decision(),
            _symbol(),
            "futures_grid_long_v1",
            account,
            _snapshot(),
            current_price=Decimal("50000"),
        )
        await controller.close_grid(str(result.grid.id), "manual", Decimal("50000"))

        closed = await controller.monitor_grids({result.grid.symbol.ccxt_symbol: Decimal("50000")})

        assert closed == []


class TestKillSwitchBlocksGridFills:
    async def test_active_kill_switch_prevents_level_fill(
        self, controller: GridController, account, adapter: MockExchangeAdapter
    ) -> None:
        result = await controller.open_grid(
            _decision(),
            _symbol(),
            "futures_grid_long_v1",
            account,
            _snapshot(),
            current_price=Decimal("50000"),
        )
        grid = result.grid

        # ExecutionEngine liest sein eigenes tenant_id-Scoping fuer den
        # Kill Switch aus get_config().tenant_id (Prozess-globale Config,
        # NICHT aus dem an GridController uebergebenen tenant_id-Parameter
        # dieses Tests) - siehe sgr.execution.engine.ExecutionEngine.
        # __init__. In der Test-Umgebung ist das unveraendert None.
        from sgr.core.config import get_config

        kill_switch = get_kill_switch(TradingMode.PAPER, tenant_id=get_config().tenant_id)
        await kill_switch.trigger("test kill switch", triggered_by="test")
        try:
            adapter.ticker_price = Decimal("49500")
            grid = await controller.on_price_tick(str(grid.id), Decimal("49500"))

            assert all(not lv.is_filled for lv in grid.levels)
            assert grid.fills_count == 0
        finally:
            await kill_switch.reset(reset_by="test cleanup")
