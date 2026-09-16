"""
Tests fuer sgr.risk.position_protection (Stop-Loss/Take-Profit/
Max-Holding-Time) - siehe Modul-Docstring dort fuer die Architektur
und die bewusste Einschraenkung (Watchdog-basiert statt native
Exchange-Orders, gleicher Mechanismus in PAPER und LIVE).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from sgr.core.config import RiskLimitsConfig, SGRConfig
from sgr.core.types import (
    ExchangeID,
    ExitReason,
    OrderResult,
    OrderStatus,
    Position,
    PositionSide,
    Symbol,
    TradingMode,
)
from sgr.risk.position_protection import PositionProtectionManager, PositionProtectionWatchdog


def _symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def _config(**risk_overrides: object) -> SGRConfig:
    return SGRConfig(
        trading_mode=TradingMode.PAPER,
        risk_limits=RiskLimitsConfig(**risk_overrides),  # type: ignore[arg-type]
    )


def _position(
    side: PositionSide = PositionSide.LONG,
    entry_price: Decimal = Decimal("50000"),
    current_price: Decimal = Decimal("50000"),
    opened_at: datetime | None = None,
    stop_loss_price: Decimal | None = None,
    take_profit_price: Decimal | None = None,
    max_holding_until: datetime | None = None,
) -> Position:
    return Position(
        symbol=_symbol(),
        side=side,
        quantity=Decimal("0.1"),
        entry_price=entry_price,
        current_price=current_price,
        opened_at=opened_at or datetime.now(tz=UTC),
        strategy_name="trend_following_v1",
        trading_mode=TradingMode.PAPER,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        max_holding_until=max_holding_until,
    )


def _filled_result(position: Position, price: Decimal) -> OrderResult:
    now = datetime.now(tz=UTC)
    return OrderResult(
        request_id=uuid4(),
        exchange_order_id=f"PAPER-{uuid4()}",
        symbol=position.symbol,
        status=OrderStatus.FILLED,
        filled_quantity=position.quantity,
        average_fill_price=price,
        fees=Decimal("1"),
        submitted_at=now,
        filled_at=now,
        trading_mode=TradingMode.PAPER,
    )


# ---------------------------------------------------------------------------
# PositionProtectionManager.on_position_opened
# ---------------------------------------------------------------------------


class TestOnPositionOpened:
    async def test_feature_off_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(protection_cutover_at=None),
        )
        manager = PositionProtectionManager()

        result = await manager.on_position_opened(_position())

        assert result is None

    async def test_legacy_position_before_cutover_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cutover = datetime.now(tz=UTC)
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(protection_cutover_at=cutover),
        )
        manager = PositionProtectionManager()
        legacy_position = _position(opened_at=cutover - timedelta(minutes=1))

        result = await manager.on_position_opened(legacy_position)

        assert result is None

    async def test_long_position_gets_sl_below_and_tp_above_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cutover = datetime.now(tz=UTC) - timedelta(minutes=1)
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(
                protection_cutover_at=cutover,
                stop_loss_pct=0.01,
                take_profit_pct=0.02,
                max_holding_minutes=30,
            ),
        )
        manager = PositionProtectionManager()
        position = _position(side=PositionSide.LONG, entry_price=Decimal("50000"))

        result = await manager.on_position_opened(position)

        assert result is not None
        assert result.stop_loss_price == Decimal("50000") * Decimal("0.99")
        assert result.take_profit_price == Decimal("50000") * Decimal("1.02")
        assert result.max_holding_until == position.opened_at + timedelta(minutes=30)

    async def test_short_position_gets_sl_above_and_tp_below_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cutover = datetime.now(tz=UTC) - timedelta(minutes=1)
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(
                protection_cutover_at=cutover,
                stop_loss_pct=0.01,
                take_profit_pct=0.02,
            ),
        )
        manager = PositionProtectionManager()
        position = _position(side=PositionSide.SHORT, entry_price=Decimal("50000"))

        result = await manager.on_position_opened(position)

        assert result is not None
        assert result.stop_loss_price == Decimal("50000") * Decimal("1.01")
        assert result.take_profit_price == Decimal("50000") * Decimal("0.98")


# ---------------------------------------------------------------------------
# PositionProtectionWatchdog.check_positions_once
# ---------------------------------------------------------------------------


def _make_watchdog(
    monkeypatch: pytest.MonkeyPatch,
    positions: list[Position],
    execute_result: OrderResult | None = None,
    execute_side_effect: Exception | None = None,
    **risk_overrides: object,
) -> tuple[PositionProtectionWatchdog, MagicMock, MagicMock]:
    risk_overrides.setdefault("protection_cutover_at", datetime.now(tz=UTC) - timedelta(days=1))
    monkeypatch.setattr(
        "sgr.risk.position_protection.get_config",
        lambda: _config(**risk_overrides),
    )

    portfolio = MagicMock()
    portfolio.positions = positions
    portfolio.on_order_filled = AsyncMock()

    execution = MagicMock()
    if execute_side_effect is not None:
        execution.execute = AsyncMock(side_effect=execute_side_effect)
    else:
        execution.execute = AsyncMock(return_value=execute_result)

    watchdog = PositionProtectionWatchdog(portfolio_engine=portfolio, execution_engine=execution)
    return watchdog, portfolio, execution


class TestMaxHoldingTimeExit:
    async def test_exit_triggered_when_deadline_passed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        position = _position(max_holding_until=datetime.now(tz=UTC) - timedelta(seconds=1))
        watchdog, portfolio, execution = _make_watchdog(
            monkeypatch,
            [position],
            execute_result=_filled_result(position, position.current_price),
        )

        await watchdog.check_positions_once()

        execution.execute.assert_awaited_once()
        submitted_order = execution.execute.await_args.args[0]
        assert submitted_order.reduce_only is True
        assert submitted_order.metadata["exit_reason"] == ExitReason.MAX_HOLDING_TIME.value
        assert execution.execute.await_args.kwargs["bypass_kill_switch"] is True
        portfolio.on_order_filled.assert_awaited_once()

    async def test_no_exit_before_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        position = _position(max_holding_until=datetime.now(tz=UTC) + timedelta(minutes=30))
        watchdog, _portfolio, execution = _make_watchdog(monkeypatch, [position])

        await watchdog.check_positions_once()

        execution.execute.assert_not_awaited()

    async def test_legacy_position_before_cutover_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cutover = datetime.now(tz=UTC)
        legacy_position = _position(
            opened_at=cutover - timedelta(days=1),
            max_holding_until=datetime.now(tz=UTC) - timedelta(seconds=1),
        )
        watchdog, _portfolio, execution = _make_watchdog(
            monkeypatch, [legacy_position], protection_cutover_at=cutover
        )

        await watchdog.check_positions_once()

        execution.execute.assert_not_awaited()

    async def test_feature_off_skips_all_positions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        position = _position(max_holding_until=datetime.now(tz=UTC) - timedelta(seconds=1))
        watchdog, _portfolio, execution = _make_watchdog(
            monkeypatch, [position], protection_cutover_at=None
        )

        await watchdog.check_positions_once()

        execution.execute.assert_not_awaited()


class TestStopLossTakeProfitExit:
    async def test_long_stop_loss_triggers_when_price_at_or_below(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        position = _position(
            side=PositionSide.LONG,
            entry_price=Decimal("50000"),
            current_price=Decimal("49500"),
            stop_loss_price=Decimal("49500"),
            take_profit_price=Decimal("51000"),
        )
        watchdog, portfolio, execution = _make_watchdog(
            monkeypatch,
            [position],
            execute_result=_filled_result(position, position.current_price),
        )

        await watchdog.check_positions_once()

        order = execution.execute.await_args.args[0]
        assert order.metadata["exit_reason"] == ExitReason.STOP_LOSS.value
        portfolio.on_order_filled.assert_awaited_once()

    async def test_long_take_profit_triggers_when_price_at_or_above(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        position = _position(
            side=PositionSide.LONG,
            entry_price=Decimal("50000"),
            current_price=Decimal("51000"),
            stop_loss_price=Decimal("49500"),
            take_profit_price=Decimal("51000"),
        )
        watchdog, _portfolio, execution = _make_watchdog(
            monkeypatch,
            [position],
            execute_result=_filled_result(position, position.current_price),
        )

        await watchdog.check_positions_once()

        order = execution.execute.await_args.args[0]
        assert order.metadata["exit_reason"] == ExitReason.TAKE_PROFIT.value

    async def test_short_stop_loss_triggers_when_price_at_or_above(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        position = _position(
            side=PositionSide.SHORT,
            entry_price=Decimal("50000"),
            current_price=Decimal("50500"),
            stop_loss_price=Decimal("50500"),
            take_profit_price=Decimal("49000"),
        )
        watchdog, _portfolio, execution = _make_watchdog(
            monkeypatch,
            [position],
            execute_result=_filled_result(position, position.current_price),
        )

        await watchdog.check_positions_once()

        order = execution.execute.await_args.args[0]
        assert order.metadata["exit_reason"] == ExitReason.STOP_LOSS.value

    async def test_short_take_profit_triggers_when_price_at_or_below(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        position = _position(
            side=PositionSide.SHORT,
            entry_price=Decimal("50000"),
            current_price=Decimal("49000"),
            stop_loss_price=Decimal("50500"),
            take_profit_price=Decimal("49000"),
        )
        watchdog, _portfolio, execution = _make_watchdog(
            monkeypatch,
            [position],
            execute_result=_filled_result(position, position.current_price),
        )

        await watchdog.check_positions_once()

        order = execution.execute.await_args.args[0]
        assert order.metadata["exit_reason"] == ExitReason.TAKE_PROFIT.value

    async def test_price_between_thresholds_does_not_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        position = _position(
            side=PositionSide.LONG,
            entry_price=Decimal("50000"),
            current_price=Decimal("50100"),
            stop_loss_price=Decimal("49500"),
            take_profit_price=Decimal("51000"),
        )
        watchdog, _portfolio, execution = _make_watchdog(monkeypatch, [position])

        await watchdog.check_positions_once()

        execution.execute.assert_not_awaited()


class TestKillSwitchBypassAndFailSafety:
    async def test_close_order_uses_bypass_kill_switch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bestehende Positionen muessen auch bei aktivem Kill Switch
        verwaltbar bleiben - siehe Modul-Docstring."""
        position = _position(max_holding_until=datetime.now(tz=UTC) - timedelta(seconds=1))
        watchdog, _portfolio, execution = _make_watchdog(
            monkeypatch,
            [position],
            execute_result=_filled_result(position, position.current_price),
        )

        await watchdog.check_positions_once()

        assert execution.execute.await_args.kwargs["bypass_kill_switch"] is True

    async def test_not_filled_result_does_not_call_on_order_filled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        position = _position(max_holding_until=datetime.now(tz=UTC) - timedelta(seconds=1))
        rejected = _filled_result(position, position.current_price).model_copy(
            update={"status": OrderStatus.REJECTED, "filled_quantity": Decimal("0")}
        )
        watchdog, portfolio, _execution = _make_watchdog(
            monkeypatch, [position], execute_result=rejected
        )

        await watchdog.check_positions_once()

        portfolio.on_order_filled.assert_not_awaited()

    async def test_execute_exception_is_swallowed_and_does_not_call_on_order_filled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        position = _position(max_holding_until=datetime.now(tz=UTC) - timedelta(seconds=1))
        watchdog, portfolio, _execution = _make_watchdog(
            monkeypatch, [position], execute_side_effect=ConnectionError("boom")
        )

        await watchdog.check_positions_once()

        portfolio.on_order_filled.assert_not_awaited()

    async def test_one_failing_position_does_not_block_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sequenzielle, per-Position isolierte Verarbeitung (siehe
        Modul-Docstring: PositionLiquidator-Muster) - ein Fehler bei
        Position A darf Position B nicht verhindern."""
        expired = _position(max_holding_until=datetime.now(tz=UTC) - timedelta(seconds=1))
        healthy = _position(max_holding_until=datetime.now(tz=UTC) + timedelta(minutes=30))

        cutover = datetime.now(tz=UTC) - timedelta(days=1)
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(protection_cutover_at=cutover),
        )

        portfolio = MagicMock()
        portfolio.positions = [expired, healthy]
        portfolio.on_order_filled = AsyncMock()

        execution = MagicMock()
        execution.execute = AsyncMock(side_effect=RuntimeError("exchange down"))

        watchdog = PositionProtectionWatchdog(
            portfolio_engine=portfolio, execution_engine=execution
        )

        # Darf nicht propagieren, obwohl execute() fuer die erste (und
        # einzige ausloesende) Position fehlschlaegt.
        await watchdog.check_positions_once()

        execution.execute.assert_awaited_once()
