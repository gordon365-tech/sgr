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

from sqlalchemy import and_, desc, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sgr.core.database import (
    CandleModel,
    OrderModel,
    RiskEventModel,
    StrategyModel,
    TradeModel,
    UserModel,
    get_session,
)
from sgr.core.logging import get_logger
from sgr.core.types import Candle, TradingMode

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Candle Repository
# ---------------------------------------------------------------------------


class CandleRepository:
    """TimescaleDB-backed Candle Storage."""

    async def upsert_batch(self, candles: list[Candle]) -> int:
        """
        Batch-Upsert von Candles. Ignoriert Duplikate (ON CONFLICT DO NOTHING).
        Optimiert für TimescaleDB: bulk insert > row-by-row.
        Returns: Anzahl eingefügter (neuer) Candles.
        """
        if not candles:
            return 0

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
            for c in candles
        ]

        async with get_session() as session:
            stmt = pg_insert(CandleModel).values(rows)
            stmt = stmt.on_conflict_do_nothing(constraint="uq_candle")
            result = await session.execute(stmt)
            inserted = result.rowcount
            log.debug("candle_repo.upserted", count=inserted, total=len(candles))
            return inserted

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

    async def update_status(
        self,
        order_id: str,
        status: str,
        filled_quantity: Decimal | None = None,
        average_fill_price: Decimal | None = None,
        fees: Decimal | None = None,
        filled_at: datetime | None = None,
    ) -> None:
        """Aktualisiert Order-Status nach Fill."""
        updates: dict[str, Any] = {"status": status}
        if filled_quantity is not None:
            updates["filled_quantity"] = filled_quantity
        if average_fill_price is not None:
            updates["average_fill_price"] = average_fill_price
        if fees is not None:
            updates["fees"] = fees
        if filled_at is not None:
            updates["filled_at"] = filled_at

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


# ---------------------------------------------------------------------------
# Trade Repository
# ---------------------------------------------------------------------------


class TradeRepository:
    """Immutable Trade Records – einmal geschrieben, nie verändert."""

    async def create(self, trade_data: dict[str, Any]) -> str:
        async with get_session() as session:
            trade = TradeModel(**trade_data)
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
    ) -> str:
        async with get_session() as session:
            now = datetime.utcnow()
            user = UserModel(
                id=str(uuid4()),
                email=email,
                hashed_password=hashed_password,
                trading_mode=trading_mode.value,
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
# Repository Factory (Dependency Injection)
# ---------------------------------------------------------------------------


class Repositories:
    """Bündelt alle Repositories für einfachen Zugriff."""

    def __init__(self) -> None:
        self.candles = CandleRepository()
        self.orders = OrderRepository()
        self.trades = TradeRepository()
        self.strategies = StrategyRepository()
        self.users = UserRepository()
        self.risk_events = RiskEventRepository()


# Singleton
_repos: Repositories | None = None


def get_repositories() -> Repositories:
    global _repos
    if _repos is None:
        _repos = Repositories()
    return _repos
