"""
Tenant-Isolation für Futures Grid (siehe Aufgabenstellung "TESTS":
Tenant Isolation).

Zwei GridController-Instanzen (ein Prozess pro Tenant, siehe bestehendes
Multi-Tenant-Muster in sgr/risk/kill_switch.py Modul-Docstring) duerfen
sich niemals gegenseitige Grids sehen oder beeinflussen koennen.
"""

from __future__ import annotations

from decimal import Decimal

from sgr.compliance.engine import ComplianceEngine
from sgr.compliance.types import AccountEligibility, ProductAvailabilityRule
from sgr.core.grid_types import FuturesGridParameters, GridDecision
from sgr.core.types import ExchangeID, GridDirection, ProductType, Symbol, TradingMode
from sgr.exchanges.factory import ExchangePool
from sgr.execution.engine import ExecutionEngine
from sgr.execution.grid_controller import GridController
from sgr.risk.grid_risk import GridPortfolioSnapshot
from tests.mocks.mock_exchange import MockExchangeAdapter


def _compliance() -> ComplianceEngine:
    engine = ComplianceEngine()
    engine.register_rule(
        ProductAvailabilityRule(
            exchange=ExchangeID.BINANCE,
            product_type=ProductType.FUTURES_GRID,
            jurisdictions_allowed=["DE"],
        )
    )
    return engine


def _account(tenant_id: str) -> AccountEligibility:
    return AccountEligibility(
        tenant_id=tenant_id,
        jurisdiction="DE",
        kyc_verified=True,
        futures_trading_enabled=True,
        risk_disclosure_acknowledged=True,
        enabled_product_types=["futures_grid"],
    )


def _params() -> FuturesGridParameters:
    return FuturesGridParameters(
        grid_lower_price=Decimal("49000"),
        grid_upper_price=Decimal("51000"),
        grid_count=5,
        long_or_short=GridDirection.LONG,
        leverage=Decimal("2"),
        position_size=Decimal("50"),
        max_notional=Decimal("250"),
    )


async def test_two_tenant_controllers_do_not_share_grid_state() -> None:
    adapter = MockExchangeAdapter(trading_mode=TradingMode.PAPER)
    await adapter.connect()
    pool = ExchangePool()
    pool._adapters[(ExchangeID.BINANCE, TradingMode.PAPER)] = adapter

    execution_engine = ExecutionEngine(pool, TradingMode.PAPER)
    compliance = _compliance()

    gordon_controller = GridController(
        execution_engine, TradingMode.PAPER, tenant_id="gordon", compliance_engine=compliance
    )
    sumo_controller = GridController(
        execution_engine, TradingMode.PAPER, tenant_id="sumo", compliance_engine=compliance
    )

    symbol = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)
    decision = GridDecision(
        direction=GridDirection.LONG, parameters=_params(), confidence=0.7, reasons=[]
    )
    snapshot = GridPortfolioSnapshot(open_grids=[], portfolio_value=Decimal("10000"))

    gordon_result = await gordon_controller.open_grid(
        decision,
        symbol,
        "futures_grid_long_v1",
        _account("gordon"),
        snapshot,
        current_price=Decimal("50000"),
    )
    assert gordon_result.approved is True
    gordon_grid_id = str(gordon_result.grid.id)

    # Sumo's Controller kennt Gordons Grid nicht.
    assert sumo_controller.get_grid(gordon_grid_id) is None
    assert sumo_controller.active_grids() == []
    assert gordon_controller.get_grid(gordon_grid_id) is not None

    # Gordons Grid traegt seine eigene tenant_id, nicht Sumos.
    assert gordon_result.grid.tenant_id == "gordon"

    sumo_result = await sumo_controller.open_grid(
        decision,
        symbol,
        "futures_grid_long_v1",
        _account("sumo"),
        snapshot,
        current_price=Decimal("50000"),
    )
    assert sumo_result.approved is True
    assert sumo_result.grid.tenant_id == "sumo"
    assert sumo_result.grid.id != gordon_result.grid.id

    # Gordons Sicht bleibt unveraendert - ein Fill in Sumos Grid darf
    # Gordons Grid-Zustand nicht beeinflussen.
    adapter.ticker_price = Decimal("49500")
    await sumo_controller.on_price_tick(str(sumo_result.grid.id), Decimal("49500"))

    gordon_grid_after = gordon_controller.get_grid(gordon_grid_id)
    assert gordon_grid_after.fills_count == 0
    assert all(not lv.is_filled for lv in gordon_grid_after.levels)
