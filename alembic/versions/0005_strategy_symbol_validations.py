"""strategy_symbol_validations table

Revision ID: 0005_strategy_symbol_validations
Revises: 0004_user_is_admin
Create Date: 2026-09-15 00:00:00.000000

Fuegt strategy_symbol_validations hinzu (siehe
StrategySymbolValidationModel Docstring in sgr/core/database.py).

Kontext (Strategieebene fuer das gesamte dynamisch entdeckte
Symbol-Universum, nicht nur BTC/USDT + ETH/USDT): StrategyModel/
StrategyRegistry kennen nur einen globalen is_active/is_validated
Zustand PRO STRATEGIE-NAME, kein Konzept "Strategie X ist fuer Symbol Y
geeignet, aber nicht fuer Symbol Z". Diese Tabelle traegt genau diese
pro-Symbol-Verfeinerung, additiv und ohne die bestehende
StrategyRegistry-Semantik zu veraendern oder zu umgehen - siehe
sgr/strategy/symbol_validation_runner.py (Batch-Orchestrierung) und
sgr/strategy/symbol_gate.py (Production-Read-Pfad).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0005_strategy_symbol_validations"
down_revision: str | None = "0004_user_is_admin"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_symbol_validations",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("symbol", sa.String(40), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("timeframe", sa.String(10), nullable=False),
        sa.Column("strategy", sa.String(100), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column(
            "is_best_for_symbol",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "parameters", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("metrics", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "data_quality", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "regime_profile", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("score", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("robustness_score", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("batch_id", sa.String(40), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "symbol",
            "exchange",
            "timeframe",
            "strategy",
            "batch_id",
            name="uq_strategy_symbol_validation",
        ),
    )
    op.create_index(
        "ix_ssv_symbol", "strategy_symbol_validations", ["symbol", "exchange", "timeframe"]
    )
    op.create_index("ix_ssv_status", "strategy_symbol_validations", ["status"])
    op.create_index(
        "ix_ssv_best", "strategy_symbol_validations", ["is_best_for_symbol"]
    )
    op.create_index("ix_ssv_batch", "strategy_symbol_validations", ["batch_id"])


def downgrade() -> None:
    op.drop_index("ix_ssv_batch", table_name="strategy_symbol_validations")
    op.drop_index("ix_ssv_best", table_name="strategy_symbol_validations")
    op.drop_index("ix_ssv_status", table_name="strategy_symbol_validations")
    op.drop_index("ix_ssv_symbol", table_name="strategy_symbol_validations")
    op.drop_table("strategy_symbol_validations")
