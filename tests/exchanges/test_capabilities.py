"""Tests für sgr.exchanges.capabilities (Exchange/Product Capability Layer)."""

from __future__ import annotations

from sgr.core.types import ExchangeID, ProductType
from sgr.exchanges.capabilities import (
    CapabilityStatus,
    check_capability,
    get_capability,
    list_capabilities,
)


class TestGetCapability:
    def test_binance_spot_capability_exists(self) -> None:
        cap = get_capability(ExchangeID.BINANCE, ProductType.SPOT)
        assert cap is not None
        assert cap.supports_long is True
        assert cap.supports_short is False
        assert cap.supports_leverage is False

    def test_binance_perpetual_supports_leverage_and_short(self) -> None:
        cap = get_capability(ExchangeID.BINANCE, ProductType.PERPETUAL)
        assert cap is not None
        assert cap.supports_leverage is True
        assert cap.supports_short is True
        assert cap.supports_futures_grid is True

    def test_pionex_futures_grid_capability_exists(self) -> None:
        cap = get_capability(ExchangeID.PIONEX, ProductType.FUTURES_GRID)
        assert cap is not None
        assert cap.supports_futures_grid is True
        assert cap.supports_long is True
        assert cap.supports_short is True

    def test_binance_futures_grid_also_technically_supported(self) -> None:
        """Futures Grid ist strategisch Pionex zugeordnet, aber technisch
        auf Binance NICHT gesperrt (siehe Aufgabenstellung: Zuordnung
        darf nicht hart codiert werden)."""
        cap = get_capability(ExchangeID.BINANCE, ProductType.FUTURES_GRID)
        assert cap is not None
        assert cap.supports_futures_grid is True

    def test_pionex_spot_does_not_support_leverage(self) -> None:
        cap = get_capability(ExchangeID.PIONEX, ProductType.SPOT)
        assert cap is not None
        assert cap.supports_leverage is False
        assert cap.supports_short is False

    def test_unknown_combination_returns_none(self) -> None:
        assert get_capability(ExchangeID.KRAKEN, ProductType.FUTURES_GRID) is None


class TestCheckCapability:
    def test_missing_combination_is_capability_missing(self) -> None:
        result = check_capability(ExchangeID.KRAKEN, ProductType.FUTURES_GRID)
        assert result.status == CapabilityStatus.EXCHANGE_CAPABILITY_MISSING
        assert not result.ok

    def test_pionex_spot_rejects_short_requirement(self) -> None:
        result = check_capability(ExchangeID.PIONEX, ProductType.SPOT, requires_short=True)
        assert not result.ok
        assert "short" in result.reason

    def test_pionex_spot_rejects_leverage_requirement(self) -> None:
        result = check_capability(ExchangeID.PIONEX, ProductType.SPOT, requires_leverage=True)
        assert not result.ok

    def test_pionex_futures_grid_accepts_long_short_and_leverage(self) -> None:
        result = check_capability(
            ExchangeID.PIONEX,
            ProductType.FUTURES_GRID,
            requires_long=True,
            requires_short=True,
            requires_leverage=True,
        )
        assert result.ok
        assert result.status == CapabilityStatus.OK

    def test_binance_spot_accepts_plain_long_order(self) -> None:
        """Bestehende Binance-Spot-Nutzung bleibt unveraendert moeglich."""
        result = check_capability(ExchangeID.BINANCE, ProductType.SPOT, requires_long=True)
        assert result.ok


class TestListCapabilities:
    def test_list_contains_all_registered_combinations(self) -> None:
        caps = list_capabilities()
        pairs = {(c.exchange, c.product_type) for c in caps}
        assert (ExchangeID.BINANCE, ProductType.SPOT) in pairs
        assert (ExchangeID.BINANCE, ProductType.PERPETUAL) in pairs
        assert (ExchangeID.BINANCE, ProductType.FUTURES_GRID) in pairs
        assert (ExchangeID.PIONEX, ProductType.SPOT) in pairs
        assert (ExchangeID.PIONEX, ProductType.PERPETUAL) in pairs
        assert (ExchangeID.PIONEX, ProductType.FUTURES_GRID) in pairs
