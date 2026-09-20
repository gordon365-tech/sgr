"""
SGR Exchange / Product Capability Layer
========================================
Beantwortet EINE Frage, bevor irgendeine Order gebaut wird: "kann diese
Exchange dieses Produkt fuer dieses Symbol technisch ueberhaupt
handeln?" - getrennt von der Frage, ob es REGULATORISCH zulaessig ist
(das entscheidet sgr.compliance, nicht dieses Modul).

Warum eine eigene Abstraktion statt "wenn exchange == PIONEX: ...")?
    Die Aufgabenstellung verlangt explizit, dass Exchange, ProductType
    und Strategy nicht hart miteinander verdrahtet werden. Eine Strategie
    deklariert (siehe sgr.strategy.futures_grid.GridTradingStrategy),
    welche ProductTypes/Exchanges sie unterstuetzt; die Execution-Schicht
    (sgr.execution.grid_controller.GridController, kuenftig auch
    ExecutionEngine fuer andere Produkte) prueft VOR jeder Order per
    require_capability(), ob die Kombination (Exchange, ProductType)
    ueberhaupt existiert - unabhaengig davon, welche Strategie sie
    angefordert hat.

Quelle der Werte:
    Dies ist eine STATISCHE, im Code gepflegte Tabelle (kein Live-Abruf
    von der Exchange) - Grund: viele der hier abgefragten Eigenschaften
    (SupportsHedgeMode, SupportsFuturesGrid als SGR-Konzept, ...) sind
    keine von ccxt/den Exchange-APIs direkt exponierten Felder, sondern
    Produktentscheidungen von SGR selbst ("bieten WIR dieses Produkt auf
    dieser Exchange an"). Technische Detail-Faehigkeiten, die eine
    Exchange-API selbst zur Laufzeit meldet (z.B. ccxt's `.has`-Dict),
    bleiben weiterhin Aufgabe des jeweiligen Adapters (siehe
    CCXTBaseAdapter._require_feature) - diese Tabelle ist die grobere,
    produktseitige Vorab-Pruefung, nicht deren Ersatz.

Wichtiger Befund (Architektur-Audit, siehe Strategiebericht):
    Das in ccxt (>=4.1, verifiziert bis 4.5.81) verfuegbare Exchange-Set
    enthaelt KEINE Pionex-Exchange-ID mehr - `sgr.exchanges.pionex.
    PionexAdapter` (CCXT-basiert) kann sich damit weder in PAPER noch in
    LIVE tatsaechlich verbinden (`getattr(ccxt, "pionex")` schlaegt fehl).
    Diese Tabelle markiert PIONEX/PERPETUAL und PIONEX/FUTURES_GRID
    deshalb technisch als "capability vorhanden" (das PRODUKT existiert
    bei Pionex uebergreifend), waehrend der tatsaechliche Verbindungsweg
    (sgr.exchanges.pionex.PionexAdapter) seit dem Fix in diesem Commit
    ueber einen ccxt-unabhaengigen Fallback (sgr.exchanges.pionex_client.
    PionexClient, nur oeffentliche Marktdaten) laeuft. Live-Order-
    Submission fuer Pionex bleibt bewusst nicht implementiert (siehe
    dortiger Modul-Docstring) - eine capability-Eintragung hier ersetzt
    NICHT die tatsaechliche Implementierung des Adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from sgr.core.types import ExchangeID, ProductType

# ---------------------------------------------------------------------------
# Capability descriptor
# ---------------------------------------------------------------------------


class CapabilityStatus(StrEnum):
    """Ergebnis-Status einer Capability-Pruefung - siehe check_capability()."""

    OK = "ok"
    EXCHANGE_CAPABILITY_MISSING = "exchange_capability_missing"


@dataclass(frozen=True)
class ExchangeCapability:
    """Statische Produkt-Faehigkeiten einer (Exchange, ProductType)-Kombination."""

    exchange: ExchangeID
    product_type: ProductType

    supports_long: bool
    supports_short: bool
    supports_leverage: bool
    supports_grid: bool
    supports_futures_grid: bool
    supports_reduce_only: bool
    supports_hedge_mode: bool
    supports_one_way_mode: bool
    supports_funding: bool
    supports_conditional_orders: bool
    supports_position_mode: bool

    max_leverage: Decimal | None = None
    notes: str = ""


@dataclass(frozen=True)
class CapabilityCheckResult:
    """Ergebnis von check_capability() - siehe dort."""

    status: CapabilityStatus
    capability: ExchangeCapability | None
    reason: str

    @property
    def ok(self) -> bool:
        return self.status == CapabilityStatus.OK


# ---------------------------------------------------------------------------
# Registry (static)
# ---------------------------------------------------------------------------

_CAPABILITIES: dict[tuple[ExchangeID, ProductType], ExchangeCapability] = {
    (ExchangeID.BINANCE, ProductType.SPOT): ExchangeCapability(
        exchange=ExchangeID.BINANCE,
        product_type=ProductType.SPOT,
        supports_long=True,
        supports_short=False,
        supports_leverage=False,
        supports_grid=True,
        supports_futures_grid=False,
        supports_reduce_only=False,
        supports_hedge_mode=False,
        supports_one_way_mode=True,
        supports_funding=False,
        supports_conditional_orders=True,
        supports_position_mode=False,
        notes="Bestehende, produktiv genutzte Spot-Integration - unveraendert.",
    ),
    (ExchangeID.BINANCE, ProductType.PERPETUAL): ExchangeCapability(
        exchange=ExchangeID.BINANCE,
        product_type=ProductType.PERPETUAL,
        supports_long=True,
        supports_short=True,
        supports_leverage=True,
        supports_grid=True,
        supports_futures_grid=True,
        supports_reduce_only=True,
        supports_hedge_mode=True,
        supports_one_way_mode=True,
        supports_funding=True,
        supports_conditional_orders=True,
        supports_position_mode=True,
        max_leverage=Decimal("125"),
        notes=(
            "Bestehende UM-Futures-Integration (BinanceAdapter futures_mode=True). "
            "Futures Grid technisch moeglich, aber gemaess Aufgabenstellung als "
            "strategische Produktklasse bevorzugt Pionex zugeordnet - siehe "
            "Strategy-Configuration/Capital-Allocation, nicht hier hart kodiert."
        ),
    ),
    (ExchangeID.BINANCE, ProductType.FUTURES_GRID): ExchangeCapability(
        exchange=ExchangeID.BINANCE,
        product_type=ProductType.FUTURES_GRID,
        supports_long=True,
        supports_short=True,
        supports_leverage=True,
        supports_grid=True,
        supports_futures_grid=True,
        supports_reduce_only=True,
        supports_hedge_mode=True,
        supports_one_way_mode=True,
        supports_funding=True,
        supports_conditional_orders=True,
        supports_position_mode=True,
        max_leverage=Decimal("125"),
        notes=(
            "Technisch vollstaendig unterstuetzt (wie PIONEX/FUTURES_GRID). Die "
            "Aufgabenstellung ordnet Futures Grid strategisch bevorzugt Pionex zu - "
            "das ist eine Strategy-Configuration/Capital-Allocation-Entscheidung "
            "(siehe sgr.strategy.capital_allocation), KEINE technische Sperre hier."
        ),
    ),
    (ExchangeID.PIONEX, ProductType.SPOT): ExchangeCapability(
        exchange=ExchangeID.PIONEX,
        product_type=ProductType.SPOT,
        supports_long=True,
        supports_short=False,
        supports_leverage=False,
        supports_grid=True,
        supports_futures_grid=False,
        supports_reduce_only=False,
        supports_hedge_mode=False,
        supports_one_way_mode=True,
        supports_funding=False,
        supports_conditional_orders=False,
        supports_position_mode=False,
        notes="Bestehende Spot-Integration (Marktdaten ueber PionexClient-Fallback).",
    ),
    (ExchangeID.PIONEX, ProductType.PERPETUAL): ExchangeCapability(
        exchange=ExchangeID.PIONEX,
        product_type=ProductType.PERPETUAL,
        supports_long=True,
        supports_short=True,
        supports_leverage=True,
        supports_grid=True,
        supports_futures_grid=True,
        supports_reduce_only=True,
        supports_hedge_mode=False,
        supports_one_way_mode=True,
        supports_funding=True,
        supports_conditional_orders=False,
        supports_position_mode=False,
        max_leverage=Decimal("20"),
        notes=(
            "Produktseitig als First-Class-Ziel fuer Futures Grid vorgesehen. "
            "LIVE-Order-Submission ist AKTUELL NICHT implementiert (siehe "
            "sgr.exchanges.pionex Modul-Docstring - ccxt fuehrt keine Pionex-"
            "Exchange-ID mehr, ein verifizierter, signierter Private-REST-Client "
            "fehlt noch). PAPER Trading funktioniert vollstaendig (oeffentliche "
            "Marktdaten via PionexClient + lokale Order-Simulation)."
        ),
    ),
    (ExchangeID.PIONEX, ProductType.FUTURES_GRID): ExchangeCapability(
        exchange=ExchangeID.PIONEX,
        product_type=ProductType.FUTURES_GRID,
        supports_long=True,
        supports_short=True,
        supports_leverage=True,
        supports_grid=True,
        supports_futures_grid=True,
        supports_reduce_only=True,
        supports_hedge_mode=False,
        supports_one_way_mode=True,
        supports_funding=True,
        supports_conditional_orders=False,
        supports_position_mode=False,
        max_leverage=Decimal("20"),
        notes="Wie PIONEX/PERPETUAL - Futures Grid nutzt denselben Markt, nur SGR-eigene Orders.",
    ),
}


def get_capability(exchange: ExchangeID, product_type: ProductType) -> ExchangeCapability | None:
    """Gibt die statische Capability zurueck, oder None wenn diese
    Kombination nicht existiert/nicht unterstuetzt wird."""
    return _CAPABILITIES.get((exchange, product_type))


def check_capability(
    exchange: ExchangeID,
    product_type: ProductType,
    *,
    requires_long: bool = False,
    requires_short: bool = False,
    requires_leverage: bool = False,
) -> CapabilityCheckResult:
    """
    Prueft eine konkrete Anforderung gegen die Capability-Tabelle.

    Muss von JEDER Stelle aufgerufen werden, die eine Order fuer ein neues
    Produkt (insbesondere FUTURES_GRID) bauen will, BEVOR ein
    OrderRequest erzeugt wird - siehe sgr.execution.grid_controller.
    GridController.open_grid(). Eine Strategie darf niemals implizit
    davon ausgehen, dass eine Exchange eine Funktion unterstuetzt.
    """
    capability = get_capability(exchange, product_type)
    if capability is None:
        return CapabilityCheckResult(
            status=CapabilityStatus.EXCHANGE_CAPABILITY_MISSING,
            capability=None,
            reason=f"{exchange.value} bietet ProductType {product_type.value} nicht an.",
        )

    missing: list[str] = []
    if requires_long and not capability.supports_long:
        missing.append("long")
    if requires_short and not capability.supports_short:
        missing.append("short")
    if requires_leverage and not capability.supports_leverage:
        missing.append("leverage")

    if missing:
        return CapabilityCheckResult(
            status=CapabilityStatus.EXCHANGE_CAPABILITY_MISSING,
            capability=capability,
            reason=(
                f"{exchange.value}/{product_type.value} unterstuetzt nicht: "
                f"{', '.join(missing)}"
            ),
        )

    return CapabilityCheckResult(status=CapabilityStatus.OK, capability=capability, reason="")


def list_capabilities() -> list[ExchangeCapability]:
    """Alle registrierten Capabilities - fuer Diagnose-Endpunkte/Tests."""
    return list(_CAPABILITIES.values())
