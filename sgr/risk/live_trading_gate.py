"""
SGR Live Trading Safety Gate
=============================
Letzte, harte Absicherung unmittelbar vor jeder LIVE-Order: verweigert
die Order, solange nicht ALLE unten aufgefuehrten Bedingungen erfuellt
sind. Fail-closed - jede Unsicherheit (fehlende Strategie, fehlender
Registry-Eintrag, fehlender Kill Switch) fuehrt zu REJECTED, nie zu
einem stillschweigenden Durchlassen.

Kontext: sgr.api.main.apply_strategy_force_activate_override()
(STRATEGY_FORCE_ACTIVATE env var) aktiviert Strategien fuer einen
Paper-Trading-Pipeline-Testlauf, obwohl sie die echte Validierung nicht
bestanden haben. is_validated/is_active werden dabei bewusst auf True
gesetzt, DAMIT der bestehende Aktivierungs-Loop in lifespan() (siehe
dortigen Kommentar: "for entry in registry.get_all().values(): if
entry.is_validated: await registry.activate(...)") die Strategie fuer
PAPER TRADING scharf schaltet. Ohne dieses Gate waere das dieselbe
is_validated/is_active-Kombination, die ein zukuenftiger echter
Live-Pfad faelschlich als "validiert genug" lesen koennte, wenn er sich
(wie ueberall sonst im System ueblich) auf is_validated/is_active statt
auf die strengeren Live-spezifischen Felder verlaesst.

Sicherstellung (siehe check_live_trading_allowed() unten):
    1. is_validated/is_active werden fuer die LIVE-Entscheidung NICHT
       herangezogen - nur ValidationStatus.live_approved (separates
       Feld, wird aktuell nirgends im System auf True gesetzt - es gibt
       noch keinen Live-Approval-Prozess) UND explizit NICHT
       is_operator_override.
    2. is_operator_override=True blockt IMMER, unabhaengig vom Wert von
       live_approved - selbst wenn ein kuenftiger Bug live_approved auf
       einer Override-Strategie versehentlich auf True setzen wuerde,
       bleibt dieser zweite, unabhaengige Marker als Sperre bestehen.
    3. Fehlt die Strategie-Zuordnung auf der Order komplett (siehe
       sgr/risk/engine.py build_order_request() - jede Order MUSS seit
       diesem Audit metadata["strategy"] tragen), wird die Order
       verweigert statt stillschweigend als "keine Strategie, also kein
       Risiko" durchgelassen zu werden.

Aufrufstelle: sgr/execution/engine.py ExecutionEngine.execute(), VOR dem
Kill-Switch-Check und jedem Exchange-Call - der fruehestmoegliche Punkt,
an dem sowohl order.trading_mode als auch order.metadata["strategy"]
bereits bekannt sind.
"""

from __future__ import annotations

from dataclasses import dataclass

from sgr.core.types import OrderRequest, TradingMode
from sgr.exchanges.factory import ExchangePool
from sgr.risk.kill_switch import KillSwitch
from sgr.strategy.registry import StrategyRegistry


@dataclass(frozen=True)
class LiveTradingGateResult:
    allowed: bool
    reason: str | None = None


def check_live_trading_allowed(
    order: OrderRequest,
    registry: StrategyRegistry | None = None,
    kill_switch: KillSwitch | None = None,
    exchange_pool: ExchangePool | None = None,
) -> LiveTradingGateResult:
    """
    Prueft, ob eine LIVE-Order ueberhaupt in Betracht kommen darf.

    Gibt allowed=True fuer JEDE PAPER-Order sofort zurueck (dieses Gate
    betrifft ausschliesslich TradingMode.LIVE - Paper Trading bleibt
    davon vollstaendig unberuehrt, siehe Modul-Docstring Testabdeckung
    in tests/unit/test_live_trading_gate.py).

    Fail-closed: registry=None oder kill_switch=None wird als "Safety-
    Infrastruktur nicht verfuegbar" behandelt und blockt LIVE-Orders,
    statt sie mangels Pruefmoeglichkeit durchzulassen.
    """
    if order.trading_mode != TradingMode.LIVE:
        return LiveTradingGateResult(allowed=True)

    # ab hier: nur noch LIVE-Pfad, jede Ruecksprungstelle ist ein REJECT

    if registry is None:
        return LiveTradingGateResult(
            allowed=False, reason="Live trading blocked: no StrategyRegistry available"
        )

    if kill_switch is None:
        return LiveTradingGateResult(
            allowed=False, reason="Live trading blocked: no KillSwitch available"
        )

    strategy_name = order.metadata.get("strategy")
    if not strategy_name or strategy_name == "unknown":
        return LiveTradingGateResult(
            allowed=False,
            reason="Live trading blocked: order has no attributable strategy_name",
        )

    entry = registry.get_entry(strategy_name)
    if entry is None:
        return LiveTradingGateResult(
            allowed=False,
            reason=f"Live trading blocked: strategy '{strategy_name}' not registered",
        )

    if entry.validation_status.is_operator_override:
        return LiveTradingGateResult(
            allowed=False,
            reason=(
                f"Live trading blocked: strategy '{strategy_name}' is active only via "
                "manual paper-trading override (STRATEGY_FORCE_ACTIVATE), not genuine "
                "validation - operator overrides can NEVER authorize live trading"
            ),
        )

    if not entry.validation_status.live_approved:
        return LiveTradingGateResult(
            allowed=False,
            reason=(
                f"Live trading blocked: strategy '{strategy_name}' has no live_approved "
                "validation status"
            ),
        )

    if not entry.is_validated or not entry.is_active:
        return LiveTradingGateResult(
            allowed=False,
            reason=(
                f"Live trading blocked: strategy '{strategy_name}' is not both "
                "is_validated and is_active"
            ),
        )

    if exchange_pool is not None:
        try:
            adapter = exchange_pool.get(order.symbol.exchange, TradingMode.LIVE)
        except KeyError:
            return LiveTradingGateResult(
                allowed=False,
                reason=(
                    f"Live trading blocked: no connected LIVE exchange adapter for "
                    f"{order.symbol.exchange.value}"
                ),
            )
        if not adapter._connected:
            return LiveTradingGateResult(
                allowed=False,
                reason=(
                    f"Live trading blocked: LIVE exchange adapter for "
                    f"{order.symbol.exchange.value} is not connected"
                ),
            )

    return LiveTradingGateResult(allowed=True)
