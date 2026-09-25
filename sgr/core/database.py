"""
SGR Database Layer
==================
Async SQLAlchemy + PostgreSQL (TimescaleDB extension for time-series).

Design decisions:
- Async engine: non-blocking DB calls, fits AsyncIO architecture
- TimescaleDB: hypertables for OHLCV data (automatic partitioning)
- Connection pool: sized per workload (trading vs. analytics)
- Separate session factories for read/write (future read replicas)
- Row-level security prepared for multi-tenant SaaS

Tables overview:
    candles         → TimescaleDB hypertable (market data)
    orders          → All orders (paper + live, labelled)
    positions       → Position snapshots
    trades          → Closed trade records (immutable audit)
    strategies      → Strategy registry + performance
    risk_events     → Risk alerts, kill switch events
    users           → SaaS user accounts (multi-tenant)
    api_keys        → Encrypted exchange API keys
    portfolio_snapshots → Periodic portfolio-value/cash/drawdown snapshots
                          (written by the worker; lets the API read the
                          latest portfolio state from DB instead of an
                          in-memory PortfolioEngine it no longer owns)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from sgr.core.config import get_config
from sgr.core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """All ORM models extend this."""

    pass


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------


class CandleModel(Base):
    """
    OHLCV candles. TimescaleDB hypertable on (symbol, timeframe, timestamp).
    Partitioned by timestamp for fast range queries.
    """

    __tablename__ = "candles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    exchange: Mapped[str] = mapped_column(String(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(5), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)

    __table_args__ = (
        UniqueConstraint("symbol", "exchange", "timeframe", "timestamp", name="uq_candle"),
        Index("ix_candles_symbol_timeframe_ts", "symbol", "timeframe", "timestamp"),
    )


class OrderModel(Base):
    """All orders – paper and live. trading_mode field separates them."""

    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True)
    signal_id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    exchange: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    order_type: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(precision=28, scale=8))
    filled_quantity: Mapped[Decimal] = mapped_column(
        Numeric(precision=28, scale=8), nullable=False, default=0, server_default="0"
    )
    average_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(precision=28, scale=8))
    fees: Mapped[Decimal] = mapped_column(
        Numeric(precision=28, scale=8), nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    trading_mode: Mapped[str] = mapped_column(String(10), nullable=False)  # "paper" | "live"
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_response: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    user_id: Mapped[str | None] = mapped_column(
        PG_UUID(as_uuid=False), ForeignKey("users.id"), nullable=True
    )

    __table_args__ = (
        Index("ix_orders_symbol_mode", "symbol", "trading_mode"),
        Index("ix_orders_submitted_at", "submitted_at"),
        Index("ix_orders_user_id", "user_id"),
    )


class PositionModel(Base):
    """Position snapshots (updated on fill, close)."""

    __tablename__ = "positions"

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    exchange: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    current_price: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    leverage: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=2), nullable=False, default=1, server_default="1"
    )
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(precision=28, scale=8), nullable=False, default=0, server_default="0"
    )
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(precision=28, scale=8), nullable=False, default=0, server_default="0"
    )
    is_open: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    trading_mode: Mapped[str] = mapped_column(String(10), nullable=False)
    user_id: Mapped[str | None] = mapped_column(
        PG_UUID(as_uuid=False), ForeignKey("users.id"), nullable=True
    )

    # Position-Protection-Felder (Migration 0006, siehe
    # sgr/risk/position_protection.py Modul-Docstring). Alle nullable,
    # additiv - NULL bedeutet "kein Schutz an dieser Position", was fuer
    # bereits vor dem Feature bestehende ("Legacy"-)Positionen exakt der
    # korrekte, unveraenderte Zustand ist (kein Backfill noetig/gewollt).
    stop_loss_price: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=28, scale=8), nullable=True
    )
    take_profit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=28, scale=8), nullable=True
    )
    max_holding_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sl_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tp_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Grund fuer das Schliessen (siehe ExitReason in sgr/core/types.py) -
    # nur bei is_open=False aussagekraeftig. String statt Enum-Spalte:
    # gleiche Wahl wie side/trading_mode oben, keine DB-seitige Enum-
    # Migration bei zukuenftigen neuen Gruenden noetig.
    close_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Entry-Regime (Migration 0008, siehe sgr/core/types.py Position.
    # entry_regime und PositionProtectionWatchdog._check_regime_exit()).
    # NULL = kein Entry-Regime bekannt (Legacy-Position oder Regime-Exit
    # nicht relevant fuer diese Strategie) - Regime-Exit bleibt inaktiv,
    # kein Backfill noetig/gewollt (identisches Prinzip wie Migration 0006).
    entry_regime: Mapped[str | None] = mapped_column(String(20), nullable=True)

    __table_args__ = (
        Index("ix_positions_open", "is_open", "trading_mode"),
        Index("ix_positions_user", "user_id", "is_open"),
    )


class PortfolioSnapshotModel(Base):
    """
    Periodischer Snapshot des Portfolio-Zustands (Cash, Portfolio-Value,
    Peak-Value, Drawdown).

    Notwendig, weil PortfolioEngine.summary() (portfolio_value, cash,
    peak_value, drawdown) reiner In-Memory-Zustand ist, der sich NICHT
    allein aus positions/trades rekonstruieren laesst - der Cash-Stand
    haengt von der vollstaendigen Fill-Historie ab, nicht nur von aktuell
    offenen Positionen. Seit sgr-api keine eigene PortfolioEngine-Instanz
    mehr besitzt (Trading Lifecycle liegt exklusiv im Worker), braucht
    die API einen persistenten Lesepfad fuer den aktuellen Portfolio-Wert.

    Der Worker schreibt periodisch (z.B. nach jedem Trading-Zyklus oder
    auf festem Intervall) einen neuen Snapshot; die API liest per
    get_latest() den jeweils neuesten Snapshot pro (user_id, trading_mode).
    Historische Snapshots bleiben fuer Performance-Charts/Drawdown-Verlauf
    erhalten (kein Update-in-place, reines Insert).
    """

    __tablename__ = "portfolio_snapshots"

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(
        PG_UUID(as_uuid=False), ForeignKey("users.id"), nullable=True
    )
    trading_mode: Mapped[str] = mapped_column(String(10), nullable=False)
    portfolio_value: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    cash: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(precision=28, scale=8), nullable=False, default=0, server_default="0"
    )
    peak_value: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    drawdown: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=6), nullable=False, default=0, server_default="0"
    )
    open_positions_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    total_trades: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_portfolio_snapshots_latest", "user_id", "trading_mode", "created_at"),
    )


class TradeModel(Base):
    """
    Closed trade records. Immutable after creation.
    These are the basis for performance calculation and performance fees.
    """

    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True)
    position_id: Mapped[str] = mapped_column(
        PG_UUID(as_uuid=False), ForeignKey("positions.id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    exchange: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    fees_total: Mapped[Decimal] = mapped_column(
        Numeric(precision=28, scale=8), nullable=False, default=0, server_default="0"
    )
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    holding_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    regime: Mapped[str] = mapped_column(String(30), nullable=False)
    trading_mode: Mapped[str] = mapped_column(String(10), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trade_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    user_id: Mapped[str | None] = mapped_column(
        PG_UUID(as_uuid=False), ForeignKey("users.id"), nullable=True
    )

    __table_args__ = (
        Index("ix_trades_strategy", "strategy_name", "closed_at"),
        Index("ix_trades_user_closed", "user_id", "closed_at"),
    )


class StrategyModel(Base):
    """Strategy registry with performance tracking."""

    __tablename__ = "strategies"

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_validated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    supported_regimes: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Performance metrics (updated by learning loop)
    sharpe_ratio: Mapped[float | None] = mapped_column(Numeric(precision=10, scale=4))
    sortino_ratio: Mapped[float | None] = mapped_column(Numeric(precision=10, scale=4))
    max_drawdown: Mapped[float | None] = mapped_column(Numeric(precision=10, scale=4))
    hit_rate: Mapped[float | None] = mapped_column(Numeric(precision=10, scale=4))
    total_trades: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    deactivation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RiskEventModel(Base):
    """Audit log for all risk events. Immutable."""

    __tablename__ = "risk_events"

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    trading_mode: Mapped[str] = mapped_column(String(10), nullable=False)
    metrics_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    user_id: Mapped[str | None] = mapped_column(PG_UUID(as_uuid=False), nullable=True)

    __table_args__ = (
        Index("ix_risk_events_ts", "timestamp"),
        Index("ix_risk_events_severity", "severity", "timestamp"),
    )


class AuditLogModel(Base):
    """
    Generisches Audit Log fuer sicherheitsrelevante Aktionen (API-Key-
    Rotation, Login-Versuche, Config-Aenderungen, etc.). Immutable.

    Getrennt von RiskEventModel: risk_events ist Trading-spezifisch und
    verlangt trading_mode (nicht optional). AuditLogModel deckt
    Security-/Account-Ereignisse ab, die keinen Trading-Mode-Bezug haben
    (z.B. LOGIN_FAILED).
    """

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(100), nullable=False, default="system", server_default="system"
    )
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_audit_log_ts", "timestamp"),
        Index("ix_audit_log_action", "action", "timestamp"),
    )


class UserModel(Base):
    """SaaS user accounts. Multi-tenant foundation."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)  # encrypted
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    is_2fa_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Vergabe bewusst NICHT ueber einen API-Endpoint (siehe
    # scripts/grant_admin.py Docstring) - ein Selbstbedienungsweg zum
    # Admin-Status waere ein Sicherheitsrisiko. Default false: neue User
    # (auch via POST /auth/register) sind niemals automatisch Admin.
    is_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    trading_mode: Mapped[str] = mapped_column(
        String(10), nullable=False, default="paper", server_default="paper"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    api_keys: Mapped[list[APIKeyModel]] = relationship(back_populates="user")

    __table_args__ = (Index("ix_users_email", "email"),)


class APIKeyModel(Base):
    """
    Encrypted exchange API keys per user.
    AES-256 encrypted at rest. KEK never stored in DB.
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        PG_UUID(as_uuid=False), ForeignKey("users.id"), nullable=False
    )
    exchange: Mapped[str] = mapped_column(String(20), nullable=False)
    trading_mode: Mapped[str] = mapped_column(String(10), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)  # AES-256 ciphertext
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)  # AES-256 ciphertext
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[UserModel] = relationship(back_populates="api_keys")

    __table_args__ = (
        UniqueConstraint("user_id", "exchange", "trading_mode", name="uq_api_key_user_exchange"),
        Index("ix_api_keys_user_id", "user_id"),
    )


class StrategySymbolValidationModel(Base):
    """
    Ergebnis eines Backtest+Walk-Forward-Validierungslaufs fuer EIN
    (symbol, timeframe, strategy)-Paar - siehe sgr/strategy/
    symbol_validation_runner.py.

    Getrennt von StrategyModel (das bleibt der globale, strategie-weite
    Aktivierungs-Gate, siehe StrategyRegistry): eine Strategie kann fuer
    Symbol A geeignet sein und fuer Symbol B nicht - StrategyModel hat
    dafuer kein Konzept (nur EIN is_active/is_validated pro
    Strategie-Name, siehe StrategyEntry). Diese Tabelle ist additiv:
    sie ersetzt/umgeht StrategyRegistry.mark_validated() nicht, sondern
    liefert die pro-Symbol-Verfeinerung obendrauf (siehe
    sgr.strategy.symbol_gate fuer den Production-Read-Pfad).

    status-Werte (siehe sgr.strategy.symbol_validation_runner.
    SymbolValidationStatus): INSUFFICIENT_DATA, INVALID_DATA,
    NO_VALID_STRATEGY, VALIDATED_BUT_NOT_ACTIVE, ACTIVE,
    TECHNICAL_FAILURE.
    """

    __tablename__ = "strategy_symbol_validations"

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(40), nullable=False)
    exchange: Mapped[str] = mapped_column(String(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    strategy: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    # "best" innerhalb der fuer dieses Symbol getesteten Strategien -
    # nur eine Zeile pro Symbol traegt is_best=True (die Auswahl,
    # siehe Phase 11 "automatische Strategy Selection").
    is_best_for_symbol: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    data_quality: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    regime_profile: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    score: Mapped[float | None] = mapped_column(Numeric(precision=10, scale=4))
    robustness_score: Mapped[float | None] = mapped_column(Numeric(precision=10, scale=4))
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    batch_id: Mapped[str] = mapped_column(String(40), nullable=False)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "exchange",
            "timeframe",
            "strategy",
            "batch_id",
            name="uq_strategy_symbol_validation",
        ),
        Index("ix_ssv_symbol", "symbol", "exchange", "timeframe"),
        Index("ix_ssv_status", "status"),
        Index("ix_ssv_best", "is_best_for_symbol"),
        Index("ix_ssv_batch", "batch_id"),
    )


class GridModel(Base):
    """
    Futures-Grid-Instanz (siehe sgr/core/grid_types.py::GridState und
    sgr/execution/grid_controller.py::GridController). Eigene Tabelle
    statt Wiederverwendung von PositionModel - ein Grid ist kein
    einzelner Fill/eine einzelne Position, sondern eine Sammlung von
    Preis-Leveln mit eigenem Lifecycle (siehe GridStatus). Die aus dem
    Grid entstehenden EINZELNEN Fills laufen weiterhin ganz normal durch
    OrderModel/PositionModel/TradeModel - GridModel/GridOrderModel sind
    rein additive Grid-Buchhaltung obendrauf, kein Ersatz.
    """

    __tablename__ = "futures_grids"

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(
        PG_UUID(as_uuid=False), ForeignKey("users.id"), nullable=True
    )
    exchange: Mapped[str] = mapped_column(String(20), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    product_type: Mapped[str] = mapped_column(String(20), nullable=False, default="futures_grid")
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    trading_mode: Mapped[str] = mapped_column(String(10), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # long | short
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    net_position_qty: Mapped[Decimal] = mapped_column(
        Numeric(precision=28, scale=8), nullable=False, default=0, server_default="0"
    )
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(precision=28, scale=8), nullable=False, default=0, server_default="0"
    )
    # Mark-to-Market (Migration 0010, siehe GridState.unrealized_pnl/
    # peak_value Docstring in sgr/core/grid_types.py).
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(precision=28, scale=8), nullable=False, default=0, server_default="0"
    )
    peak_value: Mapped[Decimal] = mapped_column(
        Numeric(precision=28, scale=8), nullable=False, default=0, server_default="0"
    )
    fees_paid: Mapped[Decimal] = mapped_column(
        Numeric(precision=28, scale=8), nullable=False, default=0, server_default="0"
    )
    funding_paid: Mapped[Decimal] = mapped_column(
        Numeric(precision=28, scale=8), nullable=False, default=0, server_default="0"
    )
    fills_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Level-Zustand (Migration 0008, siehe sgr/core/grid_types.py::
    # GridLevelState und GridController.restore_from_persistence()).
    # Serialisierte list[GridLevelState] (index/price/side/is_filled/
    # quantity/last_order_id/cycle_count/last_filled_at je Level) - vor
    # dieser Migration wurde dieser Zustand NIE persistiert, ein
    # Neustart mit aktivem Grid verlor ihn vollstaendig (dokumentierte
    # Luecke im Strategiebericht). '[]' Default fuer bereits bestehende
    # Zeilen (aktuell 0, siehe Analysebericht) - kein Backfill moeglich,
    # da der In-Memory-Zustand nach einem Neustart ohnehin nicht mehr
    # existiert.
    levels: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # Crossing-Erkennungs-Anker (Migration 0009, siehe GridState.last_price
    # Docstring in sgr/core/grid_types.py und GridController.on_price_tick()).
    # NULL nach Recovery ist sicher (siehe restore_from_persistence()
    # Docstring: on_price_tick() loest dann fuer KEIN Level faelschlich
    # aus, bis der naechste echte Preis-Tick eine neue Baseline setzt) -
    # trotzdem persistiert, um diesen einen "toten" Tick nach einem
    # Neustart zu vermeiden, wo es die Daten dafuer gibt.
    last_price: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=28, scale=8), nullable=True
    )

    __table_args__ = (
        Index("ix_futures_grids_status", "status", "trading_mode"),
        Index("ix_futures_grids_user", "user_id", "status"),
    )


class GridOrderModel(Base):
    """
    Einzelner Level-Fill innerhalb eines Futures Grid (siehe
    GridLevelState). Referenziert optional die zugehoerige Zeile in
    `orders` (dieselbe order.id, die durch den normalen
    ExecutionEngine/OrderSafety-Pfad bereits persistiert wird) - rein
    additive Grid-spezifische Attribution, kein Duplikat der Order-Daten
    selbst.
    """

    __tablename__ = "futures_grid_orders"

    id: Mapped[str] = mapped_column(PG_UUID(as_uuid=False), primary_key=True)
    grid_id: Mapped[str] = mapped_column(
        PG_UUID(as_uuid=False), ForeignKey("futures_grids.id"), nullable=False
    )
    order_id: Mapped[str | None] = mapped_column(
        PG_UUID(as_uuid=False), ForeignKey("orders.id"), nullable=True
    )
    level_index: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=28, scale=8), nullable=False)
    is_opening: Mapped[bool] = mapped_column(Boolean, nullable=False)
    cycle_pnl: Mapped[Decimal | None] = mapped_column(Numeric(precision=28, scale=8), nullable=True)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_futures_grid_orders_grid", "grid_id", "level_index"),)


# ---------------------------------------------------------------------------
# Engine & Session Factory
# ---------------------------------------------------------------------------


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db() -> None:
    """Initialize database engine and create tables."""
    global _engine, _session_factory

    config = get_config()
    _engine = create_async_engine(
        config.database.url,
        echo=config.environment.value == "development",
        pool_size=config.database.pool_size,
        max_overflow=config.database.max_overflow,
        pool_pre_ping=True,  # Detect stale connections
    )

    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    # Create tables (in production, use Alembic migrations instead)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Enable TimescaleDB extension (if available)
        try:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE"))
            # Convert candles to hypertable
            await conn.execute(
                text(
                    "SELECT create_hypertable('candles', 'timestamp', "
                    "if_not_exists => TRUE, migrate_data => TRUE)"
                )
            )
            log.info("database.timescaledb.enabled")
        except Exception as e:
            log.warning("database.timescaledb.unavailable", error=str(e))

    log.info("database.initialized", host=config.database.host)


async def close_db() -> None:
    """Close database connections."""
    global _engine
    if _engine:
        await _engine.dispose()
        log.info("database.closed")


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """
    Async context manager for database sessions.

    Usage:
        async with get_session() as session:
            result = await session.execute(select(OrderModel))
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
