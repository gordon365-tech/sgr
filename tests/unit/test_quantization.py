"""
Tests fuer sgr.execution.quantization (quantize_and_validate + quantize_price).

quantize_price() ist neu (2026-09-23, Architekturbericht "Futures-Grid-
Strategie fuer SGR", Phase E: Preis-Quantisierung auf Tick-Size fehlte im
gesamten Ausfuehrungspfad - quantize_and_validate() rundet ausschliesslich
quantity, niemals price). Deckt GATE 5 ab: mindestens fuenf Symbole mit
unterschiedlichen Tick-Size-Groessenordnungen, inklusive eines
Mikropreis-Symbols (der bekannte round(target_price, 2)-Bug), ohne dass
ein Preis auf 0 rundet oder eine ungueltige Binance-Preispraezision
entsteht.
"""

from __future__ import annotations

from decimal import Decimal

from sgr.core.types import Side
from sgr.exchanges.base import SymbolLimits
from sgr.execution.quantization import quantize_and_validate, quantize_price


def _limits(
    price_precision: int | None = None,
    min_price: Decimal | None = None,
    max_price: Decimal | None = None,
    amount_precision: int | None = None,
    min_amount: Decimal | None = None,
    min_notional: Decimal | None = None,
) -> SymbolLimits:
    return SymbolLimits(
        amount_precision=amount_precision,
        price_precision=price_precision,
        min_amount=min_amount,
        min_notional=min_notional,
        min_price=min_price,
        max_price=max_price,
    )


# ---------------------------------------------------------------------------
# quantize_and_validate (bestehend, Regressions-Basisabdeckung)
# ---------------------------------------------------------------------------


class TestQuantizeAndValidate:
    def test_none_limits_passthrough(self) -> None:
        qty, reason = quantize_and_validate(Decimal("1.23456"), Decimal("100"), None)
        assert qty == Decimal("1.23456")
        assert reason is None

    def test_rounds_down_never_up(self) -> None:
        qty, reason = quantize_and_validate(
            Decimal("1.239"), Decimal("100"), _limits(amount_precision=2)
        )
        assert qty == Decimal("1.23")
        assert reason is None

    def test_rejects_when_rounds_to_zero(self) -> None:
        qty, reason = quantize_and_validate(
            Decimal("0.004"), Decimal("100"), _limits(amount_precision=2)
        )
        assert qty == Decimal("0")
        assert reason is not None


# ---------------------------------------------------------------------------
# quantize_price - GATE 5: mindestens 5 Symbole, verschiedene Tick-Groessen
# ---------------------------------------------------------------------------


class TestQuantizePriceTickSizes:
    def test_none_limits_passthrough(self) -> None:
        price, reason = quantize_price(Decimal("65432.567"), Side.BUY, None)
        assert price == Decimal("65432.567")
        assert reason is None

    def test_btc_usdt_tick_0_1_buy_rounds_down(self) -> None:
        """BTC/USDT: realistische Binance-Futures-Tick-Size 0.10 (precision=1)."""
        price, reason = quantize_price(Decimal("65432.567"), Side.BUY, _limits(price_precision=1))
        assert reason is None
        assert price == Decimal("65432.5")

    def test_btc_usdt_tick_0_1_sell_rounds_up(self) -> None:
        price, reason = quantize_price(Decimal("65432.567"), Side.SELL, _limits(price_precision=1))
        assert reason is None
        assert price == Decimal("65432.6")

    def test_eth_usdt_tick_0_01(self) -> None:
        """ETH/USDT: Tick-Size 0.01 (precision=2)."""
        buy, _ = quantize_price(Decimal("3456.789"), Side.BUY, _limits(price_precision=2))
        sell, _ = quantize_price(Decimal("3456.789"), Side.SELL, _limits(price_precision=2))
        assert buy == Decimal("3456.78")
        assert sell == Decimal("3456.79")

    def test_mid_cap_tick_0_001(self) -> None:
        """Mid-Cap-Symbol (z.B. BNB/USDT-Groessenordnung): Tick 0.001 (precision=3)."""
        buy, _ = quantize_price(Decimal("12.3456"), Side.BUY, _limits(price_precision=3))
        sell, _ = quantize_price(Decimal("12.3456"), Side.SELL, _limits(price_precision=3))
        assert buy == Decimal("12.345")
        assert sell == Decimal("12.346")

    def test_low_price_tick_0_0001(self) -> None:
        """Low-Price-Symbol (z.B. ADA/USDT-Groessenordnung): Tick 0.0001 (precision=4)."""
        buy, _ = quantize_price(Decimal("1.23456"), Side.BUY, _limits(price_precision=4))
        sell, _ = quantize_price(Decimal("1.23456"), Side.SELL, _limits(price_precision=4))
        assert buy == Decimal("1.2345")
        assert sell == Decimal("1.2346")

    def test_micro_price_symbol_dent_like_does_not_round_to_zero(self) -> None:
        """
        GATE 5 / bekannter Bug: mean_reversion_v1/breakout_v1 berechneten
        target_price/stop_price bisher mit round(x, 2) - bei einem
        Mikropreis-Symbol wie DENT (~0.0000447 USDT) rundete das auf
        exakt 0.0. quantize_price() mit der ECHTEN Exchange-Tick-Size
        (hier: DENT/USDT precision=8, min_price 1e-8, realistische
        Binance-Werte) darf diesen Preis NICHT auf 0 runden.
        """
        limits = _limits(price_precision=8, min_price=Decimal("0.00000010"))
        price, reason = quantize_price(Decimal("0.0000447"), Side.BUY, limits)
        assert reason is None
        assert price > 0
        assert price == Decimal("0.00004470")
        assert price >= limits.min_price

    def test_price_rounding_to_zero_is_rejected_not_silently_sent(self) -> None:
        """Ein Preis, der TATSAECHLICH unterhalb der kleinsten darstellbaren
        Tick-Size liegt, wird abgelehnt (reason gesetzt), nicht als 0
        durchgereicht - Aufrufer darf niemals eine Order mit Preis 0
        senden."""
        price, reason = quantize_price(Decimal("0.000000001"), Side.BUY, _limits(price_precision=8))
        assert price == Decimal("0")
        assert reason is not None

    def test_min_price_violation_rejected(self) -> None:
        price, reason = quantize_price(
            Decimal("0.5"),
            Side.BUY,
            _limits(price_precision=2, min_price=Decimal("1.0")),
        )
        assert price == Decimal("0")
        assert reason is not None
        assert "minimum" in reason

    def test_max_price_violation_rejected(self) -> None:
        price, reason = quantize_price(
            Decimal("999999"),
            Side.SELL,
            _limits(price_precision=0, max_price=Decimal("100000")),
        )
        assert price == Decimal("0")
        assert reason is not None
        assert "maximum" in reason

    def test_price_already_on_tick_unchanged(self) -> None:
        price, reason = quantize_price(Decimal("100.50"), Side.BUY, _limits(price_precision=2))
        assert reason is None
        assert price == Decimal("100.50")

    def test_long_and_short_rounding_direction_differs(self) -> None:
        """Long (BUY) und Short (SELL) runden nachweislich in
        unterschiedliche, jeweils konservative Richtungen - identischer
        Ausgangspreis, unterschiedliches Ergebnis."""
        limits = _limits(price_precision=1)
        buy_price, _ = quantize_price(Decimal("100.27"), Side.BUY, limits)
        sell_price, _ = quantize_price(Decimal("100.27"), Side.SELL, limits)
        assert buy_price == Decimal("100.2")
        assert sell_price == Decimal("100.3")
        assert buy_price < sell_price
