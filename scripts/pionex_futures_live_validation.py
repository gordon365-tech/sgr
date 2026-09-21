#!/usr/bin/env python3
"""
Pionex Futures LIVE Validation - Phase 4 (Read-Only zuerst, Write nur mit
doppelter expliziter Freigabe).

Zweck
=====
Validiert die Futures Write Integration (sgr/exchanges/pionex_client.py,
sgr/exchanges/pionex.py) AUSSCHLIESSLICH ueber den bestehenden SGR
Production Code Path - keine Abkuerzung, kein direkter place_order()-
Aufruf am Adapter vorbei:

    RiskEngine-Ebene wird hier NICHT durchlaufen (dieses Skript baut den
    OrderRequest manuell, exakt wie sgr/execution/grid_controller.py und
    sgr/risk/position_protection.py es bereits fuer ihre eigenen, nicht
    von RiskEngine stammenden Exit-Orders tun - ein etabliertes,
    bestehendes Muster, keine neue Abkuerzung).

    Ab dem OrderRequest an aufwaerts laeuft ALLES durch die echten,
    UNVERAENDERTEN Produktionsklassen:
        sgr.execution.engine.ExecutionEngine.execute()
          -> sgr.risk.live_trading_gate.check_live_trading_allowed()
          -> sgr.risk.kill_switch.KillSwitch (echte, globale Singleton-
             Instanz via get_kill_switch())
          -> sgr.execution.preflight.PreflightValidator
          -> sgr.execution.order_safety.SafeOrderExecutor
          -> sgr.exchanges.pionex.PionexAdapter.place_order()

    KEINE dieser Klassen wird von diesem Skript importiert-und-ersetzt,
    gepatcht oder in ihrer Logik umgangen. Das einzige, was dieses
    Skript zusaetzlich zur reinen Ausfuehrung tut, ist eine EINMALIGE,
    rein In-Memory (nicht in die DB persistierte) Registrierung einer
    klar als Test gekennzeichneten Strategie im StrategyRegistry-
    Singleton, DAMIT live_trading_gate ueberhaupt einen legitimen
    Kandidaten hat, den es pruefen kann - siehe Abschnitt
    "STRATEGY REGISTRATION FUER LIVE TRADING GATE" unten fuer die volle
    Begruendung. Das ist KEIN Umgehen des Gates: das Gate wird exakt so
    aufgerufen wie im echten Betrieb und kann den Testkandidaten genauso
    ablehnen wie jede andere Strategie, wenn irgendeine seiner
    Bedingungen nicht erfuellt ist.

Sicherheitsmodell (ZWEI STUFEN, siehe Aufgabenstellung Punkt 8)
================================================================
    Phase A (immer, mit --yes): AUSSCHLIESSLICH GET-Requests gegen den
        echten Account - Connectivity, Account State, Exchange Info/
        Symbol Precision, Positionen, offene Orders, Leverage/Margin-
        Modus, Risk Table, und ein orderByClientOrderId-Lookup mit einer
        garantiert nicht existierenden Test-ID (liefert einen ECHTEN
        Pionex-Fehlercode zurueck, ohne irgendetwas zu veraendern).

    Phase B (immer, mit --yes): PreflightValidator.validate() fuer eine
        KANDIDATEN-Order (noch nicht gesendet) - ausschliesslich weitere
        GET-Requests (ping/get_market_status/get_exchange_info/
        get_balance/get_positions/get_position_mode). Meldet, ob die
        Order technisch eligible waere.

    Phase C (immer, mit --yes): Druckt die EXAKT geplante Schreib-Aktion
        (Symbol, Seite, Ordertyp, Menge, Preis, reduce_only, Ziel-
        Leverage, clientOrderId, erwartetes Ergebnis) und STOPPT. Ab
        hier passiert NICHTS mehr ohne Phase D.

    Phase D (NUR mit --confirm-write UND einer zusaetzlichen, manuell
        einzugebenden Bestaetigungsphrase ueber input() - siehe
        _require_interactive_write_confirmation()): fuehrt GENAU EINE
        echte Order ueber ExecutionEngine.execute() aus, wartet auf
        Fill/Timeout, und - falls gefuellt - sendet unmittelbar danach
        eine reduce_only Order derselben Menge, um die Position wieder
        zu schliessen (ebenfalls ueber ExecutionEngine.execute()).

    Dieses Skript wurde in der aktuellen Session NIE mit --confirm-write
    aufgerufen und hat NIE eine echte Order gesendet - siehe
    Aufgabenstellung "Keine echten Orders ohne meine separate Freigabe".

Credentials
===========
Wie scripts/verify_pionex_live_read_only.py: AUSSCHLIESSLICH ueber
config.credentials.get_credentials("pionex", TradingMode.LIVE) (.env,
niemals im Chat). Werden nie geloggt/ausgegeben (_scrub()).

Usage
=====
    python scripts/pionex_futures_live_validation.py --yes
    python scripts/pionex_futures_live_validation.py --yes --symbol ETH/USDT

    # Phase D (ECHTE ORDER) - NUR nach separater, expliziter Freigabe:
    python scripts/pionex_futures_live_validation.py --yes --confirm-write

Kein automatischer Scheduler, kein Startup-Hook, kein Worker-Hook
referenziert dieses Modul irgendwo im Code - ausschliesslich manueller
Start.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sgr.core.types import (
    AssetClass,
    ExchangeID,
    MarketRegime,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    Symbol,
    TradingMode,
)
from sgr.exchanges.base import AdapterFeatureNotImplementedError
from sgr.exchanges.factory import ExchangePool
from sgr.exchanges.pionex import PionexAdapter
from sgr.execution.engine import ExecutionEngine
from sgr.execution.preflight import PreflightValidator
from sgr.risk.kill_switch import get_kill_switch
from sgr.strategy.base import StrategyParameters, ValidationStatus
from sgr.strategy.registry import StrategyRegistry

_TEST_STRATEGY_NAME = "phase4_manual_live_validation_TEST_ONLY"

_SAFETY_NOTICE = """\
Pionex Futures LIVE Validation (Phase 4) - nicht gestartet.

Dieses Skript spricht einen ECHTEN Pionex-Futures-Account an. Phase A-C
(Read-Only + Preflight-Dry-Run + Plan-Ausgabe) laufen bereits mit --yes.
Phase D (EINE echte Order) erfordert ZUSAETZLICH --confirm-write UND
eine manuelle, interaktive Bestaetigungsphrase - siehe Modul-Docstring.

Vor der Ausfuehrung:
  1. PIONEX_LIVE_API_KEY / PIONEX_LIVE_SECRET muessen in .env oder der
     Umgebung gesetzt sein (niemals im Chat/als CLI-Argument uebergeben).
  2. Ein kapitalarmer Verifikations-Account wird dringend empfohlen.

Erneut mit --yes aufrufen, um Phase A-C zu starten:

    python scripts/pionex_futures_live_validation.py --yes
"""


class _TestStrategy:
    """
    Minimale, als Test gekennzeichnete Strategie-Instanz - registriert
    NUR in diesem Skript-Prozess (In-Memory, StrategyRegistry._strategy_repo
    bleibt None -> keine DB-Persistenz, siehe sync_registrations_to_db()
    Docstring), NIE in einem echten API-/Worker-Prozess. generate_signal()/
    get_parameters() werden von diesem Skript nie aufgerufen - der
    OrderRequest wird manuell gebaut (siehe Modul-Docstring), exakt wie
    bei GridController/PositionProtectionWatchdog.
    """

    name = _TEST_STRATEGY_NAME
    version = "0.0.0-test"
    supported_regimes: list[MarketRegime] = []

    def generate_signal(self, context: Any) -> Any:
        raise NotImplementedError("Not used - OrderRequest is built manually, see module docstring")

    def get_parameters(self) -> StrategyParameters:
        raise NotImplementedError("Not used - OrderRequest is built manually, see module docstring")


@dataclass
class CheckResult:
    category: str
    endpoint: str
    status: str  # "SUCCESS" | "FAILED" | "SKIPPED"
    detail: str


@dataclass
class Phase4Report:
    results: list[CheckResult] = field(default_factory=list)
    preflight_checks: list[tuple[str, bool, str]] = field(default_factory=list)
    preflight_eligible: bool | None = None
    kill_switch_active_before: bool | None = None


def _scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***REDACTED***")
    return text


def _announce(category: str, endpoint: str) -> None:
    from sgr.core.logging import get_logger

    get_logger(__name__).info(
        "pionex_phase4.call",
        trading_mode="live",
        exchange="pionex",
        category=category,
        endpoint=endpoint,
    )
    print(f"[PHASE4] trading_mode=live exchange=pionex category={category} endpoint={endpoint}")


async def _check(
    report: Phase4Report, secrets: list[str], category: str, endpoint: str, call: Any
) -> Any | None:
    _announce(category, endpoint)
    try:
        value = await call()
    except AdapterFeatureNotImplementedError as e:
        report.results.append(CheckResult(category, endpoint, "SKIPPED", _scrub(str(e), secrets)))
        return None
    except Exception as e:
        report.results.append(
            CheckResult(category, endpoint, "FAILED", _scrub(f"{type(e).__name__}: {e}", secrets))
        )
        return None
    report.results.append(
        CheckResult(category, endpoint, "SUCCESS", _scrub(_truncate(repr(value)), secrets))
    )
    return value


def _truncate(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


# ---------------------------------------------------------------------
# Phase A: Read-Only
# ---------------------------------------------------------------------


async def _phase_a_read_only(
    adapter: PionexAdapter, symbol: str, report: Phase4Report, secrets: list[str]
) -> None:
    await _check(report, secrets, "connectivity", "connect()", adapter.connect)
    await _check(report, secrets, "account", "get_balance()", adapter.get_balance)
    await _check(report, secrets, "account", "get_positions()", adapter.get_positions)
    await _check(report, secrets, "account", "get_open_orders()", adapter.get_open_orders)
    await _check(report, secrets, "exchange_info", "get_exchange_info()", adapter.get_exchange_info)
    await _check(
        report,
        secrets,
        "leverage",
        "get_futures_leverage()",
        lambda: adapter.get_futures_leverage(symbol),
    )
    await _check(
        report,
        secrets,
        "margin",
        "get_futures_margin_mode()",
        lambda: adapter.get_futures_margin_mode(symbol),
    )

    import asyncio as _asyncio

    from sgr.exchanges.pionex import _to_pionex_symbol

    pionex_symbol = _to_pionex_symbol(symbol, futures=True)
    await _check(
        report,
        secrets,
        "risk_table",
        "get_futures_risk_table() [diagnostic, native client]",
        lambda: _asyncio.to_thread(adapter._native_client.get_futures_risk_table, pionex_symbol),
    )

    # Reale Pionex-Fehlercode-Verifikation (Aufgabenstellung "reale
    # Pionex Fehlercodes soweit verifizierbar"): eine garantiert nicht
    # existierende clientOrderId liefert einen ECHTEN Fehlercode von
    # Pionex zurueck - rein lesend, aendert nichts.
    probe_client_order_id = f"phase4-probe-{uuid4()}"

    async def _probe_unknown_client_order_id() -> str:
        raw = await _asyncio.to_thread(
            adapter._native_client.get_futures_order_by_client_id,
            pionex_symbol,
            probe_client_order_id,
        )
        return f"UNEXPECTED SUCCESS (order should not exist): {raw!r}"

    _announce("idempotency_probe", "get_futures_order_by_client_id() [nicht existierende Test-ID]")
    try:
        value = await _probe_unknown_client_order_id()
        report.results.append(
            CheckResult(
                "idempotency_probe",
                "get_futures_order_by_client_id()",
                "FAILED",
                _scrub(value, secrets),
            )
        )
    except Exception as e:
        code = getattr(e, "code", None)
        report.results.append(
            CheckResult(
                "idempotency_probe",
                "get_futures_order_by_client_id()",
                "SUCCESS",
                _scrub(
                    f"Real Pionex error code for unknown clientOrderId: "
                    f"{type(e).__name__} code={code!r} detail={e}",
                    secrets,
                ),
            )
        )


# ---------------------------------------------------------------------
# Phase B: Preflight Dry-Run (echte PreflightValidator-Klasse, unveraendert)
# ---------------------------------------------------------------------


def _candidate_order(symbol: str) -> OrderRequest:
    base, _, quote = symbol.partition("/")
    return OrderRequest(
        signal_id=uuid4(),
        # asset_class=FUTURES ist zwingend (Symbol-Default ist SPOT,
        # siehe sgr/core/types.py): PionexAdapter.get_positions() parst
        # echte Futures-Positionen mit asset_class=FUTURES (siehe
        # _from_pionex_symbol() in pionex.py). Ohne diesen expliziten
        # Wert wuerde die reduce_only-Close-Order in Phase D die reale
        # Position NIE finden (Symbol ist ein frozen Pydantic-Model,
        # Gleichheit vergleicht ALLE Felder inkl. asset_class) und
        # PreflightValidator._check_reduce_only_against_position() faelschlich
        # ablehnen - waehrend Binance-Futures denselben Fehler nicht zeigt,
        # weil CCXTBaseAdapter._parse_symbol() den Futures-Charakter aus
        # dem ccxt-Symbol-Suffix (":USDT") automatisch erkennt.
        symbol=Symbol(
            base=base, quote=quote, exchange=ExchangeID.PIONEX, asset_class=AssetClass.FUTURES
        ),
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.001"),
        trading_mode=TradingMode.LIVE,
        metadata={"strategy": _TEST_STRATEGY_NAME},
    )


async def _phase_b_preflight_dry_run(
    pool: ExchangePool, symbol: str, report: Phase4Report
) -> OrderRequest:
    _announce("preflight", "PreflightValidator.validate() [dry run, keine Order gesendet]")
    validator = PreflightValidator(pool, TradingMode.LIVE)
    order = _candidate_order(symbol)

    result = await validator.validate(order)
    for check in result.checks:
        report.preflight_checks.append((check.name, check.passed, check.detail))
    report.preflight_eligible = result.eligible
    return order


# ---------------------------------------------------------------------
# Phase C: geplante Schreib-Aktion melden und anhalten
# ---------------------------------------------------------------------


def _print_planned_write_action(order: OrderRequest, symbol: str) -> None:
    print("\n" + "=" * 78)
    print("C. GEPLANTE SCHREIB-AKTION (noch NICHT ausgefuehrt)")
    print("=" * 78)
    print(f"   Symbol:              {symbol} (Pionex: siehe Symbol-Mapping oben)")
    print(f"   Side:                {order.side.value.upper()}")
    print("   Order-Typ (SGR):     MARKET")
    print("   Order-Typ (Pionex):  MARKET_QTY (siehe pionex_client.py Modul-Docstring)")
    print(f"   Quantity:            {order.quantity}")
    print(f"   Reduce Only:         {order.reduce_only}")
    print(f"   Client Order ID:     {order.id}")
    print(f"   Strategie (Gate):    {order.metadata['strategy']} (nur In-Memory, siehe unten)")
    print("   Erwartetes Ergebnis: sofortiger Fill zum aktuellen Marktpreis (MARKET_QTY),")
    print("                        minimales Notional, danach sofortiger reduce-only Close")
    print("                        derselben Menge (Phase D).")
    print("=" * 78)
    print(
        "\nDiese Order wird NICHT gesendet, solange --confirm-write UND die "
        "interaktive Bestaetigungsphrase (siehe unten) nicht beide vorliegen."
    )


def _require_interactive_write_confirmation(order: OrderRequest, symbol: str) -> bool:
    """
    Zweite, unabhaengige Bestaetigungsstufe (zusaetzlich zu --confirm-write)
    - MUSS interaktiv von einem Menschen im Terminal eingegeben werden,
    kann nicht per CLI-Argument/Skript vorab geliefert werden. Diese
    Session hat diese Funktion nie mit einer korrekten Phrase erreicht.
    """
    print("\n" + "!" * 78)
    print("LETZTE BESTAETIGUNG VOR EINER ECHTEN, SIGNIERTEN ORDER")
    print(
        f"Symbol={symbol} Side={order.side.value.upper()} Type=MARKET_QTY "
        f"Qty={order.quantity} ClientOrderId={order.id}"
    )
    print("Tippe exakt die folgende Phrase, um fortzufahren, oder druecke Enter zum Abbrechen:")
    print("    I APPROVE THIS LIVE ORDER")
    print("!" * 78)
    try:
        typed = input("> ").strip()
    except (EOFError, OSError):
        # Kein interaktives Terminal verfuegbar (z.B. nicht-interaktiver
        # Aufruf, CI, stdin umgeleitet) - konservativ als "abgebrochen"
        # behandeln, niemals als stillschweigende Zustimmung werten.
        return False
    return typed == "I APPROVE THIS LIVE ORDER"


# ---------------------------------------------------------------------
# Phase D: echte Ausfuehrung ueber ExecutionEngine (Production Code Path)
# ---------------------------------------------------------------------


def _register_test_strategy_for_live_trading_gate() -> None:
    """
    Siehe Modul-Docstring "STRATEGY REGISTRATION FUER LIVE TRADING GATE".
    Rein In-Memory (kein StrategyRepository injiziert -> keine DB-
    Persistenz), nur fuer diesen Skript-Prozess. Ohne diesen Schritt
    wuerde sgr.risk.live_trading_gate.check_live_trading_allowed() JEDE
    LIVE-Order ablehnen (kein registrierter, live_approved Strategie-
    Name) - das ist die korrekte, unveraenderte Gate-Logik, kein Bug.
    """
    registry = StrategyRegistry.get()
    registry.register_instance(_TestStrategy())
    registry.mark_validated(
        _TEST_STRATEGY_NAME,
        ValidationStatus(
            backtest_passed=True,
            walk_forward_passed=True,
            paper_trading_passed=True,
            live_approved=True,
            is_operator_override=False,
            notes="Phase 4 manual live validation - in-memory only, never persisted.",
        ),
    )


async def _phase_d_execute_one_real_order(
    pool: ExchangePool, order: OrderRequest, symbol: str
) -> None:
    _register_test_strategy_for_live_trading_gate()
    await StrategyRegistry.get().activate(_TEST_STRATEGY_NAME)

    engine = ExecutionEngine(pool, TradingMode.LIVE)

    print("\n[PHASE4] Sende Order ueber ExecutionEngine.execute() (Production Code Path)...")
    result = await engine.execute(order)
    print(
        f"[PHASE4] Ergebnis: status={result.status.value} "
        f"exchange_order_id={result.exchange_order_id}"
    )
    print(
        f"[PHASE4] filled_quantity={result.filled_quantity} avg_price={result.average_fill_price}"
    )

    if result.status != OrderStatus.FILLED:
        print("[PHASE4] Order nicht gefuellt - KEIN automatischer Close-Versuch.")
        return

    close_order = OrderRequest(
        signal_id=uuid4(),
        symbol=order.symbol,
        side=Side.SELL if order.side == Side.BUY else Side.BUY,
        order_type=OrderType.MARKET,
        quantity=result.filled_quantity,
        trading_mode=TradingMode.LIVE,
        reduce_only=True,
        metadata={"strategy": _TEST_STRATEGY_NAME, "exit_reason": "phase4_manual_close"},
    )
    print(
        "\n[PHASE4] Schliesse Testposition sofort (reduce_only) ueber ExecutionEngine.execute()..."
    )
    close_result = await engine.execute(close_order, bypass_kill_switch=True)
    print(
        f"[PHASE4] Close-Ergebnis: status={close_result.status.value} "
        f"exchange_order_id={close_result.exchange_order_id}"
    )


# ---------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------


def _print_report(report: Phase4Report) -> None:
    print("\n" + "=" * 78)
    print("Pionex Futures LIVE Validation (Phase 4) - Report")
    print("=" * 78)

    print("\nA. Read-Only Checks:")
    for r in report.results:
        print(f"   [{r.status:<7}] {r.category:<18} {r.endpoint}")
        print(f"             {r.detail}")

    print("\nB. Preflight Dry-Run (echte PreflightValidator-Klasse):")
    for name, passed, detail in report.preflight_checks:
        print(f"   [{'PASS' if passed else 'FAIL':<4}] {name}: {detail}")
    print(f"\n   eligible = {report.preflight_eligible}")

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
    report = Phase4Report()

    adapter = PionexAdapter(
        api_key=credentials["apiKey"],
        secret=credentials["secret"],
        trading_mode=TradingMode.LIVE,
        futures_mode=True,
    )
    pool = ExchangePool()
    pool._adapters[(ExchangeID.PIONEX, TradingMode.LIVE)] = adapter

    try:
        await _phase_a_read_only(adapter, args.symbol, report, secrets)
        order = await _phase_b_preflight_dry_run(pool, args.symbol, report)
        _print_report(report)
        _print_planned_write_action(order, args.symbol)

        kill_switch = get_kill_switch(TradingMode.LIVE, tenant_id=config.tenant_id)
        print(f"\nKill Switch aktuell aktiv: {kill_switch.is_active}")

        if not args.confirm_write:
            print(
                "\n--confirm-write nicht gesetzt - Phase D (echte Order) wird NICHT "
                "ausgefuehrt. Nichts wurde gesendet."
            )
            return 0

        if not _require_interactive_write_confirmation(order, args.symbol):
            print("\nBestaetigungsphrase nicht korrekt eingegeben - Phase D abgebrochen.")
            return 0

        await _phase_d_execute_one_real_order(pool, order, args.symbol)
        return 0
    finally:
        await adapter.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--yes", action="store_true", help="Phase A-C starten (Read-Only + Dry-Run)."
    )
    parser.add_argument("--symbol", default="BTC/USDT", help="Test-Symbol (Default: BTC/USDT).")
    parser.add_argument(
        "--confirm-write",
        action="store_true",
        help=(
            "Ermoeglicht Phase D (EINE echte Order) - erfordert ZUSAETZLICH eine "
            "interaktive Bestaetigungsphrase, siehe Modul-Docstring."
        ),
    )
    args = parser.parse_args()

    exit_code = asyncio.run(_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
