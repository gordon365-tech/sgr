"""Initial schema – all SGR tables

Revision ID: 0001_initial
Revises:
Create Date: 2024-01-01 00:00:00.000000

Erstellt alle Tabellen + TimescaleDB Hypertables.
Rollback löscht alles (nur in Development verwenden).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------------------------------------------------------------------------
    # TimescaleDB Extension
    # ---------------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")

    # ---------------------------------------------------------------------------
    # Users (SaaS Multi-Tenant)
    # ---------------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("totp_secret", sa.String(64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("is_2fa_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("trading_mode", sa.String(10), nullable=False, server_default="paper"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "ix_users_email",
        "users",
        ["email"],
    )

    # ---------------------------------------------------------------------------
    # API Keys (Encrypted)
    # ---------------------------------------------------------------------------
    op.create_table(
        "api_keys",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("trading_mode", sa.String(10), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "user_id",
            "exchange",
            "trading_mode",
            name="uq_api_key_user_exchange",
        ),
    )

    op.create_index(
        "ix_api_keys_user_id",
        "api_keys",
        ["user_id"],
    )

    # ---------------------------------------------------------------------------
    # Candles (TimescaleDB Hypertable)
    # ---------------------------------------------------------------------------
    op.create_table(
        "candles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("timeframe", sa.String(5), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(28, 8), nullable=False),
        sa.Column("high", sa.Numeric(28, 8), nullable=False),
        sa.Column("low", sa.Numeric(28, 8), nullable=False),
        sa.Column("close", sa.Numeric(28, 8), nullable=False),
        sa.Column("volume", sa.Numeric(28, 8), nullable=False),
        sa.PrimaryKeyConstraint("id", "timestamp"),
        sa.UniqueConstraint(
            "symbol",
            "exchange",
            "timeframe",
            "timestamp",
            name="uq_candle",
        ),
    )

    op.create_index(
        "ix_candles_symbol_timeframe_ts",
        "candles",
        ["symbol", "timeframe", "timestamp"],
    )

    op.execute(
        """
        SELECT create_hypertable(
            'candles',
            'timestamp',
            if_not_exists => TRUE,
            migrate_data => TRUE
        )
        """
    )

    # Compression wird separat konfiguriert.
    # Die Initialmigration bleibt unabhängig von der
    # verwendeten TimescaleDB Columnstore-Konfiguration.

    # ---------------------------------------------------------------------------
    # Strategies
    # ---------------------------------------------------------------------------
    op.create_table(
        "strategies",
        sa.Column("name", sa.String(100), primary_key=True),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_validated", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "supported_regimes",
            JSONB(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "parameters",
            JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("sharpe_ratio", sa.Numeric(10, 4), nullable=True),
        sa.Column("sortino_ratio", sa.Numeric(10, 4), nullable=True),
        sa.Column("max_drawdown", sa.Numeric(10, 4), nullable=True),
        sa.Column("hit_rate", sa.Numeric(10, 4), nullable=True),
        sa.Column(
            "total_trades",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("deactivation_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ---------------------------------------------------------------------------
    # Orders
    # ---------------------------------------------------------------------------
    op.create_table(
        "orders",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("signal_id", UUID(as_uuid=False), nullable=False),
        sa.Column("exchange_order_id", sa.String(100), nullable=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("order_type", sa.String(20), nullable=False),
        sa.Column("quantity", sa.Numeric(28, 8), nullable=False),
        sa.Column("limit_price", sa.Numeric(28, 8), nullable=True),
        sa.Column(
            "filled_quantity",
            sa.Numeric(28, 8),
            nullable=False,
            server_default="0",
        ),
        sa.Column("average_fill_price", sa.Numeric(28, 8), nullable=True),
        sa.Column(
            "fees",
            sa.Numeric(28, 8),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("trading_mode", sa.String(10), nullable=False),
        sa.Column("strategy_name", sa.String(100), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "raw_response",
            JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_orders_symbol_mode",
        "orders",
        ["symbol", "trading_mode"],
    )

    op.create_index(
        "ix_orders_submitted_at",
        "orders",
        ["submitted_at"],
    )

    op.create_index(
        "ix_orders_user_id",
        "orders",
        ["user_id"],
    )

    # ---------------------------------------------------------------------------
    # Positions
    # ---------------------------------------------------------------------------
    op.create_table(
        "positions",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("quantity", sa.Numeric(28, 8), nullable=False),
        sa.Column("entry_price", sa.Numeric(28, 8), nullable=False),
        sa.Column("current_price", sa.Numeric(28, 8), nullable=False),
        sa.Column(
            "leverage",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "unrealized_pnl",
            sa.Numeric(28, 8),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "realized_pnl",
            sa.Numeric(28, 8),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "is_open",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("strategy_name", sa.String(100), nullable=False),
        sa.Column("trading_mode", sa.String(10), nullable=False),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_positions_open",
        "positions",
        ["is_open", "trading_mode"],
    )

    op.create_index(
        "ix_positions_user",
        "positions",
        ["user_id", "is_open"],
    )

    # ---------------------------------------------------------------------------
    # Trades (Immutable Audit Records)
    # ---------------------------------------------------------------------------
    op.create_table(
        "trades",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "position_id",
            UUID(as_uuid=False),
            sa.ForeignKey("positions.id"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("entry_price", sa.Numeric(28, 8), nullable=False),
        sa.Column("exit_price", sa.Numeric(28, 8), nullable=False),
        sa.Column("quantity", sa.Numeric(28, 8), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(28, 8), nullable=False),
        sa.Column(
            "fees_total",
            sa.Numeric(28, 8),
            nullable=False,
            server_default="0",
        ),
        sa.Column("net_pnl", sa.Numeric(28, 8), nullable=False),
        sa.Column("holding_seconds", sa.Integer(), nullable=False),
        sa.Column("strategy_name", sa.String(100), nullable=False),
        sa.Column("regime", sa.String(30), nullable=False),
        sa.Column("trading_mode", sa.String(10), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "metadata",
            JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_trades_strategy",
        "trades",
        ["strategy_name", "closed_at"],
    )

    op.create_index(
        "ix_trades_user_closed",
        "trades",
        ["user_id", "closed_at"],
    )

    # ---------------------------------------------------------------------------
    # Risk Events
    # ---------------------------------------------------------------------------
    op.create_table(
        "risk_events",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("trading_mode", sa.String(10), nullable=False),
        sa.Column(
            "metrics_snapshot",
            JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=False), nullable=True),
    )

    op.create_index(
        "ix_risk_events_ts",
        "risk_events",
        ["timestamp"],
    )

    op.create_index(
        "ix_risk_events_severity",
        "risk_events",
        ["severity", "timestamp"],
    )

    # ---------------------------------------------------------------------------
    # ML Prediction Log
    # ---------------------------------------------------------------------------
    op.create_table(
        "ml_prediction_log",
        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=True,
            primary_key=True,
        ),
        sa.Column("model_id", sa.String(100), nullable=False),
        sa.Column("model_type", sa.String(50), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("timeframe", sa.String(5), nullable=False),
        sa.Column("prediction", JSONB(), nullable=False),
        sa.Column(
            "features_used",
            JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("actual_outcome", JSONB(), nullable=True),
        sa.Column("predicted_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_index(
        "ix_ml_log_symbol_ts",
        "ml_prediction_log",
        ["symbol", "predicted_at"],
    )

    op.create_index(
        "ix_ml_log_model",
        "ml_prediction_log",
        ["model_id", "predicted_at"],
    )

    # ---------------------------------------------------------------------------
    # Sentiment Signals Log
    # ---------------------------------------------------------------------------
    op.create_table(
        "sentiment_log",
        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=True,
            primary_key=True,
        ),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("entity", sa.String(20), nullable=False),
        sa.Column("raw_score", sa.Numeric(5, 4), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("event_category", sa.String(30), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_index(
        "ix_sentiment_entity_ts",
        "sentiment_log",
        ["entity", "timestamp"],
    )

    # ---------------------------------------------------------------------------
    # Row Level Security
    # ---------------------------------------------------------------------------
    op.execute("ALTER TABLE orders ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE positions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE trades ENABLE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY user_isolation_orders ON orders
        USING (
            user_id = current_setting('app.current_user_id', true)::uuid
            OR current_setting('app.is_admin', true) = 'true'
        )
        """
    )

    op.execute(
        """
        CREATE POLICY user_isolation_positions ON positions
        USING (
            user_id = current_setting('app.current_user_id', true)::uuid
            OR current_setting('app.is_admin', true) = 'true'
        )
        """
    )

    op.execute(
        """
        CREATE POLICY user_isolation_trades ON trades
        USING (
            user_id = current_setting('app.current_user_id', true)::uuid
            OR current_setting('app.is_admin', true) = 'true'
        )
        """
    )


def downgrade() -> None:
    """
    WARNUNG:
    Löscht alle Daten.
    Nur in Development verwenden.
    """

    op.execute("DROP POLICY IF EXISTS user_isolation_trades ON trades")
    op.execute("DROP POLICY IF EXISTS user_isolation_positions ON positions")
    op.execute("DROP POLICY IF EXISTS user_isolation_orders ON orders")

    op.drop_table("sentiment_log")
    op.drop_table("ml_prediction_log")
    op.drop_table("risk_events")
    op.drop_table("trades")
    op.drop_table("positions")
    op.drop_table("orders")
    op.drop_table("strategies")
    op.drop_table("candles")
    op.drop_table("api_keys")
    op.drop_table("users")