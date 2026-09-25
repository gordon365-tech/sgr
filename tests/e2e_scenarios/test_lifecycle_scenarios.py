"""
SGR E2E Lifecycle Scenarios - Phase 1
========================================
Drei Kern-Szenarien ueber den echten Produktionspfad (siehe conftest.py
fuer die vollstaendige Verdrahtungs- und Isolations-Begruendung):

    Long Entry, Short Entry, Take-Profit-Exit

Jedes Szenario ist im Testkoerper explizit als
SETUP -> ACTION -> EXPECTED -> ACTUAL -> PASS/FAIL strukturiert (siehe
Docstring/Kommentare je Test) statt eines separaten Reporting-Layers -
pytest liefert PASS/FAIL bereits nativ ueber Exit-Code + `-v`-Output;
ein zusaetzliches Text-Report-Format wuerde denselben Zustand doppelt
fuehren.

Ausfuehrung (isoliert, gegen den bereits gefixten Source-Stand, ohne
Gordon/Sumo zu beruehren):
    docker run --rm -v /home/ubuntu/sgr:/app -w /app \\
        --env-file .env.prod \\
        sgr-worker:latest \\
        sh -c "pip install --quiet pytest pytest-asyncio && \\
               python3 -m pytest tests/e2e_scenarios/ -v -o addopts=''"
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sgr.core.types import (
    MarketRegime,
    OrderStatus,
    PositionSide,
    RiskDecision,
    SignalDirection,
    TradingCycleStatus,
)
from sgr.market_data.feature_store import FeatureStore
from sgr.orchestrator.engine import TradingOrchestrator
from sgr.portfolio.engine import PortfolioEngine
from sgr.risk.position_protection import PositionProtectionWatchdog

from .conftest import E2E_SYMBOL, E2E_SYMBOL_KEY, E2E_TIMEFRAME, build_feature_set

pytestmark = pytest.mark.e2e_scenario


@pytest.mark.asyncio
async def test_long_entry(
    orchestrator: TradingOrchestrator,
    feature_store: FeatureStore,
    portfolio_engine: PortfolioEngine,
) -> None:
    """
    SETUP: echte trend_following_v1-Strategie, echte Risk/Execution/
        Portfolio-Engines, echter BinanceAdapter (Futures-Testnet,
        futures_mode=True). Features fuer HFT/USDT (futures-only
        gelistet) mit starken Long-Indikatoren (RSI 68, ADX 32,
        EMA9>EMA21>EMA50, DI+ > DI-).
    ACTION: orchestrator.run_cycle(regime=TRENDING_UP) - durchlaeuft
        Signal -> Risk -> Order -> echter Binance-Testnet-Fill ->
        Position.
    """
    await feature_store.save(build_feature_set(close=Decimal("0.0060"), direction="up"))

    result = await orchestrator.run_cycle(
        symbol_key=E2E_SYMBOL_KEY, timeframe=E2E_TIMEFRAME, regime=MarketRegime.TRENDING_UP
    )

    # EXPECTED
    expected = {
        "cycle_status": TradingCycleStatus.ORDER_FILLED,
        "signal_direction": SignalDirection.LONG,
        "risk_decision_in": (RiskDecision.APPROVED, RiskDecision.REDUCED),
        "order_status": OrderStatus.FILLED,
        "position_count": 1,
        "position_side": PositionSide.LONG,
    }

    # ACTUAL
    actual = {
        "cycle_status": result.status,
        "signal_direction": result.signal.direction if result.signal else None,
        "risk_decision": result.assessment.decision if result.assessment else None,
        "order_status": result.order_result.status if result.order_result else None,
        "position_count": len(portfolio_engine.positions),
    }

    # PASS/FAIL
    assert actual["cycle_status"] == expected["cycle_status"], actual
    assert actual["signal_direction"] == expected["signal_direction"], actual
    assert actual["risk_decision"] in expected["risk_decision_in"], actual
    assert actual["order_status"] == expected["order_status"], actual
    assert actual["position_count"] == expected["position_count"], actual

    position = portfolio_engine.positions[0]
    assert position.symbol == E2E_SYMBOL
    assert position.side == expected["position_side"]
    assert position.quantity > 0
    assert position.entry_price > 0
    assert position.strategy_name == "trend_following_v1"
    # Position-Protection-Hook (PositionProtectionManager.on_position_opened,
    # via PortfolioEngine on_position_opened-Kwarg) muss SL/TP/Max-Holding
    # angehaengt haben - RISK_PROTECTION_CUTOVER_AT liegt in der
    # Vergangenheit (siehe conftest.py).
    assert position.stop_loss_price is not None
    assert position.take_profit_price is not None
    assert position.stop_loss_price < position.entry_price < position.take_profit_price
    assert position.max_holding_until is not None


@pytest.mark.asyncio
async def test_short_entry(
    orchestrator: TradingOrchestrator,
    feature_store: FeatureStore,
    portfolio_engine: PortfolioEngine,
) -> None:
    """
    SETUP: identisch zu test_long_entry, Indikatoren gespiegelt fuer
        Short (RSI 32, DI- > DI+, EMA9<EMA21<EMA50).
    ACTION: orchestrator.run_cycle(regime=TRENDING_DOWN).
    """
    await feature_store.save(build_feature_set(close=Decimal("0.0052"), direction="down"))

    result = await orchestrator.run_cycle(
        symbol_key=E2E_SYMBOL_KEY, timeframe=E2E_TIMEFRAME, regime=MarketRegime.TRENDING_DOWN
    )

    # EXPECTED vs ACTUAL vs PASS/FAIL
    assert result.status == TradingCycleStatus.ORDER_FILLED, result
    assert result.signal is not None and result.signal.direction == SignalDirection.SHORT, result
    assert result.assessment is not None and result.assessment.decision in (
        RiskDecision.APPROVED,
        RiskDecision.REDUCED,
    ), result
    assert result.order_result is not None and result.order_result.status == OrderStatus.FILLED, (
        result
    )
    assert len(portfolio_engine.positions) == 1

    position = portfolio_engine.positions[0]
    assert position.side == PositionSide.SHORT
    assert position.quantity > 0
    assert position.entry_price > 0
    assert position.stop_loss_price is not None and position.stop_loss_price > position.entry_price
    assert (
        position.take_profit_price is not None and position.take_profit_price < position.entry_price
    )


@pytest.mark.asyncio
async def test_take_profit_exit(
    orchestrator: TradingOrchestrator,
    feature_store: FeatureStore,
    portfolio_engine: PortfolioEngine,
    watchdog: PositionProtectionWatchdog,
) -> None:
    """
    SETUP: wie test_long_entry - oeffnet zunaechst eine echte LONG-
        Position mit angehaengtem Take-Profit.
    ACTION 1: PortfolioEngine.update_prices() hebt current_price
        synthetisch ueber die TP-Schwelle (identischer Mechanismus wie
        ein echter Candle-Preis-Tick, siehe
        TradingOrchestrator.on_candle_event()) - simuliert einen
        guenstigen Marktbewegung, OHNE 30+ Minuten auf einen echten
        Preis-Move am Testnet zu warten.
    ACTION 2: watchdog.check_positions_once() - derselbe Code, den der
        produktive PositionProtectionWatchdog periodisch (alle 60s)
        aufruft (siehe dortigen Docstring: "erlaubt Tests ... ohne den
        Sleep-Loop nachzubilden"). Loest bei Ueberschreitung einen
        ECHTEN reduce-only Market-Exit gegen das Testnet aus.
    """
    await feature_store.save(build_feature_set(close=Decimal("0.0060"), direction="up"))
    entry_result = await orchestrator.run_cycle(
        symbol_key=E2E_SYMBOL_KEY, timeframe=E2E_TIMEFRAME, regime=MarketRegime.TRENDING_UP
    )
    assert entry_result.status == TradingCycleStatus.ORDER_FILLED, entry_result
    assert len(portfolio_engine.positions) == 1
    position = portfolio_engine.positions[0]
    take_profit_price = position.take_profit_price
    assert take_profit_price is not None

    # ACTION: Preis ueber die TP-Schwelle heben, dann Watchdog-Tick.
    triggering_price = take_profit_price * Decimal("1.01")
    await portfolio_engine.update_prices({E2E_SYMBOL.ccxt_symbol: triggering_price})
    await watchdog.check_positions_once()

    # EXPECTED vs ACTUAL vs PASS/FAIL
    assert len(portfolio_engine.positions) == 0, (
        "Position sollte nach TP-Trigger vollstaendig geschlossen sein",
        portfolio_engine.positions,
    )

    trades = portfolio_engine.trade_history
    assert len(trades) == 1, trades
    trade = trades[0]
    assert trade["close_reason"] == "take_profit", trade
    assert trade["symbol"] == str(E2E_SYMBOL), trade
    assert Decimal(trade["quantity"]) == position.quantity, trade

    # Realized PnL wird NICHT auf > 0 geprueft: der Exit-Fill kommt aus
    # einem echten get_ticker()-Call gegen das Testnet zum tatsaechlichen
    # Ausfuehrungszeitpunkt (siehe position_protection.py
    # _close_position()) - unabhaengig vom synthetisch angehobenen
    # current_price oben, der nur die Trigger-SCHWELLE auslöst, nicht den
    # tatsaechlichen Fuellpreis bestimmt. Ein echter Testnet-Preis kann
    # sich zwischen Entry und Exit in Millisekunden auch ungünstig bewegt
    # haben (hier live beobachtet: -0.0373 PnL trotz Take-Profit-Trigger,
    # weil der reale Preis in der Zwischenzeit leicht gefallen war) - das
    # ist korrektes, ehrliches Verhalten (kein Fake-Fill), keine
    # SGR-Fehlfunktion. Sanity-Check stattdessen: die Rechnung selbst
    # muss exakt (Exit-Preis - Entry-Preis) * Menge - Gebuehren ergeben.
    exit_price = Decimal(trade["exit_price"])
    entry_price = Decimal(trade["entry_price"])
    quantity = Decimal(trade["quantity"])
    fees = Decimal(trade["fees"])
    expected_pnl = (exit_price - entry_price) * quantity - fees
    actual_pnl = Decimal(trade["realized_pnl"])
    assert abs(actual_pnl - expected_pnl) < Decimal("0.0001"), (
        expected_pnl,
        actual_pnl,
        trade,
    )
