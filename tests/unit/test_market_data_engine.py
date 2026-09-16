"""
Tests fuer sgr.market_data.engine.MarketDataEngine.reconcile_subscriptions().

Deckt den neuen dynamischen Subscription-Pfad ab (Autonomous-Paper-
Trading-Rollout, Task-Vorgabe "wenn sich das Asset Universe aendert,
muss die Strategieauswertung diese Aenderung automatisch uebernehmen" /
"entfernte Symbole duerfen nicht weiter gehandelt werden"). subscribe()/
start()/stop() selbst sind bereits indirekt ueber die bestehende
Produktionsnutzung (sgr/api/main.py) abgedeckt - hier bewusst nur der
neue Reconciliation-Pfad.

SymbolFeed.initialize() wird gemockt (kein echter DB-/Exchange-Zugriff
noetig fuer diese Tests - reines Feed-Buchhaltungsverhalten von
MarketDataEngine steht im Fokus).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from sgr.core.types import ExchangeID, TradingMode
from sgr.market_data.engine import MarketDataEngine, SymbolFeed


def _engine(monkeypatch) -> MarketDataEngine:
    monkeypatch.setattr(SymbolFeed, "initialize", AsyncMock(return_value=None))
    pool = MagicMock()
    feature_store = MagicMock()
    feature_store._redis = MagicMock()  # umgeht den connect()-Zweig in start()
    feature_store.close = AsyncMock(return_value=None)
    return MarketDataEngine(pool, TradingMode.PAPER, feature_store=feature_store)


class TestReconcileSubscriptionsBeforeStart:
    async def test_defers_to_subscribe_when_not_running(self, monkeypatch) -> None:
        engine = _engine(monkeypatch)

        await engine.reconcile_subscriptions(
            {("BTC/USDT", "1h"), ("ETH/USDT", "1h")}, ExchangeID.BINANCE
        )

        assert set(engine._feeds.keys()) == {"BTC/USDT:1h", "ETH/USDT:1h"}
        # Noch keine Tasks - start() uebernimmt das spaeter.
        assert engine._tasks == {}


class TestReconcileSubscriptionsAfterStart:
    async def test_adds_new_feeds_and_starts_their_poll_loop(self, monkeypatch) -> None:
        engine = _engine(monkeypatch)
        await engine.start()
        try:
            assert engine._feeds == {}

            await engine.reconcile_subscriptions({("BTC/USDT", "1h")}, ExchangeID.BINANCE)

            assert "BTC/USDT:1h" in engine._feeds
            assert "BTC/USDT:1h" in engine._tasks
            assert not engine._tasks["BTC/USDT:1h"].done()
        finally:
            await engine.stop()

    async def test_removes_feeds_no_longer_in_target(self, monkeypatch) -> None:
        engine = _engine(monkeypatch)
        await engine.start()
        try:
            await engine.reconcile_subscriptions(
                {("BTC/USDT", "1h"), ("ETH/USDT", "1h")}, ExchangeID.BINANCE
            )
            assert set(engine._feeds.keys()) == {"BTC/USDT:1h", "ETH/USDT:1h"}

            # ETH/USDT ist nicht mehr TRADABLE -> aus dem Zielset entfernt.
            await engine.reconcile_subscriptions({("BTC/USDT", "1h")}, ExchangeID.BINANCE)

            assert set(engine._feeds.keys()) == {"BTC/USDT:1h"}
            assert "ETH/USDT:1h" not in engine._tasks
        finally:
            await engine.stop()

    async def test_unchanged_feed_is_left_alone(self, monkeypatch) -> None:
        """Ein Feed, der schon existiert UND weiterhin im Zielset ist,
        darf nicht neu erzeugt/neu initialisiert werden (kein
        unnoetiger erneuter History-Fetch bei jedem Discovery-Zyklus)."""
        engine = _engine(monkeypatch)
        await engine.start()
        try:
            await engine.reconcile_subscriptions({("BTC/USDT", "1h")}, ExchangeID.BINANCE)
            original_feed = engine._feeds["BTC/USDT:1h"]
            original_task = engine._tasks["BTC/USDT:1h"]

            await engine.reconcile_subscriptions({("BTC/USDT", "1h")}, ExchangeID.BINANCE)

            assert engine._feeds["BTC/USDT:1h"] is original_feed
            assert engine._tasks["BTC/USDT:1h"] is original_task
        finally:
            await engine.stop()

    async def test_empty_target_removes_all_feeds(self, monkeypatch) -> None:
        engine = _engine(monkeypatch)
        await engine.start()
        try:
            await engine.reconcile_subscriptions({("BTC/USDT", "1h")}, ExchangeID.BINANCE)
            assert engine._feeds

            await engine.reconcile_subscriptions(set(), ExchangeID.BINANCE)

            assert engine._feeds == {}
            assert engine._tasks == {}
        finally:
            await engine.stop()
