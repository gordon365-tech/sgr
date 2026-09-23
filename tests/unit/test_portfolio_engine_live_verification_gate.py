"""
Tests fuer die LiveVerificationGate-Anbindung in PortfolioEngine
(Live-Verification-Anweisung Abschnitt 5, "Realized Loss Accounting").

record_realized_loss() muss bei einem tatsaechlich realisierten Verlust
GENAU EINMAL aus dem Fill-Lifecycle heraus aufgerufen werden - siehe
PortfolioEngine._update_position(), Aufrufstelle direkt nach der
finalen realized-Berechnung. Ein Gewinn darf das Budget NIEMALS
erhoehen (siehe LiveVerificationGate.record_realized_loss() Docstring -
diese Tests pruefen nur die AUFRUFSTELLE, nicht die bereits in
test_live_verification_profile.py getestete Gate-Logik selbst).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sgr.core.types import ExchangeID, OrderResult, OrderStatus, Side, Symbol, TradingMode
from sgr.portfolio.engine import PortfolioEngine
from sgr.risk.live_verification_profile import LiveVerificationGate, LiveVerificationProfile


def _symbol(base: str = "BTC") -> Symbol:
    return Symbol(base=base, quote="USDT", exchange=ExchangeID.BINANCE)


def _fill(
    side: Side,
    price: Decimal,
    qty: Decimal = Decimal("1"),
    symbol: Symbol | None = None,
    trading_mode: TradingMode = TradingMode.LIVE,
    strategy: str = "test_strategy",
) -> OrderResult:
    now = datetime.now(tz=UTC)
    return OrderResult(
        request_id=uuid4(),
        exchange_order_id=f"LIVE-{uuid4()}",
        symbol=symbol or _symbol(),
        status=OrderStatus.FILLED,
        filled_quantity=qty,
        average_fill_price=price,
        fees=Decimal("0"),
        fee_currency="USDT",
        submitted_at=now,
        filled_at=now,
        trading_mode=trading_mode,
        raw_response={"side": side.value, "strategy": strategy},
    )


def _profile(**overrides) -> LiveVerificationProfile:
    base = dict(
        max_total_budget_usd=Decimal("10000"),
        max_loss_usd=Decimal("1000"),
        max_daily_loss_usd=Decimal("1000"),
        max_concurrent_grids=1,
        max_orders=100,
        max_exposure_usd=Decimal("10000"),
        max_leverage=Decimal("5"),
        max_position_size_usd=Decimal("10000"),
        max_duration_minutes=600,
        started_at=datetime.now(tz=UTC),
        approved_by="operator:test",
    )
    base.update(overrides)
    return LiveVerificationProfile(**base)


def _engine_with_gate(gate: LiveVerificationGate | None) -> PortfolioEngine:
    engine = PortfolioEngine(trading_mode=TradingMode.LIVE, live_verification_gate=gate)
    return engine


class TestRealizedLossAccounting:
    async def test_profitable_close_does_not_record_loss(self) -> None:
        gate = LiveVerificationGate(_profile())
        engine = _engine_with_gate(gate)

        await engine.on_order_filled(_fill(Side.BUY, Decimal("100")))
        await engine.on_order_filled(_fill(Side.SELL, Decimal("110")))  # +10 profit

        assert gate.state.cumulative_realized_loss_usd == Decimal("0")

    async def test_losing_close_records_exact_loss(self) -> None:
        gate = LiveVerificationGate(_profile())
        engine = _engine_with_gate(gate)

        await engine.on_order_filled(_fill(Side.BUY, Decimal("100")))
        await engine.on_order_filled(_fill(Side.SELL, Decimal("90")))  # -10 loss

        assert gate.state.cumulative_realized_loss_usd == Decimal("10")
        assert gate.state.daily_realized_loss_usd == Decimal("10")

    async def test_multiple_losses_across_symbols_accumulate(self) -> None:
        gate = LiveVerificationGate(_profile())
        engine = _engine_with_gate(gate)

        await engine.on_order_filled(_fill(Side.BUY, Decimal("100"), symbol=_symbol("BTC")))
        await engine.on_order_filled(_fill(Side.SELL, Decimal("90"), symbol=_symbol("BTC")))  # -10

        await engine.on_order_filled(_fill(Side.BUY, Decimal("50"), symbol=_symbol("ETH")))
        await engine.on_order_filled(_fill(Side.SELL, Decimal("45"), symbol=_symbol("ETH")))  # -5

        assert gate.state.cumulative_realized_loss_usd == Decimal("15")

    async def test_mixed_profit_and_loss_only_loss_counted(self) -> None:
        gate = LiveVerificationGate(_profile())
        engine = _engine_with_gate(gate)

        await engine.on_order_filled(_fill(Side.BUY, Decimal("100"), symbol=_symbol("BTC")))
        await engine.on_order_filled(_fill(Side.SELL, Decimal("120"), symbol=_symbol("BTC")))  # +20

        await engine.on_order_filled(_fill(Side.BUY, Decimal("50"), symbol=_symbol("ETH")))
        await engine.on_order_filled(_fill(Side.SELL, Decimal("45"), symbol=_symbol("ETH")))  # -5

        assert gate.state.cumulative_realized_loss_usd == Decimal("5")

    async def test_loss_eventually_deactivates_gate_and_blocks_further_orders(self) -> None:
        """Der eigentliche Zweck der Buchung: genug kumulierter Verlust
        MUSS das Gate tatsaechlich deaktivieren (siehe check_order_allowed()
        in live_verification_profile.py), nicht nur einen Zaehler erhoehen."""
        gate = LiveVerificationGate(_profile(max_loss_usd=Decimal("8")))
        engine = _engine_with_gate(gate)

        await engine.on_order_filled(_fill(Side.BUY, Decimal("100")))
        await engine.on_order_filled(_fill(Side.SELL, Decimal("90")))  # -10 > max_loss_usd 8

        allowed, reason = gate.check_order_allowed(
            notional_usd=Decimal("10"), leverage=Decimal("1")
        )
        assert allowed is False
        assert gate.state.deactivated is True

    async def test_paper_mode_fills_never_reach_the_gate(self) -> None:
        """Gate injiziert, aber die Order ist PAPER - production-relevanter
        Fall (Gordon/Sumo sind heute PAPER, ein Gate wird dort niemals
        wirksam)."""
        gate = LiveVerificationGate(_profile())
        engine = PortfolioEngine(trading_mode=TradingMode.PAPER, live_verification_gate=gate)

        await engine.on_order_filled(
            _fill(Side.BUY, Decimal("100"), trading_mode=TradingMode.PAPER)
        )
        await engine.on_order_filled(
            _fill(Side.SELL, Decimal("50"), trading_mode=TradingMode.PAPER)
        )  # -50 loss, but PAPER

        assert gate.state.cumulative_realized_loss_usd == Decimal("0")

    async def test_no_gate_injected_is_safe_noop(self) -> None:
        engine = _engine_with_gate(None)

        await engine.on_order_filled(_fill(Side.BUY, Decimal("100")))
        result = await engine.on_order_filled(_fill(Side.SELL, Decimal("50")))  # -50 loss

        assert result is None  # kein Fehler, kein Crash ohne Gate

    async def test_tenant_isolation_two_engines_do_not_share_gate_state(self) -> None:
        """Zwei getrennte PortfolioEngine-Instanzen (Prozesstrennung wie
        Gordon/Sumo) mit je EIGENEM Gate - ein Verlust in Engine A darf
        Engine Bs Gate niemals beeinflussen."""
        gate_a = LiveVerificationGate(_profile())
        gate_b = LiveVerificationGate(_profile())
        engine_a = _engine_with_gate(gate_a)
        _engine_b = _engine_with_gate(gate_b)  # konstruiert, um Isolation zu demonstrieren

        await engine_a.on_order_filled(_fill(Side.BUY, Decimal("100")))
        await engine_a.on_order_filled(_fill(Side.SELL, Decimal("50")))  # -50 loss in A only

        assert gate_a.state.cumulative_realized_loss_usd == Decimal("50")
        assert gate_b.state.cumulative_realized_loss_usd == Decimal("0")

    async def test_strategy_isolation_gate_sums_across_strategies_within_one_tenant(self) -> None:
        """Das Gate ist EIN Budget pro Verifikationslauf (Operator-Limit
        fuer den GESAMTEN Testlauf, nicht pro Strategie - siehe
        LiveVerificationProfile Docstring) - Verluste unterschiedlicher
        Strategien innerhalb desselben Tenants/Laufs summieren sich
        bewusst in EINEM gemeinsamen Budget, werden aber weiterhin korrekt
        pro Order verbucht (kein Strategie-Verlust wird verschluckt)."""
        gate = LiveVerificationGate(_profile())
        engine = _engine_with_gate(gate)

        await engine.on_order_filled(
            _fill(Side.BUY, Decimal("100"), symbol=_symbol("BTC"), strategy="trend_following_v1")
        )
        await engine.on_order_filled(
            _fill(Side.SELL, Decimal("90"), symbol=_symbol("BTC"), strategy="trend_following_v1")
        )  # -10

        await engine.on_order_filled(
            _fill(Side.BUY, Decimal("50"), symbol=_symbol("ETH"), strategy="momentum_v1")
        )
        await engine.on_order_filled(
            _fill(Side.SELL, Decimal("40"), symbol=_symbol("ETH"), strategy="momentum_v1")
        )  # -10

        assert gate.state.cumulative_realized_loss_usd == Decimal("20")

    async def test_duplicate_close_fill_event_does_not_double_book(self) -> None:
        """Ein doppelt zugestelltes Fill-Event fuer eine bereits
        VOLLSTAENDIG geschlossene Position darf den Verlust nicht ein
        zweites Mal buchen. Nach dem vollstaendigen Close existiert die
        Position nicht mehr in _state._positions - ein zweiter Fill fuer
        dasselbe Symbol wird strukturell als eine NEUE Position-Eroeffnung
        behandelt (Open-Pfad, kein Close-Pfad), erreicht die Gate-Buchung
        (die ausschliesslich im Close-Pfad sitzt) also gar nicht erst."""
        gate = LiveVerificationGate(_profile())
        engine = _engine_with_gate(gate)

        await engine.on_order_filled(_fill(Side.BUY, Decimal("100")))
        await engine.on_order_filled(_fill(Side.SELL, Decimal("90")))  # -10, position now closed
        assert gate.state.cumulative_realized_loss_usd == Decimal("10")

        # Dieselbe schliessende Order nochmal zugestellt (z.B. ein erneut
        # verarbeitetes Event nach einem Requeue) - Position ist bereits
        # weg, das trifft den Open-Pfad, nicht den Close-Pfad.
        duplicate = _fill(Side.SELL, Decimal("90"))
        await engine.on_order_filled(duplicate)

        assert gate.state.cumulative_realized_loss_usd == Decimal("10")  # unveraendert

    async def test_restart_resets_gate_state_documented_limitation(self) -> None:
        """Dokumentiert eine echte, noch offene Limitation (siehe
        Abschlussbericht): LiveVerificationState ist rein In-Memory, hat
        KEINE Persistenz. Ein Prozess-Neustart waehrend eines aktiven
        Live-Verifikationslaufs konstruiert zwangslaeufig ein FRISCHES
        Gate/Profile - der bis dahin kumulierte Verlust geht verloren,
        das Budget wirkt nach einem Neustart faelschlich wieder voll
        verfuegbar. Dieser Test beweist das Verhalten explizit (als
        Dokumentation, nicht als 'gewuenschtes' Verhalten) statt es
        stillschweigend anzunehmen."""
        gate_before_restart = LiveVerificationGate(_profile(max_loss_usd=Decimal("20")))
        engine = _engine_with_gate(gate_before_restart)

        await engine.on_order_filled(_fill(Side.BUY, Decimal("100")))
        await engine.on_order_filled(_fill(Side.SELL, Decimal("85")))  # -15 loss
        assert gate_before_restart.state.cumulative_realized_loss_usd == Decimal("15")

        # Simulierter Neustart: ein komplett neues Gate/Profile-Objekt
        # (wie es main.py bei einem echten Prozess-Neustart erneut
        # konstruieren wuerde) - KEIN Zustand wird uebernommen.
        gate_after_restart = LiveVerificationGate(_profile(max_loss_usd=Decimal("20")))

        assert gate_after_restart.state.cumulative_realized_loss_usd == Decimal("0")
        allowed, _reason = gate_after_restart.check_order_allowed(
            notional_usd=Decimal("1"), leverage=Decimal("1")
        )
        assert allowed is True  # faelschlich wieder "voll verfuegbar"
