"""
Unit-Tests für ExecutionEngine.

Kritischste Komponente im Order-Pfad: Risk Engine -> Execution Engine ->
Exchange. Jeder Fehlerfall hier hat direkte finanzielle Konsequenzen.

Teststrategie:
    1. Sanity Checks: trading_mode Mismatch
    2. Kill Switch: Block vor Submission, Block waehrend Fill-Monitoring
    3. Sofort-Fill-Pfad (Market Order / Paper Mode)
    4. Fill-Monitoring: Polling, Timeout -> Cancel, Fehler-Toleranz beim Polling
    5. Fail-Safe: jede unerwartete Exception -> REJECTED (kein uncontrolled State)
    6. Event-Publish-Fehler duerfen den Fill selbst nicht verhindern (best-effort)

Kill Switch wird NICHT ueber den globalen Singleton (get_kill_switch) getestet,
sondern per Dependency-Injection auf engine._kill_switch ersetzt: der globale
Singleton ist pro TradingMode geteilter State (_kill_switches dict in
kill_switch.py) und wuerde bei paralleler Testausfuehrung zu Test-Leakage
fuehren. Das eigentliche KillSwitch-Verhalten selbst ist bereits in
test_risk_engine.py::TestKillSwitch vollstaendig abgedeckt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from sgr.core.types import (
    ExchangeID,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
    Symbol,
    TradingMode,
)
from sgr.exchanges.base import ExchangeError
from sgr.execution.engine import ExecutionEngine

if TYPE_CHECKING:
    import pytest_mock


def _make_symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def _make_order_request(
    order_type: OrderType = OrderType.MARKET,
    trading_mode: TradingMode = TradingMode.PAPER,
) -> OrderRequest:
    from uuid import uuid4

    return OrderRequest(
        signal_id=uuid4(),
        symbol=_make_symbol(),
        side=Side.BUY,
        order_type=order_type,
        quantity=Decimal("0.1"),
        trading_mode=trading_mode,
    )


def _make_order_result(
    order: OrderRequest,
    status: OrderStatus = OrderStatus.FILLED,
    exchange_order_id: str = "EX-1",
) -> OrderResult:
    return OrderResult(
        request_id=order.id,
        exchange_order_id=exchange_order_id,
        symbol=order.symbol,
        status=status,
        filled_quantity=order.quantity if status == OrderStatus.FILLED else Decimal("0"),
        average_fill_price=Decimal("50000") if status == OrderStatus.FILLED else None,
        fees=Decimal("5"),
        submitted_at=datetime.now(tz=UTC),
        trading_mode=order.trading_mode,
        raw_response={},
    )


class _DelayedActiveKillSwitch:
    """
    Test-Double: is_active liefert False fuer die ersten N Checks, danach True.
    Simuliert einen Kill Switch, der WAEHREND des Fill-Monitorings aktiviert
    wird (nicht schon vor der Submission).
    """

    def __init__(self, activate_after_checks: int) -> None:
        self._checks = 0
        self._activate_after = activate_after_checks

    @property
    def is_active(self) -> bool:
        self._checks += 1
        return self._checks > self._activate_after


@pytest.fixture
def mock_pool(mocker: pytest_mock.MockerFixture) -> tuple[MagicMock, AsyncMock]:
    pool = mocker.Mock()
    adapter = mocker.AsyncMock()
    pool.get = mocker.Mock(return_value=adapter)
    return pool, adapter


@pytest.fixture
def engine(mock_pool: tuple[MagicMock, AsyncMock]) -> ExecutionEngine:
    pool, _adapter = mock_pool
    eng = ExecutionEngine(pool, TradingMode.PAPER)
    # Kill Switch per Dependency-Injection ersetzen statt globalen Singleton
    # zu nutzen (siehe Modul-Docstring).
    fake_kill_switch = MagicMock()
    fake_kill_switch.is_active = False
    eng._kill_switch = fake_kill_switch
    return eng


class TestTradingModeSanityCheck:
    async def test_mismatched_trading_mode_raises(self, engine: ExecutionEngine) -> None:
        """Order fuer LIVE an eine PAPER-Engine -> ValueError, kein stiller Fallback."""
        order = _make_order_request(trading_mode=TradingMode.LIVE)
        with pytest.raises(ValueError, match="does not match engine mode"):
            await engine.execute(order)


class TestKillSwitchBlocking:
    async def test_active_kill_switch_blocks_before_submission(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """Kill Switch aktiv -> Order wird NICHT an die Exchange geschickt."""
        _pool, adapter = mock_pool
        # is_active ist bei echtem KillSwitch read-only (by design, siehe
        # sgr/risk/kill_switch.py). engine.__init__ hat aber ein Test-Double
        # (MagicMock) injiziert, nicht die echte Klasse - dort ist die
        # Zuweisung gueltig. mypy kennt nur den statischen Typ (KillSwitch).
        engine._kill_switch.is_active = True  # type: ignore[misc]
        order = _make_order_request()

        result = await engine.execute(order)

        assert result.status == OrderStatus.REJECTED
        assert "Kill switch active" in result.raw_response["rejection_reason"]
        adapter.place_order.assert_not_awaited()

    async def test_kill_switch_activated_during_monitoring_cancels_order(
        self,
        engine: ExecutionEngine,
        mock_pool: tuple[MagicMock, AsyncMock],
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Kill Switch feuert WAEHREND Fill-Monitoring -> Order wird gecancelt."""
        _pool, adapter = mock_pool
        order = _make_order_request(order_type=OrderType.LIMIT)
        submitted = _make_order_result(order, status=OrderStatus.SUBMITTED)
        adapter.place_order = AsyncMock(return_value=submitted)
        adapter.cancel_order = AsyncMock(return_value=True)
        mocker.patch("asyncio.sleep", new=AsyncMock())

        # Kill Switch wird erst NACH der ersten Submission aktiv. Test-Double
        # statt echter KillSwitch-Instanz - siehe _DelayedActiveKillSwitch.
        engine._kill_switch = _DelayedActiveKillSwitch(activate_after_checks=1)  # type: ignore[assignment]

        result = await engine.execute(order)

        adapter.cancel_order.assert_awaited_once()
        assert result.status == OrderStatus.SUBMITTED  # unveraendert zurueckgegeben


class TestImmediateFill:
    async def test_market_order_immediate_fill_returns_filled_result(
        self,
        engine: ExecutionEngine,
        mock_pool: tuple[MagicMock, AsyncMock],
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        _pool, adapter = mock_pool
        order = _make_order_request(order_type=OrderType.MARKET)
        filled = _make_order_result(order, status=OrderStatus.FILLED)
        adapter.place_order = AsyncMock(return_value=filled)
        mocker.patch("sgr.core.event_bus.get_event_bus")

        result = await engine.execute(order)

        assert result.status == OrderStatus.FILLED
        assert result.filled_quantity == order.quantity

    async def test_immediate_fill_publishes_order_filled_event(
        self,
        engine: ExecutionEngine,
        mock_pool: tuple[MagicMock, AsyncMock],
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        _pool, adapter = mock_pool
        order = _make_order_request()
        filled = _make_order_result(order, status=OrderStatus.FILLED)
        adapter.place_order = AsyncMock(return_value=filled)
        bus = mocker.Mock()
        bus.publish = AsyncMock()
        mocker.patch("sgr.execution.engine.get_event_bus", return_value=bus)

        await engine.execute(order)

        bus.publish.assert_awaited_once()


class TestFillMonitoring:
    async def test_polls_until_filled(
        self,
        engine: ExecutionEngine,
        mock_pool: tuple[MagicMock, AsyncMock],
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        _pool, adapter = mock_pool
        order = _make_order_request(order_type=OrderType.LIMIT)
        submitted = _make_order_result(order, status=OrderStatus.SUBMITTED)
        filled = _make_order_result(order, status=OrderStatus.FILLED)
        adapter.place_order = AsyncMock(return_value=submitted)
        adapter.get_order = AsyncMock(side_effect=[submitted, filled])
        mocker.patch("asyncio.sleep", new=AsyncMock())
        mocker.patch("sgr.execution.engine.get_event_bus")

        result = await engine.execute(order)

        assert result.status == OrderStatus.FILLED
        assert adapter.get_order.await_count == 2

    async def test_poll_error_is_tolerated_and_retried(
        self,
        engine: ExecutionEngine,
        mock_pool: tuple[MagicMock, AsyncMock],
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """ExchangeError beim Polling darf die Ueberwachung nicht abbrechen."""
        _pool, adapter = mock_pool
        order = _make_order_request(order_type=OrderType.LIMIT)
        submitted = _make_order_result(order, status=OrderStatus.SUBMITTED)
        filled = _make_order_result(order, status=OrderStatus.FILLED)
        adapter.place_order = AsyncMock(return_value=submitted)
        adapter.get_order = AsyncMock(
            side_effect=[ExchangeError("timeout", exchange="binance"), filled]
        )
        mocker.patch("asyncio.sleep", new=AsyncMock())
        mocker.patch("sgr.execution.engine.get_event_bus")

        result = await engine.execute(order)

        assert result.status == OrderStatus.FILLED
        assert adapter.get_order.await_count == 2

    async def test_timeout_cancels_and_returns_last_known_status(
        self,
        engine: ExecutionEngine,
        mock_pool: tuple[MagicMock, AsyncMock],
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Order bleibt dauerhaft SUBMITTED -> Timeout erreicht -> Cancel-Versuch."""
        _pool, adapter = mock_pool
        order = _make_order_request(order_type=OrderType.LIMIT)
        submitted = _make_order_result(order, status=OrderStatus.SUBMITTED)
        adapter.place_order = AsyncMock(return_value=submitted)
        adapter.get_order = AsyncMock(return_value=submitted)  # bleibt immer SUBMITTED
        adapter.cancel_order = AsyncMock(return_value=True)
        mocker.patch("asyncio.sleep", new=AsyncMock())

        result = await engine.execute(order)

        adapter.cancel_order.assert_awaited_once()
        assert result.status == OrderStatus.SUBMITTED

    async def test_cancel_order_failure_is_logged_and_swallowed(
        self,
        engine: ExecutionEngine,
        mock_pool: tuple[MagicMock, AsyncMock],
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """
        Best-effort Cancel: wenn cancel_order() selbst crasht (nicht nur ein
        negatives Ergebnis liefert), darf das nicht den Fill-Monitoring-Ablauf
        crashen - die Order ist bereits verwaist, aber das System muss stabil
        bleiben statt eine zweite, unkontrollierte Exception zu werfen.
        """
        _pool, adapter = mock_pool
        order = _make_order_request(order_type=OrderType.LIMIT)
        submitted = _make_order_result(order, status=OrderStatus.SUBMITTED)
        adapter.place_order = AsyncMock(return_value=submitted)
        adapter.get_order = AsyncMock(return_value=submitted)
        adapter.cancel_order = AsyncMock(side_effect=RuntimeError("exchange unreachable"))
        mocker.patch("asyncio.sleep", new=AsyncMock())

        # Darf trotz Cancel-Fehler nicht crashen
        result = await engine.execute(order)

        adapter.cancel_order.assert_awaited_once()
        assert result.status == OrderStatus.SUBMITTED

    async def test_market_order_uses_fast_timeout(
        self,
        engine: ExecutionEngine,
        mock_pool: tuple[MagicMock, AsyncMock],
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Market Orders nutzen den kurzen Timeout (5s), nicht den Standard (60s)."""
        _pool, adapter = mock_pool
        order = _make_order_request(order_type=OrderType.MARKET)
        submitted = _make_order_result(order, status=OrderStatus.SUBMITTED)
        adapter.place_order = AsyncMock(return_value=submitted)
        adapter.get_order = AsyncMock(return_value=submitted)
        adapter.cancel_order = AsyncMock(return_value=True)
        sleep_mock = AsyncMock()
        mocker.patch("asyncio.sleep", new=sleep_mock)

        await engine.execute(order)

        # 5s Timeout / 2s Poll-Intervall -> max. 3 sleep-Aufrufe statt bis zu 30
        assert sleep_mock.await_count <= 3


class TestFailSafeExceptionHandling:
    async def test_unexpected_exception_returns_rejected_not_crash(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """Jede unerwartete Exception -> REJECTED Result, kein uncontrolled Crash."""
        _pool, adapter = mock_pool
        adapter.place_order = AsyncMock(side_effect=RuntimeError("exchange down"))
        order = _make_order_request()

        result = await engine.execute(order)

        assert result.status == OrderStatus.REJECTED
        assert "Execution error" in result.raw_response["rejection_reason"]

    async def test_rejected_result_has_zero_fill(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        _pool, adapter = mock_pool
        adapter.place_order = AsyncMock(side_effect=RuntimeError("boom"))
        order = _make_order_request()

        result = await engine.execute(order)

        assert result.filled_quantity == Decimal("0")
        assert result.fees == Decimal("0")


class TestEventPublishFailureIsolation:
    async def test_event_publish_failure_does_not_prevent_fill_result(
        self,
        engine: ExecutionEngine,
        mock_pool: tuple[MagicMock, AsyncMock],
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """
        Best-effort: Event-Bus-Fehler beim Publish darf den erfolgreichen Fill
        nicht in einen Fehler verwandeln - der Fill ist bereits an der Exchange
        real passiert, der Event-Publish ist nur Benachrichtigung.
        """
        _pool, adapter = mock_pool
        order = _make_order_request()
        filled = _make_order_result(order, status=OrderStatus.FILLED)
        adapter.place_order = AsyncMock(return_value=filled)
        bus = mocker.Mock()
        bus.publish = AsyncMock(side_effect=RuntimeError("event bus down"))
        mocker.patch("sgr.execution.engine.get_event_bus", return_value=bus)

        result = await engine.execute(order)

        assert result.status == OrderStatus.FILLED  # trotz Publish-Fehler
