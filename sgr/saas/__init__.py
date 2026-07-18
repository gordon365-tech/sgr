"""SGR SaaS Layer"""

from sgr.saas.auth import AuthService
from sgr.saas.fees import PerformanceFeeEngine
from sgr.saas.routers import apikey_router, auth_router, billing_router
from sgr.saas.tenant import TenantManager, TenantSession, get_tenant_manager
from sgr.saas.types import (
    BillingStatus,
    FeeStatus,
    HighWaterMark,
    Invoice,
    PerformanceFeeCalculation,
    PortfolioSnapshot,
    SubscriptionTier,
    TenantConfig,
)

__all__ = [
    "AuthService",
    "PerformanceFeeEngine",
    "TenantManager",
    "TenantSession",
    "get_tenant_manager",
    "SubscriptionTier",
    "BillingStatus",
    "FeeStatus",
    "HighWaterMark",
    "PerformanceFeeCalculation",
    "TenantConfig",
    "PortfolioSnapshot",
    "Invoice",
    "auth_router",
    "apikey_router",
    "billing_router",
]
