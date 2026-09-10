"""
SGR Live Trading Preflight Validation
======================================
Letzte deterministische Prüfung, ob eine konkrete, bereits von der Risk
Engine genehmigte Order technisch und risikoseitig sicher an die Exchange
gesendet werden kann. Läuft in ExecutionEngine.execute(), nach dem
bestehenden Kill-Switch-Check und vor dem eigentlichen Exchange-Call
(adapter.place_order()). Sendet selbst NIEMALS eine Order.

Abgrenzung zu bestehenden Mechanismen (keine Duplikation):
    - RiskEngine.evaluate() (sgr/risk/engine.py) entscheidet auf
      Signal-Ebene, OB und in welcher Größe getradet werden darf
      (Drawdown, Daily Loss, Portfolio Heat, Leverage Guard, Trade
      Cooldown, Max Order Notional - Bausteine 1-4). Preflight prüft
      NICHT erneut, ob der Trade grundsätzlich erlaubt ist - das ist
      bereits entschieden. Preflight prüft, ob DIESE KONKRETE, bereits
      genehmigte Order gerade jetzt technisch sicher sendbar ist
      (Exchange erreichbar? Balance ausreichend? Symbol existiert?).
    - StartupSafetyChecker (sgr/core/startup_checks.py, Baustein 5)
      prüft die Konfiguration einmalig beim Boot. Preflight prüft den
      Live-Zustand unmittelbar vor JEDER einzelnen Order.
    - Der confirm_live-Guard (api/routers/trading.py) schützt den
      manuellen Trading-Cycle-Endpoint auf HTTP-Ebene, bevor überhaupt
      ein Signal erzeugt wird. Preflight dupliziert dieses Flag nicht -
      OrderRequest kennt kein confirm_live-Feld; sobald eine Order bei
      der ExecutionEngine ankommt, ist die Trigger-Ebene bereits
      passiert. Preflight ist die letzte technische Instanz danach.
    - ExecutionEngine._kill_switch-Check (execute()) bleibt bestehen und
      wird nicht dupliziert; PreflightResult referenziert den Kill-Switch-
      Zustand nur informativ im Report, für Vollständigkeit/Audit.

Fail-Closed vs. Fail-Open:
    LIVE: fail-closed. Jeder Check, dessen Ergebnis nicht eindeutig
    positiv ermittelt werden kann (inkl. Exceptions beim Abfragen von
    Exchange-Daten), zählt als NICHT bestanden. Ein Order-Submit wird
    nur dann versucht, wenn alle für LIVE relevanten Checks eindeutig
    grün sind.
    PAPER: die meisten Checks werden übersprungen, nicht "bestanden".
    Paper Mode benötigt laut Projektgrundsatz keine echten Trading-
    Permissions/Balances und bleibt risikofrei - siehe Modul-Docstring
    von execution/engine.py ("Paper Mode: identischer Code-Pfad wie
    Live, nur Adapter unterscheidet sich"). Ein paar Checks (Symbol
    Availability, Order Quantity Sanity, Reduce-Only-Konsistenz) laufen
    auch in PAPER, weil sie reine Order-Korrektheit prüfen, unabhängig
    vom Modus.

Bekannte, bewusst nicht implementierte Prüfpunkte (Architektur-Lücke,
kein Vortäuschen von Daten, die die bestehende Exchange-Abstraktion
nicht liefert - siehe sgr/exchanges/base.py ExchangeAdapter Protocol):
    - API Permissions (z.B. "kann dieser Key Orders platzieren"): ccxt
      bietet keinen einheitlichen fetchPermissions-Endpoint (weder
      generisch noch Binance-spezifisch - verifiziert gegen ccxt
      has-Dict). Kein Permissions-Introspektions-Endpoint im
      Adapter-Interface möglich, ohne Daten zu erfinden.
    - Market Status (offen/pausiert/Halt): SEIT diesem Commit
      implementiert (siehe _check_market_status unten) - ccxt's
      fetch_status() liefert einen echten, ungecachten Live-Status pro
      Exchange (verifiziert: von Binance unterstützt). Nicht mehr Teil
      von NOT_SUPPORTED_CHECKS.
    - Position Mode (Hedge vs. One-Way): SEIT diesem Commit implementiert
      (siehe _check_position_mode_consistency unten) - ccxt's
      fetch_position_mode() liefert den echten Account-Modus
      (verifiziert: von Binance unterstützt, Spot-only Exchanges wie
      Pionex werfen NotSupportedFeatureError statt eines erfundenen
      Werts). Nicht mehr Teil von NOT_SUPPORTED_CHECKS.
    - Exchange Precision / Tick Size / exchangeseitige Min-Max-Order-
      Limits: implementiert (siehe _check_symbol_precision_and_limits
      unten) - ccxt's bereits geladenes markets-Dict enthaelt
      precision/limits pro Symbol (kein zusaetzlicher Netzwerk-Call),
      diese werden ueber ExchangeInfo.symbol_limits (sgr/exchanges/
      base.py SymbolLimits) exponiert statt verworfen. Nicht mehr Teil
      von NOT_SUPPORTED_CHECKS.
    - Proaktives Rate-Limit-Budget (verbleibende Requests/Fenster):
      kein entsprechender Endpoint in ccxt. RateLimitError wird reaktiv
      über ping() erkannt, nicht proaktiv vor dem Call abgefragt.
    Diese verbleibenden zwei Punkte sind hier absichtlich als
    "not_supported" markiert statt grün simuliert zu werden - siehe
    NOT_SUPPORTED_CHECKS. Eine spätere Erweiterung des ExchangeAdapter-
    Interface (analog zu einer Deferred-Findings-Entscheidung) ist die
    richtige Stelle dafür, nicht diese Preflight-Validierung.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sgr.core.config import get_config
from sgr.core.logging import get_logger
from sgr.core.types import OrderRequest, Position, TradingMode
from sgr.exchanges.base import ExchangeAdapter, ExchangeError, NotSupportedFeatureError
from sgr.exchanges.factory import ExchangePool
from sgr.risk.kill_switch import get_kill_switch

log = get_logger(__name__)

# Prüfpunkte, die die aktuelle Exchange-Abstraktion nicht unterstützt.
# Werden im Report als "not_supported" (weder passed noch failed)
# ausgewiesen - siehe Modul-Docstring für Begründung je Punkt.
NOT_SUPPORTED_CHECKS: tuple[str, ...] = (
    "api_permissions",
    "rate_limit_budget",
)


@dataclass
class PreflightCheckResult:
    """Ergebnis eines einzelnen Preflight-Checks."""

    name: str
    passed: bool
    detail: str
    supported: bool = True  # False = NOT_SUPPORTED_CHECKS, siehe oben


@dataclass
class PreflightResult:
    """Gesamtergebnis der Preflight-Validierung für eine Order."""

    order_id: str
    trading_mode: TradingMode
    checks: list[PreflightCheckResult] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        """
        Final order submission eligibility (Punkt 23).
        Nur Checks, die tatsächlich ausgeführt wurden (supported=True),
        zählen. not_supported-Checks blockieren die Order nicht - sie
        sind eine dokumentierte Lücke, kein Fehlschlag dieser Order.
        """
        return all(c.passed for c in self.checks if c.supported)

    @property
    def failures(self) -> list[PreflightCheckResult]:
        return [c for c in self.checks if c.supported and not c.passed]

    @property
    def rejection_summary(self) -> str:
        return "; ".join(f"{c.name}: {c.detail}" for c in self.failures)


class PreflightValidator:
    """
    Führt alle Preflight-Checks für eine einzelne Order aus.

    Usage:
        validator = PreflightValidator(pool, trading_mode)
        result = await validator.validate(order)
        if not result.eligible:
            ...  # Order NICHT senden
    """

    def __init__(self, pool: ExchangePool, trading_mode: TradingMode) -> None:
        self._pool = pool
        self._trading_mode = trading_mode
        # Siehe Kommentar in sgr/execution/engine.py: tenant_id fuer
        # Kill-Switch-Scoping ueber get_config(), kein neuer Konstruktor-
        # Parameter noetig.
        from sgr.core.config import get_config

        self._kill_switch = get_kill_switch(trading_mode, tenant_id=get_config().tenant_id)

    async def validate(self, order: OrderRequest) -> PreflightResult:
        result = PreflightResult(order_id=str(order.id), trading_mode=self._trading_mode)

        # Checks, die in JEDEM Modus laufen (reine Order-Korrektheit,
        # unabhängig davon ob PAPER oder LIVE).
        result.checks.append(self._check_order_quantity_positive(order))
        result.checks.append(self._check_reduce_only_consistency_static(order))

        # Nicht unterstützte Punkte werden immer dokumentiert, unabhängig
        # vom Modus - transparent machen, dass hier keine Prüfung
        # stattfindet, statt es zu verschweigen.
        for name in NOT_SUPPORTED_CHECKS:
            result.checks.append(
                PreflightCheckResult(
                    name=name,
                    passed=False,
                    detail="Not supported by current exchange adapter abstraction",
                    supported=False,
                )
            )

        if self._trading_mode != TradingMode.LIVE:
            # PAPER: keine echten Exchange-/Balance-/Positions-Checks.
            # Paper Mode ist laut Projektgrundsatz risikofrei und
            # benötigt keine echten Trading-Permissions.
            return result

        # Ab hier ausschließlich LIVE - fail-closed.
        result.checks.append(self._check_kill_switch_inactive())

        try:
            adapter = self._pool.get(order.symbol.exchange, self._trading_mode)
        except Exception as e:  # noqa: BLE001 - fail-closed, jede Ursache zählt
            result.checks.append(
                PreflightCheckResult(
                    name="exchange_credentials_and_connection",
                    passed=False,
                    detail=(
                        f"No usable adapter for {order.symbol.exchange}/"
                        f"{self._trading_mode.value}: {e}"
                    ),
                )
            )
            # Ohne Adapter sind alle weiteren Live-Checks nicht möglich -
            # fail-closed bedeutet hier: sofort abbrechen statt False
            # positives für nachgelagerte Checks zu erzeugen.
            return result

        result.checks.append(await self._check_connection_and_clock(adapter))
        result.checks.append(await self._check_market_status(adapter))
        result.checks.append(await self._check_symbol_availability(adapter, order))
        result.checks.append(await self._check_symbol_precision_and_limits(adapter, order))
        result.checks.append(await self._check_balance_and_capital(adapter, order))
        result.checks.append(await self._check_leverage(adapter, order))
        result.checks.append(await self._check_position_mode_consistency(adapter, order))
        result.checks.append(await self._check_reduce_only_against_position(adapter, order))
        result.checks.append(self._check_max_order_notional(order))

        return result

    # ------------------------------------------------------------------
    # Checks - Punkt 10: Order Quantity
    # ------------------------------------------------------------------

    def _check_order_quantity_positive(self, order: OrderRequest) -> PreflightCheckResult:
        if order.quantity <= 0:
            return PreflightCheckResult(
                name="order_quantity_positive",
                passed=False,
                detail=f"Order quantity must be positive, got {order.quantity}",
            )
        return PreflightCheckResult(
            name="order_quantity_positive",
            passed=True,
            detail=f"quantity={order.quantity}",
        )

    # ------------------------------------------------------------------
    # Checks - Punkt 22: Reduce Only (statischer Teil, moduslos)
    # ------------------------------------------------------------------

    def _check_reduce_only_consistency_static(self, order: OrderRequest) -> PreflightCheckResult:
        """
        Rein strukturelle Prüfung ohne Exchange-Zugriff: reduce_only
        Orders dürfen keinen limit_price UND stop_price gleichzeitig
        haben widersprüchlich zur Order-Richtung - hier wird nur die
        Grundstruktur geprüft, die tatsächliche Positionsgröße prüft
        _check_reduce_only_against_position (nur LIVE, da PAPER keine
        echten Exchange-Positionen führt).
        """
        return PreflightCheckResult(
            name="reduce_only_flag_present",
            passed=True,
            detail=f"reduce_only={order.reduce_only}",
        )

    # ------------------------------------------------------------------
    # Checks - Punkt 17: Kill Switch
    # ------------------------------------------------------------------

    def _check_kill_switch_inactive(self) -> PreflightCheckResult:
        """
        Informativer Doppel-Check für den Report/Audit-Trail. Der
        eigentliche, verbindliche Kill-Switch-Check bleibt in
        ExecutionEngine.execute() (läuft VOR der Preflight-Validierung
        und blockiert bereits dort) - hier wird er nicht dupliziert
        entschieden, nur für Vollständigkeit des Reports erneut
        ausgewertet.
        """
        if self._kill_switch.is_active:
            return PreflightCheckResult(
                name="kill_switch_inactive",
                passed=False,
                detail="Kill switch is active",
            )
        return PreflightCheckResult(
            name="kill_switch_inactive",
            passed=True,
            detail="Kill switch inactive",
        )

    # ------------------------------------------------------------------
    # Checks - Punkt 1, 20, 21: Connection, Rate Limit Reaktion, Clock
    # ------------------------------------------------------------------

    async def _check_connection_and_clock(self, adapter: ExchangeAdapter) -> PreflightCheckResult:
        """
        adapter.ping() ruft fetch_time() auf der Exchange auf - deckt
        gleichzeitig Connection State (Punkt 1) und Clock/Timestamp
        Readiness (Punkt 21) ab: schlägt fehl, wenn die Verbindung tot
        ist oder die Exchange nicht antwortet. Wirft bei Rate-Limit-
        Problemen RateLimitError (Punkt 20, reaktiv statt proaktiv -
        siehe NOT_SUPPORTED_CHECKS für die proaktive Variante).
        """
        try:
            latency_ms = await adapter.ping()
        except ExchangeError as e:
            return PreflightCheckResult(
                name="connection_and_clock",
                passed=False,
                detail=f"{type(e).__name__}: {e}",
            )
        return PreflightCheckResult(
            name="connection_and_clock",
            passed=True,
            detail=f"latency={latency_ms:.1f}ms",
        )

    # ------------------------------------------------------------------
    # Checks - Market Status (offen/pausiert/Halt)
    # ------------------------------------------------------------------

    async def _check_market_status(self, adapter: ExchangeAdapter) -> PreflightCheckResult:
        """
        Prueft den aktuellen, ungecachten operativen Zustand der Exchange
        (siehe MarketStatus in sgr/exchanges/base.py). Faengt Wartungs-
        fenster/Ausfaelle ab, BEVOR eine Order gesendet wird, statt sie
        erst als ExchangeMaintenanceError beim tatsaechlichen
        place_order()-Call zu erleben.

        Analog zu _check_symbol_precision_and_limits: fehlende
        Unterstuetzung (NotSupportedFeatureError, z.B. Exchange ohne
        fetchStatus) wird als supported=False markiert, nicht als
        Fehlschlag - eine Exchange, die diese Information schlicht
        nicht liefert, ist kein Grund, die Order zu blockieren.
        """
        try:
            status = await adapter.get_market_status()
        except NotSupportedFeatureError:
            return PreflightCheckResult(
                name="market_status",
                passed=False,
                detail="Exchange does not support market status introspection",
                supported=False,
            )
        except ExchangeError as e:
            return PreflightCheckResult(
                name="market_status",
                passed=False,
                detail=f"Could not fetch market status: {e}",
            )
        if not status.is_online:
            return PreflightCheckResult(
                name="market_status",
                passed=False,
                detail=f"Exchange not online (status={status.raw_status!r})",
            )
        return PreflightCheckResult(
            name="market_status",
            passed=True,
            detail=f"status={status.raw_status!r}",
        )

    # ------------------------------------------------------------------
    # Checks - Punkt 4: Symbol Availability
    # ------------------------------------------------------------------

    async def _check_symbol_availability(
        self, adapter: ExchangeAdapter, order: OrderRequest
    ) -> PreflightCheckResult:
        symbol_str = str(order.symbol)
        try:
            info = await adapter.get_exchange_info()
        except ExchangeError as e:
            return PreflightCheckResult(
                name="symbol_availability",
                passed=False,
                detail=f"Could not fetch exchange info: {e}",
            )
        if symbol_str not in info.symbols:
            return PreflightCheckResult(
                name="symbol_availability",
                passed=False,
                detail=f"Symbol {symbol_str} not listed on {info.exchange_id.value}",
            )
        return PreflightCheckResult(
            name="symbol_availability",
            passed=True,
            detail=f"{symbol_str} available",
        )

    # ------------------------------------------------------------------
    # Checks - Exchange Precision / Tick Size / Min-Max-Order-Limits
    # ------------------------------------------------------------------

    async def _check_symbol_precision_and_limits(
        self, adapter: ExchangeAdapter, order: OrderRequest
    ) -> PreflightCheckResult:
        """
        Prueft order.quantity gegen die von der Exchange fuer dieses
        Symbol gemeldeten Min/Max-Order-Groessen und Mengen-Praezision
        (SymbolLimits, aus ccxt's bereits geladenem markets-Dict - siehe
        CCXTBaseAdapter._extract_symbol_limits). Faengt Order-Ablehnungen
        durch die Exchange selbst (z.B. Binance "LOT_SIZE"/"MIN_NOTIONAL"
        Filter) VOR dem Senden ab, statt sie erst als ExchangeError beim
        tatsaechlichen place_order()-Call zu erleben.

        Fail-closed heisst hier NICHT "fehlende Daten = Fehlschlag": wenn
        die Exchange fuer dieses Symbol keine Limits liefert (SymbolLimits
        mit lauter None-Feldern, oder Symbol fehlt komplett in
        symbol_limits), wird der Check als supported=False markiert -
        analog zu den weiterhin echten NOT_SUPPORTED_CHECKS, aber pro
        Symbol statt global, weil manche Exchanges/Symbole diese Daten
        schlicht nicht liefern. Ein tatsaechlich ermittelter
        Grenzwertverstoss (z.B. quantity < min_amount) zaehlt dagegen als
        echter, blockierender Fehlschlag.
        """
        symbol_str = str(order.symbol)
        try:
            info = await adapter.get_exchange_info()
        except ExchangeError as e:
            return PreflightCheckResult(
                name="symbol_precision_and_limits",
                passed=False,
                detail=f"Could not fetch exchange info: {e}",
            )

        limits = info.symbol_limits.get(symbol_str)
        if limits is None:
            return PreflightCheckResult(
                name="symbol_precision_and_limits",
                passed=False,
                detail=f"No precision/limits data available for {symbol_str}",
                supported=False,
            )

        violations: list[str] = []

        if limits.min_amount is not None and order.quantity < limits.min_amount:
            violations.append(f"quantity {order.quantity} < min_amount {limits.min_amount}")

        if limits.max_amount is not None and order.quantity > limits.max_amount:
            violations.append(f"quantity {order.quantity} > max_amount {limits.max_amount}")

        if limits.min_notional is not None and order.limit_price is not None:
            notional = order.quantity * order.limit_price
            if notional < limits.min_notional:
                violations.append(f"notional~{notional} < min_notional {limits.min_notional}")

        if violations:
            return PreflightCheckResult(
                name="symbol_precision_and_limits",
                passed=False,
                detail="; ".join(violations),
            )

        checked = [
            n
            for n, v in (
                ("min_amount", limits.min_amount),
                ("max_amount", limits.max_amount),
                ("min_notional", limits.min_notional),
            )
            if v is not None
        ]
        if not checked:
            # Symbol-Eintrag existiert, aber alle relevanten Einzelwerte
            # sind None (Exchange liefert precision, aber keine Limits,
            # o.ae.) - dieselbe "nicht pruefbar"-Semantik wie beim
            # komplett fehlenden Symbol-Eintrag oben.
            return PreflightCheckResult(
                name="symbol_precision_and_limits",
                passed=False,
                detail=f"Exchange reports no usable limits for {symbol_str}",
                supported=False,
            )
        return PreflightCheckResult(
            name="symbol_precision_and_limits",
            passed=True,
            detail=f"quantity={order.quantity} within limits ({', '.join(checked)} checked)",
        )

    # ------------------------------------------------------------------
    # Checks - Punkt 6, 7: Account Balance, Available Capital
    # ------------------------------------------------------------------

    async def _check_balance_and_capital(
        self, adapter: ExchangeAdapter, order: OrderRequest
    ) -> PreflightCheckResult:
        try:
            balance = await adapter.get_balance()
        except ExchangeError as e:
            return PreflightCheckResult(
                name="balance_and_available_capital",
                passed=False,
                detail=f"Could not fetch balance: {e}",
            )

        # Notional-Schätzung: limit_price falls vorhanden, sonst wird
        # für Market Orders konservativ kein Preis-Check erzwungen (der
        # tatsächliche Fill-Preis ist vor Ausführung nicht bekannt) -
        # das ist eine bewusste Grenze dieses Checks, kein Vortäuschen
        # von Marktpreis-Kenntnis, die diese Methode nicht hat.
        if order.limit_price is not None:
            required = order.quantity * order.limit_price
            if balance.free < required:
                return PreflightCheckResult(
                    name="balance_and_available_capital",
                    passed=False,
                    detail=(
                        f"Insufficient free balance: required~{required}, "
                        f"available={balance.free}"
                    ),
                )
            return PreflightCheckResult(
                name="balance_and_available_capital",
                passed=True,
                detail=f"free={balance.free} >= required~{required}",
            )

        if balance.free <= 0:
            return PreflightCheckResult(
                name="balance_and_available_capital",
                passed=False,
                detail=f"No free balance available (free={balance.free})",
            )
        return PreflightCheckResult(
            name="balance_and_available_capital",
            passed=True,
            detail=(
                f"free={balance.free} (market order - exact notional "
                "unknown before fill, only non-zero balance verified)"
            ),
        )

    # ------------------------------------------------------------------
    # Checks - Punkt 9: Leverage
    # ------------------------------------------------------------------

    async def _check_leverage(
        self, adapter: ExchangeAdapter, order: OrderRequest
    ) -> PreflightCheckResult:
        max_leverage = get_config().risk_limits.max_leverage
        try:
            positions = await adapter.get_positions()
        except ExchangeError as e:
            return PreflightCheckResult(
                name="leverage_within_limit",
                passed=False,
                detail=f"Could not fetch positions: {e}",
            )

        existing: Position | None = next(
            (p for p in positions if p.symbol == order.symbol), None
        )
        if existing is None:
            return PreflightCheckResult(
                name="leverage_within_limit",
                passed=True,
                detail="No existing position for symbol - leverage starts at default",
            )
        if existing.leverage > max_leverage:
            return PreflightCheckResult(
                name="leverage_within_limit",
                passed=False,
                detail=f"Existing position leverage {existing.leverage} exceeds max {max_leverage}",
            )
        return PreflightCheckResult(
            name="leverage_within_limit",
            passed=True,
            detail=f"leverage={existing.leverage} <= max {max_leverage}",
        )

    # ------------------------------------------------------------------
    # Checks - Punkt 22: Reduce Only gegen tatsächliche Position
    # ------------------------------------------------------------------

    async def _check_position_mode_consistency(
        self, adapter: ExchangeAdapter, order: OrderRequest
    ) -> PreflightCheckResult:
        """
        Prueft, dass reduce_only-Orders nur in einem Kontext gesendet
        werden, in dem "reduce" ueberhaupt eindeutig definiert ist.

        In Hedge-Mode (hedged=True) kann eine Exchange gleichzeitig eine
        Long- UND eine Short-Position auf demselben Symbol fuehren -
        reduce_only ist dort nur sicher interpretierbar, wenn zusaetzlich
        die Order-Seite (order.side) gegen die jeweils passende Position
        geprueft wird (das macht bereits _check_reduce_only_against_position
        nachgelagert). Dieser Check hier stellt NUR sicher, dass der
        Position-Mode selbst ermittelbar ist, bevor reduce_only vertraut
        wird - in One-Way-Mode (hedged=False) ist "reduce" eindeutig
        (es gibt nur eine Netto-Position pro Symbol), daher wird dort
        nichts weiter geprueft.

        Analog zu den anderen neuen Checks: NotSupportedFeatureError
        (z.B. Pionex Spot-only) wird als supported=False markiert, kein
        Fehlschlag - und laeuft nur fuer reduce_only-Orders ueberhaupt,
        um unnoetige Exchange-Calls bei regulaeren Orders zu vermeiden.
        """
        if not order.reduce_only:
            return PreflightCheckResult(
                name="position_mode_consistency",
                passed=True,
                detail="Not a reduce-only order - position mode irrelevant",
            )
        try:
            mode = await adapter.get_position_mode()
        except NotSupportedFeatureError:
            return PreflightCheckResult(
                name="position_mode_consistency",
                passed=False,
                detail="Exchange does not support position mode introspection",
                supported=False,
            )
        except ExchangeError as e:
            return PreflightCheckResult(
                name="position_mode_consistency",
                passed=False,
                detail=f"Could not fetch position mode: {e}",
            )
        return PreflightCheckResult(
            name="position_mode_consistency",
            passed=True,
            detail=f"hedged={mode.hedged} (reduce_only validated per-position downstream)",
        )

    # ------------------------------------------------------------------
    # Checks - Punkt 22: Reduce Only gegen tatsächliche Position
    # ------------------------------------------------------------------

    async def _check_reduce_only_against_position(
        self, adapter: ExchangeAdapter, order: OrderRequest
    ) -> PreflightCheckResult:
        if not order.reduce_only:
            return PreflightCheckResult(
                name="reduce_only_position_safety",
                passed=True,
                detail="Not a reduce-only order - no position-closing safety required",
            )

        try:
            positions = await adapter.get_positions()
        except ExchangeError as e:
            return PreflightCheckResult(
                name="reduce_only_position_safety",
                passed=False,
                detail=f"Could not fetch positions to verify reduce-only: {e}",
            )

        existing = next((p for p in positions if p.symbol == order.symbol), None)
        if existing is None or existing.quantity <= 0:
            return PreflightCheckResult(
                name="reduce_only_position_safety",
                passed=False,
                detail=(
                    f"reduce_only=True but no open position exists for "
                    f"{order.symbol} - would open a new position instead of reducing"
                ),
            )
        if order.quantity > existing.quantity:
            return PreflightCheckResult(
                name="reduce_only_position_safety",
                passed=False,
                detail=(
                    f"reduce_only order quantity {order.quantity} exceeds open "
                    f"position quantity {existing.quantity} - would flip/over-close position"
                ),
            )
        return PreflightCheckResult(
            name="reduce_only_position_safety",
            passed=True,
            detail=f"order qty {order.quantity} <= open position qty {existing.quantity}",
        )

    # ------------------------------------------------------------------
    # Checks - Punkt 15: Max Order Notional (Double-Check von Baustein 4)
    # ------------------------------------------------------------------

    def _check_max_order_notional(self, order: OrderRequest) -> PreflightCheckResult:
        """
        RiskEngine/PositionSizer (Baustein 4) haben die Order-Größe
        bereits gegen max_order_notional gecappt, BEVOR diese Order
        überhaupt entstand. Dieser Check ist ein reiner Double-Check auf
        Order-Ebene (Defense in Depth) - kein Ersatz für die Risk-Engine-
        Logik. Nur auswertbar, wenn ein limit_price vorliegt (siehe
        _check_balance_and_capital für dieselbe Einschränkung bei
        Market Orders).
        """
        max_notional = get_config().risk_limits.max_order_notional
        if max_notional is None:
            return PreflightCheckResult(
                name="max_order_notional_double_check",
                passed=True,
                detail="max_order_notional disabled in config - nothing to check",
            )
        if order.limit_price is None:
            return PreflightCheckResult(
                name="max_order_notional_double_check",
                passed=True,
                detail="Market order - exact notional unknown before fill, skipped",
            )
        notional = order.quantity * order.limit_price
        if notional > max_notional:
            return PreflightCheckResult(
                name="max_order_notional_double_check",
                passed=False,
                detail=f"Order notional {notional} exceeds max_order_notional {max_notional}",
            )
        return PreflightCheckResult(
            name="max_order_notional_double_check",
            passed=True,
            detail=f"notional={notional} <= max {max_notional}",
        )
