"""
SGR Compliance Engine
=======================
Prueft VOR jedem Futures-Grid-Order-Pfad (siehe
sgr.execution.grid_controller.GridController.open_grid()), ob ein
Tenant fuer eine (Exchange, ProductType)-Kombination handeln darf.

Deny-by-default (siehe Aufgabenstellung: "Programmiere KEINE Annahme
ein, dass Pionex Futures fuer einen deutschen Retailkunden automatisch
legal oder verfuegbar sind"):
    - Ohne eine vom Operator explizit gepflegte ProductAvailabilityRule
      ist ein Produkt NICHT verfuegbar (PRODUCT_NOT_AVAILABLE).
    - Ohne bekannte Jurisdiktion des Tenants ist das Ergebnis
      COMPLIANCE_CHECK_REQUIRED, niemals stillschweigend ELIGIBLE.
    - Ohne KYC/futures_trading_enabled/Risk-Disclosure-Bestaetigung ist
      das Ergebnis ACCOUNT_NOT_ELIGIBLE.

Reihenfolge der Pruefungen (erste zutreffende gewinnt):
    1. Exchange-Capability (technisch) - siehe sgr.exchanges.capabilities
    2. Produkt-Verfuegbarkeit (regulatorisch, Exchange/Jurisdiktions-Regel)
    3. Account-Eligibility (KYC, Kontotyp, explizite Freigabe)
"""

from __future__ import annotations

from sgr.compliance.types import (
    AccountEligibility,
    ComplianceCheckResult,
    ComplianceStatus,
    ProductAvailabilityRule,
)
from sgr.core.logging import get_logger
from sgr.core.types import ExchangeID, ProductType
from sgr.exchanges.capabilities import check_capability

log = get_logger(__name__)


class ComplianceEngine:
    """
    Haelt die Operator-gepflegten ProductAvailabilityRules. In-Memory,
    analog zu sgr.risk.symbol_kill_switch - fuer Produktionsbetrieb
    idealerweise aus Tenant-Konfiguration/DB geladen (siehe "offene
    Punkte" im Strategiebericht); die Schnittstelle (register_rule) macht
    das nachtraeglich ohne Aufrufer-Aenderung moeglich.
    """

    def __init__(self) -> None:
        self._rules: dict[tuple[ExchangeID, ProductType], ProductAvailabilityRule] = {}

    def register_rule(self, rule: ProductAvailabilityRule) -> None:
        """Vom Operator/Deployment explizit aufzurufen, um ein Produkt
        fuer bestimmte Jurisdiktionen freizugeben. OHNE diesen Aufruf
        bleibt ein Produkt fuer ALLE Jurisdiktionen PRODUCT_NOT_AVAILABLE
        (Deny-by-default)."""
        self._rules[(rule.exchange, rule.product_type)] = rule
        log.info(
            "compliance.rule_registered",
            exchange=rule.exchange.value,
            product_type=rule.product_type.value,
            jurisdictions=rule.jurisdictions_allowed,
        )

    def check(
        self,
        account: AccountEligibility,
        exchange: ExchangeID,
        product_type: ProductType,
        *,
        requires_long: bool = False,
        requires_short: bool = False,
        requires_leverage: bool = False,
    ) -> ComplianceCheckResult:
        # 1. Technische Capability
        cap_result = check_capability(
            exchange,
            product_type,
            requires_long=requires_long,
            requires_short=requires_short,
            requires_leverage=requires_leverage,
        )
        if not cap_result.ok:
            return ComplianceCheckResult(
                status=ComplianceStatus.EXCHANGE_CAPABILITY_MISSING,
                allowed=False,
                reason=cap_result.reason,
                exchange=exchange,
                product_type=product_type,
            )

        # 2. Produkt-Verfuegbarkeit (regulatorisch)
        rule = self._rules.get((exchange, product_type))
        if rule is None:
            return ComplianceCheckResult(
                status=ComplianceStatus.PRODUCT_NOT_AVAILABLE,
                allowed=False,
                reason=(
                    f"Keine ProductAvailabilityRule fuer {exchange.value}/"
                    f"{product_type.value} registriert - Deny-by-default. SGR trifft "
                    f"keine Annahme ueber regulatorische Zulaessigkeit."
                ),
                exchange=exchange,
                product_type=product_type,
            )

        if rule.jurisdictions_allowed is not None:
            if account.jurisdiction is None:
                return ComplianceCheckResult(
                    status=ComplianceStatus.COMPLIANCE_CHECK_REQUIRED,
                    allowed=False,
                    reason=(
                        "Jurisdiktion des Tenants ist nicht erfasst - Freigabe kann "
                        "nicht geprueft werden (keine implizite Freigabe)."
                    ),
                    exchange=exchange,
                    product_type=product_type,
                )
            if account.jurisdiction not in rule.jurisdictions_allowed:
                return ComplianceCheckResult(
                    status=ComplianceStatus.JURISDICTION_RESTRICTED,
                    allowed=False,
                    reason=(
                        f"{product_type.value} auf {exchange.value} ist fuer Jurisdiktion "
                        f"{account.jurisdiction} nicht freigegeben."
                    ),
                    exchange=exchange,
                    product_type=product_type,
                )

        if rule.requires_manual_approval and not account.risk_disclosure_acknowledged:
            return ComplianceCheckResult(
                status=ComplianceStatus.COMPLIANCE_CHECK_REQUIRED,
                allowed=False,
                reason=(
                    f"{product_type.value} auf {exchange.value} erfordert eine manuelle "
                    f"Freigabe/Risikoaufklaerungs-Bestaetigung, die fuer diesen Tenant "
                    f"noch nicht vorliegt."
                ),
                exchange=exchange,
                product_type=product_type,
            )

        # 3. Account-Eligibility
        if not account.kyc_verified:
            return ComplianceCheckResult(
                status=ComplianceStatus.ACCOUNT_NOT_ELIGIBLE,
                allowed=False,
                reason="KYC nicht verifiziert.",
                exchange=exchange,
                product_type=product_type,
            )

        if product_type in (ProductType.PERPETUAL, ProductType.FUTURES_GRID):
            if not account.futures_trading_enabled:
                return ComplianceCheckResult(
                    status=ComplianceStatus.ACCOUNT_NOT_ELIGIBLE,
                    allowed=False,
                    reason=(
                        "Futures-Trading ist fuer diesen Tenant nicht explizit "
                        "aktiviert (futures_trading_enabled=False)."
                    ),
                    exchange=exchange,
                    product_type=product_type,
                )
            if not account.risk_disclosure_acknowledged:
                return ComplianceCheckResult(
                    status=ComplianceStatus.ACCOUNT_NOT_ELIGIBLE,
                    allowed=False,
                    reason="Risikoaufklaerung fuer gehebelte Produkte nicht bestaetigt.",
                    exchange=exchange,
                    product_type=product_type,
                )

        if product_type.value not in account.enabled_product_types:
            return ComplianceCheckResult(
                status=ComplianceStatus.ACCOUNT_NOT_ELIGIBLE,
                allowed=False,
                reason=f"ProductType {product_type.value} ist fuer diesen Tenant nicht aktiviert.",
                exchange=exchange,
                product_type=product_type,
            )

        return ComplianceCheckResult(
            status=ComplianceStatus.ELIGIBLE,
            allowed=True,
            reason="",
            exchange=exchange,
            product_type=product_type,
        )


_engine: ComplianceEngine | None = None


def get_compliance_engine() -> ComplianceEngine:
    global _engine
    if _engine is None:
        _engine = ComplianceEngine()
    return _engine
