"""futures_grids.{unrealized_pnl,peak_value}

Revision ID: 0010_grid_mtm
Revises: 0009_grid_last_price
Create Date: 2026-09-24 00:00:00.000000

Persistiert Grid-Mark-to-Market (siehe GridState.unrealized_pnl/
peak_value Docstring in sgr/core/grid_types.py und GridController.
_update_mark_to_market()) - ersetzt die zuvor als Platzhalter (0.0)
gefuehrten unrealized_pnl_usd/drawdown_pct-Metriken durch echte,
persistierte Werte. NOT NULL mit Default 0 - zum Zeitpunkt dieser
Migration existieren weiterhin 0 Zeilen in futures_grids (kein Grid
war je aktiv, siehe vorherige Migrationen), ein Backfill-Problem
entfaellt tatsaechlich.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_grid_mtm"
down_revision: str | None = "0009_grid_last_price"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "futures_grids",
        sa.Column(
            "unrealized_pnl",
            sa.Numeric(precision=28, scale=8),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "futures_grids",
        sa.Column(
            "peak_value", sa.Numeric(precision=28, scale=8), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    op.drop_column("futures_grids", "peak_value")
    op.drop_column("futures_grids", "unrealized_pnl")
