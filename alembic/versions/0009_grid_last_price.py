"""futures_grids.last_price

Revision ID: 0009_grid_last_price
Revises: 0008_grid_levels_regime
Create Date: 2026-09-23 00:10:00.000000

Persistiert GridState.last_price (Crossing-Erkennungs-Anker, siehe
GridController.on_price_tick()) - zusammen mit Migration 0008 (levels)
Voraussetzung fuer eine vollstaendige Grid-Crash-Recovery. NULLABLE,
kein Backfill (0 bestehende Zeilen zum Zeitpunkt dieser Migration,
siehe 0008-Docstring) - NULL nach einem Restart ohne vorherigen
Persist-Zeitpunkt ist ein sicherer, bereits von on_price_tick()
korrekt behandelter Zustand (kein Level loest beim naechsten Tick
faelschlich aus, siehe dortige _crossed()-Logik).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_grid_last_price"
down_revision: str | None = "0008_grid_levels_regime"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "futures_grids",
        sa.Column("last_price", sa.Numeric(precision=28, scale=8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("futures_grids", "last_price")
