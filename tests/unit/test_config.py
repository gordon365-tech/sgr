"""
Tests for configuration.
Config must validate constraints at startup – fail fast, never silently.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sgr.core.config import (
    EncryptionConfig,
    RiskLimitsConfig,
    SGRConfig,
    get_config,
)
from sgr.core.types import Environment, TradingMode


class TestRiskLimitsConfig:
    def test_defaults_are_conservative(self) -> None:
        limits = RiskLimitsConfig()
        assert limits.max_portfolio_drawdown == 0.15
        assert limits.daily_loss_limit == 0.05
        assert limits.max_single_position_pct == 0.10

    def test_drawdown_bounds(self) -> None:
        with pytest.raises(ValidationError):
            RiskLimitsConfig(max_portfolio_drawdown=0.0)  # below min
        with pytest.raises(ValidationError):
            RiskLimitsConfig(max_portfolio_drawdown=0.99)  # above max


class TestEncryptionConfig:
    def test_short_key_raises(self) -> None:
        with pytest.raises(ValidationError):
            EncryptionConfig(master_key="short")  # type: ignore


class TestSGRConfig:
    def test_default_is_paper_mode(self) -> None:
        config = SGRConfig()
        assert config.trading_mode == TradingMode.PAPER
        assert config.is_paper is True
        assert config.is_live is False

    def test_production_live_requires_changed_secrets(self) -> None:
        """Production + Live must not use default secrets."""
        with pytest.raises(ValidationError):
            SGRConfig(
                environment=Environment.PRODUCTION,
                trading_mode=TradingMode.LIVE,
                # default secret keys → must raise
            )

    def test_get_config_cached(self) -> None:
        get_config.cache_clear()
        c1 = get_config()
        c2 = get_config()
        assert c1 is c2  # same object
        get_config.cache_clear()
