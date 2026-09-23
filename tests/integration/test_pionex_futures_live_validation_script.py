"""
Offline-Sanity-Tests fuer scripts/pionex_futures_live_validation.py
(Phase 4).

Alle Tests laufen AUSSCHLIESSLICH gegen einen Fake-PionexClient (kein
Netzwerk, keine echten Credentials). Sie beweisen:

1. Phase A/B/C (Read-Only + Preflight-Dry-Run + Plan-Ausgabe) laufen
   korrekt und senden NIE eine Order, unabhaengig von --confirm-write.
2. Phase D (echte Order ueber ExecutionEngine.execute(), den echten
   Production Code Path) ist technisch funktionsfaehig UND korrekt
   doppelt gegated: ohne --confirm-write laeuft sie nie; mit
   --confirm-write aber falscher/fehlender interaktiver Bestaetigung
   laeuft sie ebenfalls nie; nur mit BEIDEN Bestaetigungen zusammen
   fuehrt sie (hier: gegen den Fake-Client) tatsaechlich eine Order aus.

Diese Tests ersetzen NICHT die eigentliche Live-Verifikation gegen
einen echten Account (siehe Skript-Docstring "Usage") - sie beweisen
nur, dass die Skript-Logik selbst korrekt und sicher ist, BEVOR sie
gegen echtes Geld laeuft.
"""

from __future__ import annotations

import argparse
from decimal import Decimal

import pytest

import scripts.pionex_futures_live_validation as script
from sgr.exchanges.pionex_client import PionexAPIError


class FakeLiveValidationClient:
    """Vollstaendiger Fake (Precision-Daten + Write-Methoden), damit
    sowohl Phase A/B (Preflight) als auch Phase D (echte Order ueber
    ExecutionEngine) gegen ihn funktionieren."""

    KLINE_INTERVALS = {"1h": "60M"}
    created_orders: list[dict] = []  # class-level: ueber Instanzen hinweg beobachtbar

    def __init__(self, api_key=None, api_secret=None) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.orders: dict[int, dict] = {}
        self.client_order_index: dict[str, int] = {}
        self._next_order_id = 7000
        self.cancel_all_calls: list[str] = []

    def close(self):
        pass

    def get_symbols(self, symbols=None, market_type=None, status=None):
        return [
            {
                "symbol": "BTC_USDT",
                "baseCurrency": "BTC",
                "quoteCurrency": "USDT",
                "basePrecision": 6,
                "quotePrecision": 2,
                "minAmount": "0.0001",
            }
        ]

    def get_futures_account_balances(self):
        return {"balances": [{"coin": "USDT", "free": "5000", "frozen": "0"}], "isolates": []}

    def get_futures_positions(self, symbol=None):
        """Simuliert Netto-Exposure aus den bisher GEFUELLTEN Orders
        (wie ein echter Pionex-Account seine Position nach einem Fill
        aktualisiert) - noetig, damit ein anschliessender reduce_only
        Close-Versuch durch PreflightValidator._check_reduce_only_
        against_position() nicht faelschlich mangels Positionsdaten
        abgelehnt wird."""
        sym = symbol or "BTC_USDT_PERP"
        net = Decimal("0")
        for o in self.orders.values():
            if o["symbol"] != sym:
                continue
            filled = Decimal(o.get("filledSize") or "0")
            if filled <= 0:
                continue
            sign = Decimal("1") if o["side"] == "BUY" else Decimal("-1")
            net += sign * filled
        if net == 0:
            return []
        return [
            {
                "positionId": "sim-1",
                "symbol": sym,
                "isolatedMode": "CROSS",
                "positionSide": "LONG" if net > 0 else "SHORT",
                "netSize": str(abs(net)),
                "avgPrice": "60000",
                "unrealizedPnL": "0",
                "markPrice": "60000",
                "leverage": "1",
                "createTime": 1786237680000,
                "updateTime": 1786237680000,
            }
        ]

    def get_futures_open_orders(self, symbol=None, end_time=None, limit=100):
        return [
            dict(o)
            for o in self.orders.values()
            if o["status"] == "OPEN" and (symbol is None or o["symbol"] == symbol)
        ]

    def get_ticker(self, symbol):
        """Fuer PionexAdapter.get_ticker() - noetig, seit Phase D (echte
        MARKET-Order ohne limit_price) den Preis ueber das
        LiveVerificationGate absichern muss (Ticker-Fallback-Pfad, siehe
        sgr/risk/live_verification_profile.py::check_live_verification_
        allowed()). Preis konsistent mit dem sonst in dieser Fake-Klasse
        angenommenen Marktpreis (siehe create_futures_order()/
        get_futures_positions(), beide nutzen "60000" als Default)."""
        return {"symbol": symbol, "close": "60000", "volume": "100", "time": 1786237680000}

    def get_futures_leverage(self, symbol):
        return {"symbol": symbol, "leverage": "1"}

    def get_futures_margin_mode(self, symbol):
        return {"symbol": symbol, "isolatedMode": "CROSS"}

    def get_futures_risk_table(self, symbol=None):
        return [{"rowNum": 1, "maxLeverage": "20"}]

    def get_futures_order_by_client_id(self, symbol, client_order_id):
        order_id = self.client_order_index.get(client_order_id)
        if order_id is None:
            raise PionexAPIError(
                "TRADE_ORDER_NOT_FOUND: no such order", code="TRADE_ORDER_NOT_FOUND"
            )
        return dict(self.orders[order_id])

    def get_futures_order(self, symbol, order_id):
        return dict(self.orders[order_id])

    def create_futures_order(
        self,
        symbol,
        side,
        order_type,
        size=None,
        price=None,
        reduce_only=None,
        client_order_id=None,
    ):
        order_id = self._next_order_id
        self._next_order_id += 1
        immediate_fill = order_type == "MARKET_QTY"
        row = {
            "orderId": order_id,
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "price": price,
            "size": size,
            "filledSize": size if immediate_fill else "0",
            "filledAmount": (
                str(Decimal(size or "0") * Decimal(price or "60000")) if immediate_fill else "0"
            ),
            "status": "CLOSED" if immediate_fill else "OPEN",
            "reduceOnly": bool(reduce_only),
            "clientOrderId": client_order_id,
            "createTime": 1786237680000,
            "updateTime": 1786237680000,
        }
        self.orders[order_id] = row
        if client_order_id:
            self.client_order_index[client_order_id] = order_id
        type(self).created_orders.append(dict(row))
        return order_id

    def cancel_futures_order(self, symbol, order_id):
        order = self.orders.get(order_id)
        if order is None or order["status"] == "CLOSED":
            raise PionexAPIError("TRADE_ORDER_NOT_FOUND", code="TRADE_ORDER_NOT_FOUND")
        order["status"] = "CLOSED"

    def cancel_all_futures_orders(self, symbol):
        self.cancel_all_calls.append(symbol)
        for order in self.orders.values():
            if order["symbol"] == symbol and order["status"] == "OPEN":
                order["status"] = "CLOSED"

    def set_futures_leverage(self, symbol, leverage):
        return {"symbol": symbol, "leverage": leverage}


@pytest.fixture(autouse=True)
def _reset_created_orders():
    FakeLiveValidationClient.created_orders = []
    yield
    FakeLiveValidationClient.created_orders = []


@pytest.fixture
def patch_client(monkeypatch):
    monkeypatch.setattr("sgr.exchanges.pionex_client.PionexClient", FakeLiveValidationClient)
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
    defaults = dict(
        yes=True,
        symbol="BTC/USDT",
        confirm_write=False,
        # Operator-Pflichtangaben fuer Phase D (LiveVerificationGate) -
        # Default None wie in main()'s argparse-Definition: kein Script-
        # seitiger Fallback-Wert, siehe _build_live_verification_gate().
        max_budget_usd=None,
        max_loss_usd=None,
        max_daily_loss_usd=None,
        max_leverage=None,
        max_position_usd=None,
        max_duration_minutes=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _approved_live_verification_args(**overrides) -> dict:
    """Realistische, vom 'Operator' (Testcode) explizit freigegebene
    Phase-D-Limits - fuer Tests, die den kompletten Order-Pfad inkl.
    LiveVerificationGate erfolgreich durchlaufen sollen."""
    base = dict(
        confirm_write=True,
        max_budget_usd=Decimal("500"),
        max_loss_usd=Decimal("200"),
        max_daily_loss_usd=Decimal("200"),
        max_leverage=Decimal("5"),
        # Order in diesem Skript: 0.001 BTC zum Fake-Marktpreis 60000 =
        # 60 USD Notional (siehe FakeLiveValidationClient) - grosszuegig
        # ueber diesem Wert, damit der Test das Gate-PASS-Verhalten
        # prueft, nicht versehentlich dessen Notional-Limit.
        max_position_usd=Decimal("500"),
        max_duration_minutes=30,
    )
    base.update(overrides)
    return base


class TestMissingCredentials:
    async def test_missing_credentials_reported_exactly(
        self, capsys, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.delenv("PIONEX_LIVE_API_KEY", raising=False)
        monkeypatch.delenv("PIONEX_LIVE_SECRET", raising=False)
        monkeypatch.chdir(tmp_path)
        from sgr.core.config import get_config

        get_config.cache_clear()

        exit_code = await script._main(_args())

        assert exit_code == 1
        assert "Pionex credentials not configured" in capsys.readouterr().out
        get_config.cache_clear()

    async def test_without_yes_nothing_runs(self, capsys) -> None:
        exit_code = await script._main(_args(yes=False))

        assert exit_code == 1
        assert "nicht gestartet" in capsys.readouterr().out


class TestPhaseAThroughC:
    async def test_read_only_and_preflight_run_without_confirm_write(
        self, patch_client, env_credentials, capsys
    ) -> None:
        exit_code = await script._main(_args())

        out = capsys.readouterr().out
        assert exit_code == 0
        assert "A. Read-Only Checks" in out
        assert "B. Preflight Dry-Run" in out
        assert "GEPLANTE SCHREIB-AKTION" in out
        assert "--confirm-write nicht gesetzt" in out
        assert FakeLiveValidationClient.created_orders == []

    async def test_preflight_reports_eligible_for_valid_candidate(
        self, patch_client, env_credentials, capsys
    ) -> None:
        exit_code = await script._main(_args())

        out = capsys.readouterr().out
        assert exit_code == 0
        assert "eligible = True" in out

    async def test_real_error_code_probe_surfaces_trade_order_not_found(
        self, patch_client, env_credentials, capsys
    ) -> None:
        exit_code = await script._main(_args())

        out = capsys.readouterr().out
        assert exit_code == 0
        assert "TRADE_ORDER_NOT_FOUND" in out

    async def test_secrets_never_appear_in_output(
        self, patch_client, env_credentials, capsys
    ) -> None:
        exit_code = await script._main(_args())

        out = capsys.readouterr().out
        assert exit_code == 0
        assert "test-live-key" not in out
        assert "test-live-secret" not in out


class TestPhaseDGating:
    async def test_confirm_write_flag_alone_is_not_enough(
        self, patch_client, env_credentials, capsys
    ) -> None:
        """--confirm-write ohne die korrekte interaktive Phrase (hier:
        EOFError, kein Terminal) darf NIE eine Order ausloesen."""
        exit_code = await script._main(_args(confirm_write=True))

        out = capsys.readouterr().out
        assert exit_code == 0
        assert FakeLiveValidationClient.created_orders == []
        assert "Bestaetigungsphrase" in out

    async def test_wrong_typed_phrase_blocks_execution(
        self, patch_client, env_credentials, capsys, monkeypatch
    ) -> None:
        monkeypatch.setattr("builtins.input", lambda _prompt="": "no thanks")

        exit_code = await script._main(_args(confirm_write=True))

        assert exit_code == 0
        assert FakeLiveValidationClient.created_orders == []

    async def test_exact_phrase_and_flag_together_execute_via_execution_engine(
        self, patch_client, env_credentials, capsys, monkeypatch
    ) -> None:
        """
        Beweist: Phase D ist technisch funktionsfaehig UND laeuft
        tatsaechlich durch ExecutionEngine.execute() (Production Code
        Path, inkl. live_trading_gate/PreflightValidator/
        SafeOrderExecutor) - NUR wenn BEIDE Bestaetigungen vorliegen.
        Reine Fake-Client-Verifikation, siehe Moduldocstring.
        """
        monkeypatch.setattr("builtins.input", lambda _prompt="": "I APPROVE THIS LIVE ORDER")

        exit_code = await script._main(_args(**_approved_live_verification_args()))

        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Sende Order ueber ExecutionEngine.execute()" in out
        # Open + reduce-only Close = 2 tatsaechlich angelegte Orders.
        assert len(FakeLiveValidationClient.created_orders) == 2
        assert FakeLiveValidationClient.created_orders[0]["type"] == "MARKET_QTY"
        assert FakeLiveValidationClient.created_orders[1]["reduceOnly"] is True

    async def test_both_confirmations_but_missing_operator_budget_blocks_phase_d(
        self, patch_client, env_credentials, capsys, monkeypatch
    ) -> None:
        """Beweist das Fail-Closed-Verhalten des LiveVerificationGate:
        selbst mit --confirm-write UND korrekter interaktiver Phrase
        darf Phase D NICHT ausgefuehrt werden, solange der Operator
        keine expliziten Budget-/Risiko-Limits uebergeben hat (siehe
        Aufgabenstellung Abschnitt U - kein Default-Budget)."""
        monkeypatch.setattr("builtins.input", lambda _prompt="": "I APPROVE THIS LIVE ORDER")

        exit_code = await script._main(_args(confirm_write=True))

        out = capsys.readouterr().out
        assert exit_code == 1
        assert FakeLiveValidationClient.created_orders == []
        assert "Fehlende Operator-Pflichtangaben" in out
        assert "--max-budget-usd" in out
