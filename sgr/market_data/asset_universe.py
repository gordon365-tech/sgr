"""
SGR Asset Universe / Market Discovery
======================================
Zentrale, exchange-uebergreifende Sicht auf welche Maerkte tatsaechlich
existieren, und in welchem Stadium sie fuer SGR nutzbar sind.

Warum ein eigenes Modul statt die Symbol-Subscription-Liste in
sgr/api/main.py einfach zu erweitern?
    LIVE_MARKET_DATA_SYMBOLS (sgr/api/main.py) ist eine bewusst manuell
    kuratierte, sicherheitsrelevante Liste ("welche Symbole bekommen
    tatsaechlich Candle-Feeds und koennen dadurch gehandelt werden") -
    das darf niemals automatisch aus einer Exchange-Discovery befuellt
    werden (siehe Klassifikations-Kaskade unten: DISCOVERED bedeutet
    NICHT ACTIVE). Dieses Modul beantwortet die vorgelagerte, rein
    informative Frage "was existiert und waere technisch handelbar",
    komplett getrennt von der Frage "was handelt SGR gerade wirklich".

Klassifikations-Kaskade (siehe Task-Vorgabe "Asset Universe
implementieren"):
    DISCOVERED  - Markt wurde bei der letzten Discovery gefunden (roh).
    SUPPORTED   - Quote-Waehrung ist USDT (SGRs durchgaengige Annahme -
                  Portfolio/Risk/Balance sind ueberall USDT-denominiert,
                  siehe sgr/exchanges/base.py Balance-Docstring) UND der
                  Markt ist laut Exchange aktiv UND vom erwarteten
                  Markttyp fuer diese Exchange (Binance: "swap" -
                  SGRs Binance-Adapter laeuft ausschliesslich im
                  Futures-Modus, siehe BinanceAdapter.futures_mode;
                  Pionex: "spot" - Pionex bietet in SGR nur Spot).
    TRADABLE    - SUPPORTED UND SGRs Order-Ausfuehrung kann diese
                  Exchange technisch ansprechen (execution_supported)
                  UND ccxt/Pionex liefert genug Precision/Limit-Daten,
                  um eine Order ueberhaupt sicher zu konstruieren
                  (siehe sgr/execution/preflight.py
                  _check_symbol_precision_and_limits - dieselben Felder).
    SUBSCRIBED  - TRADABLE UND das Symbol ist Teil der kuratierten
                  Market-Data-Subscription-Liste dieser Exchange
                  (LIVE_MARKET_DATA_SYMBOLS fuer Binance) - bekommt
                  dadurch tatsaechlich Candle-Feeds.
    ACTIVE      - SUBSCRIBED UND mindestens eine Strategie ist im
                  StrategyRegistry aktuell aktiv. SGRs Strategien sind
                  nicht symbol-spezifisch (siehe StrategyRegistry.
                  get_active(), keine Symbol-Filterung) - "aktiv" heisst
                  hier deshalb "wird vom Orchestrator fuer JEDES
                  subscribed Symbol ausgewertet", nicht "hat eine
                  eigene Subscription pro Symbol".

Wichtiger Befund waehrend der Implementierung (siehe execution_supported
Parameter): ccxt 4.5.78 (installierte Version) hat KEIN "pionex"-Modul
mehr/nie gehabt (ccxt.exchanges enthaelt "pionex" nicht - empirisch
verifiziert). sgr.exchanges.pionex.PionexAdapter (ccxt-basiert) wuerde
bei jedem connect()-Versuch mit AttributeError abstuerzen - Pionex-
Trading ist mit der aktuellen ccxt-Version schlicht nicht funktionsfaehig.
Pionex-Discovery in diesem Modul nutzt deshalb NICHT PionexAdapter,
sondern den bereits vorhandenen, aber bisher nirgends verdrahteten
sgr.exchanges.pionex_client.PionexClient (eigener REST-Client gegen
Pionex' oeffentliche /api/v1/common/symbols - echte Daten, kein Ersatz-
Fake). execution_supported ist fuer Pionex deshalb IMMER False - Pionex-
Maerkte werden korrekt bis maximal SUPPORTED klassifiziert, niemals
TRADABLE, bis die ccxt-Integration (oder ein Ersatz dafuer) tatsaechlich
funktioniert. Das ist kein Bug dieses Moduls, sondern eine ehrliche
Abbildung eines bestehenden Integrationsluecke.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from sgr.core.logging import get_logger
from sgr.core.types import ExchangeID
from sgr.exchanges.base import ExchangeError, MarketInfo

if TYPE_CHECKING:
    from sgr.exchanges.ccxt_base import CCXTBaseAdapter
    from sgr.strategy.registry import StrategyRegistry

log = get_logger(__name__)


class AssetStatus:
    """Reihenfolge ist bedeutsam (siehe RANK) - jede Stufe setzt die
    vorherige voraus, siehe Modul-Docstring fuer die Kaskade."""

    DISCOVERED = "discovered"
    SUPPORTED = "supported"
    TRADABLE = "tradable"
    SUBSCRIBED = "subscribed"
    ACTIVE = "active"


# Numerischer Rang fuer den Gauge-Wert (siehe sgr/monitoring/metrics.py
# record_asset_universe_snapshot) - erlaubt z.B. eine Grafana-Tabelle,
# nach Reifegrad zu sortieren, ohne den String-Namen parsen zu muessen.
RANK: dict[str, int] = {
    AssetStatus.DISCOVERED: 0,
    AssetStatus.SUPPORTED: 1,
    AssetStatus.TRADABLE: 2,
    AssetStatus.SUBSCRIBED: 3,
    AssetStatus.ACTIVE: 4,
}

_PIONEX_PUBLIC_SYMBOLS_QUOTE = "USDT"


class AssetUniverseEntry:
    """Ein Markt + seine aktuelle SGR-Klassifikation."""

    __slots__ = ("market", "status", "reason")

    def __init__(self, market: MarketInfo, status: str, reason: str) -> None:
        self.market = market
        self.status = status
        self.reason = reason

    def __repr__(self) -> str:
        return (
            f"AssetUniverseEntry({self.market.exchange_id.value}, "
            f"{self.market.symbol}, status={self.status})"
        )


def classify_asset_status(
    market: MarketInfo,
    *,
    expected_market_type: str,
    execution_supported: bool,
    subscribed_symbols: set[str],
    has_active_strategy: bool,
) -> AssetUniverseEntry:
    """
    Reine Funktion (keine I/O) - siehe Modul-Docstring fuer die Kaskade.
    Wandert die Stufen aufsteigend durch und stoppt bei der ersten nicht
    erfuellten Bedingung; der zugehoerige `reason`-String beschreibt
    genau diese Bedingung (fuer Logging/Debugging, kein Prometheus-Label -
    zu viele moegliche Werte fuer sinnvolle Kardinalitaet).
    """
    if market.quote_asset != "USDT":
        return AssetUniverseEntry(market, AssetStatus.DISCOVERED, "quote_not_usdt")
    if not market.active:
        return AssetUniverseEntry(market, AssetStatus.DISCOVERED, "inactive_on_exchange")
    if market.market_type != expected_market_type:
        return AssetUniverseEntry(
            market, AssetStatus.DISCOVERED, f"unexpected_market_type:{market.market_type}"
        )

    if not execution_supported:
        return AssetUniverseEntry(market, AssetStatus.SUPPORTED, "execution_not_supported")
    if market.amount_precision is None or market.price_precision is None:
        return AssetUniverseEntry(market, AssetStatus.SUPPORTED, "missing_precision")
    if market.min_amount is None:
        return AssetUniverseEntry(market, AssetStatus.SUPPORTED, "missing_min_amount")

    if market.symbol not in subscribed_symbols:
        return AssetUniverseEntry(market, AssetStatus.TRADABLE, "not_subscribed")

    if not has_active_strategy:
        return AssetUniverseEntry(market, AssetStatus.SUBSCRIBED, "no_active_strategy")

    return AssetUniverseEntry(market, AssetStatus.ACTIVE, "active_strategy_and_subscribed")


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def pionex_symbol_to_market_info(
    raw: dict[str, Any], *, discovered_at: datetime
) -> MarketInfo | None:
    """
    Uebersetzt einen einzelnen Eintrag aus PionexClient.get_symbols()
    (siehe sgr/exchanges/pionex_client.py) in MarketInfo. Gibt None
    zurueck, wenn Pflichtfelder fehlen (defensiv - Pionex' Antwort ist
    kein von SGR kontrolliertes Schema).

    Pionex-Symbole nutzen "_" als Trenner (z.B. "BTC_USDT") statt SGRs
    kanonisches "/" (siehe Symbol.ccxt_symbol in sgr/core/types.py) -
    hier normalisiert, damit Prometheus-Labels/Grafana-Variablen exakt
    dieselbe Darstellung sehen wie bei Binance.
    """
    base = raw.get("baseCurrency")
    quote = raw.get("quoteCurrency")
    if not base or not quote:
        return None

    return MarketInfo(
        exchange_id=ExchangeID.PIONEX,
        symbol=f"{base}/{quote}",
        base_asset=base,
        quote_asset=quote,
        market_type="spot",
        active=bool(raw.get("enable")),
        discovered_at=discovered_at,
        contract=False,
        linear=None,
        settle=None,
        amount_precision=_safe_int(raw.get("amountPrecision")),
        price_precision=_safe_int(raw.get("quotePrecision")),
        min_amount=_safe_decimal(raw.get("minTradeSize")),
        min_notional=_safe_decimal(raw.get("minAmount")),
        listed_at=None,  # Pionex' oeffentliche API liefert kein Listing-Datum.
    )


async def discover_pionex_markets() -> list[MarketInfo]:
    """
    Ruft PionexClient.get_symbols() (synchron, requests-basiert) in
    einem Thread auf, um den asyncio Event Loop nicht zu blockieren.
    Fail-safe: jeder Fehler (Netzwerk, Pionex API-Fehler, unerwartete
    Antwortstruktur) wird geloggt und liefert eine leere Liste zurueck -
    Discovery darf den Worker-Prozess niemals zum Absturz bringen.
    """
    from sgr.exchanges.pionex_client import PionexClient

    def _fetch() -> list[dict[str, Any]]:
        with PionexClient() as client:
            return client.get_symbols()

    try:
        raw_symbols = await asyncio.to_thread(_fetch)
    except Exception as e:
        log.warning("asset_universe.pionex_discovery_failed", error=str(e))
        return []

    now = datetime.now(tz=UTC)
    markets: list[MarketInfo] = []
    for raw in raw_symbols:
        try:
            info = pionex_symbol_to_market_info(raw, discovered_at=now)
        except Exception as e:
            log.debug("asset_universe.pionex_symbol_parse_failed", error=str(e), raw=raw)
            continue
        if info is not None:
            markets.append(info)
    return markets


async def discover_binance_markets(adapter: CCXTBaseAdapter) -> list[MarketInfo]:
    """
    Nutzt den bereits verbundenen Binance-Adapter (aus dem Worker-eigenen
    ExchangePool - keine zusaetzliche Verbindung noetig, siehe
    AssetUniverseEngine). Fail-safe analog zu discover_pionex_markets().
    """
    try:
        return await adapter.discover_markets()
    except ExchangeError as e:
        log.warning("asset_universe.binance_discovery_failed", error=str(e))
        return []


class AssetUniverseEngine:
    """
    Periodischer Hintergrund-Task im sgr-worker-Prozess (gleiches Muster
    wie MonitoringEngine/WorkerMetricsPublisher): fuehrt Discovery fuer
    Binance (ueber den bereits verbundenen Pool-Adapter) und Pionex
    (eigene, oeffentliche Verbindung - siehe PionexClient) periodisch
    aus, klassifiziert jeden gefundenen Markt und schreibt das Ergebnis
    als Prometheus-Gauge (siehe sgr.monitoring.metrics.
    record_asset_universe_snapshot).

    discovery_interval_seconds ist bewusst groesszuegig (Default 6h):
    Markt-Listen aendern sich selten genug, dass ein haeufigerer
    Discovery-Lauf keinen Mehrwert haette, aber zusaetzliche REST-Calls
    gegen die Binance-API bedeuten wuerde (siehe Modul-Docstring zum
    frueheren IP-Ban-Vorfall bei einer Bulk-Abfrage).

    WICHTIGER FUND (live am Server verifiziert, siehe Abschlussbericht):
    OTel-Gauges (opentelemetry-exporter-prometheus, wie sie SGRMetrics
    fuer ALLE Gauges in sgr/monitoring/metrics.py verwendet) tauchen nach
    einem .set()-Aufruf nur in GENAU EINEM nachfolgenden generate_latest()
    -Zyklus auf und verschwinden danach wieder aus dem Prometheus-Export,
    bis erneut .set() aufgerufen wird - anders als rohe
    prometheus_client-Gauges (die den letzten Wert dauerhaft halten).
    Jede andere Gauge in metrics.py wird bereits alle 10s von
    MonitoringEngine._collect() neu gesetzt (schneller als der 15s-
    Scrape-/Redis-Push-Zyklus), wodurch dieses Verhalten dort nie sichtbar
    wurde. Mit einem 6h-Discovery-Intervall waere sgr_asset_universe_status
    dagegen fast permanent "verschwunden" gewesen. Deshalb: ein SEPARATES,
    kurzes republish_interval_seconds haelt den zuletzt bekannten Snapshot
    (ohne erneute Netzwerk-Discovery) kontinuierlich im Export sichtbar,
    waehrend discovery_interval_seconds weiterhin die teuren REST-Calls
    selten haelt.
    """

    def __init__(
        self,
        *,
        binance_adapter: CCXTBaseAdapter | None,
        strategy_registry: StrategyRegistry | None,
        subscribed_symbols: dict[ExchangeID, set[str]],
        discovery_interval_seconds: float = 6 * 3600.0,
        republish_interval_seconds: float = 15.0,
        on_discovery: Callable[[dict[ExchangeID, set[str]]], Awaitable[None]] | None = None,
    ) -> None:
        """
        subscribed_symbols: Mindest-Sicherheitsnetz pro Exchange (z.B. die
        urspruenglich kuratierte LIVE_MARKET_DATA_SYMBOLS-Liste fuer
        Binance) - wird mit dem dynamisch ermittelten TRADABLE-Set
        vereinigt, NICHT dadurch ersetzt (siehe Task-Vorgabe "Autonomous
        Paper Trading Rollout": alle tatsaechlich handelbaren Symbole
        sollen subscribed werden, keine kuenstliche Reduktion - aber die
        bereits bestehenden, historisch kuratierten Symbole muessen
        bestehen bleiben, auch falls eine Discovery-Runde sie aus
        welchem Grund auch immer nicht als TRADABLE einstuft).

        on_discovery: optionaler Callback, der nach jedem ECHTEN
        Discovery-Zyklus (nicht nach einem reinen Republish) mit dem
        aktuellen SUBSCRIBED-Symbolset pro Exchange aufgerufen wird -
        main.py verdrahtet dies mit MarketDataEngine.
        reconcile_subscriptions(), damit neu entdeckte/entfernte Symbole
        automatisch echte Candle-Feeds bekommen bzw. verlieren (Task-
        Vorgabe: "Wenn sich das Asset Universe veraendert, muss die
        Strategieauswertung diese Aenderung automatisch uebernehmen").
        """
        self._binance_adapter = binance_adapter
        self._strategy_registry = strategy_registry
        self._subscribed_symbols = subscribed_symbols
        self._discovery_interval = discovery_interval_seconds
        self._republish_interval = republish_interval_seconds
        self._on_discovery = on_discovery
        self._task: asyncio.Task[Any] | None = None
        self._running = False
        self._last_snapshot: list[AssetUniverseEntry] = []
        self._last_exportable: list[AssetUniverseEntry] = []
        self._last_discovery_at: float = 0.0
        # Hintergrund-Tasks fuer on_discovery (siehe _run_once) - Set statt
        # Einzelfeld, weil ein neuer Discovery-Zyklus theoretisch starten
        # kann, bevor der vorherige on_discovery-Task (Feed-Reconciliation
        # fuer hunderte Symbole) fertig ist.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    @property
    def last_snapshot(self) -> list[AssetUniverseEntry]:
        return list(self._last_snapshot)

    async def start(self) -> None:
        self._running = True
        # Erster Lauf sofort (nicht erst nach discovery_interval_seconds
        # warten) - das Dashboard/$symbol soll ab dem ersten Sammelzyklus
        # Daten haben, nicht erst nach 6 Stunden.
        await self._run_once()
        self._task = asyncio.create_task(self._loop(), name="asset_universe_engine")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._republish_interval)
                if not self._running:
                    break
                elapsed = asyncio.get_event_loop().time() - self._last_discovery_at
                if elapsed >= self._discovery_interval:
                    await self._run_once()
                else:
                    self._republish()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("asset_universe_engine.run_failed", error=str(e))

    def _republish(self) -> None:
        """Setzt die Gauge fuer den zuletzt bekannten (gecachten) Snapshot
        erneut, OHNE eine neue Discovery auszufuehren - siehe Klassen-
        Docstring "Wichtiger Fund"."""
        from sgr.monitoring.metrics import record_asset_universe_snapshot

        record_asset_universe_snapshot(self._last_exportable)

    async def _run_once(self) -> None:
        entries: list[AssetUniverseEntry] = []

        has_active_strategy = bool(self._strategy_registry and self._strategy_registry.get_active())

        binance_subscribed: set[str] = set()

        if self._binance_adapter is not None:
            markets = await discover_binance_markets(self._binance_adapter)
            safety_net = self._subscribed_symbols.get(ExchangeID.BINANCE, set())

            # Zwei-Pass-Klassifikation (Task-Vorgabe "alle 922 Symbole,
            # keine kuenstliche Reduzierung auf die bereits bestehenden
            # Subscriptions"): Pass 1 ermittelt, welche Maerkte ueberhaupt
            # TRADABLE waeren (subscribed_symbols hier bewusst leer -
            # noch nicht bekannt, DAS ist ja gerade die Frage). Das
            # Ergebnis + das bestehende Sicherheitsnetz (LIVE_MARKET_DATA_
            # SYMBOLS, siehe __init__-Docstring) wird zum tatsaechlichen
            # SUBSCRIBED-Zielset - Pass 2 klassifiziert damit final (jeder
            # TRADABLE Binance-Markt erreicht dadurch automatisch
            # mindestens SUBSCRIBED, kein separat kuratierter Filter mehr
            # noetig).
            pass1 = [
                classify_asset_status(
                    m,
                    expected_market_type="swap",
                    execution_supported=True,
                    subscribed_symbols=set(),
                    has_active_strategy=has_active_strategy,
                )
                for m in markets
            ]
            tradable_rank = RANK[AssetStatus.TRADABLE]
            binance_subscribed = {
                e.market.symbol for e in pass1 if RANK.get(e.status, 0) >= tradable_rank
            } | safety_net

            for m in markets:
                entries.append(
                    classify_asset_status(
                        m,
                        expected_market_type="swap",
                        execution_supported=True,
                        subscribed_symbols=binance_subscribed,
                        has_active_strategy=has_active_strategy,
                    )
                )

        pionex_markets = await discover_pionex_markets()
        pionex_subscribed = self._subscribed_symbols.get(ExchangeID.PIONEX, set())
        for m in pionex_markets:
            entries.append(
                classify_asset_status(
                    m,
                    expected_market_type="spot",
                    # Siehe Modul-Docstring: ccxt unterstuetzt Pionex
                    # nicht (empirisch verifiziert) - PionexAdapter
                    # wuerde bei einer echten Order-Ausfuehrung
                    # abstuerzen. Ehrlich als "nicht ausfuehrbar"
                    # klassifizieren statt vorzutaeuschen, SGR koenne
                    # hier tatsaechlich handeln.
                    execution_supported=False,
                    subscribed_symbols=pionex_subscribed,
                    has_active_strategy=has_active_strategy,
                )
            )

        self._last_snapshot = entries

        from sgr.monitoring.metrics import record_asset_universe_snapshot

        # Nur USDT-quotierte Maerkte exportieren (siehe Modul-Docstring:
        # SGRs Portfolio/Risk/Balance ist durchgaengig USDT-denominiert -
        # alles andere ist kategorisch ausserhalb SGRs Handelsfaehigkeit,
        # keine willkuerliche Popularitaets-Grenze). Ohne diesen Filter
        # wuerden testnet-eigene Muell-/Test-Symbole (z.B. Nicht-Krypto-
        # Zeichenfolgen als quote currency) und tausende irrelevante
        # Nicht-USDT-Paare (BTC-, ETH-, Fiat-quotierte Maerkte) unnoetig
        # Prometheus-Kardinalitaet verbrauchen - empirisch beobachtet:
        # ohne Filter >2500 Zeitreihen fuer eine einzige Metrik.
        exportable = [e for e in entries if e.market.quote_asset == "USDT"]
        record_asset_universe_snapshot(exportable)
        self._last_exportable = exportable
        self._last_discovery_at = asyncio.get_event_loop().time()

        by_status: dict[str, int] = {}
        for e in entries:
            by_status[e.status] = by_status.get(e.status, 0) + 1
        log.info(
            "asset_universe_engine.snapshot",
            total=len(entries),
            by_status=by_status,
        )

        if self._on_discovery is not None:
            # ALS HINTERGRUND-TASK, NICHT awaited: der Callback loest
            # MarketDataEngine.reconcile_subscriptions() aus, das fuer
            # hunderte neue Symbole einen vollen History-Fetch pro Feed
            # ausfuehrt (mit Semaphore gedrosselt, aber dennoch potenziell
            # mehrere Minuten). Wuerde das hier awaited, wuerde
            # AssetUniverseEngine.start() - und damit lifespan() beim
            # Worker-Start - fuer die gesamte Dauer blockieren und den
            # Healthcheck-Start-Period ueberschreiten (server-verifiziert
            # als reales Risiko waehrend des Rollouts, siehe
            # Abschlussbericht). _run_once() selbst (Discovery +
            # Klassifikation + Metrik-Export) bleibt synchron/schnell -
            # nur die Feed-Reconciliation entkoppelt.
            task = asyncio.create_task(
                self._run_on_discovery_callback(binance_subscribed),
                name="asset_universe_on_discovery",
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _run_on_discovery_callback(self, binance_subscribed: set[str]) -> None:
        assert self._on_discovery is not None
        try:
            await self._on_discovery({ExchangeID.BINANCE: binance_subscribed})
        except Exception as e:
            # Fail-safe (wie ueberall in diesem Modul): ein Fehler bei der
            # Markt-Daten-Neuverdrahtung darf die Discovery selbst (und
            # damit das Dashboard/$symbol) niemals stoeren.
            log.error("asset_universe_engine.on_discovery_failed", error=str(e))
