#!/usr/bin/env python3
"""
LIVE READ-ONLY Verifikation der Pionex Private API (Spot + Futures).

Zweck
=====
Verifiziert die bereits implementierte, signierte Pionex-Private-API-
Integration (sgr/exchanges/pionex_client.py, sgr/exchanges/pionex.py)
gegen einen ECHTEN, kapitalarmen Pionex-Account - ausschliesslich mit
GET-Requests. Kein Order Placement, keine Order Cancellation, kein
Leverage/Margin-Write, kein Grid-Scheduler, kein automatischer Trading
Loop.

Sicherheitsaudit - WICHTIGE AKTUALISIERUNG (2026-09-21, Futures Write
Integration):
    Bis einschliesslich der Read-Only-Phase galt: PionexClient besass
    STRUKTURELL keine POST/PUT/DELETE-Methode, und PionexAdapter.
    place_order()/cancel_order()/cancel_all_orders()/set_leverage()
    warfen fuer LIVE NACHWEISLICH eine AdapterFeatureNotImplementedError
    VOR jeglichem Signing-/Netzwerk-Code - dieses Skript konnte diese
    Garantie deshalb gefahrlos LIVE selbst pruefen (fruehere Version
    dieses Docstrings, siehe Git-Historie).

    Das gilt NICHT MEHR: sgr/exchanges/pionex_client.py implementiert
    jetzt signierte POST/DELETE-Schreibmethoden (create_futures_order,
    cancel_futures_order, cancel_all_futures_orders,
    set_futures_leverage), und sgr/exchanges/pionex.py's place_order()/
    cancel_order()/cancel_all_orders()/set_leverage() fuehren fuer LIVE +
    Futures jetzt ECHTE, signierte Netzwerk-Calls aus (siehe dortiger
    Modul-Docstring "FUTURES WRITE INTEGRATION").

    KONSEQUENZ: dieses Skript ruft diese vier Methoden AUSSCHLIESSLICH
    NICHT MEHR AUF (der fruehere Write-Block-Selbsttest wurde ersatzlos
    entfernt - ein Aufruf waere jetzt selbst ein echter Write-Request,
    genau das, was dieses Skript per Namen und Zweck niemals tun darf).
    Der verbleibende Ablauf besteht ausschliesslich aus GET-Requests
    (siehe Endpunkt-Liste unten) - verifiziert durch Code-Review dieser
    Datei: kein Aufruf von place_order/cancel_order/cancel_all_orders/
    set_leverage irgendwo in diesem Modul. Ein dedizierter, separat
    freizugebender kontrollierter Live-Write-Test (Phase 4 der
    Write-Roadmap) ist ein EIGENSTAENDIGES, hier nicht enthaltenes
    Skript/Verfahren.

Credentials
===========
Werden AUSSCHLIESSLICH ueber den bestehenden Mechanismus geladen:

    config.credentials.get_credentials("pionex", TradingMode.LIVE)

d.h. aus den Environment-Variablen PIONEX_LIVE_API_KEY / PIONEX_LIVE_SECRET
(direkt gesetzt oder ueber eine .env-Datei, siehe .env.example - .env ist
in .gitignore). Dieses Skript liest, erzeugt und akzeptiert NIEMALS
Credentials auf einem anderen Weg (kein CLI-Argument, kein Prompt, kein
Hardcoding). Sind keine Credentials konfiguriert, wird KEIN Request
gemacht - stattdessen exakt "Pionex credentials not configured" gemeldet.

API Key/Secret werden an KEINER Stelle geloggt oder ausgegeben - jede
Fehlermeldung wird vor Ausgabe durch _scrub() von den geladenen
Credential-Werten bereinigt (Verteidigung in der Tiefe; die bestehende
Client-/Adapter-Implementierung baut ohnehin keine Credentials in
Fehlermeldungen ein, siehe Code-Review in pionex_client.py/pionex.py).

Usage
=====
    # In .env: PIONEX_LIVE_API_KEY=... / PIONEX_LIVE_SECRET=... setzen
    # (siehe .env.example) - NIEMALS im Chat/als CLI-Argument.

    python scripts/verify_pionex_live_read_only.py --yes
    python scripts/verify_pionex_live_read_only.py --yes --symbol ETH/USDT
    python scripts/verify_pionex_live_read_only.py --yes --skip-spot
    python scripts/verify_pionex_live_read_only.py --yes --skip-futures

Ohne --yes wird NICHTS ausgefuehrt (keine Credentials geladen, kein
Adapter gebaut, kein Request gesendet) - nur der Sicherheitshinweis wird
gedruckt. Das ist bewusst so: dieses Skript darf ausschliesslich explizit
manuell gestartet werden - kein automatischer Scheduler, kein Startup-
Hook, kein Worker-Hook referenziert dieses Modul irgendwo im Code.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Any

from sgr.core.types import ExchangeID, TradingMode
from sgr.exchanges.base import AdapterFeatureNotImplementedError
from sgr.exchanges.pionex import PionexAdapter, _to_pionex_symbol

_SAFETY_NOTICE = """\
Pionex LIVE READ-ONLY Verification - nicht gestartet.

Dieses Skript spricht einen ECHTEN Pionex-Account per HMAC-signierten
GET-Requests an (kein Order-Placement, keine Order-Cancellation, kein
Leverage/Margin-Write). Vor der Ausfuehrung:

  1. PIONEX_LIVE_API_KEY / PIONEX_LIVE_SECRET muessen in .env oder der
     Umgebung gesetzt sein (niemals im Chat/als CLI-Argument uebergeben).
  2. Ein kapitalarmer Verifikations-Account wird empfohlen.

Erneut mit --yes aufrufen, um fortzufahren:

    python scripts/verify_pionex_live_read_only.py --yes
"""


class WriteAttemptedError(RuntimeError):
    """
    Reserviert als Sicherheitsabbruch-Mechanismus fuer diesen Flow -
    aktuell von keinem Codepfad mehr ausgeloest, seit der Write-Block-
    Selbsttest entfernt wurde (siehe Modul-Docstring "Sicherheitsaudit
    - WICHTIGE AKTUALISIERUNG"): ein Aufruf der vier Schreibmethoden zum
    Zweck des Selbsttests waere seit der Futures Write Integration
    selbst ein echter Write-Request und darf in diesem GET-only-Skript
    nicht mehr stattfinden. Bleibt als importierbarer Typ bestehen,
    falls ein kuenftiger, dedizierter Sicherheits-Check hier wieder
    ansetzen soll.
    """


@dataclass
class CheckResult:
    category: str  # "spot" | "futures" | "public" | "reconciliation" | "capability"
    endpoint: str
    status: str  # "SUCCESS" | "FAILED" | "SKIPPED"
    http_status: int | None
    pionex_error_code: str | None
    detail: str


def _scrub(text: str, secrets: list[str]) -> str:
    """Entfernt jedes geladene Credential-Vorkommen aus einem String,
    bevor er geloggt/gedruckt/gespeichert wird (Verteidigung in der
    Tiefe - siehe Modul-Docstring)."""
    for s in secrets:
        if s:
            text = text.replace(s, "***REDACTED***")
    return text


def _truncate(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


def _announce(category: str, endpoint: str) -> None:
    """Aufgabenstellung Punkt 3: vor JEDEM echten API-Call trading_mode,
    Exchange, Endpoint und Read-Only-Charakter explizit feststellen -
    sowohl strukturiert geloggt als auch auf stdout sichtbar."""
    from sgr.core.logging import get_logger

    get_logger(__name__).info(
        "pionex_live_verify.call",
        trading_mode="live",
        exchange="pionex",
        category=category,
        endpoint=endpoint,
        read_only=True,
    )
    print(
        f"[VERIFY] trading_mode=live exchange=pionex category={category} "
        f"endpoint={endpoint} read_only=True"
    )


async def _check(
    results: list[CheckResult],
    secrets: list[str],
    category: str,
    endpoint: str,
    call: Any,
) -> Any | None:
    _announce(category, endpoint)
    try:
        value = await call()
    except AdapterFeatureNotImplementedError as e:
        results.append(
            CheckResult(category, endpoint, "SKIPPED", None, None, _scrub(str(e), secrets))
        )
        return None
    except Exception as e:
        http_status = getattr(e, "status_code", None)
        code = getattr(e, "code", None)
        results.append(
            CheckResult(category, endpoint, "FAILED", http_status, code, _scrub(str(e), secrets))
        )
        return None
    results.append(
        CheckResult(
            category, endpoint, "SUCCESS", None, None, _truncate(_scrub(repr(value), secrets))
        )
    )
    return value


async def _spot_checks(
    adapter: PionexAdapter,
    symbol: str,
    results: list[CheckResult],
    secrets: list[str],
    meta: dict[str, Any],
) -> None:
    await _check(
        results, secrets, "spot", "GET /api/v1/account/balances", lambda: adapter.get_balance()
    )

    orders = await _check(
        results,
        secrets,
        "spot",
        "GET /api/v1/trade/openOrders",
        lambda: adapter.get_open_orders(symbol=symbol),
    )
    order_id = orders[0].exchange_order_id if orders else None
    if order_id:
        await _check(
            results,
            secrets,
            "spot",
            "GET /api/v1/trade/order",
            lambda: adapter.get_order(order_id, symbol),
        )
    else:
        results.append(
            CheckResult(
                "spot",
                "GET /api/v1/trade/order",
                "SKIPPED",
                None,
                None,
                "no open spot order on this account to query by id (not a failure)",
            )
        )

    info = await _check(
        results, secrets, "spot", "GET /api/v1/common/symbols", lambda: adapter.get_exchange_info()
    )
    if info is not None:
        meta["spot_symbol_count"] = len(info.symbols)
        meta["spot_symbol_mapping_ok"] = symbol in info.symbols


async def _futures_checks(
    adapter: PionexAdapter,
    symbol: str,
    results: list[CheckResult],
    secrets: list[str],
    meta: dict[str, Any],
) -> None:
    await _check(
        results, secrets, "futures", "GET /uapi/v1/account/balances", lambda: adapter.get_balance()
    )

    positions = await _check(
        results,
        secrets,
        "futures",
        "GET /uapi/v1/account/positions",
        lambda: adapter.get_positions(),
    )
    meta["futures_positions_repr"] = (
        _truncate(_scrub(repr(positions), secrets)) if positions is not None else None
    )

    await _check(
        results,
        secrets,
        "futures",
        "GET /uapi/v1/account/leverage",
        lambda: adapter.get_futures_leverage(symbol),
    )
    await _check(
        results,
        secrets,
        "futures",
        "GET /uapi/v1/trade/isolatedMode",
        lambda: adapter.get_futures_margin_mode(symbol),
    )

    orders = await _check(
        results,
        secrets,
        "futures",
        "GET /uapi/v1/trade/openOrders",
        lambda: adapter.get_open_orders(),
    )
    order_id = orders[0].exchange_order_id if orders else None
    if order_id:
        await _check(
            results,
            secrets,
            "futures",
            "GET /uapi/v1/trade/order",
            lambda: adapter.get_order(order_id, symbol),
        )
    else:
        results.append(
            CheckResult(
                "futures",
                "GET /uapi/v1/trade/order",
                "SKIPPED",
                None,
                None,
                "no open futures order on this account to query by id (not a failure)",
            )
        )

    await _check(
        results,
        secrets,
        "futures",
        "GET /uapi/v1/trade/fundingFee",
        lambda: adapter.get_futures_funding_fee_history(symbol=symbol),
    )
    await _check(
        results,
        secrets,
        "public",
        "GET /api/v1/market/fundingRates",
        lambda: adapter.get_funding_rate(symbol),
    )
    await _check(
        results,
        secrets,
        "public",
        "GET /api/v1/market/openInterests",
        lambda: adapter.get_open_interest(symbol),
    )

    pionex_futures_symbol = _to_pionex_symbol(symbol, futures=True)
    await _check(
        results,
        secrets,
        "public",
        "GET /api/v1/common/riskTable",
        lambda: asyncio.to_thread(
            adapter._native_client.get_futures_risk_table, pionex_futures_symbol
        ),
    )

    raw_symbols = await _check(
        results,
        secrets,
        "public",
        "GET /api/v1/common/symbols (PERP)",
        lambda: asyncio.to_thread(adapter._native_client.get_symbols, None, "PERP"),
    )
    if raw_symbols is not None:
        listed = {row.get("symbol") for row in raw_symbols}
        meta["futures_symbol_count"] = len(raw_symbols)
        meta["futures_symbol_expected"] = pionex_futures_symbol
        meta["futures_symbol_mapping_ok"] = pionex_futures_symbol in listed


async def _run_mode(
    futures_mode: bool,
    symbol: str,
    credentials: dict[str, str],
    results: list[CheckResult],
    secrets: list[str],
    meta: dict[str, Any],
) -> PionexAdapter | None:
    category = "futures" if futures_mode else "spot"
    adapter = PionexAdapter(
        api_key=credentials["apiKey"],
        secret=credentials["secret"],
        trading_mode=TradingMode.LIVE,
        futures_mode=futures_mode,
    )

    _announce(category, "connect() [authenticated balance check, see _connect_native_fallback()]")
    try:
        await adapter.connect()
    except Exception as e:
        http_status = getattr(e, "status_code", None)
        code = getattr(e, "code", None)
        results.append(
            CheckResult(category, "connect()", "FAILED", http_status, code, _scrub(str(e), secrets))
        )
        return None
    results.append(
        CheckResult(category, "connect()", "SUCCESS", None, None, "authenticated + connected")
    )

    # KEIN Write-Block-Selbsttest mehr (siehe Modul-Docstring
    # "Sicherheitsaudit - WICHTIGE AKTUALISIERUNG"): place_order()/
    # cancel_order()/cancel_all_orders()/set_leverage() fuehren fuer
    # LIVE + Futures jetzt echte Netzwerk-Calls aus - dieser Flow ruft
    # sie deshalb ab hier bewusst NIE auf, nur die folgenden reinen
    # GET-Endpunkte.

    if futures_mode:
        await _futures_checks(adapter, symbol, results, secrets, meta)
    else:
        await _spot_checks(adapter, symbol, results, secrets, meta)

    caps = await adapter.detect_capabilities()
    meta[f"{category}_capabilities"] = caps
    results.append(CheckResult(category, "detect_capabilities()", "SUCCESS", None, None, str(caps)))

    return adapter


async def _check_reconciliation(
    futures_adapter: PionexAdapter, results: list[CheckResult], meta: dict[str, Any]
) -> None:
    from unittest.mock import MagicMock

    from sgr.exchanges.factory import ExchangePool
    from sgr.reconciliation.engine import ReconciliationEngine

    _announce(
        "reconciliation",
        "ReconciliationEngine.reconcile() [ruft nur adapter.get_positions() erneut auf]",
    )
    pool = ExchangePool()
    pool._adapters[(ExchangeID.PIONEX, TradingMode.LIVE)] = futures_adapter

    # Bewusst KEIN echtes PortfolioEngine/DB - dieses Skript weist nur
    # nach, dass der bestehende, UNVERAENDERTE Reconciliation-Code mit
    # echten Pionex-Responses laeuft (adapter.get_positions() liefert
    # korrekt geparste Position-Objekte hinein), nicht einen vollstaendigen
    # lokalen State-Vergleich (siehe Aufgabenstellung Punkt 8).
    stub_portfolio = MagicMock()
    stub_portfolio.positions = []

    engine = ReconciliationEngine(
        exchange_pool=pool,
        portfolio_engine=stub_portfolio,
        trading_mode=TradingMode.LIVE,
        exchange_id=ExchangeID.PIONEX,
    )
    try:
        result = await engine.reconcile()
    except Exception as e:
        results.append(
            CheckResult(
                "reconciliation", "ReconciliationEngine.reconcile()", "FAILED", None, None, str(e)
            )
        )
        return

    meta["reconciliation_status"] = result.status.value
    meta["reconciliation_checked_symbols"] = result.checked_symbols
    meta["reconciliation_split_brain_risk"] = result.has_split_brain_risk
    results.append(
        CheckResult(
            "reconciliation",
            "ReconciliationEngine.reconcile()",
            "SUCCESS",
            None,
            None,
            f"status={result.status.value} checked_symbols={result.checked_symbols} "
            f"discrepancies={len(result.discrepancies)} "
            f"split_brain_risk={result.has_split_brain_risk}",
        )
    )


def _print_report(results: list[CheckResult], meta: dict[str, Any]) -> None:
    print("\n" + "=" * 78)
    print("Pionex LIVE READ-ONLY Verification - Report")
    print("=" * 78)

    print("\nA. Erfolgreiche Endpunkte:")
    for r in results:
        if r.status == "SUCCESS":
            print(f"   [OK]      {r.category:<14} {r.endpoint}")

    print("\nB. Fehlgeschlagene Endpunkte:")
    failed = [r for r in results if r.status == "FAILED"]
    if not failed:
        print("   (keine)")
    for r in failed:
        print(f"   [FAILED]  {r.category:<14} {r.endpoint}")

    print("\nC. HTTP-Status / Pionex-Fehlercode pro fehlgeschlagenem Endpunkt:")
    if not failed:
        print("   (keine)")
    for r in failed:
        print(
            f"   {r.endpoint}: http_status={r.http_status} "
            f"pionex_code={r.pionex_error_code} detail={r.detail}"
        )

    print("\nD. HMAC-Authentifizierung mit echtem Account bestaetigt:")
    hmac_confirmed = any(r.status == "SUCCESS" and r.endpoint == "connect()" for r in results)
    print(f"   {hmac_confirmed}")

    print("\nE. Futures Account/Positionen erfolgreich gelesen:")
    futures_ok = any(
        r.status == "SUCCESS" and r.category == "futures" and "account/positions" in r.endpoint
        for r in results
    )
    print(f"   {futures_ok}")

    print(
        "\nF. Symbol-Mapping korrekt (lokal konstruiertes Symbol == von Pionex gelistetes Symbol):"
    )
    print(f"   spot:    {meta.get('spot_symbol_mapping_ok')}")
    print(
        f"   futures: {meta.get('futures_symbol_mapping_ok')} "
        f"(expected={meta.get('futures_symbol_expected')})"
    )

    print("\nG. Reconciliation Read Path mit echten Pionex-Daten:")
    print(
        f"   status={meta.get('reconciliation_status')} "
        f"split_brain_risk={meta.get('reconciliation_split_brain_risk')}"
    )

    print("\nH. Capability Detection unterscheidet Read/Write korrekt:")
    print("   N/A fuer diesen Lauf - der fruehere Write-Block-Selbsttest wurde entfernt,")
    print("   seit place_order/cancel_order/cancel_all_orders/set_leverage fuer LIVE+Futures")
    print("   echte Netzwerk-Calls ausfuehren (siehe Modul-Docstring 'Sicherheitsaudit -")
    print("   WICHTIGE AKTUALISIERUNG'). Dieses Skript ruft sie deshalb nicht mehr auf und")
    print("   kann diese Unterscheidung nicht mehr selbst live pruefen.")

    print("\nRaw capability detection:")
    for key in ("spot_capabilities", "futures_capabilities"):
        if key in meta:
            print(f"   {key}: {meta[key]}")

    print("\n" + "=" * 78)


async def _main(args: argparse.Namespace) -> int:
    if not args.yes:
        print(_SAFETY_NOTICE)
        return 1

    from sgr.core.config import get_config

    config = get_config()
    try:
        credentials = config.credentials.get_credentials("pionex", TradingMode.LIVE)
    except ValueError:
        print("Pionex credentials not configured")
        return 1

    secrets = [credentials["apiKey"], credentials["secret"]]
    results: list[CheckResult] = []
    meta: dict[str, Any] = {}

    spot_adapter: PionexAdapter | None = None
    futures_adapter: PionexAdapter | None = None

    try:
        if not args.skip_spot:
            spot_adapter = await _run_mode(False, args.symbol, credentials, results, secrets, meta)
        if not args.skip_futures:
            futures_adapter = await _run_mode(
                True, args.symbol, credentials, results, secrets, meta
            )

        if futures_adapter is not None:
            await _check_reconciliation(futures_adapter, results, meta)
    except WriteAttemptedError as e:
        print("\n" + "!" * 78)
        print(_scrub(str(e), secrets))
        print("!" * 78)
        return 2
    finally:
        if spot_adapter is not None:
            await spot_adapter.close()
        if futures_adapter is not None:
            await futures_adapter.close()

    _print_report(results, meta)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Explizite Bestaetigung, dass gegen einen ECHTEN Pionex-Account gelesen werden soll.",
    )
    parser.add_argument(
        "--symbol",
        default="BTC/USDT",
        help="Test-Symbol fuer symbol-gebundene Endpunkte (Default: BTC/USDT).",
    )
    parser.add_argument("--skip-spot", action="store_true", help="Spot-Endpunkte ueberspringen.")
    parser.add_argument(
        "--skip-futures", action="store_true", help="Futures-Endpunkte ueberspringen."
    )
    args = parser.parse_args()

    exit_code = asyncio.run(_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
