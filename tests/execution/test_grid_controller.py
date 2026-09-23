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
    - Symbol-Kill-Switch (Phase G): blockiert neue Level-Oeffnungen,
      schliesst bestehende Exposure via close_all_grids_for_symbol()
    - Hedge-Mode-Kompatibilitaet (Phase K): fail-closed bei hedged=True
    - Crash Recovery (Phase C): restore_from_persistence(), Ledger-Replay,
      fail-closed bei inkonsistentem Zustand (GATE 1, GATE 10, GATE 11)
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

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


# ---------------------------------------------------------------------------
# Phase G: Symbol-Kill-Switch
# ---------------------------------------------------------------------------


class TestSymbolKillSwitchBlocksGrid:
    async def test_open_grid_blocked_for_deactivated_symbol(
        self, pool: ExchangePool, compliance: ComplianceEngine, account
    ) -> None:
        from sgr.risk.symbol_kill_switch import SymbolKillSwitch

        engine = ExecutionEngine(pool, TradingMode.PAPER)
        sks = SymbolKillSwitch(tenant_id="gordon")
        await sks.deactivate("binance:BTC/USDT", reason="test")
        controller = GridController(
            engine,
            TradingMode.PAPER,
            tenant_id="gordon",
            compliance_engine=compliance,
            symbol_kill_switch=sks,
        )

        result = await controller.open_grid(
            _decision(), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )

        assert result.approved is False
        assert result.compliance_status == "symbol_kill_switch_active"

    async def test_open_grid_allowed_for_unrelated_symbol(
        self, pool: ExchangePool, compliance: ComplianceEngine, account
    ) -> None:
        from sgr.risk.symbol_kill_switch import SymbolKillSwitch

        engine = ExecutionEngine(pool, TradingMode.PAPER)
        sks = SymbolKillSwitch(tenant_id="gordon")
        await sks.deactivate("binance:ETH/USDT", reason="test")  # anderes Symbol
        controller = GridController(
            engine,
            TradingMode.PAPER,
            tenant_id="gordon",
            compliance_engine=compliance,
            symbol_kill_switch=sks,
        )

        result = await controller.open_grid(
            _decision(), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )

        assert result.approved is True

    async def test_on_price_tick_blocks_new_opens_but_allows_closes(
        self,
        pool: ExchangePool,
        compliance: ComplianceEngine,
        account,
        adapter: MockExchangeAdapter,
    ) -> None:
        """Kill-Switch waehrend ein Grid bereits laeuft: KEINE neuen
        Level-Oeffnungen mehr, aber bereits gefuellte Level duerfen noch
        schliessen (de-risking bleibt erlaubt)."""
        from sgr.risk.symbol_kill_switch import SymbolKillSwitch

        engine = ExecutionEngine(pool, TradingMode.PAPER)
        sks = SymbolKillSwitch(tenant_id="gordon")
        controller = GridController(
            engine,
            TradingMode.PAPER,
            tenant_id="gordon",
            compliance_engine=compliance,
            symbol_kill_switch=sks,
        )
        result = await controller.open_grid(
            _decision(), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )
        grid = result.grid

        # Level bei 49500 oeffnen, WAEHREND Symbol noch aktiv ist.
        adapter.ticker_price = Decimal("49500")
        grid = await controller.on_price_tick(str(grid.id), Decimal("49500"))
        assert any(lv.is_filled for lv in grid.levels)

        # Jetzt Symbol deaktivieren.
        await sks.deactivate("binance:BTC/USDT", reason="test")

        # Preis faellt weiter (49000) -> WUERDE ein neues Level oeffnen,
        # darf aber nicht mehr.
        adapter.ticker_price = Decimal("49000")
        grid = await controller.on_price_tick(str(grid.id), Decimal("49000"))
        level_49000 = next(lv for lv in grid.levels if lv.price == Decimal("49000.00000000"))
        assert level_49000.is_filled is False

        # Preis steigt zurueck auf 50000 -> das bereits offene 49500-Level
        # DARF weiterhin schliessen (kein neues Risiko, nur Reduktion).
        adapter.ticker_price = Decimal("50000")
        grid = await controller.on_price_tick(str(grid.id), Decimal("50000"))
        level_49500 = next(lv for lv in grid.levels if lv.price == Decimal("49500.00000000"))
        assert level_49500.is_filled is False  # erfolgreich geschlossen

    async def test_close_all_grids_for_symbol_closes_active_exposure(
        self,
        pool: ExchangePool,
        compliance: ComplianceEngine,
        account,
        adapter: MockExchangeAdapter,
    ) -> None:
        """close_all_grids_for_symbol() (der SymbolKillSwitch-Deaktivierungs-
        Hook) schliesst JEDE offene Exposure eines aktiven Grids auf dem
        betroffenen Symbol vollstaendig."""
        engine = ExecutionEngine(pool, TradingMode.PAPER)
        controller = GridController(
            engine, TradingMode.PAPER, tenant_id="gordon", compliance_engine=compliance
        )
        result = await controller.open_grid(
            _decision(), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )
        grid = result.grid
        adapter.ticker_price = Decimal("49000")
        grid = await controller.on_price_tick(str(grid.id), Decimal("49000"))
        assert any(lv.is_filled for lv in grid.levels)

        closed = await controller.close_all_grids_for_symbol("binance:BTC/USDT", "manual_test")

        assert len(closed) == 1
        assert closed[0].status == GridStatus.CLOSED
        assert all(not lv.is_filled for lv in closed[0].levels)

    async def test_close_all_grids_for_symbol_is_idempotent(
        self,
        pool: ExchangePool,
        compliance: ComplianceEngine,
        account,
        adapter: MockExchangeAdapter,
    ) -> None:
        engine = ExecutionEngine(pool, TradingMode.PAPER)
        controller = GridController(
            engine, TradingMode.PAPER, tenant_id="gordon", compliance_engine=compliance
        )
        result = await controller.open_grid(
            _decision(), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )
        adapter.ticker_price = Decimal("49000")
        await controller.on_price_tick(str(result.grid.id), Decimal("49000"))

        first = await controller.close_all_grids_for_symbol("binance:BTC/USDT", "r1")
        second = await controller.close_all_grids_for_symbol("binance:BTC/USDT", "r2")

        assert len(first) == 1
        assert len(second) == 0  # bereits geschlossen - kein zweiter Close-Versuch

    async def test_symbol_kill_switch_deactivate_triggers_registered_hook(
        self,
        pool: ExchangePool,
        compliance: ComplianceEngine,
        account,
        adapter: MockExchangeAdapter,
    ) -> None:
        """End-to-End: SymbolKillSwitch.deactivate() -> registrierter Hook
        (GridController.close_all_grids_for_symbol) wird tatsaechlich
        aufgerufen (siehe sgr/api/main.py Aufrufstelle)."""
        from sgr.risk.symbol_kill_switch import SymbolKillSwitch

        engine = ExecutionEngine(pool, TradingMode.PAPER)
        sks = SymbolKillSwitch(tenant_id="gordon")
        controller = GridController(
            engine,
            TradingMode.PAPER,
            tenant_id="gordon",
            compliance_engine=compliance,
            symbol_kill_switch=sks,
        )
        sks.register_deactivation_hook(controller.close_all_grids_for_symbol)

        result = await controller.open_grid(
            _decision(), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )
        adapter.ticker_price = Decimal("49000")
        await controller.on_price_tick(str(result.grid.id), Decimal("49000"))

        await sks.deactivate("binance:BTC/USDT", reason="e2e test")

        grid = controller.get_grid(str(result.grid.id))
        assert grid.status == GridStatus.CLOSED


# ---------------------------------------------------------------------------
# Phase K: Hedge-/Position-Mode
# ---------------------------------------------------------------------------


class TestHedgeModeCompatibility:
    async def test_live_hedge_mode_account_rejects_grid_open(
        self,
        pool: ExchangePool,
        compliance: ComplianceEngine,
        account,
        adapter: MockExchangeAdapter,
    ) -> None:
        from sgr.exchanges.base import PositionModeInfo

        async def hedged_mode() -> PositionModeInfo:
            from datetime import UTC, datetime as dt

            return PositionModeInfo(
                exchange_id=ExchangeID.BINANCE, hedged=True, fetched_at=dt.now(tz=UTC)
            )

        adapter.get_position_mode = hedged_mode  # type: ignore[method-assign]
        pool._adapters[(ExchangeID.BINANCE, TradingMode.LIVE)] = adapter
        engine = ExecutionEngine(pool, TradingMode.LIVE)
        controller = GridController(
            engine,
            TradingMode.LIVE,
            tenant_id="gordon",
            compliance_engine=compliance,
            exchange_pool=pool,
        )

        result = await controller.open_grid(
            _decision(), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )

        assert result.approved is False
        assert result.compliance_status == "hedge_mode_unsupported"

    async def test_live_one_way_mode_account_allows_grid_open(
        self,
        pool: ExchangePool,
        compliance: ComplianceEngine,
        account,
        adapter: MockExchangeAdapter,
    ) -> None:
        from sgr.exchanges.base import PositionModeInfo

        async def one_way_mode() -> PositionModeInfo:
            from datetime import UTC, datetime as dt

            return PositionModeInfo(
                exchange_id=ExchangeID.BINANCE, hedged=False, fetched_at=dt.now(tz=UTC)
            )

        adapter.get_position_mode = one_way_mode  # type: ignore[method-assign]
        pool._adapters[(ExchangeID.BINANCE, TradingMode.LIVE)] = adapter
        engine = ExecutionEngine(pool, TradingMode.LIVE)
        controller = GridController(
            engine,
            TradingMode.LIVE,
            tenant_id="gordon",
            compliance_engine=compliance,
            exchange_pool=pool,
        )

        result = await controller.open_grid(
            _decision(), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )

        assert result.approved is True

    async def test_paper_mode_skips_hedge_check(
        self, controller: GridController, account
    ) -> None:
        """PAPER simuliert keinen echten Account-Modus - der Check greift
        nur fuer LIVE (siehe open_grid() Kommentar)."""
        result = await controller.open_grid(
            _decision(), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )
        assert result.approved is True


# ---------------------------------------------------------------------------
# Phase C: Crash Recovery
# ---------------------------------------------------------------------------


class FakeGridRepo:
    """In-Memory-Double fuer GridRepository - testet GridController.
    restore_from_persistence()'s LOGIK isoliert von der echten DB
    (Repository-Mechanik selbst ist durch tests/unit/test_grid_repository.py
    gegen eine echte DB abgedeckt)."""

    def __init__(
        self,
        open_grids: list[dict[str, Any]] | None = None,
        fills_by_grid: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._open_grids = open_grids or []
        self._fills = fills_by_grid or {}
        self.upserted: list[Any] = []
        self.recorded_fills: list[dict[str, Any]] = []
        self.get_fills_calls: list[str] = []

    async def get_open_grids(self, trading_mode: Any, user_id: str | None = None) -> list[Any]:
        return self._open_grids

    async def get_fills(self, grid_id: str) -> list[dict[str, Any]]:
        self.get_fills_calls.append(grid_id)
        return self._fills.get(grid_id, [])

    async def upsert(self, grid: Any) -> str:
        self.upserted.append(grid)
        return str(grid.id)

    async def record_level_fill(self, **kwargs: Any) -> str:
        self.recorded_fills.append(kwargs)
        return "fake-fill-id"


class FailingGetFillsGridRepo(FakeGridRepo):
    async def get_fills(self, grid_id: str) -> list[dict[str, Any]]:
        raise RuntimeError("db connection lost")


class FailingGetOpenGridsRepo(FakeGridRepo):
    async def get_open_grids(self, trading_mode: Any, user_id: str | None = None) -> list[Any]:
        raise RuntimeError("db connection lost")


class FakeOrderRepo:
    def __init__(self, orders: dict[str, dict[str, Any]] | None = None) -> None:
        self._orders = orders or {}

    async def get_by_id(self, order_id: str) -> dict[str, Any] | None:
        return self._orders.get(order_id)


_G1 = str(uuid4())


def _grid_row(
    grid_id: str,
    *,
    direction: str = "long",
    status: str = "active",
    grid_count: int = 5,
    position_size: str = "50",
    net_position_qty: str = "0",
    last_price: str | None = "50000",
) -> dict[str, Any]:
    return {
        "id": grid_id,
        "user_id": "gordon",
        "exchange": "binance",
        "symbol": "BTC/USDT",
        "product_type": "futures_grid",
        "strategy_name": "futures_grid_long_v1",
        "trading_mode": "paper",
        "direction": direction,
        "status": status,
        "parameters": {
            "grid_lower_price": "49000",
            "grid_upper_price": "51000",
            "grid_count": grid_count,
            "grid_mode": "arithmetic",
            "leverage": "2",
            "margin_mode": "isolated",
            "position_size": position_size,
            "max_notional": "250",
            "take_profit": None,
            "stop_loss": None,
            "maximum_holding_time": None,
            "total_notional": "250",
        },
        "net_position_qty": Decimal(net_position_qty),
        "realized_pnl": Decimal("0"),
        "fees_paid": Decimal("0"),
        "funding_paid": Decimal("0"),
        "fills_count": 0,
        "opened_at": datetime.now(tz=UTC),
        "closed_at": None,
        "close_reason": None,
        "levels": [],
        "last_price": Decimal(last_price) if last_price is not None else None,
    }


def _fill(
    level_index: int,
    *,
    is_opening: bool,
    order_id: str | None = None,
    quantity: str = "0.001",
    price: str = "49500",
) -> dict[str, Any]:
    return {
        "id": "fill-" + str(uuid4()),
        "grid_id": _G1,
        "order_id": order_id,
        "level_index": level_index,
        "price": Decimal(price),
        "side": "buy",
        "quantity": Decimal(quantity),
        "is_opening": is_opening,
        "cycle_pnl": None,
        "filled_at": datetime.now(tz=UTC),
    }


def _controller_for_recovery(
    pool: ExchangePool,
    grid_repo: Any,
    order_repo: Any = None,
) -> GridController:
    engine = ExecutionEngine(pool, TradingMode.PAPER)
    return GridController(
        engine,
        TradingMode.PAPER,
        tenant_id="gordon",
        grid_repository=grid_repo,
        order_repository=order_repo,
    )


class TestGridRecoveryBasics:
    async def test_restore_without_repository_returns_empty(
        self, pool: ExchangePool
    ) -> None:
        engine = ExecutionEngine(pool, TradingMode.PAPER)
        controller = GridController(engine, TradingMode.PAPER, tenant_id="gordon")

        restored = await controller.restore_from_persistence()

        assert restored == []

    async def test_restore_with_no_open_grids_returns_empty(self, pool: ExchangePool) -> None:
        repo = FakeGridRepo(open_grids=[])
        controller = _controller_for_recovery(pool, repo)

        restored = await controller.restore_from_persistence()

        assert restored == []

    async def test_restore_load_failure_returns_empty_not_raises(
        self, pool: ExchangePool
    ) -> None:
        repo = FailingGetOpenGridsRepo()
        controller = _controller_for_recovery(pool, repo)

        restored = await controller.restore_from_persistence()

        assert restored == []

    async def test_clean_restart_no_fills_all_levels_unfilled(self, pool: ExchangePool) -> None:
        """Szenario A: sauberer Restart, Grid war eroeffnet, aber noch
        kein einziges Level wurde je gefuellt."""
        row = _grid_row(_G1)
        repo = FakeGridRepo(open_grids=[row], fills_by_grid={_G1: []})
        controller = _controller_for_recovery(pool, repo)

        restored = await controller.restore_from_persistence()

        assert len(restored) == 1
        grid = restored[0]
        assert len(grid.levels) == 5
        assert all(not lv.is_filled for lv in grid.levels)
        assert controller.get_grid(_G1) is grid

    async def test_crash_before_order_submission_no_ledger_entry(
        self, pool: ExchangePool
    ) -> None:
        """Szenario B: Crash bevor ueberhaupt eine Order gesendet wurde -
        kein Ledger-Eintrag existiert, Level bleibt unfilled - identisch
        zum sauberen Restart aus Rekonstruktions-Sicht."""
        row = _grid_row(_G1)
        repo = FakeGridRepo(open_grids=[row], fills_by_grid={})
        controller = _controller_for_recovery(pool, repo)

        restored = await controller.restore_from_persistence()

        assert all(not lv.is_filled for lv in restored[0].levels)


class TestGridRecoveryLedgerReplay:
    async def test_crash_after_fill_stale_snapshot_ledger_wins(self, pool: ExchangePool) -> None:
        """Szenario D: Ledger hat den Fill, GridModel.levels-Snapshot ist
        (durch den Absturz) stale/leer - der Ledger MUSS gewinnen."""
        order_id = str(uuid4())
        row = _grid_row(_G1)
        fills = {_G1: [_fill(0, is_opening=True, order_id=order_id, quantity="0.001")]}
        order_repo = FakeOrderRepo({order_id: {"status": "filled"}})
        repo = FakeGridRepo(open_grids=[row], fills_by_grid=fills)
        controller = _controller_for_recovery(pool, repo, order_repo)

        restored = await controller.restore_from_persistence()

        level_0 = next(lv for lv in restored[0].levels if lv.index == 0)
        assert level_0.is_filled is True
        assert level_0.quantity == Decimal("0.001")
        assert level_0.cycle_count == 1

    async def test_crash_between_fill_and_counter_order(self, pool: ExchangePool) -> None:
        """Szenario E: Ledger hat NUR die Opening-Fill, keine Closing-Fill
        (der Absturz lag zwischen Fill und dem naechsten Crossing-Tick,
        der die Gegenorder ausgeloest haette). Level wird korrekt als
        offen/gefuellt wiederhergestellt - der naechste echte
        on_price_tick()-Aufruf uebernimmt die "Gegenorder" ganz normal,
        kein separater Rekonstruktionsschritt noetig (siehe
        restore_from_persistence() Docstring)."""
        order_id = str(uuid4())
        row = _grid_row(_G1)
        fills = {_G1: [_fill(0, is_opening=True, order_id=order_id, quantity="0.001")]}
        order_repo = FakeOrderRepo({order_id: {"status": "filled"}})
        repo = FakeGridRepo(open_grids=[row], fills_by_grid=fills)
        controller = _controller_for_recovery(pool, repo, order_repo)

        restored = await controller.restore_from_persistence()

        level_0 = next(lv for lv in restored[0].levels if lv.index == 0)
        assert level_0.is_filled is True
        assert level_0.last_order_id == order_id

    async def test_full_cycle_replay_level_ends_unfilled_with_incremented_cycle_count(
        self, pool: ExchangePool
    ) -> None:
        """Ledger hat Opening UND Closing fuer dasselbe Level - Endzustand
        ist unfilled, aber cycle_count zeigt die Historie."""
        order_id_1 = str(uuid4())
        order_id_2 = str(uuid4())
        row = _grid_row(_G1)
        fills = {
            _G1: [
                _fill(0, is_opening=True, order_id=order_id_1, quantity="0.001"),
                _fill(0, is_opening=False, order_id=order_id_2, quantity="0.001"),
            ]
        }
        order_repo = FakeOrderRepo(
            {order_id_1: {"status": "filled"}, order_id_2: {"status": "filled"}}
        )
        repo = FakeGridRepo(open_grids=[row], fills_by_grid=fills)
        controller = _controller_for_recovery(pool, repo, order_repo)

        restored = await controller.restore_from_persistence()

        level_0 = next(lv for lv in restored[0].levels if lv.index == 0)
        assert level_0.is_filled is False
        assert level_0.quantity == Decimal("0")
        assert level_0.cycle_count == 1

    async def test_gate1_three_distinct_level_states_reconstructed_correctly(
        self, pool: ExchangePool
    ) -> None:
        """GATE 1: Recovery mit mindestens drei unterschiedlichen
        Level-Zustaenden (offen/unfilled, gefuellt, Gegenorder ausstehend)
        - 100% korrekte Rekonstruktion, in EINEM Grid gleichzeitig."""
        oid_a = str(uuid4())
        oid_b = str(uuid4())
        oid_c = str(uuid4())
        row = _grid_row(_G1, grid_count=5)
        fills = {
            _G1: [
                # Level 0: voller Zyklus -> aktuell unfilled (Zustand "offen/frei")
                _fill(0, is_opening=True, order_id=oid_a, quantity="0.001"),
                _fill(0, is_opening=False, order_id=oid_a, quantity="0.001"),
                # Level 1: nur geoeffnet, keine Gegenorder -> "gefuellt,
                # Gegenorder aussteht"
                _fill(1, is_opening=True, order_id=oid_b, quantity="0.002"),
                # Level 2: bleibt komplett unberuehrt -> "nie gefuellt"
            ]
        }
        order_repo = FakeOrderRepo(
            {oid_a: {"status": "filled"}, oid_b: {"status": "filled"}, oid_c: {"status": "filled"}}
        )
        repo = FakeGridRepo(open_grids=[row], fills_by_grid=fills)
        controller = _controller_for_recovery(pool, repo, order_repo)

        restored = await controller.restore_from_persistence()
        assert len(restored) == 1
        levels = {lv.index: lv for lv in restored[0].levels}

        # Zustand 1: voller Zyklus -> unfilled, aber cycle_count=1
        assert levels[0].is_filled is False
        assert levels[0].cycle_count == 1
        # Zustand 2: gefuellt, Gegenorder aussteht -> is_filled=True
        assert levels[1].is_filled is True
        assert levels[1].quantity == Decimal("0.002")
        # Zustand 3: nie beruehrt -> unfilled, cycle_count=0
        assert levels[2].is_filled is False
        assert levels[2].cycle_count == 0
        assert levels[3].is_filled is False
        assert levels[4].is_filled is False


class TestGridRecoveryFailClosed:
    async def test_ledger_load_failure_excludes_grid_fail_closed(
        self, pool: ExchangePool
    ) -> None:
        """Ein Fehler beim Laden des Fill-Ledgers darf das Grid NIEMALS
        blind als 'alles unfilled' wiederherstellen - fail-closed."""
        row = _grid_row(_G1)
        repo = FailingGetFillsGridRepo(open_grids=[row])
        controller = _controller_for_recovery(pool, repo)

        restored = await controller.restore_from_persistence()

        assert restored == []
        assert controller.get_grid(_G1) is None

    async def test_fill_referencing_unfilled_order_excludes_grid(
        self, pool: ExchangePool
    ) -> None:
        """Szenario: Order-Status ist NICHT 'filled' (z.B. noch pending
        oder fehlgeschlagen) - unklarer Zustand, GESAMTES Grid wird nicht
        wiederhergestellt, kein Teilzustand, kein Raten."""
        order_id = str(uuid4())
        row = _grid_row(_G1)
        fills = {_G1: [_fill(0, is_opening=True, order_id=order_id)]}
        order_repo = FakeOrderRepo({order_id: {"status": "pending"}})
        repo = FakeGridRepo(open_grids=[row], fills_by_grid=fills)
        controller = _controller_for_recovery(pool, repo, order_repo)

        restored = await controller.restore_from_persistence()

        assert restored == []

    async def test_fill_referencing_missing_order_excludes_grid(
        self, pool: ExchangePool
    ) -> None:
        """Order-ID im Ledger, aber KEIN entsprechender Order-Repository-
        Eintrag auffindbar - unklarer Zustand, fail-closed."""
        order_id = str(uuid4())
        row = _grid_row(_G1)
        fills = {_G1: [_fill(0, is_opening=True, order_id=order_id)]}
        order_repo = FakeOrderRepo({})  # leer
        repo = FakeGridRepo(open_grids=[row], fills_by_grid=fills)
        controller = _controller_for_recovery(pool, repo, order_repo)

        restored = await controller.restore_from_persistence()

        assert restored == []

    async def test_fill_without_order_repository_trusts_ledger_directly(
        self, pool: ExchangePool
    ) -> None:
        """Ohne injiziertes order_repository (legitime Konfiguration) wird
        NICHT fail-closed geblockt - der Ledger selbst wird direkt
        vertraut (Status-Kreuzcheck ist eine zusaetzliche, optionale
        Absicherung, keine Voraussetzung)."""
        order_id = str(uuid4())
        row = _grid_row(_G1)
        fills = {_G1: [_fill(0, is_opening=True, order_id=order_id, quantity="0.001")]}
        repo = FakeGridRepo(open_grids=[row], fills_by_grid=fills)
        controller = _controller_for_recovery(pool, repo, order_repo=None)

        restored = await controller.restore_from_persistence()

        assert len(restored) == 1
        assert next(lv for lv in restored[0].levels if lv.index == 0).is_filled is True

    async def test_fill_references_unknown_level_index_excludes_grid(
        self, pool: ExchangePool
    ) -> None:
        """Ledger widerspricht der Parameter-Grid-Struktur (Level-Index
        existiert gar nicht bei grid_count=5) -> inkonsistenter DB-Zustand,
        fail-closed statt Crash/falsche Annahme."""
        row = _grid_row(_G1, grid_count=5)
        fills = {_G1: [_fill(99, is_opening=True)]}
        repo = FakeGridRepo(open_grids=[row], fills_by_grid=fills)
        controller = _controller_for_recovery(pool, repo)

        restored = await controller.restore_from_persistence()

        assert restored == []


class TestGridRecoveryIdempotencyAndNoResubmit:
    async def test_double_recovery_execution_yields_identical_state(
        self, pool: ExchangePool
    ) -> None:
        """GATE 11 / Anforderung 'Recovery zweimal hintereinander': ein
        zweiter restore_from_persistence()-Aufruf liefert denselben
        Zustand, keine Duplikate."""
        order_id = str(uuid4())
        row = _grid_row(_G1)
        fills = {_G1: [_fill(0, is_opening=True, order_id=order_id, quantity="0.001")]}
        order_repo = FakeOrderRepo({order_id: {"status": "filled"}})
        repo = FakeGridRepo(open_grids=[row], fills_by_grid=fills)
        controller = _controller_for_recovery(pool, repo, order_repo)

        first = await controller.restore_from_persistence()
        second = await controller.restore_from_persistence()

        assert len(first) == 1
        assert len(second) == 1
        assert first[0].levels[0].is_filled == second[0].levels[0].is_filled
        assert first[0].levels[0].quantity == second[0].levels[0].quantity

    async def test_recovery_never_places_any_order(
        self, pool: ExchangePool, adapter: MockExchangeAdapter
    ) -> None:
        """GATE 11: Recovery ist rein lesend - unter KEINEN Umstaenden
        wird waehrend restore_from_persistence() eine Exchange-Order
        platziert (kein blindes Resubmit, keine Duplikate)."""
        order_id = str(uuid4())
        row = _grid_row(_G1)
        fills = {_G1: [_fill(0, is_opening=True, order_id=order_id, quantity="0.001")]}
        order_repo = FakeOrderRepo({order_id: {"status": "filled"}})
        repo = FakeGridRepo(open_grids=[row], fills_by_grid=fills)
        controller = _controller_for_recovery(pool, repo, order_repo)

        await controller.restore_from_persistence()

        assert adapter.call_count("place_order") == 0

    async def test_restored_grid_resumes_normal_operation_via_price_tick(
        self, pool: ExchangePool, adapter: MockExchangeAdapter
    ) -> None:
        """GATE 10: ein wiederhergestelltes Grid ist danach voll
        funktionsfaehig - ein normaler on_price_tick()-Aufruf verarbeitet
        es wie jedes andere aktive Grid."""
        order_id = str(uuid4())
        # Preis-Level bei grid_lower=49000/grid_upper=51000/count=5,
        # arithmetisch: [49000, 49500, 50000, 50500, 51000] - Level-
        # Index 1 (Preis 49500) ist die Eintritts-Cell fuer einen
        # LONG-Crossing von 50000 nach 49500 (siehe on_price_tick()
        # Cell-Modell).
        row = _grid_row(_G1, last_price="49500")
        fills = {
            _G1: [_fill(1, is_opening=True, order_id=order_id, quantity="0.001", price="49500")]
        }
        order_repo = FakeOrderRepo({order_id: {"status": "filled"}})
        repo = FakeGridRepo(open_grids=[row], fills_by_grid=fills)
        controller = _controller_for_recovery(pool, repo, order_repo)

        restored = await controller.restore_from_persistence()
        grid_id = str(restored[0].id)

        # Preis steigt auf 50000 -> das wiederhergestellte, offene Level
        # 1 (49500) MUSS jetzt schliessen (Cell-Exit bei 50000).
        adapter.ticker_price = Decimal("50000")
        grid = await controller.on_price_tick(grid_id, Decimal("50000"))

        level_1 = next(lv for lv in grid.levels if lv.index == 1)
        assert level_1.is_filled is False  # erfolgreich geschlossen
        assert grid.realized_pnl > Decimal("0")


# ---------------------------------------------------------------------------
# Phase P: All-or-Nothing-Fill-Semantik (explizite Grid-Limitation)
# ---------------------------------------------------------------------------


class TestPartialFillHandling:
    """Phase 7 (2026-09-24): revidierte Einordnung nach kritischer Pruefung
    - MARKET-Orders KOENNEN auf Binance Futures real PARTIALLY_FILLED
    zurueckkommen (siehe _fill_level() Docstring). Ein Fill mit
    filled_quantity>0 wird IMMER mit der tatsaechlichen Menge verarbeitet,
    unabhaengig vom exakten Status-Enum-Wert. Nur filled_quantity==0
    bleibt ein vollstaendiger No-Op."""

    async def _open_grid_and_get_execution_double(self, controller, account):
        result = await controller.open_grid(
            _decision(), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )
        return result.grid

    def _result(
        self, order, status, filled_quantity=Decimal("0"), price=Decimal("49500")
    ) -> Any:
        from sgr.core.types import OrderResult

        return OrderResult(
            request_id=order.id,
            exchange_order_id="MOCK-RESULT",
            symbol=order.symbol,
            status=status,
            filled_quantity=filled_quantity,
            average_fill_price=price if filled_quantity > 0 else None,
            fees=(
                filled_quantity * price * Decimal("0.0005")
                if filled_quantity > 0
                else Decimal("0")
            ),
            submitted_at=datetime.now(tz=UTC),
            trading_mode=order.trading_mode,
        )

    async def test_zero_filled_quantity_causes_zero_state_mutation(
        self, controller: GridController, account
    ) -> None:
        """status=PARTIALLY_FILLED, aber filled_quantity==0 (Randfall,
        z.B. Exchange meldet den Zwischenstatus, bevor irgendetwas
        tatsaechlich gefuellt wurde) - bleibt korrekt ein No-Op."""
        from sgr.core.types import OrderStatus

        grid_before = await self._open_grid_and_get_execution_double(controller, account)
        fills_before = grid_before.fills_count
        levels_before = [(lv.index, lv.is_filled, lv.quantity) for lv in grid_before.levels]

        controller._execution.execute = AsyncMock(
            side_effect=lambda order, **kw: self._result(
                order, OrderStatus.PARTIALLY_FILLED, filled_quantity=Decimal("0")
            )
        )

        grid_after = await controller.on_price_tick(str(grid_before.id), Decimal("49500"))

        assert grid_after.fills_count == fills_before
        levels_after = [(lv.index, lv.is_filled, lv.quantity) for lv in grid_after.levels]
        assert levels_after == levels_before

    async def test_rejected_status_causes_zero_state_mutation(
        self, controller: GridController, account
    ) -> None:
        from sgr.core.types import OrderStatus

        grid_before = await self._open_grid_and_get_execution_double(controller, account)

        controller._execution.execute = AsyncMock(
            side_effect=lambda order, **kw: self._result(order, OrderStatus.REJECTED)
        )

        grid_after = await controller.on_price_tick(str(grid_before.id), Decimal("49500"))

        assert grid_after.fills_count == 0
        assert all(not lv.is_filled for lv in grid_after.levels)

    async def test_partial_open_fill_uses_actual_filled_quantity(
        self, controller: GridController, account
    ) -> None:
        """Level-Preis 49500 -> Level-Index 1 (siehe Recovery-Tests weiter
        oben fuer die Preis-Level-Herleitung). Exchange fuellt nur 60% der
        beabsichtigten Menge -> level.quantity MUSS die tatsaechliche
        (kleinere) Menge tragen, is_filled trotzdem True, net_position_qty
        nutzt die tatsaechliche Menge."""
        from sgr.core.types import OrderStatus

        grid_before = await self._open_grid_and_get_execution_double(controller, account)
        requested_qty = Decimal("50") / Decimal("49500")  # position_size / price
        partial_qty = requested_qty * Decimal("0.6")

        controller._execution.execute = AsyncMock(
            side_effect=lambda order, **kw: self._result(
                order, OrderStatus.PARTIALLY_FILLED, filled_quantity=partial_qty
            )
        )

        grid_after = await controller.on_price_tick(str(grid_before.id), Decimal("49500"))

        level_1 = next(lv for lv in grid_after.levels if lv.index == 1)
        assert level_1.is_filled is True
        assert level_1.quantity == partial_qty
        assert grid_after.net_position_qty == partial_qty
        assert grid_after.fills_count == 1

    async def test_partial_close_leaves_level_open_with_remainder(
        self, controller: GridController, account, adapter: MockExchangeAdapter
    ) -> None:
        """Level vollstaendig geoeffnet, dann NUR teilweise geschlossen -
        das Level muss mit der Restmenge offen bleiben (is_filled=True),
        nicht faelschlich als komplett geschlossen gelten."""
        result = await controller.open_grid(
            _decision(), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )
        grid = result.grid
        adapter.ticker_price = Decimal("49500")
        grid = await controller.on_price_tick(str(grid.id), Decimal("49500"))
        level_1 = next(lv for lv in grid.levels if lv.index == 1)
        full_qty = level_1.quantity
        assert full_qty > 0

        from sgr.core.types import OrderStatus

        partial_close_qty = full_qty * Decimal("0.4")
        controller._execution.execute = AsyncMock(
            side_effect=lambda order, **kw: self._result(
                order, OrderStatus.PARTIALLY_FILLED, filled_quantity=partial_close_qty,
                price=Decimal("50000"),
            )
        )

        grid_after = await controller.on_price_tick(str(grid.id), Decimal("50000"))

        level_1_after = next(lv for lv in grid_after.levels if lv.index == 1)
        assert level_1_after.is_filled is True  # weiterhin offen, nur kleiner
        assert level_1_after.quantity == full_qty - partial_close_qty
        assert grid_after.net_position_qty == full_qty - partial_close_qty

    async def test_full_partial_close_sequence_eventually_flattens_level(
        self, controller: GridController, account
    ) -> None:
        """Zwei aufeinanderfolgende Partial-Closes, die zusammen die
        volle Menge ergeben, muessen das Level am Ende korrekt schliessen
        (is_filled=False, quantity=0)."""
        from sgr.core.types import OrderStatus

        grid_before = await self._open_grid_and_get_execution_double(controller, account)
        full_qty = Decimal("50") / Decimal("49500")

        controller._execution.execute = AsyncMock(
            side_effect=lambda order, **kw: self._result(
                order, OrderStatus.FILLED, filled_quantity=full_qty
            )
        )
        grid = await controller.on_price_tick(str(grid_before.id), Decimal("49500"))
        level_1 = next(lv for lv in grid.levels if lv.index == 1)
        assert level_1.quantity == full_qty

        half = full_qty / 2
        controller._execution.execute = AsyncMock(
            side_effect=lambda order, **kw: self._result(
                order, OrderStatus.PARTIALLY_FILLED, filled_quantity=half, price=Decimal("50000")
            )
        )
        grid = await controller.on_price_tick(str(grid_before.id), Decimal("50000"))
        level_1 = next(lv for lv in grid.levels if lv.index == 1)
        assert level_1.is_filled is True
        assert level_1.quantity == full_qty - half

        # Preis muss wieder unter 50000 UND dann wieder ueber 50000 laufen,
        # damit _crossed() ein zweites Mal fuer dieselbe Cell ausloest -
        # direkter zweiter _fill_level()-Aufruf simuliert das ohne den
        # vollen Crossing-Zyklus nachzubilden (dieselbe Technik wie oben).
        remaining = level_1.quantity
        controller._execution.execute = AsyncMock(
            side_effect=lambda order, **kw: self._result(
                order, OrderStatus.FILLED, filled_quantity=remaining, price=Decimal("50000")
            )
        )
        await controller._fill_level(grid, level_1, Decimal("50000"), opening=False)

        level_1_final = next(lv for lv in grid.levels if lv.index == 1)
        assert level_1_final.is_filled is False
        assert level_1_final.quantity == Decimal("0")


# ---------------------------------------------------------------------------
# Phase J: Rate-Limiter-Integration in GridController
# ---------------------------------------------------------------------------


class TestRateLimiterIntegration:
    async def test_exhausted_budget_blocks_level_fill(
        self,
        pool: ExchangePool,
        compliance: ComplianceEngine,
        account,
        adapter: MockExchangeAdapter,
    ) -> None:
        from sgr.execution.grid_rate_limiter import GridRateLimiter

        class AlwaysRejectRedis:
            async def incr(self, key):
                return 999999  # weit ueber jedem Budget

            async def expire(self, key, seconds):
                return None

        engine = ExecutionEngine(pool, TradingMode.PAPER)
        rate_limiter = GridRateLimiter(AlwaysRejectRedis(), max_calls_per_window=1)
        controller = GridController(
            engine,
            TradingMode.PAPER,
            tenant_id="gordon",
            compliance_engine=compliance,
            rate_limiter=rate_limiter,
        )
        result = await controller.open_grid(
            _decision(), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )
        grid = result.grid

        adapter.ticker_price = Decimal("49500")
        grid_after = await controller.on_price_tick(str(grid.id), Decimal("49500"))

        assert all(not lv.is_filled for lv in grid_after.levels)
        assert grid_after.fills_count == 0
        assert adapter.call_count("place_order") == 0  # kein Retry-Sturm, kein Submit

    async def test_no_rate_limiter_injected_is_unaffected(
        self, controller: GridController, account, adapter: MockExchangeAdapter
    ) -> None:
        """Default (kein rate_limiter) - exaktes Vor-Aenderungs-Verhalten."""
        result = await controller.open_grid(
            _decision(), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )
        adapter.ticker_price = Decimal("49500")
        grid = await controller.on_price_tick(str(result.grid.id), Decimal("49500"))

        assert any(lv.is_filled for lv in grid.levels)


# ---------------------------------------------------------------------------
# Phase 8: Grid Mark-to-Market (unrealized PnL / Drawdown)
# ---------------------------------------------------------------------------


class TestGridMarkToMarket:
    async def test_unrealized_pnl_zero_before_any_fill(
        self, controller: GridController, account
    ) -> None:
        result = await controller.open_grid(
            _decision(), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )
        assert result.grid.unrealized_pnl == Decimal("0")
        assert result.grid.peak_value == Decimal("0")

    async def test_long_grid_rising_price_after_fill_shows_positive_unrealized_pnl(
        self, controller: GridController, account, adapter: MockExchangeAdapter
    ) -> None:
        result = await controller.open_grid(
            _decision(GridDirection.LONG), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )
        # Level 1 (49500) oeffnen (Preisabfall).
        adapter.ticker_price = Decimal("49500")
        grid = await controller.on_price_tick(str(result.grid.id), Decimal("49500"))
        assert grid.unrealized_pnl == Decimal("0")  # exakt am Entry-Preis noch 0

        # Preis steigt (OHNE das Level zu schliessen, z.B. 49800 < exit 50000)
        grid = await controller.on_price_tick(str(grid.id), Decimal("49800"))

        assert grid.unrealized_pnl > Decimal("0")  # LONG: Preis gestiegen -> Gewinn

    async def test_long_grid_falling_price_after_fill_shows_negative_unrealized_pnl(
        self, controller: GridController, account, adapter: MockExchangeAdapter
    ) -> None:
        result = await controller.open_grid(
            _decision(GridDirection.LONG), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )
        adapter.ticker_price = Decimal("49500")
        grid = await controller.on_price_tick(str(result.grid.id), Decimal("49500"))

        grid = await controller.on_price_tick(str(grid.id), Decimal("49100"))

        assert grid.unrealized_pnl < Decimal("0")  # LONG: Preis weiter gefallen -> Verlust

    async def test_short_grid_falling_price_after_fill_shows_positive_unrealized_pnl(
        self, controller: GridController, account, adapter: MockExchangeAdapter
    ) -> None:
        """SHORT-Grid: Preisverfall NACH Entry ist ein Gewinn (gespiegelt zu LONG)."""
        result = await controller.open_grid(
            _decision(GridDirection.SHORT), _symbol(), "futures_grid_short_v1", account,
            _snapshot(), current_price=Decimal("50000"),
        )
        # SHORT: Level oeffnet bei STEIGENDEM Preis (siehe on_price_tick()
        # Cell-Modell, gespiegelt zu LONG).
        adapter.ticker_price = Decimal("50500")
        grid = await controller.on_price_tick(str(result.grid.id), Decimal("50500"))
        assert any(lv.is_filled for lv in grid.levels)

        grid = await controller.on_price_tick(str(grid.id), Decimal("50200"))

        assert grid.unrealized_pnl > Decimal("0")  # SHORT: Preis gefallen -> Gewinn

    async def test_peak_value_tracks_monotonic_maximum(
        self, controller: GridController, account, adapter: MockExchangeAdapter
    ) -> None:
        result = await controller.open_grid(
            _decision(GridDirection.LONG), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )
        adapter.ticker_price = Decimal("49500")
        grid = await controller.on_price_tick(str(result.grid.id), Decimal("49500"))

        grid = await controller.on_price_tick(str(grid.id), Decimal("49900"))
        peak_after_rise = grid.peak_value
        assert peak_after_rise > Decimal("0")

        # Preis faellt wieder - peak_value darf NICHT sinken (monotones Maximum).
        grid = await controller.on_price_tick(str(grid.id), Decimal("49300"))

        assert grid.peak_value == peak_after_rise
        assert grid.unrealized_pnl < Decimal("0")

    async def test_unrealized_pnl_falls_to_zero_after_full_close(
        self, controller: GridController, account, adapter: MockExchangeAdapter
    ) -> None:
        result = await controller.open_grid(
            _decision(GridDirection.LONG), _symbol(), "futures_grid_long_v1", account, _snapshot(),
            current_price=Decimal("50000"),
        )
        adapter.ticker_price = Decimal("49500")
        grid = await controller.on_price_tick(str(result.grid.id), Decimal("49500"))

        closed = await controller.close_grid(str(grid.id), "manual_test", Decimal("49900"))

        assert closed.unrealized_pnl == Decimal("0")  # keine offene Exposure mehr
        assert closed.realized_pnl != Decimal("0")  # dafuer realisiert

    async def test_mark_to_market_restored_after_recovery(self, pool: ExchangePool) -> None:
        """peak_value darf nach einem Neustart NICHT auf 0 zurueckfallen -
        das wuerde die bisherige Drawdown-Historie verlieren."""
        row = _grid_row(_G1, last_price="49800")
        row["peak_value"] = Decimal("42")
        row["realized_pnl"] = Decimal("10")
        row["unrealized_pnl"] = Decimal("5")
        repo = FakeGridRepo(open_grids=[row], fills_by_grid={})
        controller = _controller_for_recovery(pool, repo)

        restored = await controller.restore_from_persistence()

        assert restored[0].peak_value == Decimal("42")
        assert restored[0].unrealized_pnl == Decimal("5")
