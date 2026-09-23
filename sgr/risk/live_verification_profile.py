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

Verdrahtung (2026-09-24, Revision): check_live_verification_allowed()
wird von ExecutionEngine.execute() fuer JEDE LIVE-Order aufgerufen (nach
Preflight/Leverage/Quantization, vor dem eigentlichen Exchange-Call) -
siehe dortige Aufrufstelle. Der eigentliche LiveVerificationGate MUSS
dafuer per ExecutionEngine(..., live_verification_gate=...) injiziert
werden - OHNE Injektion (Default None) wird JEDE LIVE-Order abgelehnt
(fail-closed: "kein Operator-Budget hinterlegt" ist keine sichere
Annahme von "kein Budget noetig"). Fuer PAPER hat dieser Check in jedem
Fall KEINEN Effekt (sofortiges allowed=True, identisch zu
check_live_trading_allowed()).

Claude aktiviert diese Injektion nicht selbst und setzt keine Zahlen -
ein GridController/eine Strategie kann `LiveVerificationProfile` nicht
ueberschreiben (die Werte kommen ausschliesslich aus dem vom Operator
konstruierten, frozen Profile-Objekt).

Noch NICHT geschlossen: die Verlust-Buchung (record_realized_loss())
wird NICHT automatisch aus PortfolioEngine.on_order_filled() heraus
aufgerufen (das wuerde eine zusaetzliche Aenderung an der bestehenden,
produktiv genutzten Fill-Verarbeitung erfordern) - ein Aufrufer mit
Zugriff auf den tatsaechlich realisierten PnL einer LIVE-Closing-Order
muss gate.record_realized_loss(...) explizit aufrufen. Diese Luecke ist
im Abschlussbericht als offener Punkt benannt, nicht verschwiegen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sgr.core.logging import get_logger
from sgr.core.types import OrderRequest, Side, TradingMode

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
        # Gauge sofort mit dem tatsaechlichen Ist-Zustand initialisieren
        # (identische Begruendung wie KillSwitch.__init__: "keine Daten"
        # in Grafana ist fuer ein sicherheitskritisches Signal
        # irrefuehrend, nicht neutral).
        self._emit_metrics()

    @property
    def state(self) -> LiveVerificationState:
        return self._state

    def _emit_metrics(self) -> None:
        try:
            from sgr.monitoring.trading_metrics import record_live_verification_state

            profile = self._state.profile
            record_live_verification_state(
                approved_by=profile.approved_by,
                active=not self._state.deactivated,
                orders_submitted=self._state.orders_submitted,
                max_orders=profile.max_orders,
                loss_budget_remaining_usd=float(
                    max(
                        Decimal("0"),
                        profile.max_loss_usd - self._state.cumulative_realized_loss_usd,
                    )
                ),
                daily_loss_budget_remaining_usd=float(
                    max(
                        Decimal("0"),
                        profile.max_daily_loss_usd - self._state.daily_realized_loss_usd,
                    )
                ),
            )
        except Exception as e:
            # Metrik-Emission darf niemals eine Gate-Entscheidung
            # beeinflussen oder verhindern (gleiches Fail-Safe-Prinzip
            # wie ueberall sonst in dieser Datei).
            log.debug("live_verification_gate.metrics_emission_failed", error=str(e))

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
        self._emit_metrics()

    def record_realized_loss(self, loss_usd: Decimal) -> None:
        """Nur tatsaechliche VERLUSTE buchen (loss_usd > 0 bedeutet
        Verlust) - ein Gewinn erhoeht das Budget NICHT automatisch (siehe
        Modul-Docstring "kein 'wenn es gut laeuft, mehr Kapital'")."""
        if loss_usd <= 0:
            return
        self._state.cumulative_realized_loss_usd += loss_usd
        self._state.daily_realized_loss_usd += loss_usd
        self._emit_metrics()

    def deactivate(self, reason: str) -> None:
        if self._state.deactivated:
            return
        self._state.deactivated = True
        self._state.deactivation_reason = reason
        log.critical("live_verification_gate.deactivated", reason=reason)
        self._emit_metrics()


async def check_live_verification_allowed(
    order: OrderRequest,
    gate: LiveVerificationGate | None,
    exchange_pool: Any = None,
) -> tuple[bool, str | None]:
    """
    Aufrufstelle: ExecutionEngine.execute(), NACH Preflight/Leverage/
    Quantization (order.quantity ist final), VOR dem eigentlichen
    Exchange-Call - siehe dortigen Kommentar. Gibt (allowed, reason)
    zurueck, identisches Ergebnis-Format wie
    sgr.risk.live_trading_gate.check_live_trading_allowed().

    Gibt allowed=True SOFORT fuer jede PAPER-Order zurueck (dieses Gate
    betrifft ausschliesslich TradingMode.LIVE).

    Fail-closed in JEDEM Unsicherheitsfall:
        - gate is None ("kein Operator-Budget hinterlegt")
        - kein Preis bestimmbar (weder order.limit_price noch ein
          Ticker-Preis ueber exchange_pool verfuegbar)
        - jeder Fehler beim Ticker-Abruf

    notional_usd wird aus order.limit_price (falls gesetzt, z.B. ein
    RiskEngine-erzwungenes LIMIT bei hoher Slippage) oder sonst einem
    frisch abgerufenen Ticker-Preis berechnet - EIN zusaetzlicher
    Exchange-Read-Call, ausschliesslich fuer LIVE-Orders (fuer PAPER,
    der heute laufenden Produktionslast, voellig folgenlos).
    """
    if order.trading_mode != TradingMode.LIVE:
        return True, None

    if gate is None:
        return False, (
            "Live trading blocked: kein LiveVerificationGate konfiguriert "
            "(fail-closed - kein Operator-Budget hinterlegt)"
        )

    price = order.limit_price
    if price is None:
        if exchange_pool is None:
            return False, (
                "Live trading blocked: kein Preis fuer LiveVerificationGate "
                "bestimmbar (kein Exchange-Pool verfuegbar)"
            )
        try:
            adapter = exchange_pool.get(order.symbol.exchange, TradingMode.LIVE)
            ticker = await adapter.get_ticker(order.symbol.ccxt_symbol)
            price = ticker.ask if order.side == Side.BUY else ticker.bid
        except Exception as e:
            return False, (
                f"Live trading blocked: Preis fuer LiveVerificationGate nicht "
                f"abrufbar ({e})"
            )

    try:
        notional_usd = order.quantity * price
        try:
            leverage = Decimal(str(order.metadata.get("target_leverage", "1")))
        except (ValueError, ArithmeticError):
            leverage = Decimal("1")
        grid_id = order.metadata.get("grid_id")

        return gate.check_order_allowed(
            notional_usd=notional_usd, leverage=leverage, grid_id=grid_id
        )
    except Exception as e:
        # Fail-closed: ein unerwarteter Fehler in der Notional-Berechnung
        # oder im Gate selbst (z.B. ein nicht-numerischer Preis aus einem
        # fehlerhaft konfigurierten Test-Double/Adapter) darf niemals als
        # unbehandelte Exception aus dem Order-Pfad propagieren - er
        # blockiert die Order stattdessen explizit.
        return False, f"Live trading blocked: LiveVerificationGate-Auswertung fehlgeschlagen ({e})"
