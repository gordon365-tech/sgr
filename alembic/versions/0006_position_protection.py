"""positions.{stop_loss_price,take_profit_price,max_holding_until,sl_order_id,tp_order_id,close_reason}

Revision ID: 0006_position_protection
Revises: 0005_strategy_symbol_validations
Create Date: 2026-09-16 00:00:00.000000

Fuegt die Position-Protection-Spalten hinzu (siehe PositionModel-
Kommentar in sgr/core/database.py und Modul-Docstring von
sgr/risk/position_protection.py): stop_loss_price/take_profit_price/
max_holding_until/sl_order_id/tp_order_id/close_reason.

Alle Spalten NULLABLE, kein server_default, kein Backfill fuer
bestehende Zeilen - NULL ist fuer bereits vor diesem Feature offene
("Legacy"-)Positionen der korrekte, unveraenderte Zustand ("kein Schutz
an dieser Position"), nicht ein Platzhalter, der spaeter befuellt
werden muesste. Rein additiv, sicher gegen die aktuell produktiv
laufende Tabelle mit bestehenden offenen Positionen: kein Constraint,
kein Rewrite bestehender Zeilen, kein Risiko fuer laufenden Trading-
Betrieb waehrend/nach der Migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_position_protection"
down_revision: str | None = "0005_strategy_symbol_validations"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("stop_loss_price", sa.Numeric(precision=28, scale=8), nullable=True),
    )
    op.add_column(
        "positions",
        sa.Column("take_profit_price", sa.Numeric(precision=28, scale=8), nullable=True),
    )
    op.add_column(
        "positions",
        sa.Column("max_holding_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "positions",
        sa.Column("sl_order_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "positions",
        sa.Column("tp_order_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "positions",
        sa.Column("close_reason", sa.String(30), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("positions", "close_reason")
    op.drop_column("positions", "tp_order_id")
    op.drop_column("positions", "sl_order_id")
    op.drop_column("positions", "max_holding_until")
    op.drop_column("positions", "take_profit_price")
    op.drop_column("positions", "stop_loss_price")
