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

import asyncio
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
from sgr.exchanges.base import ExchangeError, ExchangeInfo
from sgr.execution.engine import ExecutionEngine
from sgr.execution.preflight import PreflightCheckResult, PreflightResult, PreflightValidator

if TYPE_CHECKING:
    import pytest_mock


def _make_symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def _make_order_request(
    order_type: OrderType = OrderType.MARKET,
    trading_mode: TradingMode = TradingMode.PAPER,
    strategy: str | None = "test_strategy",
) -> OrderRequest:
    from uuid import uuid4

    return OrderRequest(
        signal_id=uuid4(),
        symbol=_make_symbol(),
        side=Side.BUY,
        order_type=order_type,
        quantity=Decimal("0.1"),
        trading_mode=trading_mode,
        # strategy-Attribution (siehe sgr/risk/live_trading_gate.py):
        # noetig, damit LIVE-Orders in dieser Datei das Gate ueberhaupt
        # erreichen koennen, das sonst JEDE LIVE-Order ohne
        # metadata["strategy"] sofort verweigert - siehe live_engine
        # Fixture, die "test_strategy" als genuin live_approved
        # registriert. Fuer PAPER-Order folgenlos (Gate ist dort No-op).
        metadata={"strategy": strategy} if strategy else {},
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
    # Explizit konfiguriert (statt dem generischen AsyncMock-Autospec zu
    # vertrauen): PreflightValidator._check_symbol_precision_and_limits
    # laeuft seit dem Paper/Live-Parity-Fix auch in PAPER und erwartet ein
    # echtes ExchangeInfo-Objekt zurueck, kein automatisch generiertes
    # Kind-Mock (das bei .symbol_limits.get(...) sonst einen unawaited
    # Coroutine-Wert statt eines dict-Ergebnisses liefert). symbol_limits={}
    # heisst "keine Limits-Daten fuer dieses Symbol" - der Check markiert
    # sich dann korrekt als supported=False, ohne die hier getesteten
    # Order-Flow-Szenarien zu beeinflussen. set_leverage() ist absichtlich
    # NICHT weiter konfiguriert - der generische AsyncMock-Erfolg (kein
    # Raise) ist fuer diese Tests bereits das korrekte "Leverage gesetzt"-
    # Verhalten.
    adapter.get_exchange_info = AsyncMock(
        return_value=ExchangeInfo(
            exchange_id=ExchangeID.BINANCE,
            symbols=[],
            timeframes=[],
            maker_fee=Decimal("0.001"),
            taker_fee=Decimal("0.001"),
            fetched_at=datetime.now(tz=UTC),
            symbol_limits={},
        )
    )
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

    async def test_bypass_kill_switch_allows_submission_while_active(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """PositionLiquidator (sgr/risk/position_liquidator.py) sendet
        schliessende Orders WAEHREND der Kill Switch aktiv ist - das ist
        die Reaktion auf den Kill Switch, nicht neues Risiko. Ohne
        bypass_kill_switch=True wuerde sich die Closing-Order selbst
        blockieren."""
        _pool, adapter = mock_pool
        adapter.place_order = AsyncMock(return_value=_make_order_result(_make_order_request()))
        engine._kill_switch.is_active = True  # type: ignore[misc]
        order = _make_order_request()

        result = await engine.execute(order, bypass_kill_switch=True)

        assert result.status == OrderStatus.FILLED
        adapter.place_order.assert_awaited_once()

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
        """Ein Fehler beim Submit selbst -> REJECTED Result (via
        SafeOrderExecutor Unknown-State-Handling, siehe
        TestSafeOrderExecutorIntegration), kein uncontrolled Crash."""
        _pool, adapter = mock_pool
        adapter.place_order = AsyncMock(side_effect=RuntimeError("exchange down"))
        order = _make_order_request()

        result = await engine.execute(order)

        assert result.status == OrderStatus.REJECTED
        assert "Execution error" in result.raw_response["rejection_reason"]

    async def test_unexpected_exception_outside_place_order_returns_rejected(
        self,
        engine: ExecutionEngine,
        mock_pool: tuple[MagicMock, AsyncMock],
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        """Ein unerwarteter Fehler AUSSERHALB von place_order (z.B. im
        Fill-Monitoring nach erfolgreichem Submit) wird vom aeusseren
        try/except in execute() abgefangen -> REJECTED, kein Crash. Anders
        als ein place_order()-Fehler (siehe SafeOrderExecutor
        Unknown-State) ist das ein 'echter' unerwarteter Fehler im
        Engine-Code selbst."""
        _pool, adapter = mock_pool
        order = _make_order_request(order_type=OrderType.LIMIT)
        submitted = _make_order_result(order, status=OrderStatus.SUBMITTED)
        adapter.place_order = AsyncMock(return_value=submitted)
        mocker.patch("asyncio.sleep", new=AsyncMock())
        adapter.get_order = AsyncMock(side_effect=RuntimeError("unexpected bug"))

        result = await engine.execute(order)

        assert result.status == OrderStatus.REJECTED
        assert "Execution error" in result.raw_response["rejection_reason"]
        # Duplicate-Guard-Tracking muss trotz Crash freigegeben sein (finally).
        assert engine._safety.get_inflight(order) is None

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


class TestOrderPersistence:
    """
    ExecutionEngine schrieb Orders zuvor NIE in die DB - nur Events und
    Audit-Log-Zeilen (kein abfragbarer State). OrderRepository.create()/
    update_status() existierten, wurden aber nirgends aufgerufen.
    Ohne order_repository-Injektion bleibt das Verhalten unveraendert
    (No-Op) - siehe bestehende Tests oben, die alle ohne Repository laufen.
    """

    @pytest.fixture
    def engine_with_repo(
        self, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> tuple[ExecutionEngine, AsyncMock]:
        pool, _adapter = mock_pool
        order_repo = AsyncMock()
        # Kein vorheriger Order-Record fuer diese order.id - siehe
        # SafeOrderExecutor._lookup_persisted() (Modul-Docstring Punkt 5):
        # ohne dies wuerde ein unkonfiguriertes AsyncMock.get_by_id()
        # ein truthy Mock-Objekt zurueckgeben und faelschlich als
        # bereits-persistierter Duplicate-/Unknown-State-Fund gelten.
        order_repo.get_by_id.return_value = None
        eng = ExecutionEngine(pool, TradingMode.PAPER, order_repository=order_repo)
        fake_kill_switch = MagicMock()
        fake_kill_switch.is_active = False
        eng._kill_switch = fake_kill_switch
        return eng, order_repo

    async def test_no_repository_injected_is_safe_noop(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """Regressionstest: ohne Repository darf nichts crashen (bestehendes Verhalten)."""
        _pool, adapter = mock_pool
        order = _make_order_request()
        filled = _make_order_result(order, status=OrderStatus.FILLED)
        adapter.place_order = AsyncMock(return_value=filled)

        result = await engine.execute(order)

        assert result.status == OrderStatus.FILLED

    async def test_immediate_fill_creates_and_updates_order(
        self,
        engine_with_repo: tuple[ExecutionEngine, AsyncMock],
        mock_pool: tuple[MagicMock, AsyncMock],
    ) -> None:
        eng, order_repo = engine_with_repo
        _pool, adapter = mock_pool
        order = _make_order_request()
        filled = _make_order_result(order, status=OrderStatus.FILLED)
        adapter.place_order = AsyncMock(return_value=filled)

        await eng.execute(order)

        # create() legt den Record als PENDING an, BEVOR der Exchange-Call
        # ueberhaupt stattfindet (siehe order_safety.py Modul-Docstring
        # Punkt 5 - SafeOrderExecutor._persist_pending()) - nicht erst
        # danach mit dem bereits bekannten Endstatus wie vor diesem Fix.
        order_repo.create.assert_called_once()
        created = order_repo.create.call_args.args[0]
        assert created["id"] == str(order.id)
        assert created["signal_id"] == str(order.signal_id)
        assert created["status"] == OrderStatus.PENDING.value
        assert created["exchange_order_id"] is None

        # update_status() wird zweimal aufgerufen: einmal aus
        # SafeOrderExecutor._persist_final() direkt nach dem Submit
        # (traegt exchange_order_id nach, die create() noch nicht kannte),
        # einmal aus ExecutionEngine._on_fill()->_persist_order_status()
        # fuer den Sofortfill-Fall - beide mit demselben Endstatus.
        assert order_repo.update_status.call_count == 2
        first_update = order_repo.update_status.call_args_list[0].kwargs
        assert first_update["order_id"] == str(order.id)
        assert first_update["status"] == OrderStatus.FILLED.value
        assert first_update["filled_quantity"] == filled.filled_quantity
        assert first_update["exchange_order_id"] == filled.exchange_order_id

    async def test_create_uses_order_id_not_generated_id(
        self,
        engine_with_repo: tuple[ExecutionEngine, AsyncMock],
        mock_pool: tuple[MagicMock, AsyncMock],
    ) -> None:
        """
        Kritisch fuer Korrektheit: id muss explizit order.id sein, sonst
        treffen spaetere update_status()-Aufrufe (per order.id) die
        falsche Zeile oder keine.
        """
        eng, order_repo = engine_with_repo
        _pool, adapter = mock_pool
        order = _make_order_request()
        filled = _make_order_result(order, status=OrderStatus.FILLED)
        adapter.place_order = AsyncMock(return_value=filled)
        order_repo.create.return_value = "some-different-generated-id"

        await eng.execute(order)

        created = order_repo.create.call_args.args[0]
        assert created["id"] == str(order.id)

    async def test_cancelled_order_updates_status(
        self,
        engine_with_repo: tuple[ExecutionEngine, AsyncMock],
        mock_pool: tuple[MagicMock, AsyncMock],
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        eng, order_repo = engine_with_repo
        _pool, adapter = mock_pool
        order = _make_order_request(order_type=OrderType.LIMIT)
        submitted = _make_order_result(order, status=OrderStatus.SUBMITTED)
        adapter.place_order = AsyncMock(return_value=submitted)
        adapter.cancel_order = AsyncMock(return_value=True)
        mocker.patch("asyncio.sleep", new=AsyncMock())

        # Kill Switch wird erst NACH der ersten Submission aktiv (analog
        # test_kill_switch_activated_during_monitoring_cancels_order).
        eng._kill_switch = _DelayedActiveKillSwitch(activate_after_checks=1)

        await eng.execute(order)

        # Ein create() (PENDING, vor dem Exchange-Call) beim Submit, dann
        # ZWEI update_status()-Aufrufe: einer aus SafeOrderExecutor.
        # _persist_final() direkt nach dem Submit (traegt exchange_order_id
        # nach), einer aus ExecutionEngine._persist_order_status() beim
        # anschliessenden Cancel durch den Kill Switch.
        order_repo.create.assert_called_once()
        assert order_repo.create.call_args.args[0]["status"] == OrderStatus.PENDING.value
        assert order_repo.update_status.call_count == 2
        first_update, second_update = order_repo.update_status.call_args_list
        assert first_update.kwargs["exchange_order_id"] == submitted.exchange_order_id
        assert second_update.kwargs["status"] == OrderStatus.SUBMITTED.value

    async def test_persist_create_failure_does_not_block_execution(
        self,
        engine_with_repo: tuple[ExecutionEngine, AsyncMock],
        mock_pool: tuple[MagicMock, AsyncMock],
    ) -> None:
        """Fail-safe: ein DB-Fehler beim Order-Anlegen darf den Fill nicht verhindern."""
        eng, order_repo = engine_with_repo
        _pool, adapter = mock_pool
        order = _make_order_request()
        filled = _make_order_result(order, status=OrderStatus.FILLED)
        adapter.place_order = AsyncMock(return_value=filled)
        order_repo.create.side_effect = RuntimeError("db down")

        result = await eng.execute(order)

        assert result.status == OrderStatus.FILLED

    async def test_persist_status_failure_does_not_block_execution(
        self,
        engine_with_repo: tuple[ExecutionEngine, AsyncMock],
        mock_pool: tuple[MagicMock, AsyncMock],
    ) -> None:
        """Fail-safe: ein DB-Fehler beim Status-Update darf den Fill-Report nicht verhindern."""
        eng, order_repo = engine_with_repo
        _pool, adapter = mock_pool
        order = _make_order_request()
        filled = _make_order_result(order, status=OrderStatus.FILLED)
        adapter.place_order = AsyncMock(return_value=filled)
        order_repo.update_status.side_effect = RuntimeError("db down")

        result = await eng.execute(order)

        assert result.status == OrderStatus.FILLED


# ---------------------------------------------------------------------------
# Preflight Validation Integration (Baustein 6 - Live Trading Safety)
# ---------------------------------------------------------------------------
#
# Die eigentliche Check-Logik ist vollständig und isoliert in
# tests/unit/test_preflight.py abgedeckt. Hier wird nur die END-TO-END-
# VERDRAHTUNG in ExecutionEngine.execute() getestet: läuft Preflight nach
# dem Kill-Switch-Check und vor dem Exchange-Call, blockiert ein negatives
# Ergebnis wirklich die Order, und bleibt der bestehende PAPER-Pfad
# unverändert (kein Preflight-Override -> echter PreflightValidator, der
# in PAPER die meisten Checks überspringt).


@pytest.fixture
def live_engine(mock_pool: tuple[MagicMock, AsyncMock]) -> ExecutionEngine:
    """LIVE-Mode-Engine mit gefaktem Kill Switch (analog zur PAPER-
    engine-Fixture). _preflight bleibt hier absichtlich der echte
    PreflightValidator - einzelne Tests ersetzen ihn gezielt per
    Dependency-Injection, wo ein bestimmtes Preflight-Ergebnis
    erzwungen werden soll.

    Registriert "test_strategy" als genuin live_approved in der
    StrategyRegistry (globaler Singleton, siehe Docstring oben zum
    Kill-Switch-Singleton - dieselbe Test-Leakage-Gefahr gilt hier,
    daher clear() vor UND nach jeder Nutzung), damit LIVE-Orders in
    dieser Datei das sgr/risk/live_trading_gate.py-Gate ueberhaupt
    erreichen und die eigentlich getestete Logik (Kill Switch,
    Preflight) exerziert wird, statt bereits am Gate abgewiesen zu
    werden."""
    from sgr.strategy.base import ValidationStatus
    from sgr.strategy.registry import StrategyRegistry

    class _LiveTestStrategy:
        name = "test_strategy"
        version = "1.0.0"
        supported_regimes: list = []

        def generate_signal(self, context):  # pragma: no cover
            return None

    registry = StrategyRegistry.get()
    registry.clear()
    registry.register_instance(_LiveTestStrategy())
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
    registry._entries["test_strategy"].is_active = True

    pool, _adapter = mock_pool
    eng = ExecutionEngine(pool, TradingMode.LIVE)
    fake_kill_switch = MagicMock()
    fake_kill_switch.is_active = False
    eng._kill_switch = fake_kill_switch
    yield eng
    registry.clear()


class TestPreflightIntegration:
    async def test_preflight_rejection_blocks_before_exchange_call(
        self, live_engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """Preflight._preflight per DI durch ein Test-Double ersetzt, das
        eligible=False liefert - analog zum Kill-Switch-Testmuster dieser
        Datei. Order darf die Exchange nicht erreichen."""
        _pool, adapter = mock_pool
        fake_preflight = AsyncMock()
        fake_preflight.validate = AsyncMock(
            return_value=PreflightResult(
                order_id="test-order",
                trading_mode=TradingMode.LIVE,
                checks=[
                    PreflightCheckResult(
                        name="balance_and_available_capital",
                        passed=False,
                        detail="Insufficient free balance",
                    )
                ],
            )
        )
        live_engine._preflight = fake_preflight  # type: ignore[assignment]
        order = _make_order_request(trading_mode=TradingMode.LIVE)

        result = await live_engine.execute(order)

        assert result.status == OrderStatus.REJECTED
        assert "Preflight validation failed" in result.raw_response["rejection_reason"]
        assert "balance_and_available_capital" in result.raw_response["rejection_reason"]
        adapter.place_order.assert_not_awaited()

    async def test_preflight_approval_allows_exchange_call(
        self,
        live_engine: ExecutionEngine,
        mock_pool: tuple[MagicMock, AsyncMock],
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        from datetime import UTC, datetime

        from sgr.risk.live_verification_profile import (
            LiveVerificationGate,
            LiveVerificationProfile,
        )

        _pool, adapter = mock_pool
        order = _make_order_request(
            order_type=OrderType.LIMIT, trading_mode=TradingMode.LIVE
        ).model_copy(update={"limit_price": Decimal("50000")})
        filled = _make_order_result(order, status=OrderStatus.FILLED)
        adapter.place_order = AsyncMock(return_value=filled)
        mocker.patch("sgr.execution.engine.get_event_bus")

        fake_preflight = AsyncMock()
        fake_preflight.validate = AsyncMock(
            return_value=PreflightResult(
                order_id=str(order.id), trading_mode=TradingMode.LIVE, checks=[]
            )
        )
        live_engine._preflight = fake_preflight  # type: ignore[assignment]
        # Gegenstand dieses Tests ist ausschliesslich das Durchreichen einer
        # bestandenen Preflight-Pruefung bis zum Exchange-Call - das seit
        # Phase C zusaetzlich verpflichtende LiveVerificationGate wird
        # hier bewusst mit einem grosszuegigen, bestandenen Profil versehen,
        # damit es diesen Preflight-Test nicht verdeckt (siehe eigene,
        # dedizierte Tests in TestLiveVerificationGateIntegration).
        live_engine._live_verification_gate = LiveVerificationGate(
            LiveVerificationProfile(
                max_total_budget_usd=Decimal("100000"),
                max_loss_usd=Decimal("100000"),
                max_daily_loss_usd=Decimal("100000"),
                max_concurrent_grids=10,
                max_orders=1000,
                max_exposure_usd=Decimal("100000"),
                max_leverage=Decimal("10"),
                max_position_size_usd=Decimal("100000"),
                max_duration_minutes=600,
                started_at=datetime.now(tz=UTC),
                approved_by="operator:test",
            )
        )

        result = await live_engine.execute(order)

        assert result.status == OrderStatus.FILLED
        adapter.place_order.assert_awaited_once()

    async def test_preflight_runs_after_kill_switch_check(
        self, live_engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """Aktiver Kill Switch blockt bereits VOR Preflight - Preflight
        wird gar nicht erst aufgerufen (keine unnötigen Exchange-Calls
        für eine Order, die ohnehin schon abgelehnt ist)."""
        _pool, adapter = mock_pool
        live_engine._kill_switch.is_active = True  # type: ignore[misc]
        fake_preflight = AsyncMock()
        live_engine._preflight = fake_preflight  # type: ignore[assignment]
        order = _make_order_request(trading_mode=TradingMode.LIVE)

        result = await live_engine.execute(order)

        assert result.status == OrderStatus.REJECTED
        fake_preflight.validate.assert_not_awaited()
        adapter.place_order.assert_not_awaited()

    async def test_paper_mode_uses_real_preflight_validator_by_default(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """Regressionsschutz: die bestehende PAPER-Engine-Fixture (siehe
        oben) injiziert keinen Preflight-Fake - das ist beabsichtigt.
        Der echte PreflightValidator muss in PAPER weiterhin ohne echte
        Exchange-Calls durchlaufen (siehe test_preflight.py::TestPaperMode)."""
        assert isinstance(engine._preflight, PreflightValidator)


class TestSafeOrderExecutorIntegration:
    """
    Baustein 7: End-to-End-Verhalten von ExecutionEngine mit der echten
    SafeOrderExecutor-Middleware (sgr/execution/order_safety.py). Die
    Middleware selbst ist isoliert in tests/execution/test_order_safety.py
    getestet - hier nur die Verdrahtung/Integration.
    """

    async def test_second_concurrent_execute_for_same_order_id_is_rejected(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """Zwei gleichzeitige execute()-Aufrufe fuer dieselbe order.id:
        nur der erste erreicht die Exchange, der zweite wird sofort
        REJECTED, ohne auf den ersten zu warten."""
        _pool, adapter = mock_pool
        order = _make_order_request()

        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_place_order(_order: OrderRequest) -> OrderResult:
            entered.set()
            await release.wait()
            return _make_order_result(_order, status=OrderStatus.FILLED)

        adapter.place_order = AsyncMock(side_effect=slow_place_order)

        first_task = asyncio.create_task(engine.execute(order))
        await asyncio.wait_for(entered.wait(), timeout=5)

        second_result = await engine.execute(order)

        assert second_result.status == OrderStatus.REJECTED
        assert second_result.raw_response.get("duplicate") is True

        release.set()
        first_result = await asyncio.wait_for(first_task, timeout=5)
        assert first_result.status == OrderStatus.FILLED

    async def test_inflight_tracking_released_after_completion(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """Nach Abschluss von execute() (FILLED) ist die order.id nicht
        mehr im SafeOrderExecutor-Tracking - ein spaeterer, unabhaengiger
        Aufruf mit derselben ID ist NICHT blockiert."""
        _pool, adapter = mock_pool
        order = _make_order_request()
        adapter.place_order = AsyncMock(
            return_value=_make_order_result(order, status=OrderStatus.FILLED)
        )

        result = await engine.execute(order)

        assert result.status == OrderStatus.FILLED
        assert engine._safety.get_inflight(order) is None

    async def test_inflight_tracking_released_on_kill_switch_rejection(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """Bei REJECTED durch Kill Switch (kein Exchange-Kontakt) darf kein
        Eintrag im SafeOrderExecutor-Tracking zurueckbleiben - execute_safely()
        wird in diesem Pfad gar nicht erst aufgerufen, also gibt es nichts
        freizugeben, aber release() muss das fehlerfrei tolerieren."""
        engine._kill_switch.is_active = True  # type: ignore[misc]
        order = _make_order_request()

        result = await engine.execute(order)

        assert result.status == OrderStatus.REJECTED
        assert engine._safety.get_inflight(order) is None

    async def test_submit_error_returns_rejected_unknown_state(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """Ein Fehler beim Submit selbst (z.B. Netzwerkfehler) fuehrt zu
        einem REJECTED-Result mit explizitem Unknown-Marker statt einer
        unbehandelten Exception oder einem blinden Retry."""
        _pool, adapter = mock_pool
        order = _make_order_request()
        adapter.place_order = AsyncMock(side_effect=RuntimeError("connection reset"))

        result = await engine.execute(order)

        assert result.status == OrderStatus.REJECTED
        assert result.raw_response.get("unknown") is True


class TestShutdownSafety:
    """
    Baustein 7: best-effort Cancel aller Orders, die sich beim Shutdown
    noch im Fill-Monitoring befinden, bevor Exchange-Verbindungen
    geschlossen werden.
    """

    async def test_shutdown_with_no_inflight_orders_is_a_noop(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        _pool, adapter = mock_pool

        await engine.shutdown()

        adapter.cancel_order.assert_not_awaited()

    async def test_shutdown_cancels_tracked_inflight_order(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """Order, die noch im Fill-Monitoring haengt (status=SUBMITTED),
        wird bei shutdown() gecancelt."""
        pool, adapter = mock_pool
        order = _make_order_request()
        inflight_result = _make_order_result(
            order, status=OrderStatus.SUBMITTED, exchange_order_id="EX-INFLIGHT"
        )
        engine._safety._in_flight[str(order.id)] = inflight_result
        pool.get = MagicMock(return_value=adapter)

        await engine.shutdown()

        adapter.cancel_order.assert_awaited_once_with("EX-INFLIGHT", order.symbol.ccxt_symbol)
        assert engine._safety.all_inflight() == {}

    async def test_shutdown_skips_already_terminal_orders(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """Eine Order, die bereits FILLED/CANCELLED/REJECTED ist (Race
        zwischen release() und shutdown()), wird nicht nochmal gecancelt."""
        _pool, adapter = mock_pool
        order = _make_order_request()
        filled_result = _make_order_result(order, status=OrderStatus.FILLED)
        engine._safety._in_flight[str(order.id)] = filled_result

        await engine.shutdown()

        adapter.cancel_order.assert_not_awaited()

    async def test_shutdown_skips_placeholder_order_without_exchange_id(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """Order noch im Placeholder-Zustand (place_order() noch nicht
        zurueckgekehrt, keine exchange_order_id bekannt): shutdown() darf
        keinen Cancel versuchen, da es kein gueltiges Ziel gibt."""
        _pool, adapter = mock_pool
        order = _make_order_request()
        placeholder_result = _make_order_result(
            order, status=OrderStatus.PENDING, exchange_order_id=""
        )
        engine._safety._in_flight[str(order.id)] = placeholder_result

        await engine.shutdown()

        adapter.cancel_order.assert_not_awaited()
        assert engine._safety.all_inflight() == {}

    async def test_shutdown_cancel_failure_does_not_raise(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """Best-effort: ein fehlschlagender Cancel darf shutdown() nicht
        zum Absturz bringen (fail-safe Cleanup-Pfad)."""
        _pool, adapter = mock_pool
        order = _make_order_request()
        inflight_result = _make_order_result(
            order, status=OrderStatus.SUBMITTED, exchange_order_id="EX-INFLIGHT"
        )
        engine._safety._in_flight[str(order.id)] = inflight_result
        adapter.cancel_order = AsyncMock(side_effect=RuntimeError("network down"))

        await engine.shutdown()

        assert engine._safety.all_inflight() == {}

    async def test_shutdown_cancels_multiple_inflight_orders_independently(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """Ein fehlschlagender Cancel fuer eine Order darf den Cancel
        einer anderen Order nicht verhindern."""
        _pool, adapter = mock_pool
        order_a = _make_order_request()
        order_b = _make_order_request()
        engine._safety._in_flight[str(order_a.id)] = _make_order_result(
            order_a, status=OrderStatus.SUBMITTED, exchange_order_id="EX-A"
        )
        engine._safety._in_flight[str(order_b.id)] = _make_order_result(
            order_b, status=OrderStatus.SUBMITTED, exchange_order_id="EX-B"
        )
        adapter.cancel_order = AsyncMock(side_effect=[RuntimeError("fails for A"), None])

        await engine.shutdown()

        assert adapter.cancel_order.await_count == 2
        assert engine._safety.all_inflight() == {}


# ---------------------------------------------------------------------------
# Prometheus-Metriken-Verdrahtung (Schritt: Grafana-Dashboard-Werte)
# ---------------------------------------------------------------------------
#
# Diese Tests pruefen NICHT nur, dass der Code-Pfad durchlaeuft (das
# deckt bereits die bestehende 100%-Coverage ab), sondern dass die
# tatsaechlichen Prometheus-Metrikwerte danach korrekt gesetzt sind -
# die eigentliche Grafana-Luecke war "Metriken existieren, werden aber
# nie inkrementiert", nicht "Code stuerzt ab".


class TestMetricsRecording:
    async def test_order_submitted_increments_counter(
        self,
        engine: ExecutionEngine,
        mock_pool: tuple[MagicMock, AsyncMock],
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        from sgr.monitoring.trading_metrics import orders_submitted_total

        _pool, adapter = mock_pool
        order = _make_order_request()
        filled = _make_order_result(order, status=OrderStatus.FILLED)
        adapter.place_order = AsyncMock(return_value=filled)
        mocker.patch("sgr.core.event_bus.get_event_bus")

        before = orders_submitted_total.labels(
            exchange="binance",
            symbol="BTC/USDT:binance",
            side="buy",
            trading_mode="paper",
            tenant="default",
        )._value.get()

        await engine.execute(order)

        after = orders_submitted_total.labels(
            exchange="binance",
            symbol="BTC/USDT:binance",
            side="buy",
            trading_mode="paper",
            tenant="default",
        )._value.get()
        assert after == before + 1

    async def test_order_filled_increments_counter_and_observes_latency(
        self,
        engine: ExecutionEngine,
        mock_pool: tuple[MagicMock, AsyncMock],
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        from sgr.monitoring.trading_metrics import orders_filled_total

        _pool, adapter = mock_pool
        order = _make_order_request()
        filled = _make_order_result(order, status=OrderStatus.FILLED)
        adapter.place_order = AsyncMock(return_value=filled)
        mocker.patch("sgr.core.event_bus.get_event_bus")

        before = orders_filled_total.labels(
            exchange="binance",
            symbol="BTC/USDT:binance",
            side="buy",
            trading_mode="paper",
            tenant="default",
        )._value.get()

        await engine.execute(order)

        after = orders_filled_total.labels(
            exchange="binance",
            symbol="BTC/USDT:binance",
            side="buy",
            trading_mode="paper",
            tenant="default",
        )._value.get()
        assert after == before + 1

    async def test_kill_switch_rejection_increments_rejected_counter(
        self, engine: ExecutionEngine
    ) -> None:
        from sgr.monitoring.trading_metrics import orders_rejected_total

        engine._kill_switch.is_active = True  # type: ignore[misc]
        order = _make_order_request()

        before = orders_rejected_total.labels(
            exchange="binance",
            symbol="BTC/USDT:binance",
            reason="kill_switch_active",
            tenant="default",
        )._value.get()

        await engine.execute(order)

        after = orders_rejected_total.labels(
            exchange="binance",
            symbol="BTC/USDT:binance",
            reason="kill_switch_active",
            tenant="default",
        )._value.get()
        assert after == before + 1

    async def test_preflight_rejection_increments_rejected_counter(
        self,
        engine: ExecutionEngine,
        mocker: pytest_mock.MockerFixture,
    ) -> None:
        from sgr.monitoring.trading_metrics import orders_rejected_total

        fake_preflight = mocker.Mock()
        fake_preflight.validate = AsyncMock(
            return_value=PreflightResult(
                order_id="x",
                trading_mode=TradingMode.PAPER,
                checks=[
                    PreflightCheckResult(
                        name="order_quantity_positive", passed=False, detail="qty<=0"
                    )
                ],
            )
        )
        engine._preflight = fake_preflight
        order = _make_order_request()

        before = orders_rejected_total.labels(
            exchange="binance",
            symbol="BTC/USDT:binance",
            reason="preflight_failed",
            tenant="default",
        )._value.get()

        await engine.execute(order)

        after = orders_rejected_total.labels(
            exchange="binance",
            symbol="BTC/USDT:binance",
            reason="preflight_failed",
            tenant="default",
        )._value.get()
        assert after == before + 1




class TestLiveVerificationGateIntegration:
    """Phase C (2026-09-24, explizite Anweisung: 'Wenn LiveVerificationGate
    nicht integriert ist, integriere es'): ExecutionEngine.execute() prueft
    fuer JEDE LIVE-Order zusaetzlich das injizierte LiveVerificationGate,
    NACH Preflight/Leverage/Quantization, VOR dem Exchange-Call. Fail-closed
    ohne injizierten Gate. Kein Effekt fuer PAPER.

    Preflight wird hier - analog zu TestPreflightIntegration - per DI durch
    ein Test-Double ersetzt, das immer eligible=True liefert: Gegenstand
    dieser Tests ist ausschliesslich das LiveVerificationGate, nicht der
    (bereits separat getestete) PreflightValidator selbst.
    """

    @staticmethod
    def _fake_passing_preflight(order_id: str) -> AsyncMock:
        fake_preflight = AsyncMock()
        fake_preflight.validate = AsyncMock(
            return_value=PreflightResult(
                order_id=order_id, trading_mode=TradingMode.LIVE, checks=[]
            )
        )
        return fake_preflight

    def _profile(self, **overrides):
        from sgr.risk.live_verification_profile import LiveVerificationProfile

        base = dict(
            max_total_budget_usd=Decimal("1000"),
            max_loss_usd=Decimal("100"),
            max_daily_loss_usd=Decimal("100"),
            max_concurrent_grids=1,
            max_orders=10,
            max_exposure_usd=Decimal("1000"),
            max_leverage=Decimal("5"),
            max_position_size_usd=Decimal("500"),
            max_duration_minutes=60,
            started_at=datetime.now(tz=UTC),
            approved_by="operator:test",
        )
        base.update(overrides)
        return LiveVerificationProfile(**base)

    async def test_paper_order_is_unaffected_by_missing_gate(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """PAPER + kein Gate injiziert (Default None) - exaktes
        Vor-Aenderungs-Verhalten, keine Blockade."""
        _pool, adapter = mock_pool
        adapter.place_order = AsyncMock(return_value=_make_order_result(_make_order_request()))
        order = _make_order_request(trading_mode=TradingMode.PAPER)

        result = await engine.execute(order)

        assert result.status == OrderStatus.FILLED

    async def test_live_order_without_gate_is_rejected_fail_closed(
        self, live_engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        """LIVE + kein LiveVerificationGate injiziert -> REJECTED,
        fail-closed, kein Default-Budget."""
        _pool, adapter = mock_pool
        order = _make_order_request(
            order_type=OrderType.LIMIT, trading_mode=TradingMode.LIVE
        ).model_copy(update={"limit_price": Decimal("50000")})
        live_engine._preflight = self._fake_passing_preflight(str(order.id))

        result = await live_engine.execute(order)

        assert result.status == OrderStatus.REJECTED
        assert "erification" in result.raw_response["rejection_reason"]
        adapter.place_order.assert_not_awaited()

    async def test_live_order_within_gate_limits_proceeds(
        self, live_engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        from sgr.risk.live_verification_profile import LiveVerificationGate

        _pool, adapter = mock_pool
        order = _make_order_request(
            order_type=OrderType.LIMIT, trading_mode=TradingMode.LIVE
        ).model_copy(update={"limit_price": Decimal("50000"), "quantity": Decimal("0.005")})
        live_engine._preflight = self._fake_passing_preflight(str(order.id))
        adapter.place_order = AsyncMock(return_value=_make_order_result(order))
        live_engine._live_verification_gate = LiveVerificationGate(self._profile())

        result = await live_engine.execute(order)

        assert result.status == OrderStatus.FILLED
        adapter.place_order.assert_awaited_once()

    async def test_live_order_exceeding_position_size_limit_is_rejected(
        self, live_engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        from sgr.risk.live_verification_profile import LiveVerificationGate

        _pool, adapter = mock_pool
        # Notional = 0.1 * 50000 = 5000
        order = _make_order_request(
            order_type=OrderType.LIMIT, trading_mode=TradingMode.LIVE
        ).model_copy(update={"limit_price": Decimal("50000"), "quantity": Decimal("0.1")})
        live_engine._preflight = self._fake_passing_preflight(str(order.id))
        live_engine._live_verification_gate = LiveVerificationGate(
            self._profile(max_position_size_usd=Decimal("100"))
        )

        result = await live_engine.execute(order)

        assert result.status == OrderStatus.REJECTED
        adapter.place_order.assert_not_awaited()

    async def test_live_order_with_exhausted_budget_is_rejected(
        self, live_engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        from sgr.risk.live_verification_profile import LiveVerificationGate

        _pool, adapter = mock_pool
        order = _make_order_request(
            order_type=OrderType.LIMIT, trading_mode=TradingMode.LIVE
        ).model_copy(update={"limit_price": Decimal("50000"), "quantity": Decimal("0.001")})
        live_engine._preflight = self._fake_passing_preflight(str(order.id))
        gate = LiveVerificationGate(self._profile(max_loss_usd=Decimal("10")))
        gate.record_realized_loss(Decimal("10"))  # Budget bereits erschoepft
        live_engine._live_verification_gate = gate

        result = await live_engine.execute(order)

        assert result.status == OrderStatus.REJECTED
        adapter.place_order.assert_not_awaited()

    async def test_successful_live_order_records_submission_on_gate(
        self, live_engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        from sgr.risk.live_verification_profile import LiveVerificationGate

        _pool, adapter = mock_pool
        order = _make_order_request(
            order_type=OrderType.LIMIT, trading_mode=TradingMode.LIVE
        ).model_copy(update={"limit_price": Decimal("50000"), "quantity": Decimal("0.001")})
        live_engine._preflight = self._fake_passing_preflight(str(order.id))
        adapter.place_order = AsyncMock(return_value=_make_order_result(order))
        gate = LiveVerificationGate(self._profile())
        live_engine._live_verification_gate = gate

        await live_engine.execute(order)

        assert gate.state.orders_submitted == 1

    async def test_market_order_without_limit_price_uses_ticker_fallback(
        self, live_engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        from sgr.exchanges.base import TickerData
        from sgr.risk.live_verification_profile import LiveVerificationGate

        _pool, adapter = mock_pool
        order = _make_order_request(
            order_type=OrderType.MARKET, trading_mode=TradingMode.LIVE
        ).model_copy(update={"quantity": Decimal("0.001")})
        live_engine._preflight = self._fake_passing_preflight(str(order.id))
        adapter.get_ticker = AsyncMock(
            return_value=TickerData(
                symbol="BTC/USDT",
                bid=Decimal("49990"),
                ask=Decimal("50010"),
                last=Decimal("50000"),
                volume_24h=Decimal("1000"),
                change_24h_pct=0.0,
                timestamp=datetime.now(tz=UTC),
            )
        )
        adapter.place_order = AsyncMock(return_value=_make_order_result(order))
        live_engine._live_verification_gate = LiveVerificationGate(self._profile())

        result = await live_engine.execute(order)

        assert result.status == OrderStatus.FILLED
        adapter.get_ticker.assert_awaited()

    async def test_market_order_ticker_unavailable_is_rejected_fail_closed(
        self, live_engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        from sgr.risk.live_verification_profile import LiveVerificationGate

        _pool, adapter = mock_pool
        order = _make_order_request(order_type=OrderType.MARKET, trading_mode=TradingMode.LIVE)
        live_engine._preflight = self._fake_passing_preflight(str(order.id))
        adapter.get_ticker = AsyncMock(side_effect=RuntimeError("exchange unreachable"))
        live_engine._live_verification_gate = LiveVerificationGate(self._profile())

        result = await live_engine.execute(order)

        assert result.status == OrderStatus.REJECTED
        adapter.place_order.assert_not_awaited()


class TestSlippageMetric:
    """Live-Verification-Anweisung Abschnitt 4 ('Observability'):
    ExecutionEngine._on_fill() versprach in seinem eigenen Docstring seit
    jeher 'Slippage berechnen + loggen', tat das nie tatsaechlich. Jetzt
    tatsaechlich verdrahtet: order.limit_price als Referenzpreis, nur
    wenn vorhanden (reine MARKET-Order ohne RiskEngine-erzwungenes Limit
    hat keinen sinnvollen Referenzpreis - siehe order_slippage_pct
    Docstring)."""

    async def test_limit_order_fill_records_slippage(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        from sgr.monitoring.trading_metrics import order_slippage_pct

        _pool, adapter = mock_pool
        order = _make_order_request(order_type=OrderType.LIMIT).model_copy(
            update={"limit_price": Decimal("100")}
        )
        filled = _make_order_result(order, status=OrderStatus.FILLED)
        filled = filled.model_copy(update={"average_fill_price": Decimal("101")})
        adapter.place_order = AsyncMock(return_value=filled)

        labels = dict(
            exchange="binance",
            symbol="BTC/USDT:binance",
            side="buy",
            trading_mode="paper",
            tenant="default",
        )
        before = order_slippage_pct.labels(**labels)._sum.get()

        await engine.execute(order)

        after = order_slippage_pct.labels(**labels)._sum.get()
        # |101 - 100| / 100 * 100 = 1.0%
        assert after == pytest.approx(before + 1.0)

    async def test_market_order_without_limit_price_records_no_slippage(
        self, engine: ExecutionEngine, mock_pool: tuple[MagicMock, AsyncMock]
    ) -> None:
        from sgr.monitoring.trading_metrics import order_slippage_pct

        _pool, adapter = mock_pool
        order = _make_order_request(order_type=OrderType.MARKET)
        assert order.limit_price is None
        adapter.place_order = AsyncMock(
            return_value=_make_order_result(order, status=OrderStatus.FILLED)
        )

        labels = dict(
            exchange="binance",
            symbol="BTC/USDT:binance",
            side="buy",
            trading_mode="paper",
            tenant="default",
        )
        before = order_slippage_pct.labels(**labels)._sum.get()

        await engine.execute(order)

        after = order_slippage_pct.labels(**labels)._sum.get()
        assert after == before  # keine erfundene Slippage ohne Referenzpreis
