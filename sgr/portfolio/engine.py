"""
SGR Portfolio Engine
====================
Echtzeit-Positionsmanagement und PnL-Berechnung.

Verantwortlichkeiten:
    1. Positionen tracken (öffnen, aktualisieren, schließen)
    2. PnL berechnen (unrealized + realized, per Position + gesamt)
    3. Closed Trades als immutable Records speichern (Audit + Fees)
    4. Portfolio-Wert berechnen (Cash + offene Positionen)
    5. Auf OrderFilledEvent reagieren (öffnet/schließt Positionen)
    6. Auf KillSwitchEvent reagieren (schließt alle Positionen)

State-Management:
    - Primär in-memory (Redis Backup für Crash-Recovery)
    - Bei Startup: Reconciliation mit Exchange-State
      (DB vs. Exchange → Abweichungen werden geloggt)
    - Paper und Live: komplett getrennte State-Instanzen

PnL-Berechnung:
    Unrealized PnL: (current_price - entry_price) * qty * side_factor
    Realized PnL:   (exit_price - entry_price) * qty * side_factor - fees
    Net PnL:        Realized PnL - Fees

Portfolio Value:
    Cash (USDT) + Σ(Position Notional Value) + Unrealized PnL
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sgr.core.logging import get_logger
from sgr.core.types import (
    OrderResult,
    OrderStatus,
    Position,
    PositionSide,
    Side,
    Symbol,
    TradingMode,
)

log = get_logger(__name__)


class PortfolioState:
    """
    In-Memory Portfolio State.
    Immutable nach außen – nur Portfolio Engine mutiert intern.
    """

    def __init__(
        self,
        trading_mode: TradingMode,
        initial_cash: Decimal = Decimal("10000"),
    ) -> None:
        self.trading_mode = trading_mode
        self._cash: Decimal = initial_cash
        self._positions: dict[str, Position] = {}  # key: symbol str
        self._peak_value: Decimal = initial_cash

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def positions(self) -> list[Position]:
        return list(self._positions.values())

    @property
    def portfolio_value(self) -> Decimal:
        """Cash + Summe aller Position Notional Values."""
        position_value = sum(p.notional_value for p in self._positions.values())
        return self._cash + position_value

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum(p.unrealized_pnl for p in self._positions.values())

    @property
    def peak_value(self) -> Decimal:
        return self._peak_value

    def update_peak(self) -> None:
        v = self.portfolio_value
        if v > self._peak_value:
            self._peak_value = v


class PortfolioEngine:
    """
    Portfolio Engine – verwaltet Positionen und PnL.

    Subscribed auf OrderFilledEvent vom Event Bus.
    Publiziert keine Events selbst (pull-basiert via REST API).
    """

    def __init__(
        self,
        trading_mode: TradingMode,
        initial_cash: Decimal = Decimal("10000"),
        position_repository: Any = None,
    ) -> None:
        self._trading_mode = trading_mode
        self._state = PortfolioState(trading_mode, initial_cash)
        self._trade_history: list[dict] = []  # Closed trades
        # Optional: PositionRepository fuer Crash-Recovery und Phase 7B
        # Reconciliation. None = rein in-memory (Tests, Backtesting).
        self._position_repo: Any = position_repository

    # ------------------------------------------------------------------
    # Event Handlers
    # ------------------------------------------------------------------

    async def on_order_filled(self, result: OrderResult) -> None:
        """
        Wird bei jedem gefüllten Order aufgerufen.
        Öffnet neue Position oder schließt/reduziert bestehende.
        """
        if result.status != OrderStatus.FILLED:
            return
        if result.average_fill_price is None or result.filled_quantity <= 0:
            log.warning(
                "portfolio.invalid_fill",
                order_id=str(result.request_id),
            )
            return

        symbol_key = str(result.symbol)

        # Ist eine Position für dieses Symbol bereits offen?
        existing = self._state._positions.get(symbol_key)

        if existing is None:
            # Neue Position öffnen
            await self._open_position(result, symbol_key)
        else:
            # Bestehende Position anpassen (reduce/close/flip)
            await self._update_position(existing, result, symbol_key)

        self._state.update_peak()

        log.info(
            "portfolio.position_updated",
            symbol=symbol_key,
            portfolio_value=str(self._state.portfolio_value),
            open_positions=len(self._state.positions),
        )

    async def _open_position(self, result: OrderResult, symbol_key: str) -> None:
        """Öffnet neue Position nach Fill."""
        side = PositionSide.LONG if self._infer_side(result) == Side.BUY else PositionSide.SHORT

        position = Position(
            symbol=result.symbol,
            side=side,
            quantity=result.filled_quantity,
            entry_price=result.average_fill_price,  # type: ignore[arg-type]
            current_price=result.average_fill_price,  # type: ignore[arg-type]
            opened_at=datetime.now(tz=UTC),
            strategy_name=str(result.raw_response.get("strategy", "unknown")),
            trading_mode=self._trading_mode,
        )

        self._state._positions[symbol_key] = position

        # Cash reduzieren
        notional = result.filled_quantity * result.average_fill_price  # type: ignore[operator]
        self._state._cash -= notional + result.fees

        await self._persist_position_upsert(position)

        log.info(
            "portfolio.position_opened",
            symbol=symbol_key,
            side=side.value,
            qty=str(result.filled_quantity),
            price=str(result.average_fill_price),
            fees=str(result.fees),
        )

    async def _update_position(
        self,
        existing: Position,
        result: OrderResult,
        symbol_key: str,
    ) -> None:
        """Aktualisiert oder schließt bestehende Position."""
        fill_side = self._infer_side(result)
        is_closing = (existing.side == PositionSide.LONG and fill_side == Side.SELL) or (
            existing.side == PositionSide.SHORT and fill_side == Side.BUY
        )

        if is_closing:
            # Position schließen / reduzieren
            fill_qty = result.filled_quantity
            close_qty = min(fill_qty, existing.quantity)

            # Realized PnL berechnen
            side_factor = Decimal("1") if existing.side == PositionSide.LONG else Decimal("-1")
            entry = existing.entry_price
            exit_price = result.average_fill_price  # type: ignore[assignment]
            realized = (exit_price - entry) * close_qty * side_factor - result.fees

            # Trade Record speichern
            self._record_trade(existing, result, close_qty, realized)

            if close_qty >= existing.quantity:
                # Vollständig geschlossen
                del self._state._positions[symbol_key]
                # Cash wieder erhöhen
                self._state._cash += exit_price * close_qty - result.fees

                await self._persist_position_close(existing.id, existing.realized_pnl + realized)

                log.info(
                    "portfolio.position_closed",
                    symbol=symbol_key,
                    realized_pnl=str(realized),
                    fees=str(result.fees),
                )
            else:
                # Teilweise geschlossen
                remaining_qty = existing.quantity - close_qty
                updated = Position(
                    id=existing.id,
                    symbol=existing.symbol,
                    side=existing.side,
                    quantity=remaining_qty,
                    entry_price=existing.entry_price,
                    current_price=exit_price,
                    opened_at=existing.opened_at,
                    strategy_name=existing.strategy_name,
                    trading_mode=existing.trading_mode,
                    realized_pnl=existing.realized_pnl + realized,
                )
                self._state._positions[symbol_key] = updated
                self._state._cash += exit_price * close_qty - result.fees

                await self._persist_position_upsert(updated)

    def _record_trade(
        self,
        position: Position,
        close_result: OrderResult,
        qty: Decimal,
        realized_pnl: Decimal,
    ) -> None:
        """Speichert geschlossenen Trade als immutable Record."""
        self._trade_history.append(
            {
                "id": str(uuid4()),
                "symbol": str(position.symbol),
                "side": position.side.value,
                "entry_price": str(position.entry_price),
                "exit_price": str(close_result.average_fill_price),
                "quantity": str(qty),
                "realized_pnl": str(realized_pnl),
                "fees": str(close_result.fees),
                "net_pnl": str(realized_pnl),
                "strategy": position.strategy_name,
                "opened_at": position.opened_at.isoformat(),
                "closed_at": datetime.now(tz=UTC).isoformat(),
                "trading_mode": self._trading_mode.value,
            }
        )

    # ------------------------------------------------------------------
    # Price Updates
    # ------------------------------------------------------------------

    def update_prices(self, prices: dict[str, Decimal]) -> None:
        """
        Aktualisiert aktuelle Preise für alle Positionen.
        Berechnet Unrealized PnL neu.
        Wird vom Market Data Engine periodisch aufgerufen.

        Args:
            prices: {"BTC/USDT": Decimal("50000"), ...}
        """
        for symbol_key, position in list(self._state._positions.items()):
            symbol_str = position.symbol.ccxt_symbol
            if symbol_str not in prices:
                continue

            new_price = prices[symbol_str]
            side_factor = Decimal("1") if position.side == PositionSide.LONG else Decimal("-1")
            unrealized = (new_price - position.entry_price) * position.quantity * side_factor

            updated = Position(
                id=position.id,
                symbol=position.symbol,
                side=position.side,
                quantity=position.quantity,
                entry_price=position.entry_price,
                current_price=new_price,
                leverage=position.leverage,
                unrealized_pnl=unrealized,
                realized_pnl=position.realized_pnl,
                opened_at=position.opened_at,
                strategy_name=position.strategy_name,
                trading_mode=position.trading_mode,
            )
            self._state._positions[symbol_key] = updated

        self._state.update_peak()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def portfolio_value(self) -> Decimal:
        return self._state.portfolio_value

    @property
    def cash(self) -> Decimal:
        return self._state.cash

    @property
    def positions(self) -> list[Position]:
        return self._state.positions

    @property
    def trade_history(self) -> list[dict]:
        return list(self._trade_history)

    def get_position(self, symbol: Symbol) -> Position | None:
        return self._state._positions.get(str(symbol))

    def summary(self) -> dict:
        """Portfolio Summary für Dashboard / API."""
        return {
            "portfolio_value": str(self._state.portfolio_value),
            "cash": str(self._state.cash),
            "unrealized_pnl": str(self._state.unrealized_pnl),
            "open_positions": len(self._state.positions),
            "total_trades": len(self._trade_history),
            "peak_value": str(self._state.peak_value),
            "drawdown": str(
                (self._state.peak_value - self._state.portfolio_value) / self._state.peak_value
                if self._state.peak_value > 0
                else Decimal("0")
            ),
            "trading_mode": self._trading_mode.value,
        }

    # ------------------------------------------------------------------
    # Persistence (Crash-Recovery, Phase 7B Reconciliation)
    # ------------------------------------------------------------------

    async def restore_from_persistence(self) -> int:
        """
        Laedt offene Positionen aus der DB in den in-memory State.
        Muss beim Startup aufgerufen werden, BEVOR Trading beginnt.

        Fail-Closed: Ein DB-Fehler wird NICHT geschluckt. Silent-Empty-Start
        waere gefaehrlicher als ein expliziter Crash, weil das System sonst
        mit leerem Portfolio-State startet, obwohl real offene Positionen
        existieren (doppeltes Hedging, falsche Risk-Berechnung, verwaiste
        Exchange-Positionen ohne lokale Kill-Switch-Kontrolle).

        Returns: Anzahl wiederhergestellter Positionen.

        Raises:
            RuntimeError: wenn kein PositionRepository injiziert wurde.
            Exception: jede DB-Exception wird weitergereicht (fail-closed).
        """
        if self._position_repo is None:
            raise RuntimeError(
                "restore_from_persistence() aufgerufen ohne injiziertes "
                "PositionRepository. Fail-closed: kein impliziter Empty-Start."
            )

        rows = await self._position_repo.get_open_positions(self._trading_mode)

        restored = 0
        for row in rows:
            position = self._position_from_row(row)
            symbol_key = str(position.symbol)
            self._state._positions[symbol_key] = position
            restored += 1

        self._state.update_peak()

        log.info(
            "portfolio.restored_from_persistence",
            trading_mode=self._trading_mode.value,
            restored_positions=restored,
        )
        return restored

    @staticmethod
    def _position_from_row(row: dict[str, Any]) -> Position:
        """Rekonstruiert Position (Domain) aus PositionRepository-Row (dict)."""
        from sgr.core.types import ExchangeID

        base, _, quote = row["symbol"].partition("/")
        symbol = Symbol(base=base, quote=quote, exchange=ExchangeID(row["exchange"]))

        return Position(
            id=row["id"],
            symbol=symbol,
            side=PositionSide(row["side"]),
            quantity=row["quantity"],
            entry_price=row["entry_price"],
            current_price=row["current_price"],
            leverage=row["leverage"],
            unrealized_pnl=row["unrealized_pnl"],
            realized_pnl=row["realized_pnl"],
            opened_at=row["opened_at"],
            strategy_name=row["strategy_name"],
            trading_mode=TradingMode(row["trading_mode"]),
        )

    async def _persist_position_upsert(self, position: Position) -> None:
        """
        Schreibt eine offene/aktualisierte Position in die DB.
        Best-effort: DB-Fehler dürfen den Trading-Betrieb NICHT blockieren
        (gleiches Fail-Safe-Muster wie KillSwitch._cancel_all_orders --
        Persistenz-Fehler werden geloggt, nicht propagiert).
        """
        if self._position_repo is None:
            return
        try:
            await self._position_repo.upsert_open(
                {
                    "id": str(position.id),
                    "symbol": position.symbol.ccxt_symbol,
                    "exchange": position.symbol.exchange.value,
                    "side": position.side.value,
                    "quantity": position.quantity,
                    "entry_price": position.entry_price,
                    "current_price": position.current_price,
                    "leverage": position.leverage,
                    "unrealized_pnl": position.unrealized_pnl,
                    "realized_pnl": position.realized_pnl,
                    "opened_at": position.opened_at,
                    "strategy_name": position.strategy_name,
                    "trading_mode": position.trading_mode.value,
                }
            )
        except Exception as e:
            log.error(
                "portfolio.persist_position_failed",
                symbol=str(position.symbol),
                error=str(e),
            )

    async def _persist_position_close(
        self, position_id: Any, realized_pnl: Decimal
    ) -> None:
        """Markiert eine Position in der DB als geschlossen. Best-effort."""
        if self._position_repo is None:
            return
        try:
            await self._position_repo.close(
                position_id=str(position_id),
                closed_at=datetime.now(tz=UTC),
                realized_pnl=realized_pnl,
            )
        except Exception as e:
            log.error(
                "portfolio.persist_close_failed",
                position_id=str(position_id),
                error=str(e),
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _infer_side(self, result: OrderResult) -> Side:
        """Inferiert Side aus Raw Response (CCXT liefert 'buy'/'sell')."""
        raw_side = result.raw_response.get("side", "")
        if isinstance(raw_side, str) and raw_side.lower() == "sell":
            return Side.SELL
        return Side.BUY
