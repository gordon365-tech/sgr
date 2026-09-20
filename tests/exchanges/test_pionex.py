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

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from sgr.core.config import ExchangeCredentials
from sgr.core.types import ExchangeID, TradingMode
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

    async def test_connect_fails_fast_when_ccxt_has_no_pionex_id(
        self, live_adapter: PionexAdapter, monkeypatch
    ) -> None:
        """Architektur-Befund: die real installierte ccxt-Version (siehe
        Modul-Docstring sgr/exchanges/pionex.py) fuehrt keine 'pionex'-ID.
        LIVE Trading darf sich in diesem Fall NICHT verbinden (sonst
        wuerde erst die erste Order unkontrolliert scheitern) - es muss
        stattdessen sofort und eindeutig fehlschlagen. Explizit
        delattr statt sich auf die Umgebung zu verlassen, damit dieser
        Test deterministisch bleibt, falls eine zukuenftige ccxt-Version
        "pionex" wieder registriert."""
        import ccxt.async_support as ccxt_async

        from sgr.exchanges.base import AdapterFeatureNotImplementedError

        monkeypatch.delattr(ccxt_async, "pionex", raising=False)

        with pytest.raises(AdapterFeatureNotImplementedError, match="live_order_submission"):
            await live_adapter.connect()


# ---------------------------------------------------------------------
# from_config()
# ---------------------------------------------------------------------


class TestFromConfig:
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
        creds = ExchangeCredentials()
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
        creds = ExchangeCredentials()
        monkeypatch.setattr("sgr.core.config.get_config", lambda: self._fake_config(creds))

        with pytest.raises(ValueError, match="Credentials not configured"):
            PionexAdapter.from_config(TradingMode.LIVE)


# ---------------------------------------------------------------------
# Native fallback (ccxt hat keine "pionex"-ID, siehe Modul-Docstring)
# ---------------------------------------------------------------------


class FakePionexClient:
    """Minimaler synchroner Stand-in fuer PionexClient (native Fallback)."""

    KLINE_INTERVALS = {"1h": "60M"}

    def __init__(self) -> None:
        self.closed = False

    def get_symbols(self):
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

    async def test_live_mode_never_reaches_native_fallback(self, monkeypatch) -> None:
        """Der native Fallback ist PAPER-only (siehe Modul-Docstring) -
        LIVE muss vorher fail-fast abbrechen, nicht stillschweigend auf
        oeffentliche Marktdaten ohne echte Order-Ausfuehrung zurueckfallen."""
        import ccxt.async_support as ccxt_async

        from sgr.exchanges.base import AdapterFeatureNotImplementedError

        monkeypatch.delattr(ccxt_async, "pionex", raising=False)
        adapter = PionexAdapter(api_key="k", secret="s", trading_mode=TradingMode.LIVE)

        with pytest.raises(AdapterFeatureNotImplementedError):
            await adapter.connect()

        assert adapter._native_fallback is False

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

    async def test_unimplemented_futures_endpoints_raise_clearly(
        self, fallback_paper_adapter: PionexAdapter
    ) -> None:
        from sgr.exchanges.base import AdapterFeatureNotImplementedError

        await fallback_paper_adapter.connect()

        with pytest.raises(AdapterFeatureNotImplementedError):
            await fallback_paper_adapter.get_funding_rate("BTC/USDT")
        with pytest.raises(AdapterFeatureNotImplementedError):
            await fallback_paper_adapter.get_open_interest("BTC/USDT")
        with pytest.raises(AdapterFeatureNotImplementedError):
            await fallback_paper_adapter.set_leverage("BTC/USDT", 5)

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
