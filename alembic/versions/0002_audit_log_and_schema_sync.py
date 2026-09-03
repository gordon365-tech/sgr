"""audit_log table, trades.metadata rename, server-default sync

Revision ID: 0002_audit_log_and_schema_sync
Revises: 0001_initial
Create Date: 2026-09-03 00:00:00.000000

Behebt den durch `alembic check` erkannten Schema Drift zwischen dem
aktuellen SQLAlchemy Model State (sgr/core/database.py) und der
produktiv laufenden Datenbank.

Bewusst AUSGESCHLOSSEN aus dieser Migration (siehe Projektnotizen):
    - sentiment_log
    - ml_prediction_log
Beide Tabellen existieren in der DB (aus 0001_initial), sind aber nicht
mehr im aktuellen Model vorhanden. Weder sgr/sentiment/ noch sgr/ml/
schreiben aktuell in diese Tabellen. Ob sie künftig für Sentiment-
Persistenz bzw. ML-Prediction-Logging wiederverwendet werden sollen,
ist eine offene Produktentscheidung und wird bewusst NICHT in dieser
Migration entschieden. Ein blindes `alembic revision --autogenerate`
hätte hier `op.drop_table(...)` für beide Tabellen erzeugt – das wird
hier explizit vermieden.

Was diese Migration tut:
    1. audit_log Tabelle + Indizes anlegen (Model bereits vorhanden,
       Tabelle fehlte in der DB)
    2. trades.metadata -> trades.trade_metadata umbenennen (echtes
       Rename, KEIN drop+add – bestehende Daten bleiben erhalten)
    3. Server Defaults synchronisieren: Model deklariert jetzt explizit
       server_default= passend zu den in 0001_initial bereits gesetzten
       DB-Defaults (reine Model-Korrektur, keine DB-Änderung nötig für
       diese Spalten – server_default Werte bleiben unverändert)
    4. ix_users_email und ix_api_keys_user_id waren in der DB vorhanden,
       fehlten aber im Model – Model wurde ergänzt, keine DB-Änderung
       nötig, da die Indizes bereits existieren
    5. candles_timestamp_idx wird NICHT angefasst – das ist ein von
       TimescaleDB's create_hypertable() automatisch erzeugter Index
       auf der Partitionierungsspalte, kein alembic-verwalteter Index
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0002_audit_log_and_schema_sync"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------------------------------------------------------------------------
    # audit_log (neu – Model existiert bereits, Tabelle fehlte in der DB)
    # ---------------------------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("user_id", sa.String(100), nullable=False, server_default="system"),
        sa.Column("details", JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_log_ts", "audit_log", ["timestamp"])
    op.create_index("ix_audit_log_action", "audit_log", ["action", "timestamp"])

    # ---------------------------------------------------------------------------
    # trades.metadata -> trades.trade_metadata (echtes Rename, Daten bleiben)
    # ---------------------------------------------------------------------------
    op.alter_column("trades", "metadata", new_column_name="trade_metadata")

    # ---------------------------------------------------------------------------
    # Fehlende Indizes ergänzen (existierten in der DB seit 0001_initial,
    # fehlten nur im Model – hier idempotent mit IF NOT EXISTS, falls die
    # Ziel-DB (z.B. eine neu aufgesetzte) sie noch nicht hat)
    # ---------------------------------------------------------------------------
    op.execute("CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_api_keys_user_id ON api_keys (user_id)")

    # ---------------------------------------------------------------------------
    # Server Defaults: keine DB-Änderung notwendig (0001_initial hat sie
    # bereits korrekt gesetzt). Diese Migration bringt nur das Model in
    # Übereinstimmung mit der DB (siehe sgr/core/database.py Änderungen).
    # Kein op.alter_column(...server_default=...) hier nötig, da sich der
    # tatsächliche DB-Zustand nicht ändert.
    # ---------------------------------------------------------------------------


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_api_keys_user_id")
    op.execute("DROP INDEX IF EXISTS ix_users_email")

    op.alter_column("trades", "trade_metadata", new_column_name="metadata")

    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_ts", table_name="audit_log")
    op.drop_table("audit_log")
