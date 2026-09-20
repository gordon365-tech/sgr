"""futures_grids, futures_grid_orders

Revision ID: 0007_futures_grid
Revises: 0006_position_protection
Create Date: 2026-09-20 00:00:00.000000

Fuegt die Futures-Grid-Buchhaltungstabellen hinzu (siehe GridModel/
GridOrderModel in sgr/core/database.py und sgr/core/grid_types.py).

Rein additiv: zwei neue Tabellen, keine Aenderung an bestehenden
Tabellen/Spalten. Kein Risiko fuer laufenden Trading-Betrieb - Binance-
Spot/Futures-Handel und bestehende Directional-Strategien nutzen
weiterhin ausschliesslich orders/positions/trades unveraendert.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

from alembic import op

revision: str = "0007_futures_grid"
down_revision: str | None = "0006_position_protection"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "futures_grids",
        sa.Column("id", PG_UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", PG_UUID(as_uuid=False), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("product_type", sa.String(20), nullable=False, server_default="futures_grid"),
        sa.Column("strategy_name", sa.String(100), nullable=False),
        sa.Column("trading_mode", sa.String(10), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("parameters", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "net_position_qty", sa.Numeric(precision=28, scale=8), nullable=False, server_default="0"
        ),
        sa.Column(
            "realized_pnl", sa.Numeric(precision=28, scale=8), nullable=False, server_default="0"
        ),
        sa.Column("fees_paid", sa.Numeric(precision=28, scale=8), nullable=False, server_default="0"),
        sa.Column(
            "funding_paid", sa.Numeric(precision=28, scale=8), nullable=False, server_default="0"
        ),
        sa.Column("fills_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(40), nullable=True),
    )
    op.create_index(
        "ix_futures_grids_status", "futures_grids", ["status", "trading_mode"]
    )
    op.create_index("ix_futures_grids_user", "futures_grids", ["user_id", "status"])

    op.create_table(
        "futures_grid_orders",
        sa.Column("id", PG_UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "grid_id", PG_UUID(as_uuid=False), sa.ForeignKey("futures_grids.id"), nullable=False
        ),
        sa.Column("order_id", PG_UUID(as_uuid=False), sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("level_index", sa.Integer, nullable=False),
        sa.Column("price", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("is_opening", sa.Boolean, nullable=False),
        sa.Column("cycle_pnl", sa.Numeric(precision=28, scale=8), nullable=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_futures_grid_orders_grid", "futures_grid_orders", ["grid_id", "level_index"]
    )


def downgrade() -> None:
    op.drop_index("ix_futures_grid_orders_grid", table_name="futures_grid_orders")
    op.drop_table("futures_grid_orders")
    op.drop_index("ix_futures_grids_user", table_name="futures_grids")
    op.drop_index("ix_futures_grids_status", table_name="futures_grids")
    op.drop_table("futures_grids")
