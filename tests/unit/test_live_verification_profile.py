"""
Tests für sgr.risk.live_verification_profile (Abschnitt 13 - explizit
begrenztes Live-Verifikationsprofil). Deckt: Pflichtfelder ohne Default,
Budget-/Verlust-/Zeit-/Exposure-/Leverage-/Order-/Grid-Limits, kein
Auto-Reset nach Deaktivierung, kein automatisches Erhoehen des Budgets
bei Gewinn.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sgr.risk.live_verification_profile import LiveVerificationGate, LiveVerificationProfile


def _profile(**overrides) -> LiveVerificationProfile:
    base = dict(
        max_total_budget_usd=Decimal("100"),
        max_loss_usd=Decimal("20"),
        max_daily_loss_usd=Decimal("20"),
        max_concurrent_grids=1,
        max_orders=10,
        max_exposure_usd=Decimal("100"),
        max_leverage=Decimal("2"),
        max_position_size_usd=Decimal("50"),
        max_duration_minutes=60,
        started_at=datetime.now(tz=UTC),
        approved_by="operator:test",
    )
    base.update(overrides)
    return LiveVerificationProfile(**base)


class TestProfileValidation:
    def test_all_required_fields_have_no_default(self) -> None:
        """Fehlt IRGENDEIN Pflichtfeld, muss die Konstruktion fehlschlagen -
        kein implizites Budget."""
        with pytest.raises(TypeError):
            LiveVerificationProfile(max_total_budget_usd=Decimal("100"))  # type: ignore[call-arg]

    @pytest.mark.parametrize(
        "field_name",
        [
            "max_total_budget_usd",
            "max_loss_usd",
            "max_daily_loss_usd",
            "max_exposure_usd",
            "max_position_size_usd",
        ],
    )
    def test_zero_or_negative_dollar_fields_rejected(self, field_name: str) -> None:
        with pytest.raises(ValueError):
            _profile(**{field_name: Decimal("0")})

    def test_leverage_below_one_rejected(self) -> None:
        with pytest.raises(ValueError):
            _profile(max_leverage=Decimal("0.5"))

    def test_empty_approved_by_rejected(self) -> None:
        with pytest.raises(ValueError):
            _profile(approved_by="")


class TestExpiry:
    def test_not_expired_within_window(self) -> None:
        profile = _profile(max_duration_minutes=60)
        assert profile.is_expired(profile.started_at + timedelta(minutes=30)) is False

    def test_expired_after_window(self) -> None:
        profile = _profile(max_duration_minutes=60)
        assert profile.is_expired(profile.started_at + timedelta(minutes=61)) is True


class TestGateChecks:
    def test_order_within_all_limits_is_allowed(self) -> None:
        gate = LiveVerificationGate(_profile())
        allowed, reason = gate.check_order_allowed(
            notional_usd=Decimal("30"), leverage=Decimal("1")
        )
        assert allowed is True
        assert reason is None

    def test_leverage_above_limit_rejected(self) -> None:
        gate = LiveVerificationGate(_profile(max_leverage=Decimal("2")))
        allowed, reason = gate.check_order_allowed(
            notional_usd=Decimal("10"), leverage=Decimal("5")
        )
        assert allowed is False
        assert "Leverage" in reason

    def test_notional_above_position_size_limit_rejected(self) -> None:
        gate = LiveVerificationGate(_profile(max_position_size_usd=Decimal("50")))
        allowed, reason = gate.check_order_allowed(
            notional_usd=Decimal("60"), leverage=Decimal("1")
        )
        assert allowed is False
        assert "max_position_size_usd" in reason

    def test_notional_above_total_budget_rejected(self) -> None:
        gate = LiveVerificationGate(
            _profile(max_total_budget_usd=Decimal("40"), max_position_size_usd=Decimal("100"))
        )
        allowed, reason = gate.check_order_allowed(
            notional_usd=Decimal("50"), leverage=Decimal("1")
        )
        assert allowed is False
        assert "max_total_budget_usd" in reason

    def test_max_orders_limit_enforced(self) -> None:
        gate = LiveVerificationGate(_profile(max_orders=2))
        gate.record_order_submitted()
        gate.record_order_submitted()

        allowed, reason = gate.check_order_allowed(
            notional_usd=Decimal("10"), leverage=Decimal("1")
        )

        assert allowed is False
        assert "max_orders" in reason

    def test_max_concurrent_grids_enforced(self) -> None:
        gate = LiveVerificationGate(_profile(max_concurrent_grids=1))
        gate.record_order_submitted(grid_id="grid-a")

        allowed, reason = gate.check_order_allowed(
            notional_usd=Decimal("10"), leverage=Decimal("1"), grid_id="grid-b"
        )

        assert allowed is False
        assert "max_concurrent_grids" in reason

    def test_same_grid_id_does_not_count_twice(self) -> None:
        """Ein weiterer Fill INNERHALB desselben, bereits erfassten Grids
        darf nicht gegen max_concurrent_grids zaehlen."""
        gate = LiveVerificationGate(_profile(max_concurrent_grids=1))
        gate.record_order_submitted(grid_id="grid-a")

        allowed, _ = gate.check_order_allowed(
            notional_usd=Decimal("10"), leverage=Decimal("1"), grid_id="grid-a"
        )

        assert allowed is True

    def test_expired_profile_rejects_and_deactivates(self) -> None:
        profile = _profile(
            max_duration_minutes=1, started_at=datetime.now(tz=UTC) - timedelta(minutes=5)
        )
        gate = LiveVerificationGate(profile)

        allowed, reason = gate.check_order_allowed(
            notional_usd=Decimal("10"), leverage=Decimal("1")
        )

        assert allowed is False
        assert "abgelaufen" in reason
        assert gate.state.deactivated is True

    def test_max_loss_reached_rejects_and_deactivates(self) -> None:
        gate = LiveVerificationGate(_profile(max_loss_usd=Decimal("10")))
        gate.record_realized_loss(Decimal("10"))

        allowed, reason = gate.check_order_allowed(notional_usd=Decimal("5"), leverage=Decimal("1"))

        assert allowed is False
        assert gate.state.deactivated is True

    def test_max_daily_loss_reached_rejects(self) -> None:
        gate = LiveVerificationGate(_profile(max_daily_loss_usd=Decimal("10")))
        gate.record_realized_loss(Decimal("10"))

        allowed, _ = gate.check_order_allowed(notional_usd=Decimal("5"), leverage=Decimal("1"))

        assert allowed is False

    def test_deactivation_is_permanent_no_auto_reset(self) -> None:
        """Kein Auto-Reset - selbst wenn theoretisch wieder Budget frei
        waere, bleibt ein einmal deaktiviertes Gate deaktiviert."""
        gate = LiveVerificationGate(_profile(max_loss_usd=Decimal("10")))
        gate.record_realized_loss(Decimal("10"))
        gate.check_order_allowed(notional_usd=Decimal("5"), leverage=Decimal("1"))
        assert gate.state.deactivated is True

        # Ein zweiter Check aendert nichts - bleibt deaktiviert.
        allowed, _ = gate.check_order_allowed(notional_usd=Decimal("1"), leverage=Decimal("1"))
        assert allowed is False
        assert gate.state.deactivated is True

    def test_profit_does_not_increase_budget(self) -> None:
        """Kein 'wenn es gut laeuft, mehr Kapital' - ein negativer
        loss_usd (=Gewinn) wird schlicht ignoriert, erhoeht kein Budget."""
        gate = LiveVerificationGate(_profile(max_loss_usd=Decimal("10")))
        gate.record_realized_loss(Decimal("-50"))  # "Gewinn"

        assert gate.state.cumulative_realized_loss_usd == Decimal("0")

    def test_daily_loss_breach_permanently_deactivates(self) -> None:
        """Bewusste Design-Entscheidung: DIESES Profile ist ein zeitlich
        eng begrenzter Verifikationslauf (max_duration_minutes), kein
        Dauerbetrieb - ein Tagesverlust-Limit-Treffer stoppt den GESAMTEN
        Testlauf permanent (identisch zu max_loss_usd), statt am
        naechsten Tag automatisch fortzufahren. Ein Fortsetzen erfordert
        eine neue, explizite Operator-Entscheidung (neues Profile)."""
        gate = LiveVerificationGate(
            _profile(max_daily_loss_usd=Decimal("10"), max_duration_minutes=60 * 24 * 7)
        )
        gate.record_realized_loss(Decimal("10"))
        allowed, _ = gate.check_order_allowed(
            notional_usd=Decimal("5"),
            leverage=Decimal("1"),
            now=gate.state.profile.started_at + timedelta(hours=1),
        )
        assert allowed is False
        assert gate.state.deactivated is True

        later = gate.state.profile.started_at + timedelta(hours=25)
        allowed, _ = gate.check_order_allowed(
            notional_usd=Decimal("5"), leverage=Decimal("1"), now=later
        )
        assert allowed is False  # bleibt deaktiviert, kein Auto-Resume am naechsten Tag

    def test_daily_loss_counter_itself_resets_if_never_breached(self) -> None:
        """Der interne Tagesverlust-Zaehler selbst setzt sich nach 24h
        zurueck (Buchhaltungsdetail), relevant fuer den Fall, dass das
        Tages-Limit NIE erreicht wurde - dann bleibt das Gate aktiv, und
        ein kleiner Verlust an Tag 2 startet wieder bei 0, nicht
        kumuliert mit Tag 1."""
        gate = LiveVerificationGate(
            _profile(max_daily_loss_usd=Decimal("50"), max_duration_minutes=60 * 24 * 7)
        )
        gate.record_realized_loss(Decimal("10"))  # weit unter dem Tages-Limit
        gate.check_order_allowed(
            notional_usd=Decimal("5"),
            leverage=Decimal("1"),
            now=gate.state.profile.started_at + timedelta(hours=1),
        )
        assert gate.state.daily_realized_loss_usd == Decimal("10")

        later = gate.state.profile.started_at + timedelta(hours=25)
        gate.check_order_allowed(notional_usd=Decimal("5"), leverage=Decimal("1"), now=later)

        assert gate.state.daily_realized_loss_usd == Decimal("0")
        assert gate.state.deactivated is False

    def test_check_order_allowed_is_read_only(self) -> None:
        """check_order_allowed() selbst darf keinen Zustand mutieren -
        erst record_order_submitted()/record_realized_loss() buchen."""
        gate = LiveVerificationGate(_profile())
        gate.check_order_allowed(notional_usd=Decimal("10"), leverage=Decimal("1"))
        gate.check_order_allowed(notional_usd=Decimal("10"), leverage=Decimal("1"))

        assert gate.state.orders_submitted == 0


class TestObservabilityMetrics:
    """Live-Verification-Anweisung Abschnitt 4: kontinuierliche Gauges
    fuer active/orders_submitted/max_orders/remaining loss budgets -
    vorher nur sichtbar ueber einzelne REJECTED-Events."""

    def test_construction_immediately_emits_active_gauge(self) -> None:
        from sgr.monitoring.trading_metrics import live_verification_active

        gate = LiveVerificationGate(_profile(approved_by="operator:metrics-test-1"))

        value = live_verification_active.labels(
            approved_by="operator:metrics-test-1", tenant="default"
        )._value.get()
        assert value == 1
        assert gate.state.deactivated is False  # Kontrolle: Gate ist tatsaechlich aktiv

    def test_deactivation_sets_active_gauge_to_zero(self) -> None:
        from sgr.monitoring.trading_metrics import live_verification_active

        gate = LiveVerificationGate(_profile(approved_by="operator:metrics-test-2"))
        gate.deactivate("test reason")

        value = live_verification_active.labels(
            approved_by="operator:metrics-test-2", tenant="default"
        )._value.get()
        assert value == 0

    def test_order_submitted_updates_gauge(self) -> None:
        from sgr.monitoring.trading_metrics import live_verification_orders_submitted

        gate = LiveVerificationGate(_profile(approved_by="operator:metrics-test-3"))
        gate.record_order_submitted()
        gate.record_order_submitted()

        value = live_verification_orders_submitted.labels(
            approved_by="operator:metrics-test-3", tenant="default"
        )._value.get()
        assert value == 2

    def test_realized_loss_updates_remaining_budget_gauges(self) -> None:
        from sgr.monitoring.trading_metrics import (
            live_verification_daily_loss_budget_remaining_usd,
            live_verification_loss_budget_remaining_usd,
        )

        gate = LiveVerificationGate(
            _profile(
                approved_by="operator:metrics-test-4",
                max_loss_usd=Decimal("20"),
                max_daily_loss_usd=Decimal("20"),
            )
        )
        gate.record_realized_loss(Decimal("7"))

        loss_remaining = live_verification_loss_budget_remaining_usd.labels(
            approved_by="operator:metrics-test-4", tenant="default"
        )._value.get()
        daily_remaining = live_verification_daily_loss_budget_remaining_usd.labels(
            approved_by="operator:metrics-test-4", tenant="default"
        )._value.get()
        assert loss_remaining == pytest.approx(13.0)
        assert daily_remaining == pytest.approx(13.0)

    def test_remaining_budget_never_goes_negative_in_gauge(self) -> None:
        """Ein Verlust, der max_loss_usd ueberschreitet, deaktiviert das
        Gate (siehe bestehende Tests) - die verbleibende-Budget-Gauge
        darf trotzdem nie unter 0 fallen (kein irrefuehrender negativer
        'verbleibender' Wert)."""
        from sgr.monitoring.trading_metrics import live_verification_loss_budget_remaining_usd

        gate = LiveVerificationGate(
            _profile(approved_by="operator:metrics-test-5", max_loss_usd=Decimal("10"))
        )
        gate.record_realized_loss(Decimal("25"))  # weit ueber dem Limit

        remaining = live_verification_loss_budget_remaining_usd.labels(
            approved_by="operator:metrics-test-5", tenant="default"
        )._value.get()
        assert remaining == 0.0

    def test_no_secrets_in_any_label(self) -> None:
        """approved_by ist eine vom Operator gewaehlte Kennung, niemals
        ein Secret - dieser Test dokumentiert die Erwartung explizit."""
        gate = LiveVerificationGate(_profile(approved_by="operator:gordon-verification-run-1"))
        assert "key" not in gate.state.profile.approved_by.lower()
        assert "secret" not in gate.state.profile.approved_by.lower()
