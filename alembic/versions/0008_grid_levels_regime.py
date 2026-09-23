"""positions.entry_regime, futures_grids.levels

Revision ID: 0008_grid_levels_regime
Revises: 0007_futures_grid
Create Date: 2026-09-23 00:00:00.000000

Zwei unabhaengige, rein additive Spalten fuer zwei getrennte, im
Architekturbericht ("Architekturanalyse: Futures-Grid-Strategie fuer
SGR") identifizierte Luecken:

1. positions.entry_regime (siehe sgr/core/types.py Position.
   entry_regime, sgr/risk/position_protection.py
   PositionProtectionWatchdog._check_regime_exit()): das beim Entry
   klassifizierte Marktregime, Voraussetzung fuer einen crash-sicheren
   Live-Regime-Exit. NULLABLE, kein Backfill - NULL bedeutet "kein
   Entry-Regime bekannt", der korrekte Zustand fuer jede vor dieser
   Migration eroeffnete Position (identisches Prinzip wie Migration
   0006 fuer stop_loss_price/take_profit_price/max_holding_until).

2. futures_grids.levels (siehe sgr/core/grid_types.py::GridLevelState,
   sgr/execution/grid_controller.py::GridController.
   restore_from_persistence()): der bisher NIE persistierte
   Level-Fuellstatus eines Grids (welches Preis-Level ist gefuellt, mit
   welcher Menge, welcher Order-ID) - ohne dieses Feld ist eine
   Grid-Crash-Recovery strukturell unmoeglich (der In-Memory-Zustand
   ist die einzige Quelle). NOT NULL mit Default '[]' - zum Zeitpunkt
   dieser Migration existieren 0 Zeilen in futures_grids (verifiziert
   vor Implementierung), ein Backfill-Problem fuer bestehende Zeilen
   entfaellt damit tatsaechlich, nicht nur theoretisch.

Beide Aenderungen sind rein additiv (ADD COLUMN), keine Aenderung an
bestehenden Spalten/Constraints - kein Risiko fuer den laufenden
Trading-Betrieb (Gordon/Sumo) oder bestehende offene Positionen/Trades.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0008_grid_levels_regime"
down_revision: str | None = "0007_futures_grid"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("entry_regime", sa.String(20), nullable=True),
    )
    op.add_column(
        "futures_grids",
        sa.Column(
            "levels",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("futures_grids", "levels")
    op.drop_column("positions", "entry_regime")
