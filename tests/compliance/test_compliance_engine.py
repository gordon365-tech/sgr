"""Tests für sgr.compliance.engine.ComplianceEngine.

Deckt die zentrale Aufgabenstellungs-Anforderung ab: Pionex Futures ist
NICHT automatisch fuer einen deutschen Retailkunden freigegeben - jede
Kombination erfordert eine explizite ProductAvailabilityRule UND eine
vollstaendige AccountEligibility.
"""

from __future__ import annotations

from sgr.compliance.engine import ComplianceEngine
from sgr.compliance.types import AccountEligibility, ComplianceStatus, ProductAvailabilityRule
from sgr.core.types import ExchangeID, ProductType


def _eligible_account(**overrides) -> AccountEligibility:
    base = dict(
        tenant_id="gordon",
        jurisdiction="DE",
        kyc_verified=True,
        futures_trading_enabled=True,
        risk_disclosure_acknowledged=True,
        enabled_product_types=["spot", "futures_grid"],
    )
    base.update(overrides)
    return AccountEligibility(**base)


class TestDenyByDefault:
    def test_no_rule_registered_blocks_futures_grid(self) -> None:
        """Kernanforderung: OHNE explizite Operator-Freigabe ist Futures
        Grid fuer NIEMANDEN verfuegbar, auch nicht fuer einen vollstaendig
        KYC-verifizierten Account."""
        engine = ComplianceEngine()
        result = engine.check(_eligible_account(), ExchangeID.PIONEX, ProductType.FUTURES_GRID)

        assert not result.allowed
        assert result.status == ComplianceStatus.PRODUCT_NOT_AVAILABLE

    def test_german_retail_not_automatically_assumed_legal(self) -> None:
        """Explizit die im Auftrag genannte Annahme, die SGR NICHT
        treffen darf: ein deutscher Retail-Account erhaelt ohne Regel
        KEINE automatische Freigabe."""
        engine = ComplianceEngine()
        account = _eligible_account(jurisdiction="DE", account_type="retail")

        result = engine.check(account, ExchangeID.PIONEX, ProductType.FUTURES_GRID)

        assert not result.allowed
        assert result.status != ComplianceStatus.ELIGIBLE


class TestExchangeCapabilityMissing:
    def test_unsupported_exchange_product_combination(self) -> None:
        engine = ComplianceEngine()
        engine.register_rule(
            ProductAvailabilityRule(
                exchange=ExchangeID.KRAKEN,
                product_type=ProductType.FUTURES_GRID,
                jurisdictions_allowed=["DE"],
            )
        )
        result = engine.check(_eligible_account(), ExchangeID.KRAKEN, ProductType.FUTURES_GRID)

        assert result.status == ComplianceStatus.EXCHANGE_CAPABILITY_MISSING


class TestJurisdictionRestricted:
    def setup_method(self) -> None:
        self.engine = ComplianceEngine()
        self.engine.register_rule(
            ProductAvailabilityRule(
                exchange=ExchangeID.PIONEX,
                product_type=ProductType.FUTURES_GRID,
                jurisdictions_allowed=["SG", "US"],
            )
        )

    def test_jurisdiction_not_in_allowlist_is_restricted(self) -> None:
        account = _eligible_account(jurisdiction="DE")
        result = self.engine.check(account, ExchangeID.PIONEX, ProductType.FUTURES_GRID)

        assert not result.allowed
        assert result.status == ComplianceStatus.JURISDICTION_RESTRICTED

    def test_unknown_jurisdiction_requires_compliance_check(self) -> None:
        """None (nicht erfasst) ist NICHT gleichbedeutend mit 'ueberall
        erlaubt' - es loest eine explizite COMPLIANCE_CHECK_REQUIRED aus."""
        account = _eligible_account(jurisdiction=None)
        result = self.engine.check(account, ExchangeID.PIONEX, ProductType.FUTURES_GRID)

        assert not result.allowed
        assert result.status == ComplianceStatus.COMPLIANCE_CHECK_REQUIRED

    def test_allowed_jurisdiction_passes_this_gate(self) -> None:
        account = _eligible_account(jurisdiction="SG")
        result = self.engine.check(account, ExchangeID.PIONEX, ProductType.FUTURES_GRID)

        assert result.allowed
        assert result.status == ComplianceStatus.ELIGIBLE


class TestAccountNotEligible:
    def setup_method(self) -> None:
        self.engine = ComplianceEngine()
        self.engine.register_rule(
            ProductAvailabilityRule(
                exchange=ExchangeID.PIONEX,
                product_type=ProductType.FUTURES_GRID,
                jurisdictions_allowed=["DE"],
            )
        )

    def test_kyc_not_verified_blocks(self) -> None:
        account = _eligible_account(kyc_verified=False)
        result = self.engine.check(account, ExchangeID.PIONEX, ProductType.FUTURES_GRID)

        assert result.status == ComplianceStatus.ACCOUNT_NOT_ELIGIBLE
        assert "KYC" in result.reason

    def test_futures_trading_not_enabled_blocks(self) -> None:
        account = _eligible_account(futures_trading_enabled=False)
        result = self.engine.check(account, ExchangeID.PIONEX, ProductType.FUTURES_GRID)

        assert result.status == ComplianceStatus.ACCOUNT_NOT_ELIGIBLE

    def test_missing_risk_disclosure_blocks(self) -> None:
        account = _eligible_account(risk_disclosure_acknowledged=False)
        result = self.engine.check(account, ExchangeID.PIONEX, ProductType.FUTURES_GRID)

        assert result.status == ComplianceStatus.ACCOUNT_NOT_ELIGIBLE

    def test_product_type_not_in_enabled_list_blocks(self) -> None:
        account = _eligible_account(enabled_product_types=["spot"])
        result = self.engine.check(account, ExchangeID.PIONEX, ProductType.FUTURES_GRID)

        assert result.status == ComplianceStatus.ACCOUNT_NOT_ELIGIBLE

    def test_manual_approval_required_without_disclosure(self) -> None:
        self.engine.register_rule(
            ProductAvailabilityRule(
                exchange=ExchangeID.PIONEX,
                product_type=ProductType.FUTURES_GRID,
                jurisdictions_allowed=["DE"],
                requires_manual_approval=True,
            )
        )
        account = _eligible_account(risk_disclosure_acknowledged=False)
        result = self.engine.check(account, ExchangeID.PIONEX, ProductType.FUTURES_GRID)

        assert result.status == ComplianceStatus.COMPLIANCE_CHECK_REQUIRED


class TestFullyEligible:
    def test_fully_configured_account_is_eligible(self) -> None:
        engine = ComplianceEngine()
        engine.register_rule(
            ProductAvailabilityRule(
                exchange=ExchangeID.PIONEX,
                product_type=ProductType.FUTURES_GRID,
                jurisdictions_allowed=["DE"],
            )
        )
        result = engine.check(_eligible_account(), ExchangeID.PIONEX, ProductType.FUTURES_GRID)

        assert result.allowed
        assert result.status == ComplianceStatus.ELIGIBLE

    def test_spot_trading_unaffected_by_missing_futures_rules(self) -> None:
        """Binance/Pionex SPOT bleibt vollstaendig funktionsfaehig, auch
        ohne jegliche Futures-Grid-Regel - keine Regression fuer
        bestehenden Spot-Handel."""
        engine = ComplianceEngine()
        engine.register_rule(
            ProductAvailabilityRule(
                exchange=ExchangeID.BINANCE,
                product_type=ProductType.SPOT,
                jurisdictions_allowed=None,
            )
        )
        account = _eligible_account(
            kyc_verified=True, enabled_product_types=["spot"], futures_trading_enabled=False
        )
        result = engine.check(account, ExchangeID.BINANCE, ProductType.SPOT)

        assert result.allowed
