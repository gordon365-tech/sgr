"""
Tests für sgr.exchanges.pionex.PionexAdapter.

Teststrategie (analog tests/exchanges/test_ccxt_base.py): PionexAdapter
erbt von CCXTBaseAdapter und überschreibt nur connect() (Paper-Mode-
Sonderfall: öffentliche Marktdaten via CCXT, aber keine Order-Ausführung)
sowie from_config(). Wir patchen ccxt.async_support.pionex mit einem
FakeCCXTExchange, um die echten Adapter-Codepfade ohne Netzwerkzugriff
zu testen.

Abdeckung:
    - __init__: exchange_id/_ccxt_id/_testnet_urls, extra_options-Merge
    - connect() Paper Mode: happy path, ccxt-not-installed, load_markets-
      Fehler (inkl. sauberes close() und Fehler-in-close()-Handling)
    - connect() Live Mode: delegiert an CCXTBaseAdapter.connect()
    - from_config(): Paper Mode mit/ohne konfigurierte Dummy-Credentials,
      Live Mode mit vollständigen Credentials
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from sgr.core.config import ExchangeCredentials
from sgr.core.types import AssetClass, ExchangeID, OrderStatus, TradingMode
from sgr.exchanges.pionex import PionexAdapter

# ---------------------------------------------------------------------
# Fake CCXT double (gleiches Muster wie test_ccxt_base.py)
# ---------------------------------------------------------------------


class FakeCCXTExchange:
    """Minimaler Stand-in für eine ccxt.async_support Exchange-Instanz."""

    def __init__(self, options: dict | None = None) -> None:
        self.options = options or {}
        self.symbols = ["BTC/USDT", "ETH/USDT"]
        self.load_markets = AsyncMock(return_value={})
        self.close = AsyncMock()


def install_fake_ccxt(monkeypatch, fake_instance: FakeCCXTExchange | None = None):
    """Patcht ccxt.async_support.pionex mit einer Factory für FakeCCXTExchange."""
    import ccxt.async_support as ccxt_async

    holder = {"instance": fake_instance}

    def factory(options=None):
        inst = holder["instance"] or FakeCCXTExchange(options)
        holder["instance"] = inst
        return inst

    monkeypatch.setattr(ccxt_async, "pionex", factory, raising=False)
    return holder


@pytest.fixture
def paper_adapter() -> PionexAdapter:
    return PionexAdapter(api_key="paper_key", secret="paper_secret", trading_mode=TradingMode.PAPER)


@pytest.fixture
def live_adapter() -> PionexAdapter:
    return PionexAdapter(api_key="live_key", secret="live_secret", trading_mode=TradingMode.LIVE)


# ---------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------


class TestInit:
    def test_sets_exchange_id_and_ccxt_id(self, paper_adapter: PionexAdapter) -> None:
        assert paper_adapter.exchange_id == ExchangeID.PIONEX
        assert paper_adapter._ccxt_id == "pionex"

    def test_no_testnet_urls(self, paper_adapter: PionexAdapter) -> None:
        """Pionex besitzt kein dediziertes Testnet."""
        assert paper_adapter._testnet_urls == {}

    def test_merges_adjust_for_time_difference_option(self, paper_adapter: PionexAdapter) -> None:
        assert paper_adapter._extra_options == {
            "options": {"adjustForTimeDifference": True},
        }

    def test_stores_credentials_and_mode(self, live_adapter: PionexAdapter) -> None:
        assert live_adapter._api_key == "live_key"
        assert live_adapter._secret == "live_secret"
        assert live_adapter.trading_mode == TradingMode.LIVE

    def test_not_connected_initially(self, paper_adapter: PionexAdapter) -> None:
        assert paper_adapter._connected is False
        assert paper_adapter._ccxt is None


# ---------------------------------------------------------------------
# connect() - Paper Mode
# ---------------------------------------------------------------------


class TestConnectPaperMode:
    async def test_connect_success_loads_public_market_data(
        self, paper_adapter: PionexAdapter, monkeypatch
    ) -> None:
        holder = install_fake_ccxt(monkeypatch)

        await paper_adapter.connect()

        assert paper_adapter._connected is True
        assert paper_adapter._ccxt is holder["instance"]
        holder["instance"].load_markets.assert_awaited_once()

    async def test_connect_merges_extra_options_into_ccxt_instance(
        self, paper_adapter: PionexAdapter, monkeypatch
    ) -> None:
        holder = install_fake_ccxt(monkeypatch)

        await paper_adapter.connect()

        assert holder["instance"].options["options"] == {"adjustForTimeDifference": True}
        assert holder["instance"].options["apiKey"] == "paper_key"
        assert holder["instance"].options["secret"] == "paper_secret"

    async def test_connect_ccxt_not_installed_raises_runtime_error(
        self, paper_adapter: PionexAdapter, monkeypatch
    ) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "ccxt.async_support":
                raise ImportError("no ccxt")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with pytest.raises(RuntimeError, match="ccxt not installed"):
            await paper_adapter.connect()

    async def test_connect_load_markets_failure_cleans_up_and_reraises(
        self, paper_adapter: PionexAdapter, monkeypatch
    ) -> None:
        fake = FakeCCXTExchange()
        fake.load_markets = AsyncMock(side_effect=RuntimeError("network down"))
        install_fake_ccxt(monkeypatch, fake)

        with pytest.raises(RuntimeError, match="network down"):
            await paper_adapter.connect()

        assert paper_adapter._ccxt is None
        assert paper_adapter._connected is False
        fake.close.assert_awaited_once()

    async def test_connect_close_failure_during_cleanup_is_swallowed(
        self, paper_adapter: PionexAdapter, monkeypatch
    ) -> None:
        """Wenn sowohl load_markets als auch das Cleanup-close() fehlschlagen,
        muss der ursprüngliche load_markets-Fehler weiterhin propagiert werden."""
        fake = FakeCCXTExchange()
        fake.load_markets = AsyncMock(side_effect=RuntimeError("network down"))
        fake.close = AsyncMock(side_effect=RuntimeError("close also broken"))
        install_fake_ccxt(monkeypatch, fake)

        with pytest.raises(RuntimeError, match="network down"):
            await paper_adapter.connect()

        assert paper_adapter._ccxt is None
        assert paper_adapter._connected is False


# ---------------------------------------------------------------------
# connect() - Live Mode
# ---------------------------------------------------------------------


class TestConnectLiveMode:
    async def test_connect_delegates_to_base_class_when_ccxt_supports_pionex(
        self, live_adapter: PionexAdapter, monkeypatch
    ) -> None:
        """Falls eine (ggf. zukuenftige) ccxt-Version wieder eine
        'pionex'-Exchange-ID registriert, bleibt die bisherige Delegation
        an CCXTBaseAdapter.connect() fuer LIVE Mode unveraendert."""
        install_fake_ccxt(monkeypatch)
        base_connect = AsyncMock()
        monkeypatch.setattr("sgr.exchanges.ccxt_base.CCXTBaseAdapter.connect", base_connect)

        await live_adapter.connect()

        base_connect.assert_awaited_once()

    async def test_connect_without_ccxt_falls_back_to_native_client(
        self, live_adapter: PionexAdapter, monkeypatch
    ) -> None:
        """
        Seit der Private-API-Integration (read-only) verbindet sich LIVE
        auch OHNE ccxt-Pionex-ID erfolgreich - ueber den nativen,
        signierten Fallback (siehe TestNativeFallbackLive fuer die
        vollstaendige Abdeckung: Authentication/Connectivity/Balance/
        Positions/Orders/Funding). Diese Sperre ist absichtlich
        verschoben worden: verbinden (=lesen) ist jetzt erlaubt, SENDEN
        bleibt blockiert (siehe test_place_order_blocked_in_live_native_
        fallback in TestNativeFallbackLive)."""
        import ccxt.async_support as ccxt_async

        monkeypatch.delattr(ccxt_async, "pionex", raising=False)
        monkeypatch.setattr("sgr.exchanges.pionex_client.PionexClient", FakePionexClient)

        await live_adapter.connect()

        assert live_adapter._connected is True
        assert live_adapter._native_fallback is True


# ---------------------------------------------------------------------
# from_config()
# ---------------------------------------------------------------------


class TestFromConfig:
    # ExchangeCredentials(_env_file=None) statt des bloßen
    # ExchangeCredentials(): seit dem Bugfix fuer die Pionex Live-Read-
    # Only-Verification liest ExchangeCredentials auch aus einer .env-
    # Datei im Arbeitsverzeichnis (siehe sgr/core/config.py) - Tests, die
    # bewusst "gar keine Credentials konfiguriert" simulieren, muessen
    # diesen Fallback deterministisch deaktivieren.
    def _fake_config(self, credentials: ExchangeCredentials) -> MagicMock:
        config = MagicMock()
        config.credentials = credentials
        return config

    def test_paper_mode_uses_configured_dummy_credentials(self, monkeypatch) -> None:
        creds = ExchangeCredentials(
            pionex_paper_api_key=SecretStr("configured_paper_key"),
            pionex_paper_secret=SecretStr("configured_paper_secret"),
        )
        monkeypatch.setattr("sgr.core.config.get_config", lambda: self._fake_config(creds))

        adapter = PionexAdapter.from_config(TradingMode.PAPER)

        assert adapter._api_key == "configured_paper_key"
        assert adapter._secret == "configured_paper_secret"
        assert adapter.trading_mode == TradingMode.PAPER

    def test_paper_mode_falls_back_to_placeholder_credentials(self, monkeypatch) -> None:
        """Ohne konfigurierte Paper-Credentials werden Platzhalter verwendet,
        da die CCXT-Instanz Parameter erwartet, aber Paper Mode keine echte
        Authentifizierung benötigt."""
        creds = ExchangeCredentials(_env_file=None)
        monkeypatch.setattr("sgr.core.config.get_config", lambda: self._fake_config(creds))

        adapter = PionexAdapter.from_config(TradingMode.PAPER)

        assert adapter._api_key == "paper_key"
        assert adapter._secret == "paper_secret"

    def test_live_mode_uses_real_credentials(self, monkeypatch) -> None:
        creds = ExchangeCredentials(
            pionex_live_api_key=SecretStr("real_key"),
            pionex_live_secret=SecretStr("real_secret"),
        )
        monkeypatch.setattr("sgr.core.config.get_config", lambda: self._fake_config(creds))

        adapter = PionexAdapter.from_config(TradingMode.LIVE)

        assert adapter._api_key == "real_key"
        assert adapter._secret == "real_secret"
        assert adapter.trading_mode == TradingMode.LIVE

    def test_live_mode_without_credentials_raises(self, monkeypatch) -> None:
        creds = ExchangeCredentials(_env_file=None)
        monkeypatch.setattr("sgr.core.config.get_config", lambda: self._fake_config(creds))

        with pytest.raises(ValueError, match="Credentials not configured"):
            PionexAdapter.from_config(TradingMode.LIVE)


# ---------------------------------------------------------------------
# Native fallback (ccxt hat keine "pionex"-ID, siehe Modul-Docstring)
# ---------------------------------------------------------------------


class FakePionexClient:
    """Minimaler synchroner Stand-in fuer PionexClient (native Fallback).

    Deckt sowohl die oeffentlichen Methoden (unveraendert seit dem
    Futures-Grid-Baustein) als auch die neuen privaten (signierten)
    Methoden ab - Letztere werden nur von den LIVE-Native-Fallback-Tests
    in TestNativeFallbackLive tatsaechlich aufgerufen.
    """

    KLINE_INTERVALS = {"1h": "60M"}

    def __init__(self, api_key=None, api_secret=None) -> None:
        self.closed = False
        self.api_key = api_key
        self.api_secret = api_secret

    def get_symbols(self, symbols=None, market_type=None, status=None):
        return [
            {
                "symbol": "BTC_USDT",
                "baseCurrency": "BTC",
                "quoteCurrency": "USDT",
                "basePrecision": 6,
                "quotePrecision": 2,
                "minAmount": "0.0001",
                "minTradeAmount": "10",
            }
        ]

    def get_ticker(self, symbol):
        return {
            "symbol": symbol,
            "time": 1786237722477,
            "open": "60000",
            "close": "61000",
            "high": "62000",
            "low": "59000",
            "volume": "100",
        }

    def get_book_tickers(self, symbol, market_type=None):
        return [{"symbol": symbol, "bidPrice": "60990", "askPrice": "61010"}]

    def get_orderbook(self, symbol, limit=20):
        return {"bids": [["60990", "1.0"]], "asks": [["61010", "1.0"]]}

    def get_ohlcv(self, symbol, interval, limit=100, end_time=None):
        return [
            {
                "time": 1786237680000,
                "open": "60000",
                "close": "61000",
                "high": "62000",
                "low": "59000",
                "volume": "10",
            }
        ]

    def close(self):
        self.closed = True

    # --- Private (signed) methods - only reached in LIVE mode ---

    def get_account_balances(self):
        return [
            {"coin": "USDT", "free": "1000.00000000", "frozen": "50.00000000"},
            {"coin": "BTC", "free": "0.10000000", "frozen": "0.00000000"},
        ]

    def get_futures_account_balances(self):
        return {
            "balances": [
                {"coin": "USDT", "free": "5000.00000000", "frozen": "200.00000000", "debts": "0"}
            ],
            "isolates": [],
        }

    def get_futures_positions(self, symbol=None):
        return [
            {
                "positionId": "pos-1",
                "symbol": "BTC_USDT_PERP",
                "isolatedMode": "CROSS",
                "positionSide": "LONG",
                "netSize": "0.05",
                "avgPrice": "60000",
                "unrealizedPnL": "50.00",
                "markPrice": "61000",
                "leverage": "3",
                "createTime": 1786237680000,
                "updateTime": 1786237680000,
            }
        ]

    def get_futures_leverage(self, symbol):
        return {"symbol": symbol, "leverage": "3"}

    def get_futures_margin_mode(self, symbol):
        return {"symbol": symbol, "isolatedMode": "CROSS"}

    def get_futures_open_orders(self, symbol=None, end_time=None, limit=100):
        return [
            {
                "orderId": 987654321,
                "symbol": symbol or "BTC_USDT_PERP",
                "type": "LIMIT",
                "side": "BUY",
                "price": "59000",
                "size": "0.01",
                "filledSize": "0",
                "status": "OPEN",
                "clientOrderId": "abc-123",
                "source": "API",
                "createTime": 1786237680000,
                "updateTime": 1786237680000,
            }
        ]

    def get_futures_order(self, symbol, order_id):
        return {
            "orderId": order_id,
            "symbol": symbol,
            "type": "LIMIT",
            "side": "BUY",
            "price": "59000",
            "size": "0.01",
            "filledSize": "0.01",
            "filledAmount": "590.00",
            "status": "CLOSED",
            "createTime": 1786237680000,
            "updateTime": 1786237680000,
        }

    def get_spot_open_orders(self, symbol):
        return [
            {
                "orderId": 111222333,
                "symbol": symbol,
                "type": "LIMIT",
                "side": "SELL",
                "price": "62000",
                "size": "0.01",
                "filledSize": "0",
                "status": "OPEN",
                "createTime": 1786237680000,
                "updateTime": 1786237680000,
            }
        ]

    def get_spot_order(self, order_id):
        return {
            "orderId": order_id,
            "symbol": "BTC_USDT",
            "type": "LIMIT",
            "side": "SELL",
            "price": "62000",
            "size": "0.01",
            "filledSize": "0.01",
            "filledAmount": "620.00",
            "fee": "0.62",
            "feeCoin": "USDT",
            "status": "CLOSED",
            "createTime": 1786237680000,
            "updateTime": 1786237680000,
        }

    def get_futures_funding_fees(self, symbol=None, start_time=None, end_time=None, limit=100):
        return [
            {
                "symbol": symbol or "BTC_USDT_PERP",
                "isolatedMode": "CROSS",
                "fundingFee": "-0.42",
                "fundingCoin": "USDT",
                "timestamp": 1786237680000,
                "fundingRate": "0.0001",
            }
        ]

    def get_funding_rates(self, symbol, end_time=None, limit=1):
        return {
            "symbol": symbol,
            "rates": [{"fundingRate": "0.0001", "fundingTime": 1786237680000}],
        }

    def get_open_interests(self):
        return [{"symbol": "BTC_USDT_PERP", "openInterest": "12345.6"}]

    def get_futures_risk_table(self, symbol=None):
        return [{"symbol": symbol or "BTC_USDT_PERP", "rows": [{"maxLeverage": "20"}]}]


@pytest.fixture
def fallback_paper_adapter(monkeypatch) -> PionexAdapter:
    """PAPER-Adapter, bei dem ccxt bewusst KEINE 'pionex'-ID hat - der
    Adapter muss auf PionexClient zurueckfallen (siehe Modul-Docstring)."""
    import ccxt.async_support as ccxt_async

    monkeypatch.delattr(ccxt_async, "pionex", raising=False)
    monkeypatch.setattr("sgr.exchanges.pionex_client.PionexClient", FakePionexClient)
    return PionexAdapter(api_key="paper_key", secret="paper_secret", trading_mode=TradingMode.PAPER)


class TestNativeFallback:
    async def test_connect_uses_native_client_when_ccxt_lacks_pionex(
        self, fallback_paper_adapter: PionexAdapter
    ) -> None:
        await fallback_paper_adapter.connect()

        assert fallback_paper_adapter._connected is True
        assert fallback_paper_adapter._native_fallback is True
        assert fallback_paper_adapter._ccxt is None

    async def test_live_mode_reaches_native_fallback_spot_write_stays_blocked(
        self, monkeypatch
    ) -> None:
        """
        LIVE darf den nativen Fallback erreichen (siehe TestNativeFallbackLive
        fuer die volle Abdeckung). Dieser Adapter ist hier futures_mode=False
        (Spot, Default) - LIVE Spot Order-Submission bleibt bewusst NICHT
        implementiert (siehe sgr/exchanges/pionex.py Modul-Docstring
        "FUTURES WRITE INTEGRATION": nur der Futures-Pfad wurde gegen die
        Dokumentation verifiziert). Fuer den jetzt implementierten LIVE
        Futures Write Pfad siehe tests/exchanges/test_pionex_private_api.py
        TestFuturesWriteOperations.
        """
        import ccxt.async_support as ccxt_async

        from sgr.exchanges.base import AdapterFeatureNotImplementedError

        monkeypatch.delattr(ccxt_async, "pionex", raising=False)
        monkeypatch.setattr("sgr.exchanges.pionex_client.PionexClient", FakePionexClient)
        adapter = PionexAdapter(api_key="k", secret="s", trading_mode=TradingMode.LIVE)

        await adapter.connect()
        assert adapter._native_fallback is True
        assert adapter._connected is True

        from decimal import Decimal
        from uuid import uuid4

        from sgr.core.types import OrderRequest, OrderType, Side, Symbol

        order = OrderRequest(
            signal_id=uuid4(),
            symbol=Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX),
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.001"),
            trading_mode=TradingMode.LIVE,
        )
        with pytest.raises(AdapterFeatureNotImplementedError, match="live_spot_order_submission"):
            await adapter.place_order(order)

    async def test_get_ticker_uses_book_ticker_for_bid_ask(
        self, fallback_paper_adapter: PionexAdapter
    ) -> None:
        await fallback_paper_adapter.connect()

        ticker = await fallback_paper_adapter.get_ticker("BTC/USDT")

        assert ticker.last == 61000
        assert ticker.bid == 60990
        assert ticker.ask == 61010

    async def test_get_orderbook_parses_native_payload(
        self, fallback_paper_adapter: PionexAdapter
    ) -> None:
        await fallback_paper_adapter.connect()

        book = await fallback_paper_adapter.get_orderbook("BTC/USDT", depth=5)

        assert book.bids[0].price == 60990
        assert book.asks[0].price == 61010

    async def test_get_ohlcv_parses_native_klines(
        self, fallback_paper_adapter: PionexAdapter
    ) -> None:
        await fallback_paper_adapter.connect()

        candles = await fallback_paper_adapter.get_ohlcv("BTC/USDT", "1h", limit=10)

        assert len(candles) == 1
        assert candles[0].close == 61000

    async def test_get_exchange_info_builds_symbol_limits(
        self, fallback_paper_adapter: PionexAdapter
    ) -> None:
        await fallback_paper_adapter.connect()

        info = await fallback_paper_adapter.get_exchange_info()

        assert "BTC/USDT" in info.symbols
        limits = info.symbol_limits["BTC/USDT"]
        assert limits.amount_precision == 6
        assert limits.min_amount == 0.0001 or str(limits.min_amount) == "0.0001"

    async def test_public_funding_and_open_interest_now_implemented(
        self, fallback_paper_adapter: PionexAdapter
    ) -> None:
        """
        Seit der Private-API-Integration sind get_funding_rate()/
        get_open_interest() ueber die OEFFENTLICHEN Pionex-Endpunkte
        (/api/v1/market/fundingRates, /api/v1/market/openInterests)
        implementiert - funktionieren in PAPER wie LIVE ohne Credentials.
        Ersetzt die vorherige AdapterFeatureNotImplementedError.
        """
        await fallback_paper_adapter.connect()

        funding = await fallback_paper_adapter.get_funding_rate("BTC/USDT")
        assert funding.rate == Decimal("0.0001")

        oi = await fallback_paper_adapter.get_open_interest("BTC/USDT")
        assert oi.open_interest == Decimal("12345.6")

    async def test_set_leverage_spot_raises_not_supported(
        self, fallback_paper_adapter: PionexAdapter
    ) -> None:
        """
        fallback_paper_adapter ist futures_mode=False (Spot) - Spot kennt
        kein Leverage-Konzept, unabhaengig vom trading_mode. Seit der
        Futures Write Integration (siehe sgr/exchanges/pionex.py
        set_leverage() Docstring) ist das jetzt NotSupportedFeatureError
        (identisch zu Binance Spot via CCXTBaseAdapter._require_feature),
        nicht mehr die generische AdapterFeatureNotImplementedError von
        vorher.
        """
        from sgr.exchanges.base import NotSupportedFeatureError

        await fallback_paper_adapter.connect()

        with pytest.raises(NotSupportedFeatureError):
            await fallback_paper_adapter.set_leverage("BTC/USDT", 5)

    async def test_set_leverage_futures_paper_mode_not_implemented(self, monkeypatch) -> None:
        """
        Futures + PAPER hat kein echtes Pionex-Konto (Paper Trading bleibt
        strikt von jedem echten Account getrennt, siehe get_balance()
        Docstring) - set_leverage() bleibt dort bewusst
        AdapterFeatureNotImplementedError, unabhaengig davon, dass Pionex
        Futures das Konzept grundsaetzlich unterstuetzt und LIVE jetzt
        implementiert ist.
        """
        import ccxt.async_support as ccxt_async

        from sgr.exchanges.base import AdapterFeatureNotImplementedError

        monkeypatch.delattr(ccxt_async, "pionex", raising=False)
        monkeypatch.setattr("sgr.exchanges.pionex_client.PionexClient", FakePionexClient)
        adapter = PionexAdapter(
            api_key="paper_key",
            secret="paper_secret",
            trading_mode=TradingMode.PAPER,
            futures_mode=True,
        )
        await adapter.connect()

        with pytest.raises(AdapterFeatureNotImplementedError, match="set_leverage_paper_mode"):
            await adapter.set_leverage("BTC/USDT", 5)

    async def test_positions_and_order_query_are_safe_no_ops(
        self, fallback_paper_adapter: PionexAdapter
    ) -> None:
        await fallback_paper_adapter.connect()

        assert await fallback_paper_adapter.get_positions() == []
        assert await fallback_paper_adapter.cancel_all_orders() == 0
        assert await fallback_paper_adapter.get_open_orders() == []
        assert await fallback_paper_adapter.cancel_order("x", "BTC/USDT") is True

    async def test_paper_order_still_simulates_fill_via_native_ticker(
        self, fallback_paper_adapter: PionexAdapter
    ) -> None:
        """Ende-zu-Ende-Beleg: eine PAPER-Order fuellt weiterhin ueber den
        unveraenderten CCXTBaseAdapter._simulate_order()-Pfad, jetzt aber
        gespeist aus dem nativen Ticker-Fallback statt aus ccxt."""
        from decimal import Decimal
        from uuid import uuid4

        from sgr.core.types import OrderRequest, OrderStatus, OrderType, Side, Symbol

        await fallback_paper_adapter.connect()

        order = OrderRequest(
            signal_id=uuid4(),
            symbol=Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX),
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            trading_mode=TradingMode.PAPER,
        )

        result = await fallback_paper_adapter.place_order(order)

        assert result.status == OrderStatus.FILLED
        assert result.average_fill_price is not None and result.average_fill_price > 0

    async def test_close_closes_native_client(self, fallback_paper_adapter: PionexAdapter) -> None:
        await fallback_paper_adapter.connect()
        native_client = fallback_paper_adapter._native_client

        await fallback_paper_adapter.close()

        assert native_client.closed is True
        assert fallback_paper_adapter._connected is False
        assert fallback_paper_adapter._native_fallback is False


class TestParseNativeOrderCancelledAfterPartialFill:
    """'Cancel nach Partial Fill' (siehe Live-Verification-Anweisung
    Abschnitt G): Pionex's CLOSED-Bucket deckt sowohl FILLED als auch
    CANCELED/REJECTED ab (kein feineres Statusfeld im Schema, siehe
    _parse_native_order() Docstring). Eine Order, die storniert wurde,
    NACHDEM sie bereits teilweise gefuellt war, muss die tatsaechlich
    gefuellte Menge behalten (status wird FILLED fuer die tatsaechlich
    gefuellte Teilmenge, siehe Root-Cause-Fix-Docstring) statt sie als
    CANCELLED mit verlorener Fill-Information zu melden."""

    def test_closed_with_partial_fill_maps_to_filled_not_cancelled(
        self, paper_adapter: PionexAdapter
    ) -> None:
        raw = {
            "orderId": 42,
            "symbol": "BTC_USDT_PERP",
            "side": "BUY",
            "type": "MARKET",
            "size": "1.0",
            "filledSize": "0.3",
            "filledAmount": "18000",
            "status": "CLOSED",
            "createTime": 1786237680000,
            "updateTime": 1786237680000,
        }

        result = paper_adapter._parse_native_order(raw, AssetClass.FUTURES)

        assert result.status == OrderStatus.FILLED
        assert result.filled_quantity == Decimal("0.3")

    def test_closed_with_zero_fill_maps_to_cancelled(self, paper_adapter: PionexAdapter) -> None:
        raw = {
            "orderId": 43,
            "symbol": "BTC_USDT_PERP",
            "side": "BUY",
            "type": "LIMIT",
            "size": "1.0",
            "filledSize": "0",
            "status": "CLOSED",
            "createTime": 1786237680000,
            "updateTime": 1786237680000,
        }

        result = paper_adapter._parse_native_order(raw, AssetClass.FUTURES)

        assert result.status == OrderStatus.CANCELLED
        assert result.filled_quantity == Decimal("0")

    def test_open_with_partial_fill_maps_to_partially_filled(
        self, paper_adapter: PionexAdapter
    ) -> None:
        raw = {
            "orderId": 44,
            "symbol": "BTC_USDT_PERP",
            "side": "BUY",
            "type": "LIMIT",
            "size": "1.0",
            "filledSize": "0.3",
            "filledAmount": "18000",
            "status": "OPEN",
            "createTime": 1786237680000,
            "updateTime": 1786237680000,
        }

        result = paper_adapter._parse_native_order(raw, AssetClass.FUTURES)

        assert result.status == OrderStatus.PARTIALLY_FILLED
        assert result.filled_quantity == Decimal("0.3")
