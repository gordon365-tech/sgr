"""portfolio_snapshots table

Revision ID: 0003_portfolio_snapshots
Revises: 0002_audit_log_and_schema_sync
Create Date: 2026-09-04 00:00:00.000000

Fuegt portfolio_snapshots hinzu (siehe PortfolioSnapshotModel Docstring
in sgr/core/database.py fuer die vollstaendige Begruendung).

Kontext: sgr-api besitzt seit der Worker/API-Trennung (Duplicate-
Lifecycle-Fix) keine eigene, dauerhaft laufende PortfolioEngine-Instanz
mehr - Trading Lifecycle (inkl. PortfolioEngine) liegt jetzt exklusiv im
sgr-worker-Prozess. portfolio_value/cash/peak_value/drawdown sind
In-Memory-Zustand der PortfolioEngine und lassen sich NICHT allein aus
den bestehenden positions/trades-Tabellen rekonstruieren (Cash-Stand
haengt von der vollstaendigen Fill-Historie ab). Der Worker schreibt
daher periodisch einen Snapshot dieses Zustands in diese neue Tabelle;
die API liest darueber den zuletzt bekannten Portfolio-Stand fuer
/api/v1/portfolio/overview und /api/v1/portfolio/pnl.

Reines Insert (kein Update-in-place) - historische Snapshots bleiben
fuer spaetere Performance-/Drawdown-Charts erhalten.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0003_portfolio_snapshots"
down_revision: str | None = "0002_audit_log_and_schema_sync"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("trading_mode", sa.String(10), nullable=False),
        sa.Column("portfolio_value", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("cash", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column(
            "unrealized_pnl",
            sa.Numeric(precision=28, scale=8),
            nullable=False,
            server_default="0",
        ),
        sa.Column("peak_value", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column(
            "drawdown",
            sa.Numeric(precision=10, scale=6),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "open_positions_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("total_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_portfolio_snapshots_latest",
        "portfolio_snapshots",
        ["user_id", "trading_mode", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_portfolio_snapshots_latest", table_name="portfolio_snapshots")
    op.drop_table("portfolio_snapshots")
