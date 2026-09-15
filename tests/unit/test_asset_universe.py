"""
Tests fuer sgr.market_data.asset_universe.

Deckt ab:
    - classify_asset_status: die DISCOVERED -> SUPPORTED -> TRADABLE ->
      SUBSCRIBED -> ACTIVE Kaskade, inkl. jedes einzelnen Abbruchpunkts.
    - pionex_symbol_to_market_info: Symbol-Normalisierung ("_" -> "/"),
      defensives Verhalten bei fehlenden Feldern.
    - discover_pionex_markets/discover_binance_markets: Fail-Safe bei
      Fehlern (darf niemals werfen).
    - AssetUniverseEngine: Start/Stop-Lifecycle, dass Binance UND Pionex
      getrennt behandelt werden (siehe Task-Vorgabe "Exchange
      Unterschiede beruecksichtigen" - ein auf Pionex nicht verfuegbares
      Symbol darf nicht faelschlich als Pionex-Asset auftauchen).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from sgr.core.types import ExchangeID
from sgr.exchanges.base import MarketInfo
from sgr.market_data.asset_universe import (
    AssetStatus,
    AssetUniverseEngine,
    classify_asset_status,
    discover_binance_markets,
    discover_pionex_markets,
    pionex_symbol_to_market_info,
)


def _market(
    *,
    exchange: ExchangeID = ExchangeID.BINANCE,
    symbol: str = "BTC/USDT",
    base: str = "BTC",
    quote: str = "USDT",
    market_type: str = "swap",
    active: bool = True,
    amount_precision: int | None = 3,
    price_precision: int | None = 2,
    min_amount: Decimal | None = Decimal("0.001"),
) -> MarketInfo:
    return MarketInfo(
        exchange_id=exchange,
        symbol=symbol,
        base_asset=base,
        quote_asset=quote,
        market_type=market_type,
        active=active,
        discovered_at=datetime.now(tz=UTC),
        amount_precision=amount_precision,
        price_precision=price_precision,
        min_amount=min_amount,
    )


class TestClassifyAssetStatusCascade:
    def test_non_usdt_quote_stays_discovered(self) -> None:
        m = _market(quote="BTC")
        entry = classify_asset_status(
            m,
            expected_market_type="swap",
            execution_supported=True,
            subscribed_symbols={"BTC/USDT"},
            has_active_strategy=True,
        )
        assert entry.status == AssetStatus.DISCOVERED
        assert entry.reason == "quote_not_usdt"

    def test_inactive_market_stays_discovered(self) -> None:
        m = _market(active=False)
        entry = classify_asset_status(
            m,
            expected_market_type="swap",
            execution_supported=True,
            subscribed_symbols={"BTC/USDT"},
            has_active_strategy=True,
        )
        assert entry.status == AssetStatus.DISCOVERED
        assert entry.reason == "inactive_on_exchange"

    def test_wrong_market_type_stays_discovered(self) -> None:
        """Ein Spot-Markt darf nicht als SUPPORTED gelten, wenn SGR fuer
        diese Exchange nur Futures handelt (Binance) - Regressionsschutz
        gegen die live beobachtete Spot/Swap-Symbol-Kollision (beide
        werden zu 'BTC/USDT' normalisiert, nur market_type unterscheidet
        sie)."""
        m = _market(market_type="spot")
        entry = classify_asset_status(
            m,
            expected_market_type="swap",
            execution_supported=True,
            subscribed_symbols={"BTC/USDT"},
            has_active_strategy=True,
        )
        assert entry.status == AssetStatus.DISCOVERED
        assert "unexpected_market_type" in entry.reason

    def test_execution_not_supported_caps_at_supported(self) -> None:
        """Pionex-Kernfall: ccxt hat kein Pionex-Modul (siehe Modul-
        Docstring) - execution_supported=False MUSS TRADABLE verhindern,
        egal wie vollstaendig die uebrigen Marktdaten sind."""
        m = _market(exchange=ExchangeID.PIONEX, market_type="spot")
        entry = classify_asset_status(
            m,
            expected_market_type="spot",
            execution_supported=False,
            subscribed_symbols={"BTC/USDT"},
            has_active_strategy=True,
        )
        assert entry.status == AssetStatus.SUPPORTED
        assert entry.reason == "execution_not_supported"

    def test_missing_precision_caps_at_supported(self) -> None:
        m = _market(amount_precision=None)
        entry = classify_asset_status(
            m,
            expected_market_type="swap",
            execution_supported=True,
            subscribed_symbols={"BTC/USDT"},
            has_active_strategy=True,
        )
        assert entry.status == AssetStatus.SUPPORTED
        assert entry.reason == "missing_precision"

    def test_missing_min_amount_caps_at_supported(self) -> None:
        m = _market(min_amount=None)
        entry = classify_asset_status(
            m,
            expected_market_type="swap",
            execution_supported=True,
            subscribed_symbols={"BTC/USDT"},
            has_active_strategy=True,
        )
        assert entry.status == AssetStatus.SUPPORTED
        assert entry.reason == "missing_min_amount"

    def test_not_in_subscription_list_caps_at_tradable(self) -> None:
        """Ein neu entdecktes, technisch handelbares Asset darf NICHT
        automatisch Candle-Feeds/Trading bekommen (Task-Vorgabe 'Asset
        Discovery darf Paper Trading nicht gefaehrden')."""
        m = _market(symbol="NEWCOIN/USDT", base="NEWCOIN")
        entry = classify_asset_status(
            m,
            expected_market_type="swap",
            execution_supported=True,
            subscribed_symbols={"BTC/USDT"},  # NEWCOIN nicht enthalten
            has_active_strategy=True,
        )
        assert entry.status == AssetStatus.TRADABLE
        assert entry.reason == "not_subscribed"

    def test_no_active_strategy_caps_at_subscribed(self) -> None:
        m = _market()
        entry = classify_asset_status(
            m,
            expected_market_type="swap",
            execution_supported=True,
            subscribed_symbols={"BTC/USDT"},
            has_active_strategy=False,
        )
        assert entry.status == AssetStatus.SUBSCRIBED
        assert entry.reason == "no_active_strategy"

    def test_full_cascade_reaches_active(self) -> None:
        m = _market()
        entry = classify_asset_status(
            m,
            expected_market_type="swap",
            execution_supported=True,
            subscribed_symbols={"BTC/USDT"},
            has_active_strategy=True,
        )
        assert entry.status == AssetStatus.ACTIVE

    def test_existing_btc_eth_subscriptions_still_reach_active(self) -> None:
        """Regressionsschutz (Task-Vorgabe #10): die bestehenden BTC/USDT-
        und ETH/USDT-Subscriptions duerfen durch den neuen Discovery-
        Layer nicht kaputtgehen."""
        for symbol, base in (("BTC/USDT", "BTC"), ("ETH/USDT", "ETH")):
            m = _market(symbol=symbol, base=base)
            entry = classify_asset_status(
                m,
                expected_market_type="swap",
                execution_supported=True,
                subscribed_symbols={"BTC/USDT", "ETH/USDT"},
                has_active_strategy=True,
            )
            assert entry.status == AssetStatus.ACTIVE, symbol


class TestSymbolNormalization:
    def test_pionex_underscore_symbol_normalized_to_canonical_slash(self) -> None:
        raw = {
            "symbol": "AAVE_USDT",
            "baseCurrency": "AAVE",
            "quoteCurrency": "USDT",
            "amountPrecision": 8,
            "quotePrecision": 2,
            "minTradeSize": "0.001",
            "minAmount": "10",
            "enable": True,
        }
        info = pionex_symbol_to_market_info(raw, discovered_at=datetime.now(tz=UTC))
        assert info is not None
        assert info.symbol == "AAVE/USDT"
        assert "_" not in info.symbol
        assert info.base_asset == "AAVE"
        assert info.quote_asset == "USDT"
        assert info.market_type == "spot"
        assert info.exchange_id == ExchangeID.PIONEX

    def test_pionex_symbol_matches_binance_canonical_form_for_same_pair(self) -> None:
        """Task-Vorgabe #5: Grafanas $symbol muss dieselbe kanonische Form
        sehen wie die Prometheus-Labels, unabhaengig von der Exchange."""
        pionex = pionex_symbol_to_market_info(
            {
                "baseCurrency": "BTC",
                "quoteCurrency": "USDT",
                "enable": True,
            },
            discovered_at=datetime.now(tz=UTC),
        )
        binance = _market(symbol="BTC/USDT", base="BTC", quote="USDT")
        assert pionex is not None
        assert pionex.symbol == binance.symbol == "BTC/USDT"

    def test_missing_base_or_quote_returns_none(self) -> None:
        assert pionex_symbol_to_market_info({}, discovered_at=datetime.now(tz=UTC)) is None
        assert (
            pionex_symbol_to_market_info(
                {"baseCurrency": "BTC"}, discovered_at=datetime.now(tz=UTC)
            )
            is None
        )


class TestDiscoverPionexMarketsFailSafe:
    async def test_client_error_returns_empty_list_not_raises(self, monkeypatch) -> None:
        import sgr.exchanges.pionex_client as pionex_client_module

        class BrokenClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_symbols(self):
                raise ConnectionError("pionex unreachable")

        monkeypatch.setattr(pionex_client_module, "PionexClient", BrokenClient)

        markets = await discover_pionex_markets()

        assert markets == []

    async def test_real_shaped_response_produces_market_info_list(self, monkeypatch) -> None:
        import sgr.exchanges.pionex_client as pionex_client_module

        fake_symbols = [
            {
                "symbol": "BTC_USDT",
                "baseCurrency": "BTC",
                "quoteCurrency": "USDT",
                "amountPrecision": 6,
                "quotePrecision": 2,
                "minTradeSize": "0.0001",
                "minAmount": "10",
                "enable": True,
            },
            {
                "symbol": "JUNK_ENTRY_MISSING_FIELDS",
            },
        ]

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_symbols(self):
                return fake_symbols

        monkeypatch.setattr(pionex_client_module, "PionexClient", FakeClient)

        markets = await discover_pionex_markets()

        # Das kaputte zweite Symbol darf die uebrigen nicht blockieren.
        assert len(markets) == 1
        assert markets[0].symbol == "BTC/USDT"
        assert markets[0].exchange_id == ExchangeID.PIONEX


class TestDiscoverBinanceMarketsFailSafe:
    async def test_exchange_error_returns_empty_list_not_raises(self) -> None:
        from sgr.exchanges.base import ExchangeConnectionError

        adapter = MagicMock()
        adapter.discover_markets = AsyncMock(
            side_effect=ExchangeConnectionError("binance", "timeout")
        )

        markets = await discover_binance_markets(adapter)

        assert markets == []


class TestAssetUniverseEngine:
    async def test_run_once_classifies_both_exchanges_separately(self, monkeypatch) -> None:
        """Ein auf Binance handelbares Symbol darf nicht faelschlich als
        Pionex-tradable erscheinen und umgekehrt (Task-Vorgabe #4)."""
        binance_market = _market(symbol="XYZ/USDT", base="XYZ", exchange=ExchangeID.BINANCE)
        adapter = MagicMock()
        adapter.discover_markets = AsyncMock(return_value=[binance_market])

        pionex_market = MarketInfo(
            exchange_id=ExchangeID.PIONEX,
            symbol="XYZ/USDT",
            base_asset="XYZ",
            quote_asset="USDT",
            market_type="spot",
            active=True,
            discovered_at=datetime.now(tz=UTC),
        )

        import sgr.market_data.asset_universe as au_module

        monkeypatch.setattr(
            au_module, "discover_pionex_markets", AsyncMock(return_value=[pionex_market])
        )
        monkeypatch.setattr("sgr.monitoring.metrics.record_asset_universe_snapshot", MagicMock())

        engine = AssetUniverseEngine(
            binance_adapter=adapter,
            strategy_registry=None,
            subscribed_symbols={ExchangeID.BINANCE: {"XYZ/USDT"}},
        )

        await engine._run_once()

        snapshot = engine.last_snapshot
        binance_entry = next(e for e in snapshot if e.market.exchange_id == ExchangeID.BINANCE)
        pionex_entry = next(e for e in snapshot if e.market.exchange_id == ExchangeID.PIONEX)

        # Binance: subscribed (kein aktiver Strategy-Registry in diesem Test)
        assert binance_entry.status == AssetStatus.SUBSCRIBED
        # Pionex: XYZ/USDT nicht in der Pionex-Subscription-Liste (leer) UND
        # execution_supported=False - bleibt spaetestens bei SUPPORTED.
        assert pionex_entry.status == AssetStatus.SUPPORTED

    async def test_run_once_without_binance_adapter_still_runs_pionex(self, monkeypatch) -> None:
        import sgr.market_data.asset_universe as au_module

        monkeypatch.setattr(au_module, "discover_pionex_markets", AsyncMock(return_value=[]))
        monkeypatch.setattr("sgr.monitoring.metrics.record_asset_universe_snapshot", MagicMock())

        engine = AssetUniverseEngine(
            binance_adapter=None,
            strategy_registry=None,
            subscribed_symbols={},
        )

        await engine._run_once()  # muss nicht raisen

        assert engine.last_snapshot == []

    async def test_start_runs_immediately_and_stop_cancels_loop(self, monkeypatch) -> None:
        import sgr.market_data.asset_universe as au_module

        monkeypatch.setattr(au_module, "discover_pionex_markets", AsyncMock(return_value=[]))
        monkeypatch.setattr("sgr.monitoring.metrics.record_asset_universe_snapshot", MagicMock())

        engine = AssetUniverseEngine(
            binance_adapter=None,
            strategy_registry=None,
            subscribed_symbols={},
            discovery_interval_seconds=3600,
            republish_interval_seconds=3600,
        )

        await engine.start()
        try:
            # start() fuehrt _run_once() synchron VOR dem Hintergrund-Task
            # aus - direkt nach start() muss bereits ein Snapshot existieren.
            assert engine._task is not None
            assert not engine._task.done()
        finally:
            await engine.stop()

        assert engine._task is None

    async def test_republish_reemits_cached_snapshot_without_rediscovery(self, monkeypatch) -> None:
        """Live-Server-Fund (siehe Klassen-Docstring 'Wichtiger Fund'):
        OTel-Gauges verschwinden nach einem .set() bereits im naechsten
        Scrape-Zyklus wieder aus dem Export, wenn sie nicht erneut
        gesetzt werden. _republish() muss den zuletzt bekannten Snapshot
        erneut exportieren, OHNE eine neue (teure) Discovery auszuloesen."""
        market = _market(symbol="BTC/USDT")
        adapter = MagicMock()
        adapter.discover_markets = AsyncMock(return_value=[market])

        import sgr.market_data.asset_universe as au_module

        discover_pionex_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(au_module, "discover_pionex_markets", discover_pionex_mock)
        record_mock = MagicMock()
        monkeypatch.setattr("sgr.monitoring.metrics.record_asset_universe_snapshot", record_mock)

        engine = AssetUniverseEngine(
            binance_adapter=adapter,
            strategy_registry=None,
            subscribed_symbols={ExchangeID.BINANCE: {"BTC/USDT"}},
        )

        await engine._run_once()
        assert adapter.discover_markets.await_count == 1
        assert discover_pionex_mock.await_count == 1
        assert record_mock.call_count == 1

        engine._republish()

        # Kein weiterer Discovery-Call, aber die Gauge wurde erneut gesetzt.
        assert adapter.discover_markets.await_count == 1
        assert discover_pionex_mock.await_count == 1
        assert record_mock.call_count == 2
        assert record_mock.call_args_list[0] == record_mock.call_args_list[1]
