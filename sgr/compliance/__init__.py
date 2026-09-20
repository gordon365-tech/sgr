"""SGR Compliance & Product Availability Layer.

Trennt zwei unabhaengige Fragen (siehe Aufgabenstellung):
    1. Technische Verfuegbarkeit: kann diese Exchange dieses Produkt
       ueberhaupt (siehe sgr.exchanges.capabilities)?
    2. Regulatorische Zulaessigkeit: darf DIESER Tenant/Account in
       SEINER Jurisdiktion dieses Produkt handeln?

SGR trifft KEINE Annahme, dass ein Produkt (insbesondere Futures/Futures
Grid) fuer einen Tenant automatisch legal oder verfuegbar ist. Der
Default ist "nicht freigegeben", solange kein Operator/Tenant es
explizit konfiguriert (Deny-by-default, siehe engine.py).
"""

from sgr.compliance.engine import ComplianceEngine, get_compliance_engine
from sgr.compliance.types import (
    AccountEligibility,
    ComplianceCheckResult,
    ComplianceStatus,
    ProductAvailabilityRule,
)

__all__ = [
    "ComplianceEngine",
    "get_compliance_engine",
    "AccountEligibility",
    "ComplianceCheckResult",
    "ComplianceStatus",
    "ProductAvailabilityRule",
]
