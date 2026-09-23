"""
SGR Live Verification Profile
================================
Explizit, eng begrenztes Risikoprofil fuer einen kontrollierten
Live-Trading-Verifikationslauf (Abschnitt 13 der Aufgabenstellung).

Kein Feld hat einen Default-Wert - JEDER Parameter MUSS vom Operator
explizit gesetzt werden. Das ist eine bewusste Design-Entscheidung gegen
jede Form impliziter Kapitalfreigabe (siehe Aufgabenstellung: "Keine
unbegrenzte Kapitalfreigabe. Keine automatische Erhoehung des Risikos.
Kein Martingale. Keine Verlusteskalation. Kein 'wenn es gut laeuft, mehr
Kapital'.").

Ergaenzt (nicht ersetzt) sgr.risk.live_trading_gate.check_live_trading_allowed():
jenes Gate entscheidet "darf DIESE Strategie ueberhaupt live handeln"
(strukturell, unabhaengig von einem konkreten Testlauf, siehe dortigen
Modul-Docstring - aktuell blockt es JEDE Live-Order, da live_approved
nirgends im System gesetzt wird). Dieses Modul entscheidet zusaetzlich
"ist DIESER KONKRETE, vom Operator genehmigte Live-Verifikationslauf
noch innerhalb seiner Grenzen". Beide muessen JA sagen, bevor eine
LIVE-Order gesendet werden darf.

Noch NICHT in ExecutionEngine/GridController verdrahtet (siehe
Abschlussbericht) - dieses Modul ist bewusst nur bereitgestellt, nicht
automatisch aktiviert. Eine Aktivierung erfordert eine explizite,
separate Entscheidung des Operators (welche konkreten Zahlen, welcher
Account) - Claude legt diese Zahlen nicht selbst fest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sgr.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class LiveVerificationProfile:
    """Alle Felder PFLICHT (kein Default) - siehe Modul-Docstring."""

    max_total_budget_usd: Decimal
    max_loss_usd: Decimal
    max_daily_loss_usd: Decimal
    max_concurrent_grids: int
    max_orders: int
    max_exposure_usd: Decimal
    max_leverage: Decimal
    max_position_size_usd: Decimal
    max_duration_minutes: int
    started_at: datetime
    approved_by: str  # menschliche Kennung (z.B. "operator:gordon"), kein Default

    def __post_init__(self) -> None:
        for name in (
            "max_total_budget_usd",
            "max_loss_usd",
            "max_daily_loss_usd",
            "max_exposure_usd",
            "max_position_size_usd",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"LiveVerificationProfile.{name} muss > 0 sein")
        for name in ("max_concurrent_grids", "max_orders", "max_duration_minutes"):
            if getattr(self, name) <= 0:
                raise ValueError(f"LiveVerificationProfile.{name} muss > 0 sein")
        if self.max_leverage < 1:
            raise ValueError("LiveVerificationProfile.max_leverage muss >= 1 sein")
        if not self.approved_by:
            raise ValueError("LiveVerificationProfile.approved_by darf nicht leer sein")

    @property
    def expires_at(self) -> datetime:
        return self.started_at + timedelta(minutes=self.max_duration_minutes)

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(tz=UTC)
        return now >= self.expires_at


@dataclass
class LiveVerificationState:
    """Laufender, veraenderlicher Zustand EINES konkreten Testlaufs unter
    einem Profile - getrennt vom (frozen) Profile selbst."""

    profile: LiveVerificationProfile
    cumulative_realized_loss_usd: Decimal = Decimal("0")
    daily_realized_loss_usd: Decimal = Decimal("0")
    daily_loss_reset_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    orders_submitted: int = 0
    active_grid_ids: set = field(default_factory=set)
    deactivated: bool = False
    deactivation_reason: str | None = None


class LiveVerificationGate:
    """
    Stateful Gate - EINE Instanz pro laufendem Live-Verifikationstest.
    Fail-closed: jede Unsicherheit fuehrt zu REJECTED, nie zu einem
    stillschweigenden Durchlassen. Einmal deaktiviert (Budget/Zeit/Verlust
    ueberschritten), bleibt das Gate deaktiviert - kein Auto-Reset (siehe
    Aufgabenstellung "Kein 'wenn es gut laeuft, mehr Kapital'" - ein
    manueller Reset erfordert ein neues Profile mit einer neuen,
    expliziten Operator-Entscheidung, kein in-place-Anheben der Limits).
    """

    def __init__(self, profile: LiveVerificationProfile) -> None:
        self._state = LiveVerificationState(profile=profile)

    @property
    def state(self) -> LiveVerificationState:
        return self._state

    def _maybe_reset_daily_loss(self, now: datetime) -> None:
        if now - self._state.daily_loss_reset_at >= timedelta(hours=24):
            self._state.daily_realized_loss_usd = Decimal("0")
            self._state.daily_loss_reset_at = now

    def check_order_allowed(
        self,
        *,
        notional_usd: Decimal,
        leverage: Decimal,
        grid_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[bool, str | None]:
        """Rein lesende Pruefung - mutiert keinen Zustand (siehe
        record_order_submitted()/record_realized_loss() fuer die
        tatsaechliche Buchung NACH einer bestaetigten Order)."""
        now = now or datetime.now(tz=UTC)
        profile = self._state.profile

        if self._state.deactivated:
            return False, (
                f"Live-Verifikationsprofil bereits deaktiviert: "
                f"{self._state.deactivation_reason}"
            )

        if profile.is_expired(now):
            self.deactivate("Zeitfenster abgelaufen")
            return False, "Live-Verifikationsprofil Zeitfenster abgelaufen"

        if self._state.cumulative_realized_loss_usd >= profile.max_loss_usd:
            self.deactivate("max_loss_usd erreicht")
            return False, "Maximal zulaessiger Gesamtverlust erreicht"

        self._maybe_reset_daily_loss(now)
        if self._state.daily_realized_loss_usd >= profile.max_daily_loss_usd:
            self.deactivate("max_daily_loss_usd erreicht")
            return False, "Maximal zulaessiger Tagesverlust erreicht"

        if self._state.orders_submitted >= profile.max_orders:
            return False, f"max_orders ({profile.max_orders}) erreicht"

        if grid_id is not None and grid_id not in self._state.active_grid_ids:
            if len(self._state.active_grid_ids) >= profile.max_concurrent_grids:
                return False, f"max_concurrent_grids ({profile.max_concurrent_grids}) erreicht"

        if leverage > profile.max_leverage:
            return False, (
                f"Leverage {leverage}x ueberschreitet max_leverage {profile.max_leverage}x"
            )

        if notional_usd > profile.max_position_size_usd:
            return False, (
                f"Notional {notional_usd} ueberschreitet max_position_size_usd "
                f"{profile.max_position_size_usd}"
            )

        if notional_usd > profile.max_total_budget_usd:
            return False, (
                f"Notional {notional_usd} ueberschreitet max_total_budget_usd "
                f"{profile.max_total_budget_usd}"
            )

        return True, None

    def record_order_submitted(self, grid_id: str | None = None) -> None:
        self._state.orders_submitted += 1
        if grid_id is not None:
            self._state.active_grid_ids.add(grid_id)

    def record_realized_loss(self, loss_usd: Decimal) -> None:
        """Nur tatsaechliche VERLUSTE buchen (loss_usd > 0 bedeutet
        Verlust) - ein Gewinn erhoeht das Budget NICHT automatisch (siehe
        Modul-Docstring "kein 'wenn es gut laeuft, mehr Kapital'")."""
        if loss_usd <= 0:
            return
        self._state.cumulative_realized_loss_usd += loss_usd
        self._state.daily_realized_loss_usd += loss_usd

    def deactivate(self, reason: str) -> None:
        if self._state.deactivated:
            return
        self._state.deactivated = True
        self._state.deactivation_reason = reason
        log.critical("live_verification_gate.deactivated", reason=reason)
