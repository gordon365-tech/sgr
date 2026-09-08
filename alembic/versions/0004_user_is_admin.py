"""users.is_admin column

Revision ID: 0004_user_is_admin
Revises: 0003_portfolio_snapshots
Create Date: 2026-09-08 00:00:00.000000

Fuegt users.is_admin hinzu (Default false). Vorher existierte is_admin
nur als transienter JWT-Claim (Funktionsparameter beim Token-Erstellen,
hartcodiert False in allen Aufrufstellen von create_access_token) -
es gab in der Datenbank ueberhaupt keine dauerhafte Speicherung dieses
Flags und folglich keinen Weg, einen User tatsaechlich zum Admin zu
machen (require_admin-Endpoints wie POST /risk/kill-switch/reset waren
fuer JEDEN User unerreichbar, unabhaengig von Absicht).

Vergabe von Admin-Rechten bewusst NICHT ueber einen neuen API-Endpoint
(ein Weg, sich selbst per HTTP zum Admin zu machen, waere ein
Sicherheitsrisiko) - siehe stattdessen scripts/grant_admin.py, das
direkt auf der DB arbeitet und Server-Zugriff voraussetzt.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_user_is_admin"
down_revision: str | None = "0003_portfolio_snapshots"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "is_admin")
