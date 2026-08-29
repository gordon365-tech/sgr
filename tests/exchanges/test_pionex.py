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
    async def test_connect_delegates_to_base_class(
        self, live_adapter: PionexAdapter, monkeypatch
    ) -> None:
        base_connect = AsyncMock()
        monkeypatch.setattr(
            "sgr.exchanges.ccxt_base.CCXTBaseAdapter.connect", base_connect
        )

        await live_adapter.connect()

        base_connect.assert_awaited_once()


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
        monkeypatch.setattr(
            "sgr.core.config.get_config", lambda: self._fake_config(creds)
        )

        adapter = PionexAdapter.from_config(TradingMode.PAPER)

        assert adapter._api_key == "configured_paper_key"
        assert adapter._secret == "configured_paper_secret"
        assert adapter.trading_mode == TradingMode.PAPER

    def test_paper_mode_falls_back_to_placeholder_credentials(self, monkeypatch) -> None:
        """Ohne konfigurierte Paper-Credentials werden Platzhalter verwendet,
        da die CCXT-Instanz Parameter erwartet, aber Paper Mode keine echte
        Authentifizierung benötigt."""
        creds = ExchangeCredentials()
        monkeypatch.setattr(
            "sgr.core.config.get_config", lambda: self._fake_config(creds)
        )

        adapter = PionexAdapter.from_config(TradingMode.PAPER)

        assert adapter._api_key == "paper_key"
        assert adapter._secret == "paper_secret"

    def test_live_mode_uses_real_credentials(self, monkeypatch) -> None:
        creds = ExchangeCredentials(
            pionex_live_api_key=SecretStr("real_key"),
            pionex_live_secret=SecretStr("real_secret"),
        )
        monkeypatch.setattr(
            "sgr.core.config.get_config", lambda: self._fake_config(creds)
        )

        adapter = PionexAdapter.from_config(TradingMode.LIVE)

        assert adapter._api_key == "real_key"
        assert adapter._secret == "real_secret"
        assert adapter.trading_mode == TradingMode.LIVE

    def test_live_mode_without_credentials_raises(self, monkeypatch) -> None:
        creds = ExchangeCredentials()
        monkeypatch.setattr(
            "sgr.core.config.get_config", lambda: self._fake_config(creds)
        )

        with pytest.raises(ValueError, match="Credentials not configured"):
            PionexAdapter.from_config(TradingMode.LIVE)
