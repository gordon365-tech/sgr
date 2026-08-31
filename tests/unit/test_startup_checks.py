"""
Tests für sgr.core.startup_checks.StartupSafetyChecker.

Baustein 5 (Phase 2 - Live Trading Safety Mechanisms): erweiterte Startup
Safety Checks. Vorher lief keinerlei explizite Fail-Fast-Prüfung vor dem
Boot - eine unsichere LIVE-Konfiguration (fehlende Credentials, deaktivierte
Fat-Finger-Caps, zu lasche Hard Limits, bereits aktiver Kill Switch) hätte
den Server scheinbar erfolgreich hochfahren lassen.

Teststrategie: jeder Check einzeln (unit-level über StartupSafetyChecker
direkt), plus Integration über run_or_raise() (Gesamtverhalten inkl.
Exception-Typ und Report-Struktur).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import SecretStr

from sgr.core.config import ExchangeCredentials, RiskLimitsConfig, SGRConfig
from sgr.core.startup_checks import StartupSafetyChecker, StartupSafetyError
from sgr.core.types import TradingMode
from sgr.risk.kill_switch import get_kill_switch


def _live_config(**risk_overrides: object) -> SGRConfig:
    """Baut eine LIVE-Config mit vollständigen Credentials und optional
    überschriebenen Risk Limits."""
    return SGRConfig(
        trading_mode=TradingMode.LIVE,
        risk_limits=RiskLimitsConfig(**risk_overrides),  # type: ignore[arg-type]
        credentials=ExchangeCredentials(
            pionex_live_api_key=SecretStr("real_key"),
            pionex_live_secret=SecretStr("real_secret"),
        ),
    )


@pytest.fixture(autouse=True)
def _reset_kill_switches() -> None:
    """Kill Switches sind Prozess-weite Singletons (get_kill_switch).
    Zustand vor und nach jedem Test zurücksetzen, damit Tests sich nicht
    gegenseitig beeinflussen (analog zum StrategyRegistry-Isolationsmuster,
    das in einer früheren Session als Bug dokumentiert wurde)."""
    for mode in (TradingMode.PAPER, TradingMode.LIVE):
        ks = get_kill_switch(mode)
        ks._state.is_active = False
    yield
    for mode in (TradingMode.PAPER, TradingMode.LIVE):
        ks = get_kill_switch(mode)
        ks._state.is_active = False


class TestKillSwitchNotPreactivated:
    def test_passes_when_inactive(self) -> None:
        config = SGRConfig(trading_mode=TradingMode.PAPER)
        result = StartupSafetyChecker(config)._check_kill_switch_not_preactivated()
        assert result.passed is True

    def test_fails_when_already_active(self) -> None:
        config = SGRConfig(trading_mode=TradingMode.PAPER)
        get_kill_switch(TradingMode.PAPER)._state.is_active = True

        result = StartupSafetyChecker(config)._check_kill_switch_not_preactivated()
        assert result.passed is False
        assert "already active" in result.detail

    def test_checked_regardless_of_trading_mode(self) -> None:
        """Dieser Check läuft auch in PAPER Mode - ein bereits aktiver Kill
        Switch beim Boot ist immer verwirrend, unabhängig vom Modus."""
        config = SGRConfig(trading_mode=TradingMode.PAPER)
        report = StartupSafetyChecker(config).run()
        names = [c.name for c in report.checks]
        assert "kill_switch_not_preactivated" in names


class TestLiveCredentialsPresent:
    def test_passes_with_full_credentials(self) -> None:
        config = _live_config()
        result = StartupSafetyChecker(config)._check_live_credentials_present()
        assert result.passed is True

    def test_fails_without_credentials(self) -> None:
        config = SGRConfig(
            trading_mode=TradingMode.LIVE,
            credentials=ExchangeCredentials(),
        )
        result = StartupSafetyChecker(config)._check_live_credentials_present()
        assert result.passed is False
        assert "not configured" in result.detail.lower()

    def test_not_run_in_paper_mode(self) -> None:
        """PAPER Mode benötigt keine Live-Credentials - dieser Check darf
        dort gar nicht erst ausgeführt werden."""
        config = SGRConfig(
            trading_mode=TradingMode.PAPER,
            credentials=ExchangeCredentials(),
        )
        report = StartupSafetyChecker(config).run()
        names = [c.name for c in report.checks]
        assert "live_credentials_present" not in names


class TestRiskLimitsNotDisabled:
    def test_passes_with_default_limits(self) -> None:
        config = _live_config()
        result = StartupSafetyChecker(config)._check_risk_limits_not_disabled()
        assert result.passed is True

    def test_fails_when_drawdown_at_ceiling(self) -> None:
        config = _live_config(max_portfolio_drawdown=0.50)
        result = StartupSafetyChecker(config)._check_risk_limits_not_disabled()
        assert result.passed is False
        assert "max_portfolio_drawdown" in result.detail

    def test_fails_when_daily_loss_limit_at_ceiling(self) -> None:
        config = _live_config(daily_loss_limit=0.20)
        result = StartupSafetyChecker(config)._check_risk_limits_not_disabled()
        assert result.passed is False
        assert "daily_loss_limit" in result.detail

    def test_reports_multiple_problems_together(self) -> None:
        config = _live_config(max_portfolio_drawdown=0.50, daily_loss_limit=0.20)
        result = StartupSafetyChecker(config)._check_risk_limits_not_disabled()
        assert result.passed is False
        assert "max_portfolio_drawdown" in result.detail
        assert "daily_loss_limit" in result.detail


class TestMaxLeverageSane:
    def test_passes_within_bounds(self) -> None:
        config = _live_config(max_leverage=Decimal("3.0"))
        result = StartupSafetyChecker(config)._check_max_leverage_sane()
        assert result.passed is True

    def test_fails_above_ceiling(self) -> None:
        config = _live_config(max_leverage=Decimal("20.0"))
        result = StartupSafetyChecker(config)._check_max_leverage_sane()
        assert result.passed is False
        assert "max_leverage" in result.detail

    def test_boundary_exactly_ten_passes(self) -> None:
        """max_leverage == 10 ist noch erlaubt (Grenze ist > 10, nicht >=)."""
        config = _live_config(max_leverage=Decimal("10.0"))
        result = StartupSafetyChecker(config)._check_max_leverage_sane()
        assert result.passed is True


class TestMaxOrderNotionalSane:
    def test_passes_when_configured(self) -> None:
        config = _live_config(max_order_notional=Decimal("10000"))
        result = StartupSafetyChecker(config)._check_max_order_notional_sane()
        assert result.passed is True

    def test_fails_when_disabled(self) -> None:
        config = _live_config(max_order_notional=None)
        result = StartupSafetyChecker(config)._check_max_order_notional_sane()
        assert result.passed is False
        assert "disabled" in result.detail.lower()

    def test_not_run_in_paper_mode(self) -> None:
        config = SGRConfig(
            trading_mode=TradingMode.PAPER,
            risk_limits=RiskLimitsConfig(max_order_notional=None),
        )
        report = StartupSafetyChecker(config).run()
        names = [c.name for c in report.checks]
        assert "max_order_notional_sane" not in names


class TestRunOrRaise:
    def test_paper_mode_all_pass_by_default(self) -> None:
        config = SGRConfig(trading_mode=TradingMode.PAPER)
        report = StartupSafetyChecker(config).run_or_raise()
        assert report.all_passed is True

    def test_live_mode_all_pass_with_sane_config(self) -> None:
        config = _live_config()
        report = StartupSafetyChecker(config).run_or_raise()
        assert report.all_passed is True
        # Alle vier LIVE-spezifischen Checks + der generelle Kill-Switch-Check.
        assert len(report.checks) == 5

    def test_live_mode_missing_credentials_raises(self) -> None:
        config = SGRConfig(
            trading_mode=TradingMode.LIVE,
            credentials=ExchangeCredentials(),
        )
        with pytest.raises(StartupSafetyError, match="live_credentials_present"):
            StartupSafetyChecker(config).run_or_raise()

    def test_live_mode_disabled_order_cap_raises(self) -> None:
        config = _live_config(max_order_notional=None)
        with pytest.raises(StartupSafetyError, match="max_order_notional_sane"):
            StartupSafetyChecker(config).run_or_raise()

    def test_live_mode_preactivated_kill_switch_raises(self) -> None:
        get_kill_switch(TradingMode.LIVE)._state.is_active = True
        config = _live_config()
        with pytest.raises(StartupSafetyError, match="kill_switch_not_preactivated"):
            StartupSafetyChecker(config).run_or_raise()

    def test_multiple_failures_all_reported_in_single_exception(self) -> None:
        """run() bricht nicht beim ersten Fehler ab - alle Probleme sollen
        in einer einzigen Fehlermeldung sichtbar sein (besseres Debugging
        einer fehlgeschlagenen Config als mehrere Boot-Versuche)."""
        config = SGRConfig(
            trading_mode=TradingMode.LIVE,
            credentials=ExchangeCredentials(),
            risk_limits=RiskLimitsConfig(max_order_notional=None),
        )
        with pytest.raises(StartupSafetyError) as exc_info:
            StartupSafetyChecker(config).run_or_raise()

        message = str(exc_info.value)
        assert "live_credentials_present" in message
        assert "max_order_notional_sane" in message

    def test_report_failures_property(self) -> None:
        config = SGRConfig(
            trading_mode=TradingMode.LIVE,
            credentials=ExchangeCredentials(),
        )
        report = StartupSafetyChecker(config).run()
        assert report.all_passed is False
        failure_names = [c.name for c in report.failures]
        assert "live_credentials_present" in failure_names
