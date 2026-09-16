"""
SGR Order Quantity/Notional Quantization
==========================================
Zentrale, einmal implementierte Rundung/Validierung einer Order-Menge
gegen die von der Exchange fuer dieses Symbol gemeldeten Precision-/
Min-Notional-Regeln (SymbolLimits, siehe sgr/exchanges/base.py).

Warum hier und nicht in PositionSizer/RiskEngine?
    Exchange-Precision/Minimum-Order-Groesse ist eine EXECUTION-seitige
    Tatsache ("kann diese konkrete Order technisch so gesendet werden"),
    keine RISK-Entscheidung ("wie viel Risiko ist akzeptabel") - siehe
    Aufgabentrennung: Risk Engine entscheidet die maximal zulaessige
    Position, Execution Engine entscheidet die Ausfuehrung. Ein
    einziger Aufrufpunkt (ExecutionEngine.execute(), siehe dort) deckt
    dadurch PAPER und LIVE identisch ab, statt dieselbe Logik an
    mehreren Stellen zu duplizieren.

Regel: NIEMALS aufrunden. Reicht die auf Precision abgerundete Menge
nicht mehr fuer min_amount/min_notional, wird die Order abgelehnt
statt automatisch vergroessert - eine Vergroesserung wuerde das vom
Risk Profile definierte Risiko-Budget verletzen (siehe Aufgabenstellung:
"Keine automatische Aufrundung auf eine wesentlich groessere Position,
wenn dadurch das definierte Risk Profile verletzt wird").
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from sgr.exchanges.base import SymbolLimits


def quantize_and_validate(
    quantity: Decimal,
    price: Decimal,
    limits: SymbolLimits | None,
) -> tuple[Decimal, str | None]:
    """
    Rundet quantity auf limits.amount_precision ab (falls gesetzt) und
    prueft das Ergebnis gegen min_amount/max_amount/min_notional.

    Returns:
        (quantized_quantity, None) bei Erfolg.
        (Decimal("0"), reason) wenn die Order nach dem Abrunden nicht
        mehr die Exchange-Minimalgroesse erreicht - der Aufrufer lehnt
        die Order dann mit `reason` ab, statt sie zu senden.
    """
    if limits is None:
        return quantity, None

    quantized = quantity
    if limits.amount_precision is not None:
        step = Decimal("1").scaleb(-limits.amount_precision)
        quantized = quantity.quantize(step, rounding=ROUND_DOWN)

    if quantized <= 0:
        return Decimal("0"), (
            f"Quantity {quantity} rounds down to 0 at exchange precision "
            f"{limits.amount_precision}"
        )

    if limits.min_amount is not None and quantized < limits.min_amount:
        return Decimal("0"), (
            f"Quantity {quantized} below exchange minimum amount {limits.min_amount}"
        )

    if limits.max_amount is not None and quantized > limits.max_amount:
        return Decimal("0"), (
            f"Quantity {quantized} exceeds exchange maximum amount {limits.max_amount}"
        )

    if limits.min_notional is not None and price > 0:
        notional = quantized * price
        if notional < limits.min_notional:
            return Decimal("0"), (
                f"Notional {notional} below exchange minimum notional {limits.min_notional} "
                f"for quantity {quantized} at price {price}"
            )

    return quantized, None
