"""
Vollstaendiger Futures-Grid-Paper-Trading-Lifecycle-Test (siehe
Aufgabenstellung "PAPER TRADING"):

    Grid erstellen -> Grid aktivieren -> Grid Orders erzeugen ->
    Order Fill simulieren -> Position erhoehen -> Position reduzieren ->
    Grid Level wechseln -> Grid schliessen -> Position schliessen ->
    Performance berechnen.

Nutzt DENSELBEN ExecutionEngine-Pfad wie jede andere SGR-Order (siehe
GridController-Modul-Docstring: "keine Fake Shortcut Execution") - das
ist der zentrale Beleg dafuer, dass Paper Trading fuer Futures Grid
keine Sonderloesung ist.
"""

from __future__ import annotations

from decimal import Decimal

from sgr.compliance.engine import ComplianceEngine
from sgr.compliance.types import AccountEligibility, ProductAvailabilityRule
from sgr.core.grid_types import FuturesGridParameters, GridDecision
from sgr.core.types import ExchangeID, GridDirection, GridStatus, ProductType, Symbol, TradingMode
from sgr.exchanges.factory import ExchangePool
from sgr.execution.engine import ExecutionEngine
from sgr.execution.grid_controller import GridController
from sgr.risk.grid_risk import GridPortfolioSnapshot
from tests.mocks.mock_exchange import MockExchangeAdapter


async def test_full_paper_trading_grid_lifecycle() -> None:
    # --- Setup: derselbe ExecutionEngine/ExchangePool-Pfad wie live ---
    adapter = MockExchangeAdapter(trading_mode=TradingMode.PAPER)
    await adapter.connect()
    pool = ExchangePool()
    pool._adapters[(ExchangeID.BINANCE, TradingMode.PAPER)] = adapter

    execution_engine = ExecutionEngine(pool, TradingMode.PAPER)
    compliance = ComplianceEngine()
    compliance.register_rule(
        ProductAvailabilityRule(
            exchange=ExchangeID.BINANCE,
            product_type=ProductType.FUTURES_GRID,
            jurisdictions_allowed=["DE"],
        )
    )
    controller = GridController(
        execution_engine, TradingMode.PAPER, tenant_id="gordon", compliance_engine=compliance
    )
    account = AccountEligibility(
        tenant_id="gordon",
        jurisdiction="DE",
        kyc_verified=True,
        futures_trading_enabled=True,
        risk_disclosure_acknowledged=True,
        enabled_product_types=["futures_grid"],
    )
    symbol = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)

    # --- 1. Grid erstellen + aktivieren ---
    params = FuturesGridParameters(
        grid_lower_price=Decimal("49000"),
        grid_upper_price=Decimal("51000"),
        grid_count=5,
        long_or_short=GridDirection.LONG,
        leverage=Decimal("2"),
        position_size=Decimal("50"),
        max_notional=Decimal("250"),
        stop_loss=Decimal("40000"),
    )
    decision = GridDecision(
        direction=GridDirection.LONG, parameters=params, confidence=0.7, reasons=["test"]
    )
    open_result = await controller.open_grid(
        decision,
        symbol,
        "futures_grid_long_v1",
        account,
        GridPortfolioSnapshot(open_grids=[], portfolio_value=Decimal("10000")),
        current_price=Decimal("50000"),
    )
    assert open_result.approved is True
    grid = open_result.grid
    assert grid.status == GridStatus.ACTIVE

    # --- 2. Grid Orders erzeugen + Order Fill simulieren (Position erhoehen) ---
    adapter.ticker_price = Decimal("49500")
    grid = await controller.on_price_tick(str(grid.id), Decimal("49500"))
    assert grid.fills_count == 1
    assert grid.net_position_qty > 0  # Position erhoeht (Long-Entry)
    assert adapter.call_count("place_order") == 1

    adapter.ticker_price = Decimal("49000")
    grid = await controller.on_price_tick(str(grid.id), Decimal("49000"))
    assert grid.fills_count == 2
    assert grid.net_position_qty > 0

    # --- 3. Grid Level wechseln + Position reduzieren (Preis erholt sich) ---
    adapter.ticker_price = Decimal("49750")
    grid = await controller.on_price_tick(str(grid.id), Decimal("49750"))
    assert grid.fills_count == 3
    filled_levels_after_partial_close = [lv.is_filled for lv in grid.levels]
    assert False in filled_levels_after_partial_close  # mindestens ein Level wieder frei

    # --- 4. Grid Level erneut wechseln (voller Round-Trip) ---
    adapter.ticker_price = Decimal("50250")
    grid = await controller.on_price_tick(str(grid.id), Decimal("50250"))
    assert grid.fills_count == 4
    assert abs(grid.net_position_qty) < Decimal("0.0000001")  # Position vollstaendig ausgeglichen
    assert grid.realized_pnl > 0  # Performance berechnet: Grid Capture realisiert

    # --- 5. Grid schliessen (Position schliessen, falls noch etwas offen) ---
    closed_grid = await controller.close_grid(str(grid.id), "manual_close", Decimal("50250"))
    assert closed_grid.status == GridStatus.CLOSED
    assert closed_grid.closed_at is not None
    assert all(not lv.is_filled for lv in closed_grid.levels)

    # --- 6. Performance ist nachvollziehbar (Fees + PnL konsistent) ---
    assert closed_grid.fees_paid > 0
    assert closed_grid.fills_count == 4
