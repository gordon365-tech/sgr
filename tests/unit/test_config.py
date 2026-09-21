"""
Tests for configuration.
Config must validate constraints at startup – fail fast, never silently.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sgr.core.config import (
    EncryptionConfig,
    ExchangeCredentials,
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
        with pytest.raises(Exception):
            RiskLimitsConfig(max_portfolio_drawdown=0.0)  # below min
        with pytest.raises(Exception):
            RiskLimitsConfig(max_portfolio_drawdown=0.99)  # above max


class TestEncryptionConfig:
    def test_short_key_raises(self) -> None:
        with pytest.raises(Exception):
            EncryptionConfig(master_key="short")  # type: ignore


class TestSGRConfig:
    def test_default_is_paper_mode(self) -> None:
        config = SGRConfig()
        assert config.trading_mode == TradingMode.PAPER
        assert config.is_paper is True
        assert config.is_live is False

    def test_production_live_requires_changed_secrets(self) -> None:
        """Production + Live must not use default secrets."""
        with pytest.raises(Exception):
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


class TestPaperInitialCapital:
    """
    Root-Cause-Fund (Asset-Universe/Paper-Capital-Audit): main.py's
    lifespan() instanziierte PortfolioEngine bisher OHNE initial_cash zu
    uebergeben - der Klassendefault wurde deshalb immer verwendet, egal
    was konfiguriert war (es gab bis dahin gar keine env var dafuer).
    Diese Tests decken die Config-Seite der Behebung ab; die main.py-
    Verdrahtung selbst siehe test_main_lifespan.py (falls vorhanden)
    bzw. wurde live am laufenden Stack verifiziert (siehe Abschlussbericht).
    """

    def test_default_matches_previous_hardcoded_behavior(self) -> None:
        config = SGRConfig()
        assert config.paper_initial_capital == Decimal("10000")

    def test_configurable_via_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PAPER_INITIAL_CAPITAL", "25000")
        config = SGRConfig()
        assert config.paper_initial_capital == Decimal("25000")

    def test_zero_or_negative_capital_rejected(self) -> None:
        with pytest.raises(Exception):
            SGRConfig(paper_initial_capital=Decimal("0"))
        with pytest.raises(Exception):
            SGRConfig(paper_initial_capital=Decimal("-100"))


class TestSGRConfigTenantId:
    """
    Commit 5 (Option A): tenant_id steuert, ob lifespan() Exchange-
    Credentials aus der DB (Multi-Tenant-Worker) oder aus .env
    (Single-Tenant, unveraendertes Verhalten) laedt.
    """

    def test_default_tenant_id_is_none(self) -> None:
        config = SGRConfig()
        assert config.tenant_id is None

    def test_tenant_id_from_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TENANT_ID", "gordon")
        config = SGRConfig()
        assert config.tenant_id == "gordon"

    def test_tenant_id_explicit_constructor_arg(self) -> None:
        config = SGRConfig(tenant_id="sumo")
        assert config.tenant_id == "sumo"


class TestExchangeCredentialsEnvFileLoading:
    """
    Bugfix (Pionex Live Read-Only Verification, gefunden waehrend eines
    echten Laufs gegen einen realen Account): SGRConfig.credentials wird
    per `Field(default_factory=ExchangeCredentials)` gebaut -
    `ExchangeCredentials()` ist eine EIGENSTAENDIGE BaseSettings-Instanz
    und erbte das `env_file=".env"` von SGRConfig bisher NICHT. Damit
    konnte `python scripts/verify_pionex_live_read_only.py --yes` (und
    jeder andere reine .env-basierte, nicht-Docker-Aufruf) Pionex-
    Credentials aus .env nie finden, obwohl .env.example genau das
    suggeriert. Siehe sgr/core/config.py ExchangeCredentials.model_config
    fuer die eigentliche Behebung; diese Tests verifizieren sie isoliert
    von jedem echten Repo-.env (eigenes tmp_path-Arbeitsverzeichnis).
    """

    def test_reads_pionex_credentials_from_env_file_without_process_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.delenv("PIONEX_LIVE_API_KEY", raising=False)
        monkeypatch.delenv("PIONEX_LIVE_SECRET", raising=False)
        (tmp_path / ".env").write_text(
            "PIONEX_LIVE_API_KEY=dotenv_test_key\nPIONEX_LIVE_SECRET=dotenv_test_secret\n"
        )
        monkeypatch.chdir(tmp_path)

        credentials = ExchangeCredentials()
        result = credentials.get_credentials("pionex", TradingMode.LIVE)

        assert result["apiKey"] == "dotenv_test_key"
        assert result["secret"] == "dotenv_test_secret"

    def test_real_process_env_var_takes_priority_over_env_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Sicherheitsgarantie fuer Docker (siehe sgr/core/config.py
        Kommentar): ein bereits im echten Prozess-Environment gesetzter
        Wert (z.B. via docker-compose env_file-Direktive) darf NIEMALS
        von einer zufaellig vorhandenen .env-Datei ueberschrieben werden."""
        monkeypatch.setenv("PIONEX_LIVE_API_KEY", "real_process_env_key")
        monkeypatch.setenv("PIONEX_LIVE_SECRET", "real_process_env_secret")
        (tmp_path / ".env").write_text(
            "PIONEX_LIVE_API_KEY=stale_dotenv_key\nPIONEX_LIVE_SECRET=stale_dotenv_secret\n"
        )
        monkeypatch.chdir(tmp_path)

        credentials = ExchangeCredentials()
        result = credentials.get_credentials("pionex", TradingMode.LIVE)

        assert result["apiKey"] == "real_process_env_key"
        assert result["secret"] == "real_process_env_secret"

    def test_env_file_override_none_ignores_env_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Escape Hatch fuer Tests, die bewusst 'gar keine Credentials
        konfiguriert' simulieren wollen (siehe test_pionex.py,
        test_startup_checks.py) - muss den Fallback vollstaendig
        deaktivieren koennen, unabhaengig vom Arbeitsverzeichnis."""
        monkeypatch.delenv("PIONEX_LIVE_API_KEY", raising=False)
        monkeypatch.delenv("PIONEX_LIVE_SECRET", raising=False)
        (tmp_path / ".env").write_text(
            "PIONEX_LIVE_API_KEY=should_not_be_read\nPIONEX_LIVE_SECRET=should_not_be_read\n"
        )
        monkeypatch.chdir(tmp_path)

        credentials = ExchangeCredentials(_env_file=None)

        with pytest.raises(ValueError, match="Credentials not configured"):
            credentials.get_credentials("pionex", TradingMode.LIVE)

    def test_missing_everywhere_still_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.delenv("PIONEX_LIVE_API_KEY", raising=False)
        monkeypatch.delenv("PIONEX_LIVE_SECRET", raising=False)
        monkeypatch.chdir(tmp_path)  # kein .env vorhanden

        credentials = ExchangeCredentials()

        with pytest.raises(ValueError, match="Credentials not configured"):
            credentials.get_credentials("pionex", TradingMode.LIVE)

    def test_binance_credentials_also_benefit_from_the_fix(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Der Fix ist exchange-uebergreifend (ExchangeCredentials selbst,
        nicht Pionex-spezifisch) - Binance profitiert identisch, keine
        Pionex-Sonderlogik noetig."""
        monkeypatch.delenv("BINANCE_LIVE_API_KEY", raising=False)
        monkeypatch.delenv("BINANCE_LIVE_SECRET", raising=False)
        (tmp_path / ".env").write_text(
            "BINANCE_LIVE_API_KEY=binance_dotenv_key\nBINANCE_LIVE_SECRET=binance_dotenv_secret\n"
        )
        monkeypatch.chdir(tmp_path)

        credentials = ExchangeCredentials()
        result = credentials.get_credentials("binance", TradingMode.LIVE)

        assert result["apiKey"] == "binance_dotenv_key"
        assert result["secret"] == "binance_dotenv_secret"
