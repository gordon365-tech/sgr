"""
Tests fuer sgr.portfolio.engine.PortfolioEngine - Short-Position-Pfad.

Root-Cause-Fund (Post-Live-Verifikation, 2026-09-15): vor diesem Test
existierte KEIN dedizierter PortfolioEngine-Test - weder fuer den
Open-Pfad noch fuer den Short-Fall. Der Bug (jede Paper-SHORT-Position
wurde strukturell als PositionSide.LONG gespeichert, weil
_simulate_order() in sgr/exchanges/ccxt_base.py kein raw_response
lieferte und PortfolioEngine._infer_side() dadurch immer auf Side.BUY
zurueckfiel) blieb deshalb unbemerkt, bis er live am 2026-09-15
14:17-14:23 UTC beobachtet wurde (BTC/USDT, FET/USDT, u.a. -
"portfolio.position_opened" zeigte "side": "long" fuer einen
bestaetigten SELL/short-Trade). Der eigentliche Fix liegt in
sgr/exchanges/ccxt_base.py::_simulate_order() (raw_response wird jetzt
korrekt befuellt) - diese Tests decken den KONSUMIERENDEN Teil ab:
gegeben ein korrekt befuelltes OrderResult, muss PortfolioEngine einen
SELL-Fill tatsaechlich als PositionSide.SHORT speichern, mit korrekter
Cash-Buchung und korrektem (nicht invertiertem) PnL-Vorzeichen.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sgr.core.types import (
    ExchangeID,
    OrderResult,
    OrderStatus,
    PositionSide,
    Side,
    Symbol,
    TradingMode,
)
from sgr.portfolio.engine import PortfolioEngine


def _symbol() -> Symbol:
    return Symbol(base="BTC", quote="USDT", exchange=ExchangeID.BINANCE)


def _fill(
    side: Side, price: Decimal = Decimal("50000"), qty: Decimal = Decimal("1")
) -> OrderResult:
    now = datetime.now(tz=UTC)
    return OrderResult(
        request_id=uuid4(),
        exchange_order_id=f"PAPER-{uuid4()}",
        symbol=_symbol(),
        status=OrderStatus.FILLED,
        filled_quantity=qty,
        average_fill_price=price,
        fees=Decimal("5"),
        fee_currency="USDT",
        submitted_at=now,
        filled_at=now,
        trading_mode=TradingMode.PAPER,
        # Genau das Feld, das _simulate_order() vor dem Fix NIE gesetzt
        # hat - siehe Modul-Docstring.
        raw_response={"side": side.value},
    )


class TestShortPositionOpen:
    async def test_sell_fill_opens_a_short_position_not_long(self) -> None:
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("10000"))

        await engine.on_order_filled(_fill(Side.SELL))

        position = engine._state._positions[str(_symbol())]
        assert position.side == PositionSide.SHORT

    async def test_buy_fill_still_opens_a_long_position(self) -> None:
        """Regressionsschutz in die andere Richtung - der Fix darf BUY
        nicht kaputt machen."""
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("10000"))

        await engine.on_order_filled(_fill(Side.BUY))

        position = engine._state._positions[str(_symbol())]
        assert position.side == PositionSide.LONG

    async def test_short_open_credits_cash_not_debits(self) -> None:
        """SHORT: Verkaufserloes wird beim Open gutgeschrieben (Cash
        steigt abzueglich Fee) - nicht wie bei LONG abgebucht."""
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("10000"))
        fill = _fill(Side.SELL, price=Decimal("50000"), qty=Decimal("1"))

        await engine.on_order_filled(fill)

        expected_cash = Decimal("10000") + Decimal("50000") - Decimal("5")
        assert engine._state.cash == expected_cash

    async def test_short_position_pnl_sign_is_not_inverted(self) -> None:
        """Fuer eine SHORT-Position muss ein fallender Preis einen
        POSITIVEN unrealized PnL ergeben (Gewinn) - mit dem Bug war das
        Vorzeichen invertiert (side_factor immer +1, weil position.side
        faelschlich LONG war)."""
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("10000"))
        await engine.on_order_filled(_fill(Side.SELL, price=Decimal("50000")))

        # Preis faellt -> Short-Position sollte im Gewinn sein.
        engine.update_prices({"BTC/USDT": Decimal("48000")})

        position = engine._state._positions[str(_symbol())]
        assert position.side == PositionSide.SHORT
        assert position.unrealized_pnl > 0

    async def test_portfolio_value_reflects_short_as_liability(self) -> None:
        """PortfolioState.portfolio_value zieht den Marktwert einer
        SHORT-Position ab (Rueckkaufverbindlichkeit) statt ihn zu
        addieren wie bei LONG - siehe PortfolioState.portfolio_value
        Docstring."""
        engine = PortfolioEngine(TradingMode.PAPER, initial_cash=Decimal("10000"))
        await engine.on_order_filled(_fill(Side.SELL, price=Decimal("50000"), qty=Decimal("1")))

        # Direkt nach Open: cash=10000+50000-5=10049995... value sollte
        # cash - notional(=50000, current_price=entry_price) sein.
        expected_value = engine._state.cash - Decimal("50000")
        assert engine._state.portfolio_value == expected_value
