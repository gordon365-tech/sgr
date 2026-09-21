"""
Offline-Sanity-Tests fuer scripts/verify_pionex_live_read_only.py.

Diese Tests fuehren den Verification-Flow GEGEN EINEN FAKE-PionexClient
aus (kein Netzwerk, keine echten Credentials) - sie bestaetigen, dass die
Skript-LOGIK selbst korrekt ist (Control Flow, Fehlerbehandlung, Safety-
Selbsttest, Report-Generierung), BEVOR das Skript je gegen einen echten,
kapitalarmen Pionex-Account laeuft. Ersetzen NICHT die eigentliche Live-
Verifikation (die erfordert echte Credentials + manuellen Start, siehe
Skript-Docstring "Usage").
"""

from __future__ import annotations

import argparse

import pytest

from scripts.verify_pionex_live_read_only import (
    CheckResult,
    WriteAttemptedError,
    _main,
    _scrub,
    _selftest_write_endpoints_blocked,
)
from sgr.core.types import TradingMode
from sgr.exchanges.pionex import PionexAdapter
from sgr.exchanges.pionex_client import PionexAPIError


class FakeScriptClient:
    """Minimaler Fake, deckt genau die vom Skript verwendeten Aufrufe ab."""

    KLINE_INTERVALS = {"1h": "60M"}

    def __init__(self, api_key=None, api_secret=None) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.closed = False

    def close(self):
        self.closed = True

    def get_symbols(self, symbols=None, market_type=None, status=None):
        if market_type == "PERP":
            return [{"symbol": "BTC_USDT_PERP", "baseCurrency": "BTC", "quoteCurrency": "USDT"}]
        return [{"symbol": "BTC_USDT", "baseCurrency": "BTC", "quoteCurrency": "USDT"}]

    def get_account_balances(self):
        return [{"coin": "USDT", "free": "100.0", "frozen": "0"}]

    def get_futures_account_balances(self):
        return {"balances": [{"coin": "USDT", "free": "50.0", "frozen": "0"}], "isolates": []}

    def get_futures_positions(self, symbol=None):
        return []

    def get_futures_leverage(self, symbol):
        return {"symbol": symbol, "leverage": "5"}

    def get_futures_margin_mode(self, symbol):
        return {"symbol": symbol, "isolatedMode": "CROSS"}

    def get_futures_open_orders(self, symbol=None, end_time=None, limit=100):
        return []

    def get_spot_open_orders(self, symbol):
        return []

    def get_futures_funding_fees(self, symbol=None, start_time=None, end_time=None, limit=100):
        return []

    def get_funding_rates(self, symbol, end_time=None, limit=1):
        return {
            "symbol": symbol,
            "rates": [{"fundingRate": "0.0001", "fundingTime": 1786237680000}],
        }

    def get_open_interests(self):
        return [{"symbol": "BTC_USDT_PERP", "openInterest": "1.0"}]

    def get_futures_risk_table(self, symbol=None):
        return [{"rowNum": 1, "maxLeverage": "20"}]


@pytest.fixture
def patch_client(monkeypatch):
    monkeypatch.setattr("sgr.exchanges.pionex_client.PionexClient", FakeScriptClient)
    import ccxt.async_support as ccxt_async

    monkeypatch.delattr(ccxt_async, "pionex", raising=False)


@pytest.fixture
def env_credentials(monkeypatch):
    monkeypatch.setenv("PIONEX_LIVE_API_KEY", "test-live-key")
    monkeypatch.setenv("PIONEX_LIVE_SECRET", "test-live-secret")
    from sgr.core.config import get_config

    get_config.cache_clear()
    yield
    get_config.cache_clear()


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(yes=True, symbol="BTC/USDT", skip_spot=False, skip_futures=False)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestMissingCredentials:
    async def test_missing_credentials_reported_exactly_and_no_request_made(
        self, capsys, monkeypatch, tmp_path
    ) -> None:
        """
        monkeypatch.chdir(tmp_path) ist hier notwendig (nicht nur delenv):
        seit dem Bugfix fuer die Credential-Loading-Luecke (siehe
        sgr/core/config.py) liest ExchangeCredentials zusaetzlich zur
        Umgebung auch aus einer .env-Datei im Arbeitsverzeichnis - dieser
        Test simuliert bewusst "gar nichts konfiguriert" und muss deshalb
        unabhaengig davon sein, ob das echte Repo-.env (z.B. waehrend
        einer echten Live-Verifikationssitzung) gerade befuellt ist.
        """
        monkeypatch.delenv("PIONEX_LIVE_API_KEY", raising=False)
        monkeypatch.delenv("PIONEX_LIVE_SECRET", raising=False)
        monkeypatch.chdir(tmp_path)
        from sgr.core.config import get_config

        get_config.cache_clear()

        exit_code = await _main(_args())

        assert exit_code == 1
        assert "Pionex credentials not configured" in capsys.readouterr().out
        get_config.cache_clear()

    async def test_without_yes_flag_nothing_runs(self, capsys) -> None:
        exit_code = await _main(_args(yes=False))

        assert exit_code == 1
        assert "nicht gestartet" in capsys.readouterr().out


class TestWriteSelftest:
    async def test_selftest_passes_for_current_production_code(self, patch_client) -> None:
        """Der Write-Block-Selbsttest muss gegen den ECHTEN (nicht
        gemockten) PionexAdapter-Code aus sgr/exchanges/pionex.py gruen
        sein - das ist die eigentliche Sicherheitsgarantie."""
        adapter = PionexAdapter(
            api_key="k", secret="s", trading_mode=TradingMode.LIVE, futures_mode=True
        )
        await adapter.connect()

        await _selftest_write_endpoints_blocked(adapter, "BTC/USDT")

    async def test_selftest_aborts_if_place_order_stops_raising(
        self, patch_client, monkeypatch
    ) -> None:
        """Simuliert eine hypothetische Regression: place_order() wuerde
        (faelschlich) erfolgreich zurueckkehren statt zu blockieren -
        der Selbsttest MUSS das als SAFETY ABORT erkennen."""
        adapter = PionexAdapter(
            api_key="k", secret="s", trading_mode=TradingMode.LIVE, futures_mode=True
        )
        await adapter.connect()

        async def _fake_success(order):
            from uuid import uuid4

            from sgr.core.types import OrderResult, OrderStatus

            return OrderResult(
                request_id=uuid4(),
                exchange_order_id="fake",
                symbol=order.symbol,
                status=OrderStatus.FILLED,
                trading_mode=TradingMode.LIVE,
            )

        monkeypatch.setattr(adapter, "place_order", _fake_success)

        with pytest.raises(WriteAttemptedError, match="SAFETY ABORT"):
            await _selftest_write_endpoints_blocked(adapter, "BTC/USDT")


class TestFullFlow:
    async def test_happy_path_produces_report_without_abort(
        self, patch_client, env_credentials, capsys
    ) -> None:
        exit_code = await _main(_args())

        out = capsys.readouterr().out
        assert exit_code == 0
        assert "SAFETY ABORT" not in out
        assert "test-live-key" not in out
        assert "test-live-secret" not in out
        assert "A. Erfolgreiche Endpunkte" in out

    async def test_skip_futures_only_runs_spot(self, patch_client, env_credentials, capsys) -> None:
        exit_code = await _main(_args(skip_futures=True))

        out = capsys.readouterr().out
        assert exit_code == 0
        assert "category=futures" not in out

    async def test_auth_failure_recorded_not_raised(
        self, patch_client, env_credentials, monkeypatch, capsys
    ) -> None:
        def _boom(self):
            raise PionexAPIError("INVALID_SIGNATURE: bad", code="INVALID_SIGNATURE")

        monkeypatch.setattr(FakeScriptClient, "get_account_balances", _boom)
        monkeypatch.setattr(FakeScriptClient, "get_futures_account_balances", _boom)

        exit_code = await _main(_args())

        out = capsys.readouterr().out
        assert exit_code == 0
        assert "FAILED" in out
        assert "test-live-secret" not in out


class TestScrub:
    def test_scrub_removes_secret_from_text(self) -> None:
        text = _scrub("error talking to key=abc123 secret=xyz789", ["abc123", "xyz789"])

        assert "abc123" not in text
        assert "xyz789" not in text
        assert "***REDACTED***" in text

    def test_result_dataclass_holds_fields(self) -> None:
        r = CheckResult("spot", "GET /x", "SUCCESS", None, None, "ok")
        assert r.status == "SUCCESS"
        assert r.http_status is None
