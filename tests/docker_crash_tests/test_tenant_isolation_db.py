"""
Multi-Tenant DB-Isolation - Tests gegen die echte PostgreSQL-Instanz.

Kontext (Go-Live-Audit): PortfolioEngine.restore_from_persistence() rief
PositionRepository.get_open_positions(trading_mode) bisher OHNE user_id
auf - bei getrennten Worker-Prozessen pro Tenant (Gordon/Sumo, Commit 5)
haette das beim naechsten Worker-Neustart ALLE offenen Positionen ueber
ALLE Tenants in den in-memory State des jeweils neu startenden Prozesses
geladen. Zusaetzlich fehlte user_id im upsert_open()-Lookup, wodurch zwei
Tenants mit einer offenen Position auf demselben Symbol/Exchange/Mode
sich gegenseitig ueberschrieben haetten.

Die DB-Row-Level-Security-Policies (siehe docker/init-db.sql:
user_isolation_positions/orders/trades) greifen dabei NICHT als
Sicherheitsnetz: die App verbindet sich als Tabelleneigentuemer "sgr"
ohne FORCE ROW LEVEL SECURITY, und nirgends im Code wird
app.current_user_id/app.is_admin gesetzt - RLS ist aktuell rein
dekorativ. Diese Tests pruefen daher die tatsaechlich wirksame
Isolation: explizite user_id-Filterung auf Anwendungsebene.

Benoetigt eine echte, laufende PostgreSQL-Instanz (DB_HOST etc.) - siehe
docker_crash-Marker-Beschreibung in pyproject.toml.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from sgr.core.repositories import PositionRepository
from sgr.core.types import TradingMode

pytestmark = pytest.mark.docker_crash

# positions.user_id hat eine FK-Constraint auf users(id) - frei erfundene
# UUIDs schlagen mit ForeignKeyViolationError fehl. Die echten Tenant-IDs
# aus der laufenden Produktions-DB (siehe docker-compose.prod.yml
# TENANT_ID) sind stabile, tatsaechlich existierende users-Rows und damit
# die richtige Wahl fuer diesen Test - er verifiziert schliesslich genau
# deren Isolation.
GORDON_ID = "a47d994d-35cc-4619-83bb-86fd0cb48447"
SUMO_ID = "144f7bb0-113d-4c66-b5d3-7a34ab1d551a"


def _position_row(
    user_id: str, symbol: str = "BTC/USDT:USDT", exchange: str = "binance"
) -> dict:
    return {
        "id": str(uuid4()),
        "symbol": symbol,
        "exchange": exchange,
        "side": "long",
        "quantity": Decimal("0.01"),
        "entry_price": Decimal("50000"),
        "current_price": Decimal("50000"),
        "leverage": Decimal("1"),
        "unrealized_pnl": Decimal("0"),
        "realized_pnl": Decimal("0"),
        "opened_at": datetime.now(tz=UTC),
        "strategy_name": "test_strategy",
        "trading_mode": TradingMode.PAPER.value,
        "user_id": user_id,
    }


@pytest.fixture(autouse=True)
async def _db_initialized():
    from sgr.core.database import init_db

    await init_db()


@pytest.fixture
def repo() -> PositionRepository:
    return PositionRepository()


class TestPositionIsolationAcrossTenants:
    async def test_get_open_positions_filters_by_user_id(
        self, repo: PositionRepository
    ) -> None:
        """Kernbeweis: gleiche trading_mode, zwei Tenants, jeder sieht nur
        die eigene Position - genau das Szenario, das
        restore_from_persistence() bei einem Worker-Neustart durchlaeuft."""
        gordon_id = GORDON_ID
        sumo_id = SUMO_ID

        gordon_pos = _position_row(gordon_id, symbol="BTC/USDT:USDT")
        sumo_pos = _position_row(sumo_id, symbol="ETH/USDT:USDT")

        await repo.upsert_open(gordon_pos)
        await repo.upsert_open(sumo_pos)

        try:
            gordon_rows = await repo.get_open_positions(TradingMode.PAPER, user_id=gordon_id)
            sumo_rows = await repo.get_open_positions(TradingMode.PAPER, user_id=sumo_id)

            gordon_symbols = {r["symbol"] for r in gordon_rows}
            sumo_symbols = {r["symbol"] for r in sumo_rows}

            assert "BTC/USDT:USDT" in gordon_symbols
            assert "ETH/USDT:USDT" not in gordon_symbols
            assert "ETH/USDT:USDT" in sumo_symbols
            assert "BTC/USDT:USDT" not in sumo_symbols
        finally:
            await repo.close(gordon_pos["id"], datetime.now(tz=UTC))
            await repo.close(sumo_pos["id"], datetime.now(tz=UTC))

    async def test_upsert_same_symbol_different_tenants_does_not_collide(
        self, repo: PositionRepository
    ) -> None:
        """Zwei Tenants oeffnen zeitgleich dieselbe Position (gleiches
        Symbol/Exchange/Mode) - vorher fand upsert_open() ohne user_id im
        Lookup die FALSCHE Tenant-Zeile und ueberschrieb sie statt eine
        zweite anzulegen."""
        gordon_id = GORDON_ID
        sumo_id = SUMO_ID
        symbol = f"C{uuid4().hex[:6]}/USDT:USDT"

        gordon_pos = _position_row(gordon_id, symbol=symbol)
        gordon_pos["entry_price"] = Decimal("111")
        sumo_pos = _position_row(sumo_id, symbol=symbol)
        sumo_pos["entry_price"] = Decimal("222")

        gordon_row_id = await repo.upsert_open(gordon_pos)
        sumo_row_id = await repo.upsert_open(sumo_pos)

        try:
            assert gordon_row_id != sumo_row_id, (
                "upsert_open() fand faelschlich dieselbe Zeile fuer beide "
                "Tenants und hat eine Position ueberschrieben statt zwei "
                "getrennte anzulegen"
            )

            gordon_rows = await repo.get_open_positions(TradingMode.PAPER, user_id=gordon_id)
            sumo_rows = await repo.get_open_positions(TradingMode.PAPER, user_id=sumo_id)

            gordon_match = next(r for r in gordon_rows if r["symbol"] == symbol)
            sumo_match = next(r for r in sumo_rows if r["symbol"] == symbol)

            assert gordon_match["entry_price"] == Decimal("111")
            assert sumo_match["entry_price"] == Decimal("222")
        finally:
            await repo.close(gordon_row_id, datetime.now(tz=UTC))
            await repo.close(sumo_row_id, datetime.now(tz=UTC))

    async def test_get_open_positions_without_user_id_sees_all_tenants(
        self, repo: PositionRepository
    ) -> None:
        """Dokumentiert bewusst das Gegenteil: user_id=None (Single-Tenant-
        Fallback/Admin-Sicht) filtert NICHT - das ist die vorherige,
        weiterhin unveraenderte Semantik fuer Single-Tenant-Deployments,
        kein Bug."""
        gordon_id = GORDON_ID
        symbol = f"A{uuid4().hex[:6]}/USDT:USDT"
        pos = _position_row(gordon_id, symbol=symbol)
        await repo.upsert_open(pos)

        try:
            all_rows = await repo.get_open_positions(TradingMode.PAPER, user_id=None)
            assert any(r["symbol"] == symbol for r in all_rows)
        finally:
            await repo.close(pos["id"], datetime.now(tz=UTC))
