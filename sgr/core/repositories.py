"""
SGR Repository Layer
====================
Saubere Datenbankzugriffs-Abstraktion für alle Module.

Design-Prinzipien:
    - Repository Pattern: Business Logic kennt keine SQL-Details
    - Async-first: alle Methoden sind async
    - Typed Returns: nie rohe DB-Rows nach außen
    - Upsert wo sinnvoll (idempotente Operationen)
    - Bulk-Operationen für Performance (Batch-Insert für Candles)

Warum Repository statt direkter SQLAlchemy-Calls?
    - Austauschbar: PostgreSQL → TimescaleDB Cloud einfach wechselbar
    - Testbar: Mock-Repository für Unit Tests
    - Single Responsibility: DB-Logik an einem Ort
    - Query-Optimierung zentral (kein N+1 in Routen)
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, desc, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sgr.core.database import (
    AuditLogModel,
    CandleModel,
    GridModel,
    GridOrderModel,
    OrderModel,
    PortfolioSnapshotModel,
    PositionModel,
    RiskEventModel,
    StrategyModel,
    StrategySymbolValidationModel,
    TradeModel,
    UserModel,
    get_session,
)
from sgr.core.logging import get_logger
from sgr.core.types import Candle, OrderStatus, TradingMode

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Candle Repository
# ---------------------------------------------------------------------------


class CandleRepository:
    """TimescaleDB-backed Candle Storage."""

    # PostgreSQL/asyncpg-Grenze: maximal 32767 Query-Parameter pro
    # Statement. Bei 9 Spalten pro Candle-Row ergibt das ein theoretisches
    # Maximum von 32767 // 9 = 3640 Rows pro INSERT - _CHUNK_SIZE bleibt
    # bewusst deutlich darunter als Sicherheitsmarge. Ohne dieses Chunking
    # schlägt upsert_batch() bei großen Batches (z.B. lange Backtest-
    # Validierungszeiträume aus BacktestDataLoader) mit
    # "the number of query arguments cannot exceed 32767" fehl - auf dem
    # Server durch echten Log-Fehler bestätigt (asyncpg.exceptions.
    # _base.InterfaceError), kein theoretisches Risiko.
    _CHUNK_SIZE = 3000

    async def upsert_batch(self, candles: list[Candle]) -> int:
        """
        Batch-Upsert von Candles. Ignoriert Duplikate (ON CONFLICT DO NOTHING).
        Optimiert für TimescaleDB: bulk insert > row-by-row.

        Chunked in Gruppen von _CHUNK_SIZE, um die asyncpg-Parameterlimite
        (32767 Query-Parameter pro Statement) nicht zu überschreiten - siehe
        _CHUNK_SIZE Docstring. Für Aufrufer transparent: ein einziger
        awaitbarer Call, mehrere INSERTs intern.

        Returns: Anzahl eingefügter (neuer) Candles (Summe über alle Chunks).
        """
        if not candles:
            return 0

        total_inserted = 0
        for i in range(0, len(candles), self._CHUNK_SIZE):
            chunk = candles[i : i + self._CHUNK_SIZE]
            rows = [
                {
                    "symbol": c.symbol.ccxt_symbol,
                    "exchange": c.symbol.exchange.value,
                    "timeframe": c.timeframe,
                    "timestamp": c.timestamp,
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                }
                for c in chunk
            ]

            async with get_session() as session:
                stmt = pg_insert(CandleModel).values(rows)
                stmt = stmt.on_conflict_do_nothing(constraint="uq_candle")
                result = await session.execute(stmt)
                total_inserted += result.rowcount

        log.debug("candle_repo.upserted", count=total_inserted, total=len(candles))
        return total_inserted

    async def get_ohlcv(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Holt OHLCV-Daten für einen Zeitraum."""
        async with get_session() as session:
            stmt = (
                select(CandleModel)
                .where(
                    and_(
                        CandleModel.symbol == symbol,
                        CandleModel.exchange == exchange,
                        CandleModel.timeframe == timeframe,
                        CandleModel.timestamp >= start,
                        CandleModel.timestamp <= end,
                    )
                )
                .order_by(CandleModel.timestamp)
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [
                {
                    "timestamp": r.timestamp,
                    "open": r.open,
                    "high": r.high,
                    "low": r.low,
                    "close": r.close,
                    "volume": r.volume,
                }
                for r in rows
            ]

    async def get_latest_timestamp(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
    ) -> datetime | None:
        """Gibt den Timestamp des neuesten Candles zurück."""
        async with get_session() as session:
            stmt = (
                select(CandleModel.timestamp)
                .where(
                    and_(
                        CandleModel.symbol == symbol,
                        CandleModel.exchange == exchange,
                        CandleModel.timeframe == timeframe,
                    )
                )
                .order_by(desc(CandleModel.timestamp))
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row


# ---------------------------------------------------------------------------
# Order Repository
# ---------------------------------------------------------------------------


class OrderRepository:
    """Persistenz für alle Orders (Paper + Live)."""

    async def create(self, order_data: dict[str, Any]) -> str:
        """Erstellt neuen Order-Record. Returns: order_id."""
        async with get_session() as session:
            order = OrderModel(**order_data)
            session.add(order)
            await session.flush()
            return str(order.id)

    async def get_by_id(self, order_id: str) -> dict[str, Any] | None:
        """
        Laedt einen einzelnen Order-Record per Primary Key.

        Grundlage fuer die DB-gestuetzte Idempotenzpruefung in
        SafeOrderExecutor (siehe sgr/execution/order_safety.py Punkt 5):
        die In-Process-Duplicate-Detection ist bei einem Prozess-Neustart
        wirkungslos (frischer, leerer In-Memory-State) und die Exchange-
        seitige clientOrderId-Pruefung (ccxt_base.py::place_order) laeuft
        nur im LIVE-Zweig - im PAPER-Modus (_simulate_order()) existiert
        vor diesem Fix keine prozessuebergreifende Absicherung. Diese
        Methode macht die DB zur zusaetzlichen, fuer PAPER einzigen
        durablen Quelle der Wahrheit.
        """
        async with get_session() as session:
            stmt = select(OrderModel).where(OrderModel.id == order_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return {
                "id": str(row.id),
                "signal_id": str(row.signal_id),
                "exchange_order_id": row.exchange_order_id,
                "symbol": row.symbol,
                "exchange": row.exchange,
                "side": row.side,
                "order_type": row.order_type,
                "quantity": row.quantity,
                "limit_price": row.limit_price,
                "filled_quantity": row.filled_quantity,
                "average_fill_price": row.average_fill_price,
                "fees": row.fees,
                "status": row.status,
                "trading_mode": row.trading_mode,
                "strategy_name": row.strategy_name,
                "submitted_at": row.submitted_at,
                "filled_at": row.filled_at,
                "raw_response": row.raw_response,
                "user_id": row.user_id,
            }

    async def update_status(
        self,
        order_id: str,
        status: str,
        filled_quantity: Decimal | None = None,
        average_fill_price: Decimal | None = None,
        fees: Decimal | None = None,
        filled_at: datetime | None = None,
        exchange_order_id: str | None = None,
    ) -> None:
        """
        Aktualisiert Order-Status nach Fill.

        exchange_order_id: optional, wird gesetzt wenn die Order beim
        initialen create() (siehe SafeOrderExecutor._persist_pending(),
        Baustein 7 Punkt 5) noch als PENDING ohne bekannte
        exchange_order_id angelegt wurde - der Exchange-Call liefert sie
        erst danach.
        """
        updates: dict[str, Any] = {"status": status}
        if filled_quantity is not None:
            updates["filled_quantity"] = filled_quantity
        if average_fill_price is not None:
            updates["average_fill_price"] = average_fill_price
        if fees is not None:
            updates["fees"] = fees
        if filled_at is not None:
            updates["filled_at"] = filled_at
        if exchange_order_id is not None:
            updates["exchange_order_id"] = exchange_order_id

        async with get_session() as session:
            stmt = update(OrderModel).where(OrderModel.id == order_id).values(**updates)
            await session.execute(stmt)

    async def get_by_user(
        self,
        user_id: str,
        trading_mode: TradingMode,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        async with get_session() as session:
            stmt = (
                select(OrderModel)
                .where(
                    and_(
                        OrderModel.user_id == user_id,
                        OrderModel.trading_mode == trading_mode.value,
                    )
                )
                .order_by(desc(OrderModel.submitted_at))
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [
                {
                    "id": str(r.id),
                    "symbol": r.symbol,
                    "side": r.side,
                    "quantity": str(r.quantity),
                    "status": r.status,
                    "filled_quantity": str(r.filled_quantity),
                    "average_fill_price": str(r.average_fill_price)
                    if r.average_fill_price
                    else None,
                    "fees": str(r.fees),
                    "strategy": r.strategy_name,
                    "submitted_at": r.submitted_at.isoformat(),
                    "filled_at": r.filled_at.isoformat() if r.filled_at else None,
                }
                for r in rows
            ]

    async def get_open_orders(
        self,
        trading_mode: TradingMode,
    ) -> list[dict[str, Any]]:
        """
        Alle Orders in einem nicht-terminalen Status (PENDING/SUBMITTED/
        PARTIALLY_FILLED) fuer einen Trading Mode - system-weit, nicht
        nach user_id gefiltert. Fuer Startup-Recovery gedacht: nach einem
        Crash muss geprueft werden, ob eine als "offen" bekannte Order
        inzwischen auf der Exchange gefuellt/storniert wurde, unabhaengig
        davon welcher User sie ausgeloest hat.
        """
        open_statuses = (
            OrderStatus.PENDING.value,
            OrderStatus.SUBMITTED.value,
            OrderStatus.PARTIALLY_FILLED.value,
        )
        async with get_session() as session:
            stmt = select(OrderModel).where(
                and_(
                    OrderModel.trading_mode == trading_mode.value,
                    OrderModel.status.in_(open_statuses),
                )
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [
                {
                    "id": str(r.id),
                    "signal_id": str(r.signal_id),
                    "exchange_order_id": r.exchange_order_id,
                    "symbol": r.symbol,
                    "exchange": r.exchange,
                    "side": r.side,
                    "order_type": r.order_type,
                    "quantity": r.quantity,
                    "status": r.status,
                    "trading_mode": r.trading_mode,
                    "strategy_name": r.strategy_name,
                    "submitted_at": r.submitted_at,
                }
                for r in rows
            ]


# ---------------------------------------------------------------------------
# Position Repository
# ---------------------------------------------------------------------------


class PositionRepository:
    """
    Persistenz für offene und geschlossene Positionen.

    Notwendig für:
        - Crash-Recovery: PortfolioEngine haelt Positionen aktuell nur
          in-memory. Bei Neustart gehen offene Positionen sonst verloren.
        - Phase 7B Reconciliation: Abgleich DB-Positionen <-> Exchange-State.

    Upsert-Semantik ueber (symbol, exchange, trading_mode, is_open) statt
    reinem Insert: PortfolioEngine haelt pro Symbol maximal eine offene
    Position (siehe PortfolioState._positions: dict[str, Position], ein
    Eintrag pro Symbol-Key). Ein zweiter Fill auf dasselbe Symbol aktualisiert
    dieselbe offene Position, statt eine zweite Zeile anzulegen.
    """

    async def upsert_open(self, position_data: dict[str, Any]) -> str:
        """
        Legt eine offene Position an oder aktualisiert die bestehende offene
        Position fuer (symbol, exchange, trading_mode).

        Erwartete Keys in position_data: id, symbol, exchange, side, quantity,
        entry_price, current_price, leverage, unrealized_pnl, realized_pnl,
        opened_at, strategy_name, trading_mode, user_id (optional).

        Returns: position_id (str)
        """
        async with get_session() as session:
            # user_id MUSS Teil des Lookups sein, sonst findet ein Tenant
            # die offene Position eines ANDEREN Tenants auf demselben
            # Symbol/Exchange/Mode und aktualisiert (ueberschreibt) dessen
            # Zeile statt eine eigene anzulegen (gefunden beim Multi-
            # Tenant-Isolation-Audit, siehe PortfolioEngine.__init__
            # Kommentar zu tenant_id) - kann bei zwei Tenants, die
            # gleichzeitig dasselbe Symbol handeln, sonst Positionen
            # zwischen Tenants vertauschen/ueberschreiben.
            conditions = [
                PositionModel.symbol == position_data["symbol"],
                PositionModel.exchange == position_data["exchange"],
                PositionModel.trading_mode == position_data["trading_mode"],
                PositionModel.is_open.is_(True),
            ]
            user_id = position_data.get("user_id")
            if user_id is not None:
                conditions.append(PositionModel.user_id == user_id)
            else:
                conditions.append(PositionModel.user_id.is_(None))
            stmt = select(PositionModel).where(and_(*conditions)).limit(1)
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing is not None:
                updates = {
                    k: v
                    for k, v in position_data.items()
                    if k not in ("id", "symbol", "exchange", "trading_mode", "opened_at")
                }
                update_stmt = (
                    update(PositionModel)
                    .where(PositionModel.id == existing.id)
                    .values(**updates)
                )
                await session.execute(update_stmt)
                return str(existing.id)

            position_id = position_data.get("id") or str(uuid4())
            row = {**position_data, "id": position_id, "is_open": True}
            position = PositionModel(**row)
            session.add(position)
            await session.flush()
            return str(position.id)

    async def close(
        self,
        position_id: str,
        closed_at: datetime,
        realized_pnl: Decimal | None = None,
        close_reason: str | None = None,
    ) -> None:
        """Markiert eine Position als geschlossen. Idempotent (kein Fehler bei doppeltem Close).

        close_reason: siehe ExitReason (sgr/core/types.py) - None laesst
        die Spalte unveraendert (z.B. bei einem best-effort Retry desselben
        Close, der reason bereits beim ersten Aufruf gesetzt hat).
        """
        updates: dict[str, Any] = {"is_open": False, "closed_at": closed_at}
        if realized_pnl is not None:
            updates["realized_pnl"] = realized_pnl
        if close_reason is not None:
            updates["close_reason"] = close_reason

        async with get_session() as session:
            stmt = update(PositionModel).where(PositionModel.id == position_id).values(**updates)
            await session.execute(stmt)

    async def get_open_positions(
        self,
        trading_mode: TradingMode,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Alle aktuell offenen Positionen fuer einen Trading Mode (fuer Startup-Restore)."""
        async with get_session() as session:
            conditions = [
                PositionModel.trading_mode == trading_mode.value,
                PositionModel.is_open.is_(True),
            ]
            if user_id is not None:
                conditions.append(PositionModel.user_id == user_id)

            stmt = select(PositionModel).where(and_(*conditions)).order_by(PositionModel.opened_at)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [self._to_dict(r) for r in rows]

    async def get_by_symbol(
        self,
        symbol: str,
        exchange: str,
        trading_mode: TradingMode,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Aktuell offene Position fuer ein Symbol, falls vorhanden.
        user_id: siehe get_open_positions()/upsert_open() - ohne Filter
        koennte dies bei zwei Tenants auf demselben Symbol die falsche
        Tenant-Position zurueckgeben. Derzeit kein Produktionsaufrufer
        (Stand Multi-Tenant-Isolation-Audit), Parameter dennoch ergaenzt,
        damit ein kuenftiger Aufrufer nicht in dieselbe Falle laeuft."""
        async with get_session() as session:
            conditions = [
                PositionModel.symbol == symbol,
                PositionModel.exchange == exchange,
                PositionModel.trading_mode == trading_mode.value,
                PositionModel.is_open.is_(True),
            ]
            if user_id is not None:
                conditions.append(PositionModel.user_id == user_id)
            stmt = select(PositionModel).where(and_(*conditions))
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return self._to_dict(row) if row is not None else None

    @staticmethod
    def _to_dict(r: PositionModel) -> dict[str, Any]:
        return {
            "id": str(r.id),
            "symbol": r.symbol,
            "exchange": r.exchange,
            "side": r.side,
            "quantity": r.quantity,
            "entry_price": r.entry_price,
            "current_price": r.current_price,
            "leverage": r.leverage,
            "unrealized_pnl": r.unrealized_pnl,
            "realized_pnl": r.realized_pnl,
            "is_open": r.is_open,
            "opened_at": r.opened_at,
            "closed_at": r.closed_at,
            "strategy_name": r.strategy_name,
            "trading_mode": r.trading_mode,
            "user_id": str(r.user_id) if r.user_id else None,
            "stop_loss_price": r.stop_loss_price,
            "take_profit_price": r.take_profit_price,
            "max_holding_until": r.max_holding_until,
            "sl_order_id": r.sl_order_id,
            "tp_order_id": r.tp_order_id,
            "close_reason": r.close_reason,
        }


# ---------------------------------------------------------------------------
# Portfolio Snapshot Repository
# ---------------------------------------------------------------------------


class PortfolioSnapshotRepository:
    """
    Periodische Portfolio-Zustands-Snapshots (Cash, Wert, Drawdown).

    Geschrieben vom Worker (alleiniger PortfolioEngine-Owner), gelesen von
    der API (kein eigener In-Memory-State mehr, siehe PortfolioSnapshotModel
    Docstring). Reines Insert, keine Updates - historische Snapshots bleiben
    fuer spaetere Performance-/Drawdown-Charts erhalten.
    """

    async def create(self, snapshot_data: dict[str, Any]) -> str:
        """
        Erwartete Keys: user_id (optional), trading_mode, portfolio_value,
        cash, unrealized_pnl, peak_value, drawdown, open_positions_count,
        total_trades, created_at.
        """
        async with get_session() as session:
            snapshot_id = snapshot_data.get("id") or str(uuid4())
            row = {**snapshot_data, "id": snapshot_id}
            snapshot = PortfolioSnapshotModel(**row)
            session.add(snapshot)
            await session.flush()
            return str(snapshot.id)

    async def get_latest(
        self,
        trading_mode: TradingMode,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Neuester Snapshot fuer (user_id, trading_mode). None, falls noch keiner existiert
        (z.B. Worker lief noch nicht lang genug fuer den ersten Snapshot)."""
        async with get_session() as session:
            conditions = [PortfolioSnapshotModel.trading_mode == trading_mode.value]
            if user_id is not None:
                conditions.append(PortfolioSnapshotModel.user_id == user_id)
            else:
                conditions.append(PortfolioSnapshotModel.user_id.is_(None))

            stmt = (
                select(PortfolioSnapshotModel)
                .where(and_(*conditions))
                .order_by(desc(PortfolioSnapshotModel.created_at))
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return self._to_dict(row) if row is not None else None

    @staticmethod
    def _to_dict(r: PortfolioSnapshotModel) -> dict[str, Any]:
        return {
            "id": str(r.id),
            "user_id": str(r.user_id) if r.user_id else None,
            "trading_mode": r.trading_mode,
            "portfolio_value": r.portfolio_value,
            "cash": r.cash,
            "unrealized_pnl": r.unrealized_pnl,
            "peak_value": r.peak_value,
            "drawdown": r.drawdown,
            "open_positions_count": r.open_positions_count,
            "total_trades": r.total_trades,
            "created_at": r.created_at,
        }


# ---------------------------------------------------------------------------
# Trade Repository
# ---------------------------------------------------------------------------


class TradeRepository:
    """Immutable Trade Records – einmal geschrieben, nie verändert."""

    async def create(self, trade_data: dict[str, Any]) -> str:
        """
        Erwartete Keys: position_id, symbol, exchange, side, entry_price,
        exit_price, quantity, realized_pnl, fees_total, net_pnl,
        holding_seconds, strategy_name, regime, trading_mode, opened_at,
        closed_at, trade_metadata (optional dict), user_id (optional).

        id wird bei Bedarf automatisch erzeugt und trade_metadata defaultet
        auf {} (analog zu PositionRepository.upsert_open()/
        PortfolioSnapshotRepository.create() - der Aufrufer muss beides
        nicht selbst setzen). Root-Cause-Fix (Position-Protection-Audit
        2026-09-16): dieses create() wurde bisher an KEINER Stelle im
        Code aufgerufen - PortfolioEngine._record_trade() hielt
        geschlossene Trades ausschliesslich in einer In-Memory-Liste
        (self._trade_history), verloren bei jedem Worker-Neustart. Siehe
        PortfolioEngine._persist_trade() fuer den neuen Aufrufer.
        """
        async with get_session() as session:
            trade_id = trade_data.get("id") or str(uuid4())
            row = {**trade_data, "id": trade_id}
            row.setdefault("trade_metadata", {})
            trade = TradeModel(**row)
            session.add(trade)
            await session.flush()
            return str(trade.id)

    async def get_performance_by_strategy(
        self,
        strategy_name: str,
        trading_mode: TradingMode,
        since: datetime,
    ) -> dict[str, Any]:
        """Berechnet Performance-Metriken für eine Strategie."""
        async with get_session() as session:
            stmt = (
                select(TradeModel)
                .where(
                    and_(
                        TradeModel.strategy_name == strategy_name,
                        TradeModel.trading_mode == trading_mode.value,
                        TradeModel.closed_at >= since,
                    )
                )
                .order_by(TradeModel.closed_at)
            )
            result = await session.execute(stmt)
            trades = result.scalars().all()

            if not trades:
                return {"total_trades": 0}

            net_pnls = [float(t.net_pnl) for t in trades]
            winners = [p for p in net_pnls if p > 0]
            losers = [p for p in net_pnls if p <= 0]

            return {
                "total_trades": len(trades),
                "winning_trades": len(winners),
                "losing_trades": len(losers),
                "hit_rate": len(winners) / len(trades),
                "total_net_pnl": sum(net_pnls),
                "avg_winner": sum(winners) / len(winners) if winners else 0.0,
                "avg_loser": sum(losers) / len(losers) if losers else 0.0,
                "profit_factor": (
                    sum(winners) / abs(sum(losers)) if losers and sum(losers) != 0 else float("inf")
                ),
            }


    async def get_recent(
        self,
        trading_mode: TradingMode,
        limit: int = 50,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Zuletzt geschlossene Trades, neueste zuerst. Fuer /portfolio/trades."""
        async with get_session() as session:
            conditions = [TradeModel.trading_mode == trading_mode.value]
            if user_id is not None:
                conditions.append(TradeModel.user_id == user_id)

            stmt = (
                select(TradeModel)
                .where(and_(*conditions))
                .order_by(desc(TradeModel.closed_at))
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [self._to_dict(r) for r in rows]

    async def get_pnl_summary(
        self,
        trading_mode: TradingMode,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Aggregierte realized-PnL-Kennzahlen ueber ALLE geschlossenen Trades
        (nicht nur die letzten N wie get_recent()). Aggregation erfolgt in
        SQL statt Python, damit keine unbeschraenkte Row-Menge in den
        API-Prozess geladen werden muss - fuer GET /portfolio/pnl.
        """
        async with get_session() as session:
            conditions = [TradeModel.trading_mode == trading_mode.value]
            if user_id is not None:
                conditions.append(TradeModel.user_id == user_id)

            stmt = select(
                func.count(TradeModel.id),
                func.coalesce(func.sum(TradeModel.realized_pnl), 0),
                func.coalesce(func.sum(TradeModel.fees_total), 0),
                func.count(TradeModel.id).filter(TradeModel.realized_pnl > 0),
            ).where(and_(*conditions))
            result = await session.execute(stmt)
            total_trades, total_realized, total_fees, winning_trades = result.one()

            hit_rate = (winning_trades / total_trades) if total_trades else 0.0

            return {
                "total_trades": total_trades,
                "winning_trades": winning_trades,
                "total_realized_pnl": Decimal(total_realized),
                "total_fees": Decimal(total_fees),
                "hit_rate": hit_rate,
            }

    @staticmethod
    def _to_dict(r: TradeModel) -> dict[str, Any]:
        return {
            "id": str(r.id),
            "symbol": r.symbol,
            "side": r.side,
            "entry_price": str(r.entry_price),
            "exit_price": str(r.exit_price),
            "quantity": str(r.quantity),
            "realized_pnl": str(r.realized_pnl),
            "fees": str(r.fees_total),
            "net_pnl": str(r.net_pnl),
            "strategy": r.strategy_name,
            "opened_at": r.opened_at.isoformat(),
            "closed_at": r.closed_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# Strategy Repository
# ---------------------------------------------------------------------------


class StrategyRepository:
    """Persistenz für Strategy-Registry und Performance-Updates."""

    async def upsert(self, name: str, version: str, supported_regimes: list[str]) -> None:
        async with get_session() as session:
            now = datetime.utcnow()
            stmt = pg_insert(StrategyModel).values(
                name=name,
                version=version,
                supported_regimes=supported_regimes,
                created_at=now,
                updated_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["name"],
                set_={"version": version, "updated_at": now},
            )
            await session.execute(stmt)

    async def update_performance(
        self,
        name: str,
        sharpe: float,
        sortino: float,
        max_drawdown: float,
        hit_rate: float,
        total_trades: int,
    ) -> None:
        async with get_session() as session:
            stmt = (
                update(StrategyModel)
                .where(StrategyModel.name == name)
                .values(
                    sharpe_ratio=sharpe,
                    sortino_ratio=sortino,
                    max_drawdown=max_drawdown,
                    hit_rate=hit_rate,
                    total_trades=total_trades,
                    updated_at=datetime.utcnow(),
                )
            )
            await session.execute(stmt)

    async def set_validated(self, name: str, is_validated: bool) -> None:
        """Persistiert den Validierungsstatus. Best-effort, vom Aufrufer
        gegen Exceptions abzusichern (siehe StrategyRegistry.mark_validated
        Docstring - dort bewusst NICHT aufgerufen, um die Methode synchron
        zu halten; Aufrufer mit Event-Loop-Kontext, z.B. sgr/api/main.py
        lifespan(), rufen dies direkt zusaetzlich auf)."""
        async with get_session() as session:
            stmt = (
                update(StrategyModel)
                .where(StrategyModel.name == name)
                .values(is_validated=is_validated, updated_at=datetime.utcnow())
            )
            await session.execute(stmt)

    async def set_active(self, name: str, is_active: bool, reason: str | None = None) -> None:
        async with get_session() as session:
            updates: dict[str, Any] = {
                "is_active": is_active,
                "updated_at": datetime.utcnow(),
            }
            if not is_active and reason:
                updates["deactivation_reason"] = reason
            stmt = update(StrategyModel).where(StrategyModel.name == name).values(**updates)
            await session.execute(stmt)

    async def get_active_names(self) -> list[str]:
        """
        Namen aller Strategien, die beim letzten Shutdown als aktiv
        markiert waren. Fuer Startup-Recovery: StrategyRegistry haelt
        is_active nur in-memory (siehe strategy/registry.py) - ohne
        diesen Restore-Schritt startet jede Strategie nach einem Neustart
        deaktiviert, unabhaengig vom Zustand davor.
        """
        async with get_session() as session:
            stmt = select(StrategyModel.name).where(StrategyModel.is_active.is_(True))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_all(self) -> list[dict[str, Any]]:
        """
        Alle registrierten Strategien mit vollstaendigem Status + Performance.

        Fuer sgr-api (GET /api/v1/strategy/): die API besitzt seit der
        sgr-api/sgr-worker-Trennung keine eigene StrategyRegistry-Instanz
        mehr, die etwas bedeuten wuerde (StrategyRegistry._entries ist
        In-Memory und prozesslokal - eine Instanz im API-Prozess wuesste
        nichts von den tatsaechlich im Worker laufenden Strategien). Die
        DB ist daher die einzige Quelle, die beide Prozesse teilen.
        """
        async with get_session() as session:
            stmt = select(StrategyModel).order_by(StrategyModel.name)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [self._to_dict(r) for r in rows]

    async def get_by_name(self, name: str) -> dict[str, Any] | None:
        """Einzelne Strategie nach Name, oder None falls nicht registriert.
        Fuer den 404-Check in den activate/deactivate-Endpunkten - siehe
        get_all() Docstring fuer den Hintergrund, warum das ueber die DB
        statt StrategyRegistry laeuft."""
        async with get_session() as session:
            stmt = select(StrategyModel).where(StrategyModel.name == name)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return self._to_dict(row) if row is not None else None

    @staticmethod
    def _to_dict(r: StrategyModel) -> dict[str, Any]:
        return {
            "name": r.name,
            "version": r.version,
            "is_active": r.is_active,
            "is_validated": r.is_validated,
            "supported_regimes": r.supported_regimes,
            "deactivation_reason": r.deactivation_reason,
            "sharpe_ratio": float(r.sharpe_ratio) if r.sharpe_ratio is not None else None,
            "sortino_ratio": float(r.sortino_ratio) if r.sortino_ratio is not None else None,
            "max_drawdown": float(r.max_drawdown) if r.max_drawdown is not None else None,
            "hit_rate": float(r.hit_rate) if r.hit_rate is not None else None,
            "total_trades": r.total_trades,
        }


# ---------------------------------------------------------------------------
# Strategy Symbol Validation Repository
# ---------------------------------------------------------------------------


class StrategySymbolValidationRepository:
    """
    Persistenz fuer pro-(symbol, timeframe, strategy)-Validierungsergebnisse
    - siehe StrategySymbolValidationModel Docstring und
    sgr/strategy/symbol_validation_runner.py.
    """

    async def upsert(
        self,
        *,
        symbol: str,
        exchange: str,
        timeframe: str,
        strategy: str,
        status: str,
        batch_id: str,
        parameters: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        data_quality: dict[str, Any] | None = None,
        regime_profile: dict[str, Any] | None = None,
        score: float | None = None,
        robustness_score: float | None = None,
        failure_reason: str | None = None,
        is_best_for_symbol: bool = False,
    ) -> None:
        async with get_session() as session:
            now = datetime.utcnow()
            values = {
                "id": str(uuid4()),
                "symbol": symbol,
                "exchange": exchange,
                "timeframe": timeframe,
                "strategy": strategy,
                "status": status,
                "is_best_for_symbol": is_best_for_symbol,
                "parameters": parameters or {},
                "metrics": metrics or {},
                "data_quality": data_quality or {},
                "regime_profile": regime_profile or {},
                "score": score,
                "robustness_score": robustness_score,
                "failure_reason": failure_reason,
                "batch_id": batch_id,
                "validated_at": now,
            }
            stmt = pg_insert(StrategySymbolValidationModel).values(**values)
            update_cols = {k: v for k, v in values.items() if k not in ("id",)}
            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol", "exchange", "timeframe", "strategy", "batch_id"],
                set_=update_cols,
            )
            await session.execute(stmt)

    async def clear_best_flag(
        self, *, symbol: str, exchange: str, timeframe: str, batch_id: str
    ) -> None:
        """Setzt is_best_for_symbol fuer alle Zeilen dieses Symbols in
        diesem Batch auf False, bevor der neue Gewinner markiert wird -
        stellt sicher, dass hoechstens eine Zeile pro Symbol+Batch
        is_best_for_symbol=True traegt."""
        async with get_session() as session:
            stmt = (
                update(StrategySymbolValidationModel)
                .where(
                    and_(
                        StrategySymbolValidationModel.symbol == symbol,
                        StrategySymbolValidationModel.exchange == exchange,
                        StrategySymbolValidationModel.timeframe == timeframe,
                        StrategySymbolValidationModel.batch_id == batch_id,
                    )
                )
                .values(is_best_for_symbol=False)
            )
            await session.execute(stmt)

    async def get_by_symbol(
        self, *, symbol: str, exchange: str, timeframe: str, batch_id: str | None = None
    ) -> list[dict[str, Any]]:
        async with get_session() as session:
            conditions = [
                StrategySymbolValidationModel.symbol == symbol,
                StrategySymbolValidationModel.exchange == exchange,
                StrategySymbolValidationModel.timeframe == timeframe,
            ]
            if batch_id:
                conditions.append(StrategySymbolValidationModel.batch_id == batch_id)
            stmt = (
                select(StrategySymbolValidationModel)
                .where(and_(*conditions))
                .order_by(desc(StrategySymbolValidationModel.validated_at))
            )
            result = await session.execute(stmt)
            return [self._to_dict(r) for r in result.scalars().all()]

    async def get_processed_symbols(self, *, batch_id: str) -> set[str]:
        """Fuer Resume/Idempotenz (Phase 17): Symbole, die in diesem
        Batch ein VOLLSTAENDIGES finales Ergebnis haben.

        BUG (gefunden 2026-09-15, live beobachtet): vorher zaehlte JEDE
        Row mit diesem batch_id als "verarbeitet", unabhaengig von
        is_best_for_symbol. sgr.strategy.symbol_validation_runner.
        _validate_symbol() schreibt aber pro Kandidaten-Strategie eine
        eigene Zwischen-Row (is_best_for_symbol=False), BEVOR am Ende
        die eine finale "Gewinner"-Row (is_best_for_symbol=True)
        geschrieben wird. Wird der Prozess dazwischen abgebrochen
        (SIGTERM/SIGKILL, z.B. bei einer Speicher-bedingten Drosselung
        der Batch-Parallelitaet), existieren fuer dieses Symbol bereits
        Candidate-Rows, aber NIE eine finale Row - das alte Verhalten
        stufte das Symbol trotzdem als "processed" ein und ein Resume
        hat es dadurch STILLSCHWEIGEND fuer immer uebersprungen. Live
        beobachtet: 4 von 718 Symbolen (AVA/USDT, QTUM/USDT, SONIC/USDT,
        MANA/USDT) blieben nach einem absichtlichen SIGTERM ohne
        finales Ergebnis, bis dies manuell per Row-Loeschung + Rerun
        korrigiert wurde.

        Jeder der drei Abschluss-Pfade in _validate_symbol() (Data-
        Quality-Ablehnung, kein Regime-Match, oder die volle Strategie-
        Schleife) UND der TECHNICAL_FAILURE-Pfad in run_batch() schreiben
        exakt eine Row mit is_best_for_symbol=True fuer dieses Symbol -
        das ist damit ein zuverlaessiger Marker fuer "vollstaendig
        abgeschlossen", unabhaengig vom konkreten Ergebnis (ACTIVE,
        NO_VALID_STRATEGY, INSUFFICIENT_DATA, INVALID_DATA,
        TECHNICAL_FAILURE - jeder dieser Endzustaende setzt ihn).
        Reine Candidate-Rows (Zwischenergebnisse pro Strategie waehrend
        der Schleife) setzen ihn nie - ein Symbol mit ausschliesslich
        solchen Rows gilt daher korrekt als NICHT abgeschlossen und wird
        beim naechsten Resume erneut vollstaendig verarbeitet."""
        async with get_session() as session:
            stmt = (
                select(StrategySymbolValidationModel.symbol)
                .where(
                    and_(
                        StrategySymbolValidationModel.batch_id == batch_id,
                        StrategySymbolValidationModel.is_best_for_symbol.is_(True),
                    )
                )
                .distinct()
            )
            result = await session.execute(stmt)
            return set(result.scalars().all())

    async def get_best_by_symbol(
        self, *, batch_id: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        async with get_session() as session:
            conditions = [StrategySymbolValidationModel.is_best_for_symbol.is_(True)]
            if batch_id:
                conditions.append(StrategySymbolValidationModel.batch_id == batch_id)
            stmt = (
                select(StrategySymbolValidationModel)
                .where(and_(*conditions))
                .order_by(desc(StrategySymbolValidationModel.score))
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [self._to_dict(r) for r in result.scalars().all()]

    async def get_active(self, *, batch_id: str | None = None) -> list[dict[str, Any]]:
        async with get_session() as session:
            conditions = [StrategySymbolValidationModel.status == "active"]
            if batch_id:
                conditions.append(StrategySymbolValidationModel.batch_id == batch_id)
            stmt = select(StrategySymbolValidationModel).where(and_(*conditions))
            result = await session.execute(stmt)
            return [self._to_dict(r) for r in result.scalars().all()]

    async def get_active_for_symbol(
        self, *, symbol: str, exchange: str, timeframe: str
    ) -> set[str]:
        """Namen der Strategien, die fuer dieses (symbol, exchange,
        timeframe) aktuell als 'active' markiert sind - Production-
        Read-Pfad, siehe sgr/strategy/symbol_gate.py. Betrachtet nur die
        neueste validated_at Zeile pro Strategie (falls mehrere Batches
        existieren)."""
        async with get_session() as session:
            stmt = (
                select(StrategySymbolValidationModel.strategy)
                .where(
                    and_(
                        StrategySymbolValidationModel.symbol == symbol,
                        StrategySymbolValidationModel.exchange == exchange,
                        StrategySymbolValidationModel.timeframe == timeframe,
                        StrategySymbolValidationModel.status == "active",
                    )
                )
                .distinct()
            )
            result = await session.execute(stmt)
            return set(result.scalars().all())

    async def has_any_result_for_symbol(
        self, *, symbol: str, exchange: str, timeframe: str
    ) -> bool:
        """Ob ueberhaupt schon ein Validierungsergebnis fuer dieses
        Symbol existiert (irgendein Batch) - fuer symbol_gate.py's
        Fallback-Entscheidung (kein Ergebnis = bestehendes globales
        Verhalten NICHT einschraenken, siehe dortigen Docstring)."""
        async with get_session() as session:
            stmt = (
                select(func.count())
                .select_from(StrategySymbolValidationModel)
                .where(
                    and_(
                        StrategySymbolValidationModel.symbol == symbol,
                        StrategySymbolValidationModel.exchange == exchange,
                        StrategySymbolValidationModel.timeframe == timeframe,
                    )
                )
            )
            result = await session.execute(stmt)
            count = result.scalar_one()
            return bool(count and count > 0)

    async def get_all_for_batch(self, *, batch_id: str) -> list[dict[str, Any]]:
        """Alle Rows (Candidate- UND finale Rows) eines Batches - fuer
        die aggregierte Failure-/Gate-/Strategy-Comparison-Analyse
        (scripts/analyze_strategy_validation.py). Im Unterschied zu
        get_best_by_symbol() bewusst OHNE is_best_for_symbol-Filter:
        die Analyse braucht die vollstaendigen Zwischenergebnisse jeder
        einzelnen getesteten Strategie pro Symbol, nicht nur die
        jeweilige Gewinner-Zeile."""
        async with get_session() as session:
            stmt = select(StrategySymbolValidationModel).where(
                StrategySymbolValidationModel.batch_id == batch_id
            )
            result = await session.execute(stmt)
            return [self._to_dict(r) for r in result.scalars().all()]

    async def get_status_counts(self, *, batch_id: str | None = None) -> dict[str, int]:
        async with get_session() as session:
            stmt = select(
                StrategySymbolValidationModel.status,
                func.count(func.distinct(StrategySymbolValidationModel.symbol)),
            ).group_by(StrategySymbolValidationModel.status)
            if batch_id:
                stmt = stmt.where(StrategySymbolValidationModel.batch_id == batch_id)
            result = await session.execute(stmt)
            return {status: count for status, count in result.all()}

    async def get_strategy_distribution(self, *, batch_id: str | None = None) -> dict[str, int]:
        """Verteilung der Strategien unter den ACTIVE-Ergebnissen (fuer
        den Final Report - Phase 21 'Strategy distribution')."""
        async with get_session() as session:
            conditions = [StrategySymbolValidationModel.is_best_for_symbol.is_(True)]
            if batch_id:
                conditions.append(StrategySymbolValidationModel.batch_id == batch_id)
            stmt = (
                select(
                    StrategySymbolValidationModel.strategy,
                    func.count(),
                )
                .where(and_(*conditions))
                .group_by(StrategySymbolValidationModel.strategy)
            )
            result = await session.execute(stmt)
            return {strategy: count for strategy, count in result.all()}

    async def get_top_n(
        self, *, order_by: str, limit: int = 20, batch_id: str | None = None
    ) -> list[dict[str, Any]]:
        """order_by: 'score' | 'sharpe' | 'return' | 'robustness' -
        siehe Final Report Phase 21 ('Top 20 nach Score/Sharpe/Return/
        Robustness')."""
        column_map = {
            "score": StrategySymbolValidationModel.score,
            "robustness": StrategySymbolValidationModel.robustness_score,
        }
        async with get_session() as session:
            conditions = [StrategySymbolValidationModel.is_best_for_symbol.is_(True)]
            if batch_id:
                conditions.append(StrategySymbolValidationModel.batch_id == batch_id)
            if order_by in column_map:
                stmt = (
                    select(StrategySymbolValidationModel)
                    .where(and_(*conditions))
                    .order_by(desc(column_map[order_by]))
                    .limit(limit)
                )
                result = await session.execute(stmt)
                rows = list(result.scalars().all())
            else:
                # sharpe/return liegen in der JSONB metrics-Spalte - in
                # Python sortieren statt einer JSONB-Pfad-Expression, um
                # DB-Dialekt-Kopplung in diesem Read-Pfad zu vermeiden.
                stmt = select(StrategySymbolValidationModel).where(and_(*conditions))
                result = await session.execute(stmt)
                metric_key = "sharpe_ratio" if order_by == "sharpe" else "total_return_pct"
                rows = sorted(
                    result.scalars().all(),
                    key=lambda r: (r.metrics or {}).get(metric_key, float("-inf")) or float("-inf"),
                    reverse=True,
                )[:limit]
            return [self._to_dict(r) for r in rows]

    @staticmethod
    def _to_dict(r: StrategySymbolValidationModel) -> dict[str, Any]:
        return {
            "symbol": r.symbol,
            "exchange": r.exchange,
            "timeframe": r.timeframe,
            "strategy": r.strategy,
            "status": r.status,
            "is_best_for_symbol": r.is_best_for_symbol,
            "parameters": r.parameters,
            "metrics": r.metrics,
            "data_quality": r.data_quality,
            "regime_profile": r.regime_profile,
            "score": float(r.score) if r.score is not None else None,
            "robustness_score": (
                float(r.robustness_score) if r.robustness_score is not None else None
            ),
            "failure_reason": r.failure_reason,
            "batch_id": r.batch_id,
            "validated_at": r.validated_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# User Repository (SaaS)
# ---------------------------------------------------------------------------


class UserRepository:
    """User-Management für SaaS-Layer."""

    async def create(
        self,
        email: str,
        hashed_password: str,
        trading_mode: TradingMode = TradingMode.PAPER,
        is_admin: bool = False,
    ) -> str:
        async with get_session() as session:
            now = datetime.utcnow()
            user = UserModel(
                id=str(uuid4()),
                email=email,
                hashed_password=hashed_password,
                trading_mode=trading_mode.value,
                is_admin=is_admin,
                created_at=now,
            )
            session.add(user)
            await session.flush()
            return str(user.id)

    async def get_by_email(self, email: str) -> dict[str, Any] | None:
        async with get_session() as session:
            stmt = select(UserModel).where(UserModel.email == email)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()
            if user is None:
                return None
            return {
                "id": str(user.id),
                "email": user.email,
                "hashed_password": user.hashed_password,
                "is_active": user.is_active,
                "is_2fa_enabled": user.is_2fa_enabled,
                "is_admin": user.is_admin,
                "trading_mode": user.trading_mode,
                "totp_secret": user.totp_secret,
            }

    async def update_last_login(self, user_id: str) -> None:
        async with get_session() as session:
            stmt = (
                update(UserModel)
                .where(UserModel.id == user_id)
                .values(last_login_at=datetime.utcnow())
            )
            await session.execute(stmt)

    async def set_admin_status(self, email: str, is_admin: bool) -> bool:
        """
        Setzt is_admin fuer einen User per Email. Fuer scripts/grant_admin.py
        (siehe dort) - bewusst kein API-Endpoint dafuer, siehe UserModel
        Docstring bei is_admin.

        Returns:
            True wenn ein User aktualisiert wurde, False wenn kein User
            mit dieser Email existiert.
        """
        async with get_session() as session:
            stmt = (
                update(UserModel).where(UserModel.email == email).values(is_admin=is_admin)
            )
            result = await session.execute(stmt)
            return bool(result.rowcount and result.rowcount > 0)


# ---------------------------------------------------------------------------
# Risk Event Repository (Audit Log)
# ---------------------------------------------------------------------------


class RiskEventRepository:
    """Immutable Audit Log für Risk Events."""

    async def log_event(
        self,
        event_type: str,
        severity: str,
        title: str,
        message: str,
        trading_mode: TradingMode,
        metrics_snapshot: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> None:
        async with get_session() as session:
            event = RiskEventModel(
                id=str(uuid4()),
                event_type=event_type,
                severity=severity,
                title=title,
                message=message,
                trading_mode=trading_mode.value,
                metrics_snapshot=metrics_snapshot or {},
                timestamp=datetime.utcnow(),
                user_id=user_id,
            )
            session.add(event)


# ---------------------------------------------------------------------------
# Audit Log Repository (Security)
# ---------------------------------------------------------------------------


class AuditLogRepository:
    """
    Immutable Audit Log fuer sicherheitsrelevante Aktionen.
    Backing-Store fuer sgr.core.security.audit_log().
    """

    async def log_action(
        self,
        action: str,
        user_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        async with get_session() as session:
            entry = AuditLogModel(
                id=str(uuid4()),
                action=action,
                user_id=user_id or "system",
                details=details or {},
                timestamp=datetime.utcnow(),
            )
            session.add(entry)


# ---------------------------------------------------------------------------
# Repository Factory (Dependency Injection)
# ---------------------------------------------------------------------------


class GridRepository:
    """
    Persistenz fuer Futures-Grid-Instanzen (siehe GridModel/GridOrderModel
    in sgr/core/database.py und GridController._persist()). Best-effort
    aus Aufrufer-Sicht (Controller-Aufrufer faengt Exceptions bereits ab,
    analog zu PortfolioEngine._persist_position_upsert()) - dieses
    Repository selbst wirft normale Exceptions weiter, wie alle anderen
    Repositories hier auch.
    """

    async def upsert(self, grid: Any) -> str:
        """
        grid: sgr.core.grid_types.GridState. Nimmt das Pydantic-Objekt
        direkt entgegen (statt eines rohen dict wie die aelteren
        Repositories) - GridState ist bereits die kanonische, validierte
        Repraesentation, ein zusaetzliches dict-Mapping an jeder
        Aufrufstelle waere reine Verdopplung.
        """
        async with get_session() as session:
            stmt = select(GridModel).where(GridModel.id == str(grid.id)).limit(1)
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            values = {
                "user_id": grid.tenant_id,
                "exchange": grid.exchange.value,
                "symbol": grid.symbol.ccxt_symbol,
                "product_type": "futures_grid",
                "strategy_name": grid.strategy_name,
                "trading_mode": grid.trading_mode.value,
                "direction": grid.direction.value,
                "status": grid.status.value,
                "parameters": grid.parameters,
                "net_position_qty": grid.net_position_qty,
                "realized_pnl": grid.realized_pnl,
                "fees_paid": grid.fees_paid,
                "funding_paid": grid.funding_paid,
                "fills_count": grid.fills_count,
                "closed_at": grid.closed_at,
                "close_reason": grid.close_reason,
            }

            if existing is not None:
                await session.execute(
                    update(GridModel).where(GridModel.id == str(grid.id)).values(**values)
                )
                return str(grid.id)

            row = GridModel(id=str(grid.id), opened_at=grid.opened_at, **values)
            session.add(row)
            await session.flush()
            return str(grid.id)

    async def record_level_fill(
        self,
        grid_id: str,
        level_index: int,
        price: Decimal,
        side: str,
        quantity: Decimal,
        is_opening: bool,
        filled_at: datetime,
        order_id: str | None = None,
        cycle_pnl: Decimal | None = None,
    ) -> str:
        fill_id = str(uuid4())
        async with get_session() as session:
            session.add(
                GridOrderModel(
                    id=fill_id,
                    grid_id=grid_id,
                    order_id=order_id,
                    level_index=level_index,
                    price=price,
                    side=side,
                    quantity=quantity,
                    is_opening=is_opening,
                    cycle_pnl=cycle_pnl,
                    filled_at=filled_at,
                )
            )
            await session.flush()
        return fill_id

    async def get_open_grids(
        self, trading_mode: TradingMode, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        conditions = [
            GridModel.trading_mode == trading_mode.value,
            GridModel.status.in_(["pending", "active", "paused", "closing"]),
        ]
        if user_id is not None:
            conditions.append(GridModel.user_id == user_id)

        async with get_session() as session:
            stmt = select(GridModel).where(and_(*conditions))
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [self._to_dict(row) for row in rows]

    async def get_by_id(self, grid_id: str) -> dict[str, Any] | None:
        async with get_session() as session:
            stmt = select(GridModel).where(GridModel.id == grid_id).limit(1)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return self._to_dict(row) if row else None

    @staticmethod
    def _to_dict(row: GridModel) -> dict[str, Any]:
        return {
            "id": row.id,
            "user_id": row.user_id,
            "exchange": row.exchange,
            "symbol": row.symbol,
            "product_type": row.product_type,
            "strategy_name": row.strategy_name,
            "trading_mode": row.trading_mode,
            "direction": row.direction,
            "status": row.status,
            "parameters": row.parameters,
            "net_position_qty": row.net_position_qty,
            "realized_pnl": row.realized_pnl,
            "fees_paid": row.fees_paid,
            "funding_paid": row.funding_paid,
            "fills_count": row.fills_count,
            "opened_at": row.opened_at,
            "closed_at": row.closed_at,
            "close_reason": row.close_reason,
        }


class Repositories:
    """Bündelt alle Repositories für einfachen Zugriff."""

    def __init__(self) -> None:
        self.candles = CandleRepository()
        self.orders = OrderRepository()
        self.positions = PositionRepository()
        self.portfolio_snapshots = PortfolioSnapshotRepository()
        self.trades = TradeRepository()
        self.strategies = StrategyRepository()
        self.strategy_symbol_validations = StrategySymbolValidationRepository()
        self.users = UserRepository()
        self.risk_events = RiskEventRepository()
        self.audit_log = AuditLogRepository()
        self.grids = GridRepository()


# Singleton
_repos: Repositories | None = None


def get_repositories() -> Repositories:
    global _repos
    if _repos is None:
        _repos = Repositories()
    return _repos
