"""
Tests für sgr.strategy.registry.StrategyRegistry Tenant-Scoping.

BUG-FIX-Kontext (Produktions-Audit): StrategyRegistry._entries war zuvor
ein KLASSEN-Attribut, nicht in __init__ gesetzt - jede direkt konstruierte
Instanz haette denselben Dict geteilt. Diese Tests decken die instance-
level Korrektur sowie das neu hinzugefuegte, KillSwitch-analoge
Tenant-Scoping ab (get_strategy_registry(tenant_id=...)), ohne das
bestehende Verhalten von StrategyRegistry.get() (Default-Tenant-Singleton,
das @register befuellt) zu veraendern - siehe Modul-Docstring in
sgr/strategy/registry.py für die Begründung der Scoping-Grenze.
"""

from __future__ import annotations

import pytest

from sgr.core.types import MarketRegime
from sgr.strategy import registry as registry_module
from sgr.strategy.registry import (
    StrategyRegistry,
    get_strategy_registry,
)

_TEST_TENANT_KEYS = ("test-tenant-a", "test-tenant-b")


@pytest.fixture(autouse=True)
def _cleanup_test_tenant_registries():
    """Entfernt NUR die synthetischen Test-Tenant-Keys aus dem modul-
    globalen Dict (niemals den None-Key - der haelt die per @register zur
    Modul-Importzeit registrierten echten Strategien fuer die gesamte
    restliche Testsession)."""
    yield
    for key in _TEST_TENANT_KEYS:
        registry_module._registries.pop(key, None)


class FakeStrategy:
    name = "fake_strategy"
    version = "1.0.0"
    supported_regimes = [MarketRegime.TRENDING_UP]

    def generate_signal(self, context):
        return None

    def get_parameters(self):
        from sgr.strategy.base import StrategyParameters

        return StrategyParameters(name=self.name, version=self.version, params={})

    def validate_context(self, context) -> bool:
        return True


class TestInstanceLevelEntries:
    def test_two_direct_instances_do_not_share_entries(self) -> None:
        """Kern des Bugs: _entries war ein Klassenattribut - zwei direkt
        konstruierte Instanzen haetten sich denselben Dict geteilt."""
        a = StrategyRegistry()
        b = StrategyRegistry()

        a.register_instance(FakeStrategy())

        assert a.get_entry("fake_strategy") is not None
        assert b.get_entry("fake_strategy") is None

    def test_clear_on_one_instance_does_not_affect_another(self) -> None:
        a = StrategyRegistry()
        b = StrategyRegistry()

        a.register_instance(FakeStrategy())
        b.register_instance(FakeStrategy())

        a.clear()

        assert a.get_entry("fake_strategy") is None
        assert b.get_entry("fake_strategy") is not None


class TestTenantScoping:
    def test_different_tenants_get_different_instances(self) -> None:
        a = get_strategy_registry(tenant_id="test-tenant-a")
        b = get_strategy_registry(tenant_id="test-tenant-b")
        assert a is not b

    def test_same_tenant_returns_same_instance(self) -> None:
        first = get_strategy_registry(tenant_id="test-tenant-a")
        second = get_strategy_registry(tenant_id="test-tenant-a")
        assert first is second

    def test_default_none_tenant_matches_classmethod_get(self) -> None:
        """StrategyRegistry.get() (verwendet von @register und allen
        bestehenden Call-Sites) muss weiterhin exakt dieselbe Instanz sein
        wie get_strategy_registry() ohne tenant_id - sonst wuerden
        registrierte Strategien fuer bestehenden Code unsichtbar."""
        assert StrategyRegistry.get() is get_strategy_registry(tenant_id=None)
        assert StrategyRegistry.get() is get_strategy_registry()

    def test_registration_on_one_tenant_does_not_leak_to_another(self) -> None:
        tenant_a = get_strategy_registry(tenant_id="test-tenant-a")
        tenant_b = get_strategy_registry(tenant_id="test-tenant-b")

        tenant_a.register_instance(FakeStrategy())

        assert tenant_a.get_entry("fake_strategy") is not None
        assert tenant_b.get_entry("fake_strategy") is None
