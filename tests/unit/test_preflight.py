"""
Tests für sgr.execution.preflight.PreflightValidator (Baustein 6 - Live
Trading Preflight Validation, Phase 2 Live Trading Safety Mechanisms).

Teststrategie:
    1. Jeder Check einzeln (unit-level, Methode direkt aufgerufen)
    2. validate() end-to-end: PAPER-Pfad (Checks übersprungen, nicht
       "bestanden"), LIVE-Pfad (fail-closed bei jedem einzelnen
       Fehlerfall), NOT_SUPPORTED_CHECKS-Transparenz
    3. Positive und negative LIVE-Fälle, Kill Switch, Invalid Credentials,
       Invalid Symbol, Invalid Quantity, Exchange Limit Violation,
       Max Order Notional Violation, Leverage Violation, Insufficient
       Balance, Reduce-Only-Sicherheit

Der globale Kill-Switch-Singleton (get_kill_switch) wird NICHT direkt
verwendet - stattdessen wird validator._kill_switch nach Konstruktion durch
ein Test-Double ersetzt (siehe test_execution_engine.py-Vorbild), um
Test-Leckage zwischen parallel laufenden Tests zu vermeiden.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from sgr.core.config import RiskLimitsConfig, SGRConfig
from sgr.core.types import (
    ExchangeID,
    OrderRequest,
    OrderType,
    Position,
    PositionSide,
    Side,
    Symbol,
    TradingMode,
)
from sgr.exchanges.base import Balance, ExchangeError, ExchangeInfo, RateLimitError
from sgr.execution.preflight import (
    NOT_SUPPORTED_CHECKS,
    PreflightResult,
    PreflightValidator,
)


def _make_symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def _make_order(
    quantity: Decimal = Decimal("0.1"),
    trading_mode: TradingMode = TradingMode.PAPER,
    limit_price: Decimal | None = None,
    reduce_only: bool = False,
    order_type: OrderType = OrderType.MARKET,
) -> OrderRequest:
    return OrderRequest(
        signal_id=uuid4(),
        symbol=_make_symbol(),
        side=Side.BUY,
        order_type=order_type,
        quantity=quantity,
        limit_price=limit_price,
        trading_mode=trading_mode,
        reduce_only=reduce_only,
    )


def _make_position(
    quantity: Decimal = Decimal("1.0"),
    leverage: Decimal = Decimal("2"),
) -> Position:
    return Position(
        symbol=_make_symbol(),
        side=PositionSide.LONG,
        quantity=quantity,
        entry_price=Decimal("50000"),
        current_price=Decimal("51000"),
        leverage=leverage,
        opened_at=datetime.now(tz=UTC),
        strategy_name="test_strategy",
        trading_mode=TradingMode.LIVE,
    )


def _make_balance(free: Decimal = Decimal("10000")) -> Balance:
    return Balance(
        total=free,
        free=free,
        used=Decimal("0"),
        assets={"USDT": free},
        timestamp=datetime.now(tz=UTC),
    )


def _make_exchange_info(symbols: list[str] | None = None) -> ExchangeInfo:
    return ExchangeInfo(
        exchange_id=ExchangeID.BINANCE,
        symbols=symbols if symbols is not None else [str(_make_symbol())],
        timeframes=["1m", "5m"],
        maker_fee=Decimal("0.001"),
        taker_fee=Decimal("0.001"),
        fetched_at=datetime.now(tz=UTC),
    )


@pytest.fixture
def fake_kill_switch() -> MagicMock:
    ks = MagicMock()
    ks.is_active = False
    return ks


@pytest.fixture
def mock_pool() -> tuple[MagicMock, AsyncMock]:
    pool = MagicMock()
    adapter = AsyncMock()
    adapter.ping = AsyncMock(return_value=12.5)
    adapter.get_exchange_info = AsyncMock(return_value=_make_exchange_info())
    adapter.get_balance = AsyncMock(return_value=_make_balance())
    adapter.get_positions = AsyncMock(return_value=[])
    pool.get = MagicMock(return_value=adapter)
    return pool, adapter


def _make_validator(
    mock_pool: tuple[MagicMock, AsyncMock],
    fake_kill_switch: MagicMock,
    trading_mode: TradingMode = TradingMode.LIVE,
) -> PreflightValidator:
    pool, _adapter = mock_pool
    validator = PreflightValidator(pool, trading_mode)
    validator._kill_switch = fake_kill_switch  # type: ignore[assignment]
    return validator


def _live_risk_config(**overrides: object) -> SGRConfig:
    return SGRConfig(
        trading_mode=TradingMode.LIVE,
        risk_limits=RiskLimitsConfig(**overrides),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# NOT_SUPPORTED_CHECKS Transparenz
# ---------------------------------------------------------------------------


class TestNotSupportedChecks:
    async def test_not_supported_checks_present_in_every_result(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        """Undokumentierte Lücken dürfen nicht verschwiegen werden - jeder
        Report enthält sie explizit, unabhängig vom Modus."""
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.PAPER)
        result = await validator.validate(_make_order())

        reported_names = {c.name for c in result.checks if not c.supported}
        assert reported_names == set(NOT_SUPPORTED_CHECKS)

    async def test_not_supported_checks_do_not_block_eligibility(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)
        result = await validator.validate(_make_order(trading_mode=TradingMode.LIVE))

        assert result.eligible is True
        not_supported = [c for c in result.checks if not c.supported]
        assert all(not c.passed for c in not_supported)  # informativ "failed"...
        assert not any(  # ...aber nicht in failures() (die nur supported zaehlt)
            c.name in NOT_SUPPORTED_CHECKS for c in result.failures
        )


# ---------------------------------------------------------------------------
# PAPER Mode: keine echten Exchange-Checks
# ---------------------------------------------------------------------------


class TestPaperMode:
    async def test_paper_mode_skips_live_checks(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        pool, adapter = mock_pool
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.PAPER)

        result = await validator.validate(_make_order(trading_mode=TradingMode.PAPER))

        assert result.eligible is True
        adapter.ping.assert_not_awaited()
        adapter.get_balance.assert_not_awaited()
        pool.get.assert_not_called()

    async def test_paper_mode_still_rejects_structurally_invalid_order(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        """PAPER ist risikofrei ggü. echten Exchange-Zuständen, aber eine
        strukturell ungültige Order (Menge <= 0) bleibt in jedem Modus
        ungültig."""
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.PAPER)
        result = await validator.validate(
            _make_order(trading_mode=TradingMode.PAPER, quantity=Decimal("0"))
        )

        assert result.eligible is False
        assert any(c.name == "order_quantity_positive" for c in result.failures)

    async def test_paper_mode_ignores_kill_switch(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        """kill_switch_inactive wird nur im LIVE-Zweig ausgewertet - der
        verbindliche Kill-Switch-Schutz sitzt bereits in
        ExecutionEngine.execute() vor JEDEM Modus, Preflight dupliziert
        das für PAPER nicht."""
        fake_kill_switch.is_active = True
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.PAPER)

        result = await validator.validate(_make_order(trading_mode=TradingMode.PAPER))

        assert result.eligible is True
        assert not any(c.name == "kill_switch_inactive" for c in result.checks)


# ---------------------------------------------------------------------------
# LIVE Mode: positive Grundfälle
# ---------------------------------------------------------------------------


class TestLiveModeHappyPath:
    async def test_valid_live_order_is_eligible(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(_make_order(trading_mode=TradingMode.LIVE))

        assert isinstance(result, PreflightResult)
        assert result.eligible is True
        assert result.failures == []

    async def test_live_order_runs_all_expected_supported_checks(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)
        result = await validator.validate(_make_order(trading_mode=TradingMode.LIVE))

        supported_names = {c.name for c in result.checks if c.supported}
        assert supported_names == {
            "order_quantity_positive",
            "reduce_only_flag_present",
            "kill_switch_inactive",
            "connection_and_clock",
            "symbol_availability",
            "balance_and_available_capital",
            "leverage_within_limit",
            "reduce_only_position_safety",
            "max_order_notional_double_check",
        }


# ---------------------------------------------------------------------------
# LIVE Mode: Kill Switch
# ---------------------------------------------------------------------------


class TestLiveKillSwitch:
    async def test_active_kill_switch_blocks(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        fake_kill_switch.is_active = True
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(_make_order(trading_mode=TradingMode.LIVE))

        assert result.eligible is False
        assert any(c.name == "kill_switch_inactive" for c in result.failures)


# ---------------------------------------------------------------------------
# LIVE Mode: Invalid Credentials / Connection
# ---------------------------------------------------------------------------


class TestLiveCredentialsAndConnection:
    async def test_no_adapter_in_pool_blocks_all_further_checks(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        pool, _adapter = mock_pool
        pool.get = MagicMock(side_effect=KeyError("not in pool"))
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(_make_order(trading_mode=TradingMode.LIVE))

        assert result.eligible is False
        assert any(
            c.name == "exchange_credentials_and_connection" for c in result.failures
        )
        # fail-closed heisst: keine nachgelagerten Live-Checks mehr, keine
        # falschen "passed"-Ergebnisse fuer Checks, die nie liefen.
        ran_names = {c.name for c in result.checks}
        assert "balance_and_available_capital" not in ran_names

    async def test_ping_failure_fails_connection_check(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        _pool, adapter = mock_pool
        adapter.ping = AsyncMock(
            side_effect=ExchangeError("timeout", exchange="binance")
        )
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(_make_order(trading_mode=TradingMode.LIVE))

        assert result.eligible is False
        assert any(c.name == "connection_and_clock" for c in result.failures)

    async def test_rate_limit_error_fails_connection_check(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        """RateLimitError ist ein ExchangeError - Punkt 20 (reaktiv)."""
        _pool, adapter = mock_pool
        adapter.ping = AsyncMock(side_effect=RateLimitError(exchange="binance"))
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(_make_order(trading_mode=TradingMode.LIVE))

        assert result.eligible is False
        assert any(c.name == "connection_and_clock" for c in result.failures)


# ---------------------------------------------------------------------------
# LIVE Mode: Invalid Symbol
# ---------------------------------------------------------------------------


class TestLiveSymbolAvailability:
    async def test_symbol_not_listed_blocks(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        _pool, adapter = mock_pool
        adapter.get_exchange_info = AsyncMock(
            return_value=_make_exchange_info(symbols=["ETH/USDT:binance"])
        )
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(_make_order(trading_mode=TradingMode.LIVE))

        assert result.eligible is False
        assert any(c.name == "symbol_availability" for c in result.failures)

    async def test_exchange_info_fetch_failure_blocks(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        _pool, adapter = mock_pool
        adapter.get_exchange_info = AsyncMock(
            side_effect=ExchangeError("down", exchange="binance")
        )
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(_make_order(trading_mode=TradingMode.LIVE))

        assert result.eligible is False
        assert any(c.name == "symbol_availability" for c in result.failures)


# ---------------------------------------------------------------------------
# LIVE Mode: Invalid Quantity
# ---------------------------------------------------------------------------


class TestOrderQuantity:
    def test_zero_quantity_fails(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)
        result = validator._check_order_quantity_positive(
            _make_order(quantity=Decimal("0"))
        )
        assert result.passed is False

    def test_negative_quantity_fails(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)
        result = validator._check_order_quantity_positive(
            _make_order(quantity=Decimal("-1"))
        )
        assert result.passed is False

    def test_positive_quantity_passes(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)
        result = validator._check_order_quantity_positive(
            _make_order(quantity=Decimal("0.001"))
        )
        assert result.passed is True


# ---------------------------------------------------------------------------
# LIVE Mode: Insufficient Balance
# ---------------------------------------------------------------------------


class TestBalanceAndCapital:
    async def test_insufficient_free_balance_with_limit_price_fails(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        _pool, adapter = mock_pool
        adapter.get_balance = AsyncMock(return_value=_make_balance(free=Decimal("100")))
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        order = _make_order(
            trading_mode=TradingMode.LIVE,
            quantity=Decimal("1"),
            limit_price=Decimal("50000"),
            order_type=OrderType.LIMIT,
        )
        result = await validator.validate(order)

        assert result.eligible is False
        assert any(
            c.name == "balance_and_available_capital" for c in result.failures
        )

    async def test_sufficient_free_balance_with_limit_price_passes(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        _pool, adapter = mock_pool
        adapter.get_balance = AsyncMock(return_value=_make_balance(free=Decimal("10000")))
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        order = _make_order(
            trading_mode=TradingMode.LIVE,
            quantity=Decimal("0.1"),
            limit_price=Decimal("50000"),
            order_type=OrderType.LIMIT,
        )
        result = await validator.validate(order)

        assert result.eligible is True

    async def test_zero_free_balance_market_order_fails(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        _pool, adapter = mock_pool
        adapter.get_balance = AsyncMock(return_value=_make_balance(free=Decimal("0")))
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(
            _make_order(trading_mode=TradingMode.LIVE, order_type=OrderType.MARKET)
        )

        assert result.eligible is False
        assert any(
            c.name == "balance_and_available_capital" for c in result.failures
        )

    async def test_balance_fetch_failure_fails_closed(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        _pool, adapter = mock_pool
        adapter.get_balance = AsyncMock(
            side_effect=ExchangeError("down", exchange="binance")
        )
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(_make_order(trading_mode=TradingMode.LIVE))

        assert result.eligible is False
        assert any(
            c.name == "balance_and_available_capital" for c in result.failures
        )


# ---------------------------------------------------------------------------
# LIVE Mode: Leverage Violation
# ---------------------------------------------------------------------------


class TestLeverage:
    async def test_no_existing_position_passes(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        _pool, adapter = mock_pool
        adapter.get_positions = AsyncMock(return_value=[])
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(_make_order(trading_mode=TradingMode.LIVE))

        assert result.eligible is True

    async def test_leverage_within_limit_passes(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        _pool, adapter = mock_pool
        adapter.get_positions = AsyncMock(
            return_value=[_make_position(leverage=Decimal("3"))]
        )
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(_make_order(trading_mode=TradingMode.LIVE))

        assert result.eligible is True

    async def test_leverage_over_limit_fails(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        _pool, adapter = mock_pool
        adapter.get_positions = AsyncMock(
            return_value=[_make_position(leverage=Decimal("50"))]
        )
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(_make_order(trading_mode=TradingMode.LIVE))

        assert result.eligible is False
        assert any(c.name == "leverage_within_limit" for c in result.failures)

    async def test_positions_fetch_failure_fails_closed(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        _pool, adapter = mock_pool
        adapter.get_positions = AsyncMock(
            side_effect=ExchangeError("down", exchange="binance")
        )
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(_make_order(trading_mode=TradingMode.LIVE))

        assert result.eligible is False
        assert any(c.name == "leverage_within_limit" for c in result.failures)


# ---------------------------------------------------------------------------
# LIVE Mode: Reduce Only Position Safety
# ---------------------------------------------------------------------------


class TestReduceOnlySafety:
    async def test_non_reduce_only_order_skips_position_check(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)
        result = await validator.validate(
            _make_order(trading_mode=TradingMode.LIVE, reduce_only=False)
        )
        assert result.eligible is True

    async def test_reduce_only_without_open_position_fails(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        _pool, adapter = mock_pool
        adapter.get_positions = AsyncMock(return_value=[])
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(
            _make_order(trading_mode=TradingMode.LIVE, reduce_only=True)
        )

        assert result.eligible is False
        assert any(c.name == "reduce_only_position_safety" for c in result.failures)

    async def test_reduce_only_exceeding_position_size_fails(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        _pool, adapter = mock_pool
        adapter.get_positions = AsyncMock(
            return_value=[_make_position(quantity=Decimal("0.05"))]
        )
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(
            _make_order(
                trading_mode=TradingMode.LIVE,
                reduce_only=True,
                quantity=Decimal("0.1"),
            )
        )

        assert result.eligible is False
        assert any(c.name == "reduce_only_position_safety" for c in result.failures)

    async def test_reduce_only_within_position_size_passes(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        _pool, adapter = mock_pool
        adapter.get_positions = AsyncMock(
            return_value=[_make_position(quantity=Decimal("1.0"))]
        )
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(
            _make_order(
                trading_mode=TradingMode.LIVE,
                reduce_only=True,
                quantity=Decimal("0.5"),
            )
        )

        assert result.eligible is True

    async def test_reduce_only_positions_fetch_failure_fails_closed(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        _pool, adapter = mock_pool
        adapter.get_positions = AsyncMock(
            side_effect=ExchangeError("down", exchange="binance")
        )
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(
            _make_order(trading_mode=TradingMode.LIVE, reduce_only=True)
        )

        assert result.eligible is False
        assert any(c.name == "reduce_only_position_safety" for c in result.failures)


# ---------------------------------------------------------------------------
# LIVE Mode: Max Order Notional Violation (Double-Check, Baustein 4)
# ---------------------------------------------------------------------------


class TestMaxOrderNotionalDoubleCheck:
    def test_notional_within_limit_passes(
        self,
        mock_pool: tuple[MagicMock, AsyncMock],
        fake_kill_switch: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "sgr.execution.preflight.get_config",
            lambda: _live_risk_config(max_order_notional=Decimal("10000")),
        )
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)
        order = _make_order(
            trading_mode=TradingMode.LIVE,
            quantity=Decimal("0.1"),
            limit_price=Decimal("50000"),
        )
        result = validator._check_max_order_notional(order)
        assert result.passed is True

    def test_notional_exceeding_limit_fails(
        self,
        mock_pool: tuple[MagicMock, AsyncMock],
        fake_kill_switch: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "sgr.execution.preflight.get_config",
            lambda: _live_risk_config(max_order_notional=Decimal("1000")),
        )
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)
        order = _make_order(
            trading_mode=TradingMode.LIVE,
            quantity=Decimal("1"),
            limit_price=Decimal("50000"),
        )
        result = validator._check_max_order_notional(order)
        assert result.passed is False

    def test_market_order_skips_notional_check(
        self,
        mock_pool: tuple[MagicMock, AsyncMock],
        fake_kill_switch: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "sgr.execution.preflight.get_config",
            lambda: _live_risk_config(max_order_notional=Decimal("1")),
        )
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)
        order = _make_order(
            trading_mode=TradingMode.LIVE,
            order_type=OrderType.MARKET,
            limit_price=None,
        )
        result = validator._check_max_order_notional(order)
        assert result.passed is True

    def test_disabled_max_order_notional_passes(
        self,
        mock_pool: tuple[MagicMock, AsyncMock],
        fake_kill_switch: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "sgr.execution.preflight.get_config",
            lambda: _live_risk_config(max_order_notional=None),
        )
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)
        order = _make_order(
            trading_mode=TradingMode.LIVE,
            quantity=Decimal("1000"),
            limit_price=Decimal("50000"),
        )
        result = validator._check_max_order_notional(order)
        assert result.passed is True


# ---------------------------------------------------------------------------
# PreflightResult - Report-Struktur
# ---------------------------------------------------------------------------


class TestPreflightResultReport:
    async def test_rejection_summary_lists_all_failures(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        fake_kill_switch.is_active = True
        _pool, adapter = mock_pool
        adapter.get_balance = AsyncMock(return_value=_make_balance(free=Decimal("0")))
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)

        result = await validator.validate(_make_order(trading_mode=TradingMode.LIVE))

        assert "kill_switch_inactive" in result.rejection_summary
        assert "balance_and_available_capital" in result.rejection_summary

    async def test_order_id_matches_order(
        self, mock_pool: tuple[MagicMock, AsyncMock], fake_kill_switch: MagicMock
    ) -> None:
        validator = _make_validator(mock_pool, fake_kill_switch, TradingMode.LIVE)
        order = _make_order(trading_mode=TradingMode.LIVE)

        result = await validator.validate(order)

        assert result.order_id == str(order.id)
        assert result.trading_mode == TradingMode.LIVE
