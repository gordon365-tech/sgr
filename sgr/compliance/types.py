"""SGR Compliance - Domain Types."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from sgr.core.types import ExchangeID, ProductType


class ComplianceStatus(StrEnum):
    """Eindeutiger Status-Code, den jede Compliance-Pruefung liefert -
    siehe Aufgabenstellung fuer die exakt geforderten Werte."""

    ELIGIBLE = "eligible"
    PRODUCT_NOT_AVAILABLE = "product_not_available"
    JURISDICTION_RESTRICTED = "jurisdiction_restricted"
    ACCOUNT_NOT_ELIGIBLE = "account_not_eligible"
    EXCHANGE_CAPABILITY_MISSING = "exchange_capability_missing"
    COMPLIANCE_CHECK_REQUIRED = "compliance_check_required"


class AccountEligibility(BaseModel):
    """
    Vom Tenant/Operator explizit gepflegte Zulassungs-Angaben - NIEMALS
    aus der IP-Adresse, dem Sprachcode des Browsers o.ae. abgeleitet
    (siehe Aufgabenstellung: "Keine Nutzung von VPN oder falscher
    Residency"). jurisdiction=None bedeutet "unbekannt/nicht erfasst" -
    das ist ein expliziter COMPLIANCE_CHECK_REQUIRED-Fall, kein impliziter
    Freibrief.
    """

    tenant_id: str
    jurisdiction: str | None = None  # ISO-3166-1 alpha-2, z.B. "DE"
    account_type: str = "retail"  # "retail" | "professional" | "institutional"
    kyc_verified: bool = False
    futures_trading_enabled: bool = False
    risk_disclosure_acknowledged: bool = False
    enabled_product_types: list[str] = Field(default_factory=lambda: [ProductType.SPOT.value])


class ProductAvailabilityRule(BaseModel):
    """
    Operator-gepflegte Regel: "wird ProductType X auf Exchange Y fuer
    Jurisdiktion Z ueberhaupt angeboten". jurisdictions_allowed=None
    bedeutet "ueberall" (nur fuer unkritische Produkte wie SPOT
    sinnvoll) - fuer PERPETUAL/FUTURES_GRID MUSS eine explizite Liste
    gepflegt werden (siehe ComplianceEngine._default_rules(): Default
    ist eine LEERE Liste = nirgendwo freigegeben, bis der Operator es
    aendert).
    """

    exchange: ExchangeID
    product_type: ProductType
    jurisdictions_allowed: list[str] | None = None
    requires_manual_approval: bool = False
    notes: str = ""


class ComplianceCheckResult(BaseModel):
    status: ComplianceStatus
    allowed: bool
    reason: str
    exchange: ExchangeID
    product_type: ProductType
