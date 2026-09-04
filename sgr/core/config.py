"""
SGR Configuration
=================
Single source of truth for all settings.
Loaded once at startup from environment variables.

Design decisions:
- pydantic-settings: type-safe, validated at startup (fail fast)
- Secrets never logged (SecretStr)
- Separate DB URLs for paper vs live (isolation)
- All limits configurable (no hardcoded values in business logic)
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sgr.core.types import Environment, ExchangeID, TradingMode


class DatabaseConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DB_", extra="ignore")

    host: str = "localhost"
    port: int = 5432
    name: str = "sgr"
    user: str = "sgr"
    password: SecretStr = SecretStr("changeme")
    pool_size: int = 10
    max_overflow: int = 20

    @property
    def url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.user}:"
            f"{self.password.get_secret_value()}@"
            f"{self.host}:{self.port}/{self.name}"
        )

    @property
    def url_sync(self) -> str:
        return (
            f"postgresql://{self.user}:"
            f"{self.password.get_secret_value()}@"
            f"{self.host}:{self.port}/{self.name}"
        )


class RedisConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REDIS_", extra="ignore")

    host: str = "localhost"
    port: int = 6379
    password: SecretStr | None = None
    db: int = 0
    max_connections: int = 50

    @property
    def url(self) -> str:
        if self.password:
            return f"redis://:{self.password.get_secret_value()}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"


class RiskLimitsConfig(BaseSettings):
    """
    All risk limits configurable per environment.
    Production defaults are conservative – loosen deliberately.
    """

    model_config = SettingsConfigDict(env_prefix="RISK_", extra="ignore")

    # Hard Limits (trigger Kill Switch)
    max_portfolio_drawdown: float = Field(default=0.15, ge=0.01, le=0.50)
    daily_loss_limit: float = Field(default=0.05, ge=0.01, le=0.20)
    max_single_position_pct: float = Field(default=0.10, ge=0.01, le=0.30)

    # Soft Limits (warnings + size reduction)
    var_95_limit: float = Field(default=0.03, ge=0.005, le=0.10)
    portfolio_heat_limit: float = Field(default=0.70, ge=0.20, le=1.00)
    max_correlation_exposure: float = Field(default=0.80, ge=0.30, le=1.00)
    max_slippage_pct: float = Field(default=0.003, ge=0.001, le=0.02)

    # Futures-specific
    max_leverage: Decimal = Field(default=Decimal("3.0"))
    max_open_positions: int = Field(default=10, ge=1, le=50)

    # Exchange connectivity
    exchange_timeout_seconds: int = Field(default=30, ge=5, le=120)

    # Performance monitoring (trigger deactivation)
    min_sharpe_ratio: float = Field(default=0.5)
    min_hit_rate: float = Field(default=0.35)
    strategy_evaluation_window_days: int = Field(default=30)

    # Cooldown nach Trades (pro Symbol+Strategie), verhindert
    # Overtrading/Signal-Flackern direkt nach einem ausgeführten Trade.
    trade_cooldown_seconds: int = Field(default=300, ge=0, le=86400)

    # Absoluter Hard Cap für den Notional-Wert (qty * price) einer
    # einzelnen Order, unabhängig vom Portfolio-Wert. Schützt gegen
    # Fat-Finger-Fehler und Konfigurationsfehler (z.B. size_hint=1.0
    # bei ungewöhnlich hohem Portfolio-Wert nach starkem Wachstum).
    # None = deaktiviert. Getrennt von max_single_position_pct, welches
    # relativ zum Portfolio-Wert begrenzt - dieser Cap ist absolut und
    # greift zusätzlich, unabhängig davon wie groß das Portfolio ist.
    max_order_notional: Decimal | None = Field(default=Decimal("10000"))


class ExchangeCredentials(BaseSettings):
    """
    Per-exchange credentials.
    Paper and live are ALWAYS separate keys.
    """

    model_config = SettingsConfigDict(extra="ignore")

    # Binance – Paper (Testnet)
    binance_paper_api_key: SecretStr | None = None
    binance_paper_secret: SecretStr | None = None
    binance_paper_testnet: bool = True

    # Binance – Live
    binance_live_api_key: SecretStr | None = None
    binance_live_secret: SecretStr | None = None

    # Pionex – Paper
    pionex_paper_api_key: SecretStr | None = None
    pionex_paper_secret: SecretStr | None = None

    # Pionex – Live
    pionex_live_api_key: SecretStr | None = None
    pionex_live_secret: SecretStr | None = None

    def get_credentials(
        self,
        exchange_id: str,
        trading_mode: TradingMode,
    ) -> dict[str, Any]:
        """
        Returns decrypted credentials for a specific exchange + mode.
        Raises if credentials are not configured.
        Never logs the returned values.
        """
        prefix = f"{exchange_id}_{trading_mode.value}"
        api_key_field = f"{prefix}_api_key"
        secret_field = f"{prefix}_secret"

        api_key: SecretStr | None = getattr(self, api_key_field, None)
        secret: SecretStr | None = getattr(self, secret_field, None)

        if api_key is None or secret is None:
            raise ValueError(
                f"Credentials not configured for {exchange_id} in {trading_mode.value} mode. "
                f"Set {api_key_field.upper()} and {secret_field.upper()} env vars."
            )

        result: dict[str, Any] = {
            "apiKey": api_key.get_secret_value(),
            "secret": secret.get_secret_value(),
        }

        # Testnet flag for paper mode
        testnet_field = f"{prefix}_testnet"
        if hasattr(self, testnet_field):
            result["testnet"] = getattr(self, testnet_field)

        return result


class APIConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="API_", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    secret_key: SecretStr = SecretStr("change-me-in-production-min-32-chars")
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 30
    algorithm: str = "HS256"
    cors_origins: list[str] = Field(default=["http://localhost:3000"])
    rate_limit_per_minute: int = 60


class EncryptionConfig(BaseSettings):
    """
    For encrypting API keys at rest.
    KEK (Key Encryption Key) never stored in DB.
    """

    model_config = SettingsConfigDict(env_prefix="ENCRYPTION_", extra="ignore")

    master_key: SecretStr = SecretStr("change-me-32-byte-key-for-prod!!")

    @field_validator("master_key")
    @classmethod
    def validate_key_length(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32:
            raise ValueError("ENCRYPTION_MASTER_KEY must be at least 32 characters")
        return v


class MonitoringConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONITORING_", extra="ignore")

    prometheus_port: int = 9090
    enable_tracing: bool = True
    log_level: str = "INFO"
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    sentry_dsn: SecretStr | None = None


class SGRConfig(BaseSettings):
    """
    Master config. All sub-configs loaded from environment.
    Usage: config = get_config()
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = Environment.DEVELOPMENT
    trading_mode: TradingMode = TradingMode.PAPER
    app_name: str = "SGR"
    version: str = "0.1.0"

    # Welche Exchange der Lifecycle standardmaessig verwendet (Market Data
    # Subscriptions + Exchange Pool). Default bleibt PIONEX fuer
    # Abwaertskompatibilitaet; per PRIMARY_EXCHANGE=binance env var
    # umschaltbar, z.B. solange Pionex nicht via ccxt unterstuetzt wird.
    primary_exchange: ExchangeID = ExchangeID.PIONEX

    # Sub-configs (nested, loaded from env with prefixes)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    risk_limits: RiskLimitsConfig = Field(default_factory=RiskLimitsConfig)
    credentials: ExchangeCredentials = Field(default_factory=ExchangeCredentials)
    api: APIConfig = Field(default_factory=APIConfig)
    encryption: EncryptionConfig = Field(default_factory=EncryptionConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)

    @model_validator(mode="after")
    def validate_production_constraints(self) -> SGRConfig:
        """
        Enforce production safety rules.
        Live trading in production requires explicit configuration.
        """
        if self.environment == Environment.PRODUCTION and self.trading_mode == TradingMode.LIVE:
            # Ensure not using default secret key
            if self.api.secret_key.get_secret_value() == "change-me-in-production-min-32-chars":
                raise ValueError("API_SECRET_KEY must be changed from default in production!")
            if self.encryption.master_key.get_secret_value() == "change-me-32-byte-key-for-prod!!":
                raise ValueError(
                    "ENCRYPTION_MASTER_KEY must be changed from default in production!"
                )
            if self.api.debug:
                raise ValueError("API_DEBUG must be False in production!")

        return self

    @property
    def is_live(self) -> bool:
        return self.trading_mode == TradingMode.LIVE

    @property
    def is_paper(self) -> bool:
        return self.trading_mode == TradingMode.PAPER

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION


@lru_cache(maxsize=1)
def get_config() -> SGRConfig:
    """
    Singleton config. Loaded once, cached forever.
    Tests should call get_config.cache_clear() to reset.
    """
    return SGRConfig()
