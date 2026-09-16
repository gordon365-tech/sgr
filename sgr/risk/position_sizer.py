"""
SGR Position Sizer
==================
Berechnet optimale Positionsgrößen basierend auf:
    - Portfolio-Wert und verfügbares Kapital
    - ATR-basiertes Risiko (Volatilität)
    - Kelly Criterion (historische Win Rate + Profit Factor)
    - Hard Limits (max. % des Portfolios)

Warum kein reines Kelly Criterion?
    Full Kelly ist für Live-Trading zu aggressiv (hohe Drawdowns).
    SGR nutzt Fractional Kelly (0.25x) + Hard Cap:
    Position = min(Kelly * 0.25, max_position_pct) * portfolio_value

Warum ATR-basiertes Sizing?
    ATR misst aktuelle Volatilität in Preis-Einheiten.
    Positionsgröße = Risk / (ATR * Multiplier)
    Bei hoher Volatilität → kleinere Position (automatische Anpassung).
    Konsistentes Risiko pro Trade unabhängig vom Asset.

Risk Unit Konzept:
    1 Risk Unit = 1% des Portfolios riskiert pro Trade.
    Portfolio Heat = Summe aller offenen Risk Units.
    Max Heat = 70% → max. 70 gleichzeitige 1%-Risk-Trades
    (in Praxis: weniger durch Korrelation).
"""

from __future__ import annotations

from decimal import Decimal

from sgr.core.logging import get_logger
from sgr.core.types import Signal

log = get_logger(__name__)


class PositionSizer:
    """
    Berechnet Positionsgröße für ein Signal unter gegebenen Constraints.
    Stateless – kein interner State.
    """

    def compute(
        self,
        signal: Signal,
        portfolio_value: Decimal,
        available_capital: Decimal,
        current_price: Decimal,
        atr: Decimal | None,
        portfolio_heat: float,
        max_position_pct: float,
        max_portfolio_heat: float,
        win_rate: float | None = None,
        profit_factor: float | None = None,
        min_order_size: Decimal = Decimal("0.001"),
        max_order_notional: Decimal | None = None,
        position_size_usd: Decimal | None = None,
        leverage: Decimal = Decimal("1"),
        stop_loss_pct: float | None = None,
        risk_per_trade_pct: float | None = None,
    ) -> tuple[Decimal, str | None]:
        """
        Berechnet optimale Qty unter allen Constraints.

        position_size_usd (opt-in, TEST_1X / kontrollierte Risk-Profile,
        siehe RiskLimitsConfig): wenn gesetzt, ersetzt dies den
        adaptiven ATR/Kelly/Heat-Blend unten durch einen FESTEN
        Notional-Zielwert (z.B. 20 USD) - deterministisch und
        auditierbar statt adaptiv. Bleibt weiterhin durch
        available_capital/max_order_notional gedeckelt (Fat-Finger-
        Schutz bleibt aktiv). Wird zusaetzlich gegen risk_per_trade_pct
        geprueft: wuerde ein Stop bei stop_loss_pct Abstand mehr als
        risk_per_trade_pct des Kapitals verlieren, wird die Order
        ABGELEHNT (nicht automatisch verkleinert oder vergroessert) -
        siehe Aufgabenstellung "keine automatische Erhoehung der
        Positionsgroesse aufgrund eines einzelnen technischen Signals".
        margin_required = notional / leverage wird nur geloggt (Audit),
        aendert die Positionsgroesse selbst nicht - Leverage wird von
        ExecutionEngine.set_leverage() unabhaengig davon durchgesetzt.

        Returns:
            (approved_quantity, reduction_reason)
            reduction_reason ist None wenn keine Reduktion nötig.
        """
        if portfolio_value <= 0 or current_price <= 0:
            return Decimal("0"), "Invalid portfolio_value or price"

        if available_capital <= 0:
            return Decimal("0"), "No available capital"

        if position_size_usd is not None:
            return self._compute_fixed_notional(
                position_size_usd=position_size_usd,
                available_capital=available_capital,
                max_order_notional=max_order_notional,
                current_price=current_price,
                leverage=leverage,
                stop_loss_pct=stop_loss_pct,
                risk_per_trade_pct=risk_per_trade_pct,
            )

        # 1. Basis-Größe: signal.size_hint × max_position
        max_notional = portfolio_value * Decimal(str(max_position_pct))
        base_notional = max_notional * Decimal(str(signal.size_hint))

        # 2. ATR-basiertes Sizing (falls verfügbar)
        atr_notional: Decimal | None = None
        if atr and atr > 0:
            # Risk = 1% des Portfolios pro Trade
            risk_budget = portfolio_value * Decimal("0.01")
            atr_multiplier = Decimal("2.0")  # Stop = 2x ATR
            atr_qty = risk_budget / (atr * atr_multiplier)
            atr_notional = atr_qty * current_price

        # 3. Kelly-basiertes Sizing (falls Performance-Daten verfügbar)
        kelly_notional: Decimal | None = None
        if win_rate is not None and profit_factor is not None and win_rate > 0:
            kelly_fraction = self._fractional_kelly(win_rate, profit_factor)
            if kelly_fraction > 0:
                kelly_notional = portfolio_value * Decimal(str(kelly_fraction))

        # 4. Portfolio Heat Constraint
        remaining_heat = max_portfolio_heat - portfolio_heat
        if remaining_heat <= 0:
            return Decimal("0"), f"Portfolio heat at maximum ({portfolio_heat:.1%})"

        heat_notional = portfolio_value * Decimal(str(remaining_heat)) * Decimal("0.1")

        # 5. Konservativstes Ergebnis nehmen (min aller Methoden)
        candidates = [base_notional]
        if atr_notional is not None:
            candidates.append(atr_notional)
        if kelly_notional is not None:
            candidates.append(kelly_notional)
        candidates.append(heat_notional)

        final_notional = min(candidates)

        # 6. Hard Cap: nie mehr als max_position_pct
        final_notional = min(final_notional, max_notional)

        # 7. Available Capital Cap
        final_notional = min(final_notional, available_capital)

        # 7b. Max Order Size Cap (absoluter Hard Cap, unabhängig vom
        # Portfolio-Wert). Schützt gegen Fat-Finger-/Konfigurationsfehler,
        # auch wenn alle relativen Constraints (max_position_pct, Heat,
        # verfügbares Kapital) technisch eine größere Order zulassen würden.
        order_size_capped = False
        if max_order_notional is not None and final_notional > max_order_notional:
            final_notional = max_order_notional
            order_size_capped = True

        # 8. Konfidenz-Reduktion (bei niedrigem Signal-Confidence)
        if signal.confidence < 0.6:
            confidence_factor = Decimal(str(signal.confidence / 0.6))
            final_notional *= confidence_factor

        # 9. Qty aus Notional
        qty = final_notional / current_price

        # 10. Mindestgröße prüfen
        if qty < min_order_size:
            return Decimal("0"), f"Quantity {qty:.8f} below minimum {min_order_size}"

        # Reduction Reason bestimmen
        reduction_reason: str | None = None
        if order_size_capped:
            reduction_reason = f"Max order size cap ({max_order_notional})"
        elif final_notional < base_notional * Decimal("0.9"):
            if atr_notional and atr_notional < base_notional:
                reduction_reason = "ATR-based sizing (high volatility)"
            elif kelly_notional and kelly_notional < base_notional:
                reduction_reason = "Kelly criterion reduction"
            elif heat_notional < base_notional:
                reduction_reason = f"Portfolio heat limit ({portfolio_heat:.1%})"

        # Auf 8 Dezimalstellen runden (Exchange-üblich)
        qty = qty.quantize(Decimal("0.00000001"))

        log.debug(
            "position_sizer.computed",
            symbol=str(signal.symbol),
            qty=str(qty),
            final_notional=str(final_notional),
            reduction_reason=reduction_reason,
            confidence=signal.confidence,
        )

        return qty, reduction_reason

    def _compute_fixed_notional(
        self,
        position_size_usd: Decimal,
        available_capital: Decimal,
        max_order_notional: Decimal | None,
        current_price: Decimal,
        leverage: Decimal,
        stop_loss_pct: float | None,
        risk_per_trade_pct: float | None,
    ) -> tuple[Decimal, str | None]:
        """Fixed-Notional-Zweig fuer TEST_1X / kontrollierte Risk-Profile.

        Trennt explizit: Account-Kapital, Risk-per-Trade, Position-
        Notional, Margin, Leverage, Stop-Loss-Distanz (siehe
        Aufgabenstellung "POSITION SIZING" - diese Groessen duerfen
        nicht vermischt werden).

        Root-Cause-Fix (E2E-Harness-Fund gegen echtes Binance Futures
        Testnet): frueher wurde hier gegen den generischen, fuer die
        ADAPTIVE Groessenberechnung gedachten min_order_size-Parameter
        geprueft (Default 0.001) - ein Wert, der implizit von "billigen"
        Altcoins ausgeht. Bei einem teuren Asset wie BTC (~75000 USDT)
        ergibt ein fixer $20-Notional aber legitim nur ~0.00026 BTC,
        weit unter diesem generischen Default - die Order wurde
        faelschlich als "zu klein" abgelehnt, OBWOHL sie exchange-seitig
        vollkommen gueltig sein kann. Die tatsaechliche, symbol-
        spezifische Exchange-Mindestgroesse wird bereits von
        ExecutionEngine ueber echte SymbolLimits durchgesetzt (siehe
        sgr/execution/quantization.py) - hier bleibt nur noch eine reine
        Rundungsartefakt-Pruefung (qty <= 0), keine geratene, symbol-
        unabhaengige Schwelle mehr.
        """
        notional = position_size_usd
        capped_reason: str | None = None

        if notional > available_capital:
            notional = available_capital
            capped_reason = "Fixed position size capped by available capital"

        if max_order_notional is not None and notional > max_order_notional:
            notional = max_order_notional
            capped_reason = (
                f"Fixed position size capped by max_order_notional ({max_order_notional})"
            )

        margin_required = notional / leverage if leverage > 0 else notional

        if stop_loss_pct is not None and risk_per_trade_pct is not None:
            implied_risk_amount = notional * Decimal(str(stop_loss_pct))
            risk_budget = available_capital * Decimal(str(risk_per_trade_pct))
            if implied_risk_amount > risk_budget:
                log.warning(
                    "position_sizer.fixed_notional_rejected_risk_budget",
                    notional=str(notional),
                    stop_loss_pct=stop_loss_pct,
                    implied_risk_amount=str(implied_risk_amount),
                    risk_budget=str(risk_budget),
                )
                return Decimal("0"), (
                    f"Implied risk {implied_risk_amount:.2f} at {stop_loss_pct:.1%} stop "
                    f"exceeds risk-per-trade budget {risk_budget:.2f} "
                    f"({risk_per_trade_pct:.1%} of capital) - rejecting rather than resizing"
                )

        qty = (notional / current_price).quantize(Decimal("0.00000001"))
        if qty <= 0:
            return Decimal("0"), (
                f"Quantity rounds to 0 at notional {notional}/price {current_price}"
            )

        log.info(
            "position_sizer.fixed_notional_computed",
            account_capital=str(available_capital),
            position_notional=str(notional),
            margin_required=str(margin_required),
            leverage=str(leverage),
            qty=str(qty),
            reduction_reason=capped_reason,
        )
        return qty, capped_reason

    def _fractional_kelly(
        self,
        win_rate: float,
        profit_factor: float,
        fraction: float = 0.25,
    ) -> float:
        """
        Fractional Kelly Criterion.
        Kelly% = (W * R - L) / R
        W = win_rate, L = loss_rate = 1 - W
        R = avg_win / avg_loss = profit_factor (approximation)
        Fraction = 0.25 (Quarter Kelly, sehr konservativ)
        """
        loss_rate = 1.0 - win_rate
        if profit_factor <= 0:
            return 0.0

        kelly = (win_rate * profit_factor - loss_rate) / profit_factor
        kelly = max(kelly, 0.0)  # Kein negatives Kelly
        return kelly * fraction
