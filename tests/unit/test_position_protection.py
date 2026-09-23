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
    MarketRegime,
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
    entry_regime: MarketRegime | None = None,
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
        entry_regime=entry_regime,
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


def _mock_kill_switch(
    *, is_active: bool, reason: str = "Open positions 8 exceeds max 8"
) -> MagicMock:
    ks = MagicMock()
    ks.is_active = is_active
    triggered_at = datetime(2026, 9, 22, 7, 23, 9, tzinfo=UTC)
    ks.state = MagicMock(reason=reason, triggered_at=triggered_at)
    ks.reset = AsyncMock()
    return ks


class TestPaperStressAutoRecovery:
    """
    Tests fuer PositionProtectionWatchdog.enable_paper_stress_auto_recovery()
    (2026-09-22, aggressive PAPER-Stress-Testphase) - schliesst die
    Luecke, dass der Kill Switch nach max_open_positions permanent
    haengen bleibt, obwohl alle ausloesenden Positionen laengst wieder
    geschlossen sind.
    """

    def test_refuses_to_enable_for_live_mode(self) -> None:
        """Anforderung 9: strikt PAPER-only, hart geprueft, nicht nur
        vom Aufrufer vorausgesetzt."""
        watchdog = PositionProtectionWatchdog(
            portfolio_engine=MagicMock(), execution_engine=MagicMock()
        )
        ks = _mock_kill_switch(is_active=True)

        watchdog.enable_paper_stress_auto_recovery(
            kill_switch=ks, trading_mode=TradingMode.LIVE, tenant_id="tenant-a"
        )

        assert watchdog._kill_switch is None

    async def test_does_not_reset_while_positions_still_open(self) -> None:
        """Anforderung 5: kein Reset, solange noch Positionen offen sind."""
        portfolio = MagicMock()
        portfolio.positions = [_position()]
        watchdog = PositionProtectionWatchdog(
            portfolio_engine=portfolio, execution_engine=MagicMock()
        )
        ks = _mock_kill_switch(is_active=True)
        watchdog.enable_paper_stress_auto_recovery(
            kill_switch=ks, trading_mode=TradingMode.PAPER, tenant_id="tenant-a"
        )

        await watchdog._maybe_paper_stress_auto_recover()

        ks.reset.assert_not_awaited()

    async def test_resets_when_active_and_zero_positions(self) -> None:
        """Anforderung 6/7: sobald 0 offene Positionen, automatischer
        Reset - Worker kann danach wieder neue Signale annehmen."""
        portfolio = MagicMock()
        portfolio.positions = []
        watchdog = PositionProtectionWatchdog(
            portfolio_engine=portfolio, execution_engine=MagicMock()
        )
        ks = _mock_kill_switch(is_active=True, reason="Open positions 8 exceeds max 8")
        watchdog.enable_paper_stress_auto_recovery(
            kill_switch=ks, trading_mode=TradingMode.PAPER, tenant_id="tenant-a"
        )

        await watchdog._maybe_paper_stress_auto_recover()

        ks.reset.assert_awaited_once_with(reset_by="paper_stress_auto_recovery")

    async def test_noop_when_kill_switch_not_active(self) -> None:
        portfolio = MagicMock()
        portfolio.positions = []
        watchdog = PositionProtectionWatchdog(
            portfolio_engine=portfolio, execution_engine=MagicMock()
        )
        ks = _mock_kill_switch(is_active=False)
        watchdog.enable_paper_stress_auto_recovery(
            kill_switch=ks, trading_mode=TradingMode.PAPER, tenant_id="tenant-a"
        )

        await watchdog._maybe_paper_stress_auto_recover()

        ks.reset.assert_not_awaited()

    async def test_idempotent_does_not_reset_twice_for_same_trigger(self) -> None:
        """Anforderung 13: wiederholte Watchdog-Ticks fuer denselben
        Trigger duerfen keinen zweiten reset()-Aufruf ausloesen, selbst
        wenn is_active (unrealistisch, aber als Grenzfall) zwischen den
        Ticks nicht auf False wechselt."""
        portfolio = MagicMock()
        portfolio.positions = []
        watchdog = PositionProtectionWatchdog(
            portfolio_engine=portfolio, execution_engine=MagicMock()
        )
        ks = _mock_kill_switch(is_active=True)
        watchdog.enable_paper_stress_auto_recovery(
            kill_switch=ks, trading_mode=TradingMode.PAPER, tenant_id="tenant-a"
        )

        await watchdog._maybe_paper_stress_auto_recover()
        await watchdog._maybe_paper_stress_auto_recover()

        ks.reset.assert_awaited_once()

    async def test_disabled_by_default_no_auto_recovery(self) -> None:
        """Ohne enable_paper_stress_auto_recovery() bleibt das Verhalten
        exakt wie zuvor - kein Reset, kein Zugriff auf einen Kill Switch."""
        portfolio = MagicMock()
        portfolio.positions = []
        watchdog = PositionProtectionWatchdog(
            portfolio_engine=portfolio, execution_engine=MagicMock()
        )

        await watchdog._maybe_paper_stress_auto_recover()

        assert watchdog._kill_switch is None

    async def test_positions_closed_count_resets_after_recovery(self) -> None:
        """Anforderung 14 (positions_closed im Audit-Log): der Zaehler
        wird nach einem erfolgreichen Auto-Recovery auf 0 zurueckgesetzt,
        damit der naechste Batch unabhaengig gezaehlt wird."""
        portfolio = MagicMock()
        portfolio.positions = []
        watchdog = PositionProtectionWatchdog(
            portfolio_engine=portfolio, execution_engine=MagicMock()
        )
        ks = _mock_kill_switch(is_active=True)
        watchdog.enable_paper_stress_auto_recovery(
            kill_switch=ks, trading_mode=TradingMode.PAPER, tenant_id="tenant-a"
        )
        watchdog._positions_closed_since_recovery = 8

        await watchdog._maybe_paper_stress_auto_recover()

        assert watchdog._positions_closed_since_recovery == 0


# ---------------------------------------------------------------------------
# Strategiegetriebener Exit (2026-09-23): target_price/stop_price aus
# order_metadata (result.raw_response der Entry-Order), Kosten-Guard,
# explizite Exit-Prioritaet, Regime-Exit-Luecke, Restart/Recovery-
# Unveraenderlichkeit. Siehe PositionProtectionManager/Watchdog Docstrings.
# ---------------------------------------------------------------------------


class TestStrategyDrivenExitPrices:
    """A, B, C, G, H, I: strategiegetriebene SL/TP-Uebernahme + Kosten-Guard."""

    async def test_a_strategy_target_price_is_used_when_plausible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cutover = datetime.now(tz=UTC) - timedelta(minutes=1)
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(protection_cutover_at=cutover, take_profit_pct=0.02),
        )
        manager = PositionProtectionManager()
        position = _position(side=PositionSide.LONG, entry_price=Decimal("100"))

        result = await manager.on_position_opened(
            position, {"target_price": "101.0", "stop_price": "99.0"}
        )

        assert result is not None
        # Strategie-Target (101.0, +1%) statt Config-Fallback (102.0, +2%).
        assert result.take_profit_price == Decimal("101.0")

    async def test_b_strategy_stop_price_is_used_when_plausible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cutover = datetime.now(tz=UTC) - timedelta(minutes=1)
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(protection_cutover_at=cutover, stop_loss_pct=0.01),
        )
        manager = PositionProtectionManager()
        position = _position(side=PositionSide.LONG, entry_price=Decimal("100"))

        result = await manager.on_position_opened(
            position, {"target_price": "101.0", "stop_price": "99.0"}
        )

        assert result is not None
        # Strategie-Stop (99.0, -1%) - identisch zum Config-Default hier,
        # aber ueber den strategy_metadata-Pfad gesetzt, nicht den Fallback
        # (siehe test_c_* fuer den expliziten Unterschied bei anderen Werten).
        assert result.stop_loss_price == Decimal("99.0")

    async def test_c_no_metadata_falls_back_to_global_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cutover = datetime.now(tz=UTC) - timedelta(minutes=1)
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(
                protection_cutover_at=cutover, stop_loss_pct=0.01, take_profit_pct=0.02
            ),
        )
        manager = PositionProtectionManager()
        position = _position(side=PositionSide.LONG, entry_price=Decimal("100"))

        result_none = await manager.on_position_opened(position, None)
        result_empty = await manager.on_position_opened(position, {})
        result_missing_stop = await manager.on_position_opened(position, {"target_price": "101"})

        for result in (result_none, result_empty, result_missing_stop):
            assert result is not None
            assert result.stop_loss_price == Decimal("100") * Decimal("0.99")
            assert result.take_profit_price == Decimal("100") * Decimal("1.02")

    async def test_implausible_metadata_falls_back_to_global_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ein Target auf der falschen Seite des Entries (z.B. durch den
        bekannten round(...,2)-Bug bei Mikro-Preis-Symbolen) darf nicht
        teilweise uebernommen werden - beide Werte verworfen, kompletter
        Fallback."""
        cutover = datetime.now(tz=UTC) - timedelta(minutes=1)
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(
                protection_cutover_at=cutover, stop_loss_pct=0.01, take_profit_pct=0.02
            ),
        )
        manager = PositionProtectionManager()
        position = _position(side=PositionSide.LONG, entry_price=Decimal("100"))

        # target_price rounds to 0.0 (der bekannte Mikro-Preis-Bug).
        result = await manager.on_position_opened(
            position, {"target_price": "0.0", "stop_price": "99.0"}
        )

        assert result is not None
        assert result.take_profit_price == Decimal("100") * Decimal("1.02")
        assert result.stop_loss_price == Decimal("100") * Decimal("0.99")

    async def test_g_cost_guard_clamps_target_below_breakeven(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round-Trip-Kosten bei Defaults: 2*(0.05%+0.05%) = 0.20%. Ein
        Strategie-Target bei nur +0.05% muss auf die Kostenschwelle
        angehoben werden, nicht blind uebernommen werden."""
        cutover = datetime.now(tz=UTC) - timedelta(minutes=1)
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(protection_cutover_at=cutover),
        )
        manager = PositionProtectionManager()
        entry = Decimal("100")
        position = _position(side=PositionSide.LONG, entry_price=entry)

        result = await manager.on_position_opened(
            position, {"target_price": "100.05", "stop_price": "99.0"}
        )

        assert result is not None
        expected_min = entry + entry * Decimal("0.0020")
        assert result.take_profit_price == expected_min
        assert result.take_profit_price > Decimal("100.05")

    async def test_cost_guard_does_not_touch_target_above_breakeven(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cutover = datetime.now(tz=UTC) - timedelta(minutes=1)
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(protection_cutover_at=cutover),
        )
        manager = PositionProtectionManager()
        position = _position(side=PositionSide.LONG, entry_price=Decimal("100"))

        # +1%, klar oberhalb der 0.20%-Kostenschwelle.
        result = await manager.on_position_opened(
            position, {"target_price": "101.0", "stop_price": "99.0"}
        )

        assert result is not None
        assert result.take_profit_price == Decimal("101.0")

    async def test_h_long_strategy_driven_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cutover = datetime.now(tz=UTC) - timedelta(minutes=1)
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(protection_cutover_at=cutover),
        )
        manager = PositionProtectionManager()
        position = _position(side=PositionSide.LONG, entry_price=Decimal("100"))

        result = await manager.on_position_opened(
            position, {"target_price": "105.0", "stop_price": "95.0"}
        )

        assert result is not None
        assert result.take_profit_price == Decimal("105.0")
        assert result.stop_loss_price == Decimal("95.0")

    async def test_i_short_strategy_driven_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cutover = datetime.now(tz=UTC) - timedelta(minutes=1)
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(protection_cutover_at=cutover),
        )
        manager = PositionProtectionManager()
        position = _position(side=PositionSide.SHORT, entry_price=Decimal("100"))

        # Short: target < entry < stop.
        result = await manager.on_position_opened(
            position, {"target_price": "95.0", "stop_price": "105.0"}
        )

        assert result is not None
        assert result.take_profit_price == Decimal("95.0")
        assert result.stop_loss_price == Decimal("105.0")

    async def test_i_short_implausible_direction_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Long-Werte (target > entry) an einer Short-Position uebergeben
        (Verwechslungs-/Bug-Schutz) - muss verworfen werden, nicht
        richtungswidrig uebernommen."""
        cutover = datetime.now(tz=UTC) - timedelta(minutes=1)
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(
                protection_cutover_at=cutover, stop_loss_pct=0.01, take_profit_pct=0.02
            ),
        )
        manager = PositionProtectionManager()
        position = _position(side=PositionSide.SHORT, entry_price=Decimal("100"))

        result = await manager.on_position_opened(
            position, {"target_price": "105.0", "stop_price": "95.0"}
        )

        assert result is not None
        assert result.take_profit_price == Decimal("100") * Decimal("0.98")
        assert result.stop_loss_price == Decimal("100") * Decimal("1.01")


class TestExplicitExitPriority:
    """D, E, F: TP vor SL, Regime-Exit-Luecke (dokumentiert, no-op),
    Max-Holding nur als Fallback."""

    async def test_d_take_profit_checked_before_stop_loss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Konstruierter Grenzfall: current_price erfuellt SOWOHL die
        SL- als auch die TP-Bedingung gleichzeitig (price(105) >=
        take_profit_price(102) UND price(105) <= stop_loss_price(108) -
        kann bei echten, monoton fallenden/steigenden Kursverlaeufen so
        nicht vorkommen, ist aber der praezise, deterministische Test
        fuer die Prioritaet selbst, unabhaengig vom Preis)."""
        position = _position(
            side=PositionSide.LONG,
            entry_price=Decimal("100"),
            current_price=Decimal("105"),
            stop_loss_price=Decimal("108"),
            take_profit_price=Decimal("102"),
        )

        watchdog, portfolio, execution = _make_watchdog(monkeypatch, [position])

        await watchdog.check_positions_once()

        execution.execute.assert_awaited_once()
        submitted_order = execution.execute.await_args.args[0]
        assert submitted_order.metadata["exit_reason"] == ExitReason.TAKE_PROFIT.value

    async def test_e_regime_exit_noop_without_feature_store(self) -> None:
        """Ohne injizierten feature_store (Default None) bleibt der Check
        ein reiner No-Op - unveraendertes Verhalten fuer jeden Aufrufer,
        der diesen optionalen Parameter nicht setzt."""
        watchdog = PositionProtectionWatchdog(
            portfolio_engine=MagicMock(), execution_engine=MagicMock()
        )
        long_pos = _position(side=PositionSide.LONG, entry_regime=MarketRegime.RANGING)
        short_pos = _position(side=PositionSide.SHORT, entry_regime=MarketRegime.RANGING)

        assert await watchdog._check_regime_exit(long_pos) is None
        assert await watchdog._check_regime_exit(short_pos) is None

    async def test_e_regime_exit_noop_without_entry_regime(self) -> None:
        """Legacy-Position ohne entry_regime (vor Migration 0008 oder
        Strategie ohne Regime-Bezug) - kein Exit, auch mit feature_store."""
        store = AsyncMock()
        store.get_latest_regime.return_value = MarketRegime.TRENDING_UP
        watchdog = PositionProtectionWatchdog(
            portfolio_engine=MagicMock(), execution_engine=MagicMock(), feature_store=store
        )
        position = _position(entry_regime=None)

        assert await watchdog._check_regime_exit(position) is None
        store.get_latest_regime.assert_not_called()

    async def test_e_regime_exit_noop_for_non_ranging_entry(self) -> None:
        """entry_regime != RANGING (z.B. trend_following_v1-Entry) -> Check
        bleibt strukturell No-Op, kein aktueller Regime-Lookup noetig."""
        store = AsyncMock()
        watchdog = PositionProtectionWatchdog(
            portfolio_engine=MagicMock(), execution_engine=MagicMock(), feature_store=store
        )
        position = _position(entry_regime=MarketRegime.TRENDING_UP)

        assert await watchdog._check_regime_exit(position) is None
        store.get_latest_regime.assert_not_called()

    async def test_e_regime_exit_fires_long_when_regime_changes(self) -> None:
        """entry_regime=RANGING, aktuelles Regime TRENDING_UP -> REGIME_CHANGE."""
        store = AsyncMock()
        store.get_latest_regime.return_value = MarketRegime.TRENDING_UP
        watchdog = PositionProtectionWatchdog(
            portfolio_engine=MagicMock(), execution_engine=MagicMock(), feature_store=store
        )
        position = _position(side=PositionSide.LONG, entry_regime=MarketRegime.RANGING)

        assert await watchdog._check_regime_exit(position) == ExitReason.REGIME_CHANGE

    async def test_e_regime_exit_fires_short_when_regime_changes(self) -> None:
        """Identisch fuer Short - Regime-Exit ist richtungsunabhaengig."""
        store = AsyncMock()
        store.get_latest_regime.return_value = MarketRegime.TRENDING_DOWN
        watchdog = PositionProtectionWatchdog(
            portfolio_engine=MagicMock(), execution_engine=MagicMock(), feature_store=store
        )
        position = _position(side=PositionSide.SHORT, entry_regime=MarketRegime.RANGING)

        assert await watchdog._check_regime_exit(position) == ExitReason.REGIME_CHANGE

    async def test_e_regime_exit_noop_when_still_ranging(self) -> None:
        """Regime unveraendert (weiterhin RANGING) -> kein Exit."""
        store = AsyncMock()
        store.get_latest_regime.return_value = MarketRegime.RANGING
        watchdog = PositionProtectionWatchdog(
            portfolio_engine=MagicMock(), execution_engine=MagicMock(), feature_store=store
        )
        position = _position(entry_regime=MarketRegime.RANGING)

        assert await watchdog._check_regime_exit(position) is None

    async def test_e_regime_exit_noop_on_missing_current_regime(self) -> None:
        """FeatureStore liefert None (z.B. direkt nach Neustart, bevor der
        erste Candle verarbeitet wurde) -> KEIN Exit (fehlende Daten
        duerfen nie einen Exit erzwingen, keine stille Fallback-Annahme)."""
        store = AsyncMock()
        store.get_latest_regime.return_value = None
        watchdog = PositionProtectionWatchdog(
            portfolio_engine=MagicMock(), execution_engine=MagicMock(), feature_store=store
        )
        position = _position(entry_regime=MarketRegime.RANGING)

        assert await watchdog._check_regime_exit(position) is None

    async def test_e_regime_exit_noop_on_lookup_exception(self) -> None:
        """Ein Fehler beim Redis-Lookup darf niemals propagieren oder
        faelschlich einen Exit ausloesen - fail-safe, kein Trigger."""
        store = AsyncMock()
        store.get_latest_regime.side_effect = RuntimeError("redis down")
        watchdog = PositionProtectionWatchdog(
            portfolio_engine=MagicMock(), execution_engine=MagicMock(), feature_store=store
        )
        position = _position(entry_regime=MarketRegime.RANGING)

        assert await watchdog._check_regime_exit(position) is None

    async def test_manager_extracts_entry_regime_from_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PositionProtectionManager.on_position_opened() setzt
        entry_regime aus order_metadata["entry_regime"] (siehe
        RiskEngine.build_order_request())."""
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(protection_cutover_at=datetime(2020, 1, 1, tzinfo=UTC)),
        )
        manager = PositionProtectionManager()
        position = _position()

        result = await manager.on_position_opened(
            position, {"strategy": "mean_reversion_v1", "entry_regime": "ranging"}
        )

        assert result is not None
        assert result.entry_regime == MarketRegime.RANGING

    async def test_manager_entry_regime_none_without_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(protection_cutover_at=datetime(2020, 1, 1, tzinfo=UTC)),
        )
        manager = PositionProtectionManager()
        position = _position()

        result = await manager.on_position_opened(position, None)

        assert result is not None
        assert result.entry_regime is None

    async def test_f_max_holding_time_only_fires_when_nothing_else_triggered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deadline bereits ueberschritten UND Preis erfuellt gleichzeitig
        die TP-Bedingung -> TP gewinnt, nicht Max-Holding-Time."""
        position = _position(
            side=PositionSide.LONG,
            entry_price=Decimal("100"),
            current_price=Decimal("105"),
            take_profit_price=Decimal("102"),
            max_holding_until=datetime.now(tz=UTC) - timedelta(seconds=1),
        )
        watchdog, _portfolio, execution = _make_watchdog(monkeypatch, [position])

        await watchdog.check_positions_once()

        submitted_order = execution.execute.await_args.args[0]
        assert submitted_order.metadata["exit_reason"] == ExitReason.TAKE_PROFIT.value

    async def test_f_max_holding_time_fires_when_nothing_else_applies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        position = _position(
            side=PositionSide.LONG,
            entry_price=Decimal("100"),
            current_price=Decimal("100"),
            stop_loss_price=Decimal("90"),
            take_profit_price=Decimal("110"),
            max_holding_until=datetime.now(tz=UTC) - timedelta(seconds=1),
        )
        watchdog, _portfolio, execution = _make_watchdog(monkeypatch, [position])

        await watchdog.check_positions_once()

        submitted_order = execution.execute.await_args.args[0]
        assert submitted_order.metadata["exit_reason"] == ExitReason.MAX_HOLDING_TIME.value


class TestRestartRecoveryPreservesExitParameters:
    """J: Restart/Recovery veraendert die Exit-Parameter nicht."""

    def test_position_from_row_reconstructs_exit_fields_verbatim(self) -> None:
        """PortfolioEngine._position_from_row() (Recovery-Pfad) liest
        stop_loss_price/take_profit_price/max_holding_until direkt aus
        der persistierten DB-Zeile - kein erneuter Aufruf von
        on_position_opened(), also keine Neuberechnung/Verschiebung der
        beim Entry einmalig gesetzten Werte."""
        from sgr.portfolio.engine import PortfolioEngine

        opened_at = datetime.now(tz=UTC) - timedelta(minutes=10)
        max_holding = opened_at + timedelta(minutes=30)
        row = {
            "id": uuid4(),
            "symbol": "BTC/USDT",
            "exchange": "binance",
            "side": "long",
            "quantity": Decimal("0.1"),
            "entry_price": Decimal("100"),
            "current_price": Decimal("101"),
            "leverage": Decimal("1"),
            "unrealized_pnl": Decimal("0.1"),
            "realized_pnl": Decimal("0"),
            "opened_at": opened_at,
            "strategy_name": "mean_reversion_v1",
            "trading_mode": "paper",
            "stop_loss_price": Decimal("95"),
            "take_profit_price": Decimal("105"),
            "max_holding_until": max_holding,
            "sl_order_id": None,
            "tp_order_id": None,
            "entry_regime": "ranging",
        }

        position = PortfolioEngine._position_from_row(row)

        assert position.stop_loss_price == Decimal("95")
        assert position.take_profit_price == Decimal("105")
        assert position.max_holding_until == max_holding
        assert position.entry_regime == MarketRegime.RANGING

    def test_position_from_row_entry_regime_none_when_absent(self) -> None:
        """Legacy-Zeile ohne entry_regime-Spalte (vor Migration 0008) oder
        NULL -> entry_regime bleibt None, kein Fehler."""
        from sgr.portfolio.engine import PortfolioEngine

        opened_at = datetime.now(tz=UTC) - timedelta(minutes=10)
        row = {
            "id": uuid4(),
            "symbol": "BTC/USDT",
            "exchange": "binance",
            "side": "long",
            "quantity": Decimal("0.1"),
            "entry_price": Decimal("100"),
            "current_price": Decimal("101"),
            "leverage": Decimal("1"),
            "unrealized_pnl": Decimal("0.1"),
            "realized_pnl": Decimal("0"),
            "opened_at": opened_at,
            "strategy_name": "trend_following_v1",
            "trading_mode": "paper",
            "stop_loss_price": None,
            "take_profit_price": None,
            "max_holding_until": None,
            "sl_order_id": None,
            "tp_order_id": None,
        }

        position = PortfolioEngine._position_from_row(row)

        assert position.entry_regime is None

    async def test_on_position_opened_is_deterministic_given_same_inputs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zusaetzliche Absicherung: on_position_opened() ist eine reine
        Funktion ihrer Eingaben (Position + order_metadata + Config) -
        zweimaliger Aufruf mit identischen Eingaben (simuliert einen
        hypothetischen erneuten Aufruf nach einem Neustart) liefert
        identische Exit-Parameter, keine Drift."""
        cutover = datetime.now(tz=UTC) - timedelta(minutes=1)
        monkeypatch.setattr(
            "sgr.risk.position_protection.get_config",
            lambda: _config(protection_cutover_at=cutover),
        )
        manager = PositionProtectionManager()
        position = _position(side=PositionSide.LONG, entry_price=Decimal("100"))
        metadata = {"target_price": "105.0", "stop_price": "95.0"}

        first = await manager.on_position_opened(position, metadata)
        second = await manager.on_position_opened(position, metadata)

        assert first is not None and second is not None
        assert first.stop_loss_price == second.stop_loss_price
        assert first.take_profit_price == second.take_profit_price
        assert first.max_holding_until == second.max_holding_until
