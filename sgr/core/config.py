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

from datetime import datetime
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

    # ------------------------------------------------------------------
    # TEST_1X / kontrollierter Funktionstest-Risk-Profile (Baustein:
    # Paper-Trading-Lifecycle-Parity). Rein additiv - alle Felder haben
    # konservative Defaults, die das bestehende Verhalten NICHT
    # veraendern, solange sie nicht ueber RISK_-env-vars gesetzt werden
    # (Ausnahme: default_leverage/stop_loss_pct/take_profit_pct/
    # max_holding_minutes/risk_per_trade_pct/paper_taker_fee_pct/
    # paper_slippage_pct sind neue, bisher nicht existierende Controls -
    # ihre Defaults spiegeln das TEST_1X-Profil, sind aber inaktiv fuer
    # bestehende Deployments, solange position_size_usd None bleibt,
    # siehe PositionSizer.compute() Fixed-Notional-Zweig).
    #
    # risk_profile_name ist reines Audit-/Log-Label - KEINE
    # dynamische Profil-Umschalt-Logik. Ein spaeteres automatisches
    # Risk-Level-Scaling (siehe Aufgabenstellung "RISK SCALING FUER
    # SPAETER") ist bewusst NICHT Teil dieses Feldes und darf erst
    # aktiviert werden, wenn das TEST_1X-Baseline-Profil nachweislich
    # funktioniert.
    risk_profile_name: str = Field(default="TEST_1X")

    # Fixer Notional-Zielwert pro Position in Quote-Currency (z.B. USD),
    # z.B. 20 fuer den ersten Funktionstest. None = bestehendes,
    # adaptives Sizing (ATR/Kelly/Heat, siehe PositionSizer) bleibt
    # unveraendert aktiv - dieses Feld ist ein OPT-IN pro Profil, kein
    # Ersatz des bestehenden Verhaltens.
    position_size_usd: Decimal | None = Field(default=None, gt=0)

    # Default-Leverage, die ExecutionEngine vor jeder eroeffnenden Order
    # explizit auf der Exchange setzt (siehe CCXTBaseAdapter.set_leverage).
    # 1 = kein Hebel (TEST_1X-Baseline).
    default_leverage: Decimal = Field(default=Decimal("1"), ge=Decimal("1"))

    # Stop-Loss/Take-Profit als Prozent-Abstand vom Entry-Preis
    # (richtungsabhaengig in PositionProtectionManager angewendet).
    stop_loss_pct: float = Field(default=0.01, gt=0.0, le=0.5)
    take_profit_pct: float = Field(default=0.02, gt=0.0, le=1.0)

    # Max Holding Time in Minuten, danach zwingender Exit ueber den
    # normalen Order-Lifecycle (siehe PositionProtectionWatchdog).
    max_holding_minutes: int = Field(default=30, ge=1, le=10_080)

    # Maximal zulaessiges Risiko pro Trade als Anteil des Account-
    # Kapitals (nicht des Notional-Werts!) - siehe PositionSizer
    # Fixed-Notional-Zweig: wird die bei stop_loss_pct implizierte
    # Verlusthoehe bei position_size_usd groesser als dieser Anteil des
    # verfuegbaren Kapitals, wird die Order abgelehnt statt automatisch
    # verkleinert oder vergroessert.
    risk_per_trade_pct: float = Field(default=0.01, gt=0.0, le=0.20)

    # Globaler Exposure-/Capital-Allocation-Cap (2026-09-24, operative
    # Anweisung "dynamisches 25-Prozent-Exposure-Limit") - NICHT zu
    # verwechseln mit risk_per_trade_pct (max. Verlust EINES Trades bei
    # SL-Treffer) oder max_single_position_pct (Groesse EINER Position).
    # Begrenzt die SUMME aller gleichzeitig offenen direktionalen
    # Positionen (Notional) auf diesen Anteil der aktuellen Account-
    # Equity (PortfolioEngine.portfolio_value, dynamisch - nicht ein
    # einmalig fixierter USD-Betrag). None (Default) = deaktiviert,
    # identisches Verhalten zu vorher fuer jedes bestehende Profil, das
    # diese Variable nicht setzt - siehe RiskEngine._evaluate_internal()
    # fuer den harten Reject-Check (kein Downsizing wie beim Leverage-
    # Cap, explizit als Ablehnung gefordert). Grid-Exposure hat einen
    # eigenen, analogen Parameter (GridRiskLimitsConfig.
    # max_total_exposure_pct), da GridRiskEngine ein separates,
    # eigenstaendiges Limit-System ist (siehe dortigen Docstring) - beide
    # sollten auf denselben Wert gesetzt werden, wenn ein einheitliches
    # Gesamt-Cap gewuenscht ist.
    max_total_exposure_pct: float | None = Field(default=None, gt=0.0, le=1.0)

    # Ab diesem Zeitpunkt (UTC) geoeffnete Positionen erhalten SL/TP/
    # Max-Holding-Time-Schutz. None = Feature global inaktiv. Bereits
    # VOR diesem Zeitpunkt offene ("Legacy"-)Positionen werden davon
    # bewusst NICHT erfasst - sie sollen nicht durch einen Deploy
    # unbeaufsichtigt und gleichzeitig geschlossen werden, siehe
    # Modul-Docstring in sgr/risk/position_protection.py.
    protection_cutover_at: datetime | None = Field(default=None)

    # Zentrale, konfigurierbare Paper-Simulationswerte (ersetzen die
    # zuvor in CCXTBaseAdapter._simulate_order() hartkodierten
    # Konstanten 0.001/0.0005). Realistische Binance-USDT-M-Futures-
    # Taker-Fee liegt bei ca. 0.04-0.05% (VIP0), nicht bei den zuvor
    # verwendeten 0.1% (das ist der Spot-Default). Slippage bewusst
    # klein aber nicht Null gehalten (siehe Aufgabenstellung: weder
    # unrealistisch niedrig noch kuenstlich extrem).
    paper_taker_fee_pct: float = Field(default=0.0005, ge=0.0, le=0.01)
    paper_slippage_pct: float = Field(default=0.0005, ge=0.0, le=0.01)

    # Maker-Fee (2026-09-23, Phase I - Grid-Paper-Limit-Order-Semantik):
    # bisher gab es in der Paper-Simulation NUR eine Taker-Fee, weil
    # Market-Orders strukturell immer Taker sind (siehe
    # CCXTBaseAdapter._simulate_order()). Ein Futures-Grid-Level-Fill
    # (siehe GridController._fill_level(): eine per Preis-Crossing
    # ausgeloeste Order, die wirtschaftlich einer ruhenden, gefuellten
    # Limit-Order entspricht) ist dagegen typischerweise ein Maker-Fill -
    # realistische Binance-USDT-M-Futures-Maker-Fee liegt bei ca.
    # 0.02% (VIP0), niedriger als die Taker-Fee. Nur verwendet, wenn
    # order.metadata["grid_fill_type"] == "level_cross" (siehe dort) -
    # unveraendertes Taker-Verhalten fuer JEDE andere Order (direktional
    # UND Grid-Force-Exits).
    paper_maker_fee_pct: float = Field(default=0.0002, ge=0.0, le=0.01)


class ExchangeCredentials(BaseSettings):
    """
    Per-exchange credentials.
    Paper and live are ALWAYS separate keys.
    """

    # Bugfix (Pionex Live Read-Only Verification, live gegen echten
    # Account nachgewiesen): SGRConfig.credentials wird per
    # `Field(default_factory=ExchangeCredentials)` gebaut - das ruft
    # `ExchangeCredentials()` OHNE Argumente auf und erzeugt damit eine
    # EIGENSTAENDIGE BaseSettings-Instanz mit IHRER EIGENEN
    # Sources-Konfiguration. SGRConfig's env_file=".env" (siehe unten)
    # wird an diese verschachtelte Instanz NICHT vererbt - ohne dieses
    # env_file HIER liest ExchangeCredentials Credentials ausschliesslich
    # aus dem tatsaechlichen Prozess-Environment (os.environ), NIEMALS
    # aus einer .env-Datei, obwohl .env.example genau das suggeriert und
    # scripts/verify_pionex_live_read_only.py (Usage-Docstring) genau
    # das voraussetzt.
    #
    # Docker bleibt unveraendert/regressionsfrei: docker-compose setzt
    # PIONEX_*/BINANCE_*-Werte bereits ueber `env_file:`-Direktiven als
    # ECHTE Prozess-Environment-Variablen (siehe docker/docker-compose*.
    # yml Kommentare "env_file laedt .env explizit"). pydantic-settings'
    # Standard-Quellenreihenfolge ist init > env (os.environ) > dotenv >
    # file secrets - eine hier zusaetzlich gelesene .env-Datei wirkt
    # daher NUR als Fallback fuer Werte, die im echten Environment noch
    # fehlen, und ueberschreibt nie einen bereits gesetzten echten
    # Environment-Wert. Fuer ein direktes `python scripts/...` ausserhalb
    # von Docker (kein Prozess-Environment gesetzt) wird die .env-Datei
    # dadurch ueberhaupt erst als Quelle wirksam - das war die Luecke.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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

    # Paper-Trading-Startkapital pro Tenant-Worker (siehe
    # sgr.portfolio.engine.PortfolioEngine.__init__ initial_cash).
    # Root-Cause-Fund (Asset-Universe/Paper-Capital-Audit): main.py's
    # lifespan() instanziierte PortfolioEngine bisher OHNE initial_cash
    # zu uebergeben - der Klassendefault (Decimal("10000")) wurde
    # deshalb IMMER verwendet, unabhaengig von jeder Konfiguration. Es
    # gab bis hierher ueberhaupt keine env var, die das Testkapital
    # haette veraendern koennen. Default bleibt 10000 (identisch zum
    # bisherigen, faktischen Verhalten - kein stiller Kapitalwechsel fuer
    # bestehende Deployments), aber ab jetzt tatsaechlich per
    # PAPER_INITIAL_CAPITAL env var konfigurierbar.
    paper_initial_capital: Decimal = Field(default=Decimal("10000"), gt=0)

    # Welche Exchange der Lifecycle standardmaessig verwendet (Market Data
    # Subscriptions + Exchange Pool). Default bleibt PIONEX fuer
    # Abwaertskompatibilitaet; per PRIMARY_EXCHANGE=binance env var
    # umschaltbar, z.B. solange Pionex nicht via ccxt unterstuetzt wird.
    primary_exchange: ExchangeID = ExchangeID.PIONEX

    # Welche Exchange fuer FUTURES/FUTURES_GRID bevorzugt verwendet wird
    # (siehe Aufgabenstellung: Pionex als strategisch bevorzugte Exchange
    # fuer Futures Grid, waehrend primary_exchange weiterhin die
    # allgemeine/Spot-Standardboerse bleibt). Reine Konfigurations-
    # Praeferenz fuer Capital-Allocation/Strategy-Routing (siehe
    # sgr.strategy.capital_allocation) - KEINE Kill-Switch/Risk-Umgehung:
    # ein Wechsel dieser Einstellung aendert an keiner Stelle Risk-Limits.
    primary_futures_exchange: ExchangeID = Field(default=ExchangeID.PIONEX)

    # Feature-Flags fuer die drei in der Aufgabenstellung genannten
    # Tenant-Schalter. Default: Binance bleibt aktiv (kein Verhaltens-
    # wechsel fuer bestehende Deployments); Pionex Futures Grid ist
    # bewusst OPT-IN (False), da LIVE-Order-Submission fuer Pionex noch
    # nicht implementiert ist (siehe sgr/exchanges/pionex.py) und
    # Futures/Leverage-Produkte zusaetzlich eine Compliance-Freigabe
    # benoetigen (siehe sgr.compliance) - diese Flags schalten NUR die
    # jeweilige Exchange/das Produkt fuer die Strategy-Zuteilung frei,
    # sie umgehen niemals Risk Engine, Compliance Engine oder
    # Exchange-Capability-Pruefungen.
    enable_binance: bool = Field(default=True)
    enable_pionex_futures_grid: bool = Field(default=False)

    # Multi-Tenant-Worker (Commit 5, Option A): wenn gesetzt, laedt
    # lifespan() Exchange-Credentials fuer diesen Worker-Prozess aus der
    # DB (APIKeyModel, verschluesselt mit get_cipher(), siehe
    # sgr/core/tenant_credentials.py) statt aus config.credentials
    # (.env). Jeder Tenant (z.B. Gordon, Sumo) laeuft als eigener
    # sgr-worker-Container mit eigener TENANT_ID env var - Isolation
    # entsteht durch OS-Prozesstrennung, nicht durch In-Memory-State im
    # API-Prozess (siehe Entscheidung zu Commit 5, Option A vs. B).
    # Default None = unveraendertes Single-Tenant-Verhalten (.env).
    tenant_id: str | None = Field(default=None)

    # Sub-configs (nested, loaded from env with prefixes)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    risk_limits: RiskLimitsConfig = Field(default_factory=RiskLimitsConfig)
    credentials: ExchangeCredentials = Field(default_factory=ExchangeCredentials)
    api: APIConfig = Field(default_factory=APIConfig)
    encryption: EncryptionConfig = Field(default_factory=EncryptionConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    # Any statt GridRiskLimitsConfig als Typ-Annotation: sgr.risk.grid_risk
    # kann hier NICHT auf Modulebene importiert werden - `import sgr.risk.
    # grid_risk` initialisiert zuerst das sgr.risk-Paket
    # (sgr/risk/__init__.py), das seinerseits sgr.risk.engine importiert,
    # welches wiederum `from sgr.core.config import get_config` auf
    # Modulebene aufruft - ein klassischer Package-__init__-Zyklus.
    # default_factory importiert deshalb lazy (erst wenn eine SGRConfig-
    # Instanz tatsaechlich gebaut wird, nach Abschluss des Modul-Imports).
    grid_risk_limits: Any = Field(default_factory=lambda: _build_default_grid_risk_limits())

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


def _build_default_grid_risk_limits() -> Any:
    """Lazy-Import-Helper - siehe SGRConfig.grid_risk_limits Docstring
    fuer die Begruendung (Package-__init__-Zyklus ueber sgr.risk)."""
    from sgr.risk.grid_risk import GridRiskLimitsConfig

    return GridRiskLimitsConfig()


@lru_cache(maxsize=1)
def get_config() -> SGRConfig:
    """
    Singleton config. Loaded once, cached forever.
    Tests should call get_config.cache_clear() to reset.
    """
    return SGRConfig()
