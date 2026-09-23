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

from decimal import ROUND_DOWN, ROUND_UP, Decimal

from sgr.core.types import Side
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


def quantize_price(
    price: Decimal,
    side: Side,
    limits: SymbolLimits | None,
) -> tuple[Decimal, str | None]:
    """
    Rundet einen Limit-Preis auf limits.price_precision (Tick-Size) und
    validiert gegen min_price/max_price (PRICE_FILTER). Schliesst die im
    Architekturbericht identifizierte Luecke: quantize_and_validate()
    quantisiert ausschliesslich `quantity`, NIEMALS `price` - ein Preis,
    der nicht exakt ein Vielfaches der Tick-Size ist, wird von Binance mit
    Fehler -1111 ("Precision is over the maximum defined for this asset")
    abgelehnt. Betrifft JEDE LIMIT-Order mit explizitem Preis (u.a.
    RiskEngine.build_order_request()'s "hohe Slippage -> LIMIT statt
    MARKET"-Zweig, sowie eine kuenftige Live-Futures-Grid-Implementierung
    mit echten ruhenden Limit-Orders auf FuturesGridParameters.
    compute_levels()-Preisen).

    Richtung bewusst asymmetrisch (identisches Sicherheitsprinzip wie
    quantize_and_validate()'s "NIEMALS aufrunden" fuer quantity, hier auf
    Preis uebertragen): Runden darf eine Order niemals wirtschaftlich
    AGGRESSIVER machen als vom Aufrufer beabsichtigt.
        BUY:  ROUND_DOWN - ein abgerundeter Kauf-Limit-Preis ist
              konservativer (zahlt nie mehr als beabsichtigt, fuellt nie
              frueher/aggressiver als der urspruengliche Preis erlaubt
              haette).
        SELL: ROUND_UP   - ein aufgerundeter Verkaufs-Limit-Preis ist
              konservativer (verlangt nie weniger als beabsichtigt).
    Diese Richtung kann den Preis um bis zu eine Tick-Size vom
    urspruenglichen Wert verschieben, aber NIE ueber eine explizite
    Sicherheitsgrenze (min_price/max_price) hinaus - das wird separat
    geprueft und fuehrt zur Ablehnung, nicht zu einer stillen Korrektur.

    Returns:
        (quantized_price, None) bei Erfolg.
        (Decimal("0"), reason) wenn der Preis nach dem Runden auf 0 faellt
        oder ausserhalb von [min_price, max_price] liegt - der Aufrufer
        lehnt die Order dann ab, statt sie mit einem ungueltigen Preis zu
        senden.
    """
    if limits is None:
        return price, None

    quantized = price
    if limits.price_precision is not None:
        step = Decimal("1").scaleb(-limits.price_precision)
        rounding = ROUND_DOWN if side == Side.BUY else ROUND_UP
        quantized = price.quantize(step, rounding=rounding)

    if quantized <= 0:
        return Decimal("0"), (
            f"Price {price} rounds to {quantized} at exchange precision "
            f"{limits.price_precision}"
        )

    if limits.min_price is not None and quantized < limits.min_price:
        return Decimal("0"), (
            f"Price {quantized} below exchange minimum price {limits.min_price}"
        )

    if limits.max_price is not None and quantized > limits.max_price:
        return Decimal("0"), (
            f"Price {quantized} exceeds exchange maximum price {limits.max_price}"
        )

    return quantized, None
