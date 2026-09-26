"""orders.symbol VARCHAR(20) -> VARCHAR(40)

Revision ID: 0011_orders_symbol_width
Revises: 0010_grid_mtm
Create Date: 2026-09-26 00:00:00.000000

Root-Cause-Fund (2026-09-26, aggressiver Paper-Trading-Test, live
beobachtet): orders.symbol speichert - anders als positions.symbol/
trades.symbol (dort nur "1000FLOKI/USDT", 14 Zeichen) - die volle
"{symbol}:{exchange}"-Form (z.B. "1000FLOKI/USDT:binance", 23 Zeichen,
siehe ExecutionEngine-Aufrufer). Die "1000X"-Binance-Namenskonvention
fuer sehr niedrigpreisige Coins (1000FLOKI/1000BONK/1000SHIB/1000PEPE,
siehe LIVE_MARKET_DATA_SYMBOLS-Kommentar in sgr/api/main.py) sprengt
damit das bisherige VARCHAR(20) - live bestaetigt: mehrfache
"value too long for type character varying(20)"-Fehler beim Order-
Insert (safe_executor.persist_pending_failed), der eigentliche Trade
(Order/Fill/Position) lief trotzdem korrekt durch (fail-safe), nur der
DB-Datensatz fuer diese Order fehlte. VARCHAR(40) matcht die bereits
an anderer Stelle etablierte Konvention fuer symbol-Spalten mit
groesserem Headroom (siehe StrategySymbolValidationModel.symbol in
sgr/core/database.py).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_orders_symbol_width"
down_revision: str | None = "0010_grid_mtm"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "orders",
        "symbol",
        existing_type=sa.String(length=20),
        type_=sa.String(length=40),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "orders",
        "symbol",
        existing_type=sa.String(length=40),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
