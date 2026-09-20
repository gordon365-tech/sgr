"""
Custom Metrics für SGR Trading System
======================================
Portfolio, Risk, Strategy & Market Data Metrics.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from opentelemetry import metrics
from opentelemetry.metrics import Meter

from sgr.core.config import get_config
from sgr.core.logging import get_logger

log = get_logger(__name__)


class _TenantScopedInstrument:
    """
    Transparenter Wrapper um ein OTel-Gauge/Counter-Instrument: injiziert
    automatisch ein "tenant" Label in jeden .set()/.add()-Aufruf.

    Hintergrund (Schritt 6, Server-verifiziert): sgr-worker laeuft als ein
    Prozess PRO Tenant (worker-gordon, worker-sumo - siehe
    docker-compose.prod.yml), aber SGRMetrics ist ein reines In-Prozess-
    Singleton ohne jegliches Tenant-Label. Jeder Worker-Prozess erzeugte
    seine Gauges bisher mit identischen Labels (z.B. {"strategy": "...",
    "trading_mode": "paper"}), die beim Zusammenfuehren in
    worker_metrics_bridge.collect_worker_metrics() (Redis-Snapshots werden
    roh aneinandergehaengt, kein Label-Rewriting) fuer Prometheus
    ununterscheidbar sind - bestaetigt per curl auf dem Server: zwei
    identische sgr_strategy_active_count-Zeilen fuer Gordon und Sumo.

    Dieser Wrapper macht es strukturell unmoeglich, das Tenant-Label zu
    vergessen: jeder Aufrufer (record_*-Funktionen, MonitoringEngine, jeder
    zukuenftige neue Call) muss KEIN zusaetzliches Label mitgeben - es wird
    hier immer automatisch injiziert, auch wenn der Aufrufer selbst kein
    "tenant" im uebergebenen Dict hat. Ein explizit im Aufrufer-Dict
    gesetztes "tenant" wuerde NICHT ueberschrieben (siehe dict-merge-
    Reihenfolge unten) - kommt in der Praxis aber nicht vor, da kein
    Aufrufer aktuell "tenant" selbst setzt.
    """

    __slots__ = ("_instrument", "_tenant_id")

    def __init__(self, instrument: Any, tenant_id: str) -> None:
        self._instrument = instrument
        self._tenant_id = tenant_id

    def _with_tenant(self, attributes: dict[str, str] | None) -> dict[str, str]:
        merged = {"tenant": self._tenant_id}
        if attributes:
            merged.update(attributes)
        return merged

    def set(self, amount: float, attributes: dict[str, str] | None = None) -> None:
        self._instrument.set(amount, self._with_tenant(attributes))

    def add(self, amount: float, attributes: dict[str, str] | None = None) -> None:
        self._instrument.add(amount, self._with_tenant(attributes))


class SGRMetrics:
    """Zentralisierte Custom Metrics.

    tenant_id: identifiziert den Tenant-Prozess (z.B. "gordon", "sumo"),
    der diese Instanz erzeugt hat - siehe _TenantScopedInstrument
    Docstring fuer den Hintergrund. Default "default" fuer Single-Tenant-
    Deployments/Tests, in denen kein Tenant-Kontext existiert.
    """

    def __init__(self, tenant_id: str | None = None) -> None:
        self._meter: Meter = metrics.get_meter(__name__)
        self._tenant_id = tenant_id or _resolve_tenant_id()

        def gauge(name: str, description: str, unit: str = "") -> _TenantScopedInstrument:
            instrument = self._meter.create_gauge(name=name, description=description, unit=unit)
            return _TenantScopedInstrument(instrument, self._tenant_id)

        def counter(name: str, description: str) -> _TenantScopedInstrument:
            instrument = self._meter.create_counter(name=name, description=description)
            return _TenantScopedInstrument(instrument, self._tenant_id)

        # Portfolio Metrics
        #
        # WICHTIG (Grafana-Observability-Audit): kein `unit=` Argument mehr
        # auf Gauges, deren Name die Einheit bereits im letzten Wortteil
        # traegt (_usd, _pct). Der PrometheusMetricReader haengt den `unit`-
        # Wert IMMER als zusaetzlichen Namens-Suffix an (empirisch am
        # exportierten /metrics-Text verifiziert) - "value_usd" + unit="USD"
        # exportierte bisher faelschlich als "sgr_portfolio_value_usd_USD"
        # (Doppel-Suffix, nirgends von Dashboard/Tests erwartet). Die
        # Einheit steht bereits im Namen selbst - `unit=` ist hier
        # redundant und wird komplett weggelassen statt umbenannt, damit
        # der Name weiterhin mit den bestehenden Grafana-Queries und dem
        # Dashboard-Test (test_grafana_dashboard.py) uebereinstimmt.
        self.portfolio_value = gauge("sgr.portfolio.value_usd", "Current portfolio value")
        self.portfolio_cash = gauge("sgr.portfolio.cash_usd", "Available cash")
        self.daily_pnl = gauge("sgr.portfolio.daily_pnl_usd", "Daily profit/loss")
        self.daily_pnl_pct = gauge("sgr.portfolio.daily_pnl_pct", "Daily profit/loss percentage")

        # Risk Metrics
        self.portfolio_heat = gauge("sgr.risk.portfolio_heat", "Portfolio heat (0-1)")
        self.max_drawdown = gauge("sgr.risk.max_drawdown_pct", "Maximum drawdown")
        self.leverage = gauge("sgr.risk.leverage", "Current leverage ratio")
        self.open_positions_count = gauge("sgr.risk.open_positions", "Number of open positions")
        self.var_95 = gauge("sgr.risk.var_95_pct", "Value at Risk (95% confidence)")

        # Position Metrics (Asset/Position Breakdown im Grafana-Dashboard,
        # siehe monitoring/grafana/dashboards/sgr-trading.json): eine
        # Zeitreihe pro offener Position, gelabelt mit symbol/side/
        # trading_mode/exchange. tenant kommt wie bei allen SGRMetrics-
        # Instrumenten automatisch ueber _TenantScopedInstrument dazu.
        # Vorher existierte keine einzige Metrik auf Positions-Ebene -
        # das Dashboard haette pro Asset/Position nichts anzuzeigen gehabt.
        self.position_size = gauge("sgr.position.size", "Current position size (quantity)")
        self.position_exposure = gauge(
            "sgr.position.exposure_usd", "Position notional exposure (quantity * price)"
        )
        self.position_leverage = gauge("sgr.position.leverage", "Position leverage")
        self.position_unrealized_pnl = gauge(
            "sgr.position.unrealized_pnl_usd", "Position unrealized profit/loss"
        )
        self.position_entry_price = gauge(
            "sgr.position.entry_price_usd", "Position average entry price"
        )
        self.position_current_price = gauge(
            "sgr.position.current_price_usd", "Position current mark price"
        )

        # Position-Protection-Metriken (siehe sgr/risk/position_protection.py):
        # vorher nicht existent, da SL/TP/Max-Holding-Time vor diesem
        # Feature ueberhaupt nicht existierten. margin_usd = notional /
        # leverage (siehe PositionSizer-Docstring "Position Sizing"-
        # Trennung Account-Kapital/Notional/Margin/Leverage).
        self.position_stop_loss_price = gauge(
            "sgr.position.stop_loss_price_usd", "Attached stop-loss trigger price"
        )
        self.position_take_profit_price = gauge(
            "sgr.position.take_profit_price_usd", "Attached take-profit trigger price"
        )
        self.position_margin_usd = gauge(
            "sgr.position.margin_usd", "Margin required for this position (notional / leverage)"
        )
        self.position_holding_seconds = gauge(
            "sgr.position.holding_seconds", "Seconds since this position was opened"
        )

        # Asset Universe (Market Discovery, siehe sgr/market_data/
        # asset_universe.py): eine Zeitreihe pro entdecktem Markt ueber
        # alle unterstuetzten Exchanges hinweg, unabhaengig davon, ob
        # SGR gerade eine Position darin haelt. Wert = Rang der
        # DISCOVERED/SUPPORTED/TRADABLE/SUBSCRIBED/ACTIVE-Kaskade
        # (siehe asset_universe.RANK) - status zusaetzlich als Label
        # fuer direktes Filtern/Anzeigen in Grafana. Das ist bewusst die
        # Datenquelle fuer Grafanas $symbol-Variable (label_values(...)) -
        # sgr.position.size traegt NUR aktuell offene Positionen und war
        # deshalb leer, solange keine Position offen ist (siehe Audit-
        # Fund: $symbol war in Grafana leer).
        self.asset_universe_status = gauge(
            "sgr.asset.universe_status",
            "Asset universe classification rank (0=discovered .. 4=active)",
        )

        # Trading Metrics
        #
        # Namensaudit: OTel's PrometheusMetricReader haengt an jeden
        # Counter automatisch "_total" an (Spec-Verhalten, empirisch
        # verifiziert). "sgr.trades.total" wuerde daher als
        # "sgr_trades_total_total" exportieren (Doppel-Suffix) - "executed"
        # statt "total" vermeidet das, ohne die Bedeutung zu aendern.
        self.trades_total = counter("sgr.trades.executed", "Total trades executed")
        self.trades_winning = counter("sgr.trades.winning", "Winning trades")
        self.trades_losing = counter("sgr.trades.losing", "Losing trades")

        # Cumulative realized PnL (Gauge, nicht Counter: kann fallen -
        # jeder verlustreiche Trade senkt den kumulierten Wert. Ein
        # Prometheus-Counter darf per Definition nie sinken, waere hier
        # also falsch). Tenant-Ebene bewusst ohne Symbol/Strategie-
        # Aufschluesselung (Kardinalitaet, siehe Autonomous-Paper-
        # Trading-Vorgabe "nicht unnoetig hochdimensioniert") - Detail
        # auf Positionsebene liefert bereits sgr.position.unrealized_pnl_usd
        # fuer offene Positionen.
        self.realized_pnl = gauge(
            "sgr.trading.realized_pnl_usd", "Cumulative realized profit/loss"
        )

        # Exit-Grund pro geschlossenem Trade (siehe ExitReason,
        # sgr/core/types.py) - vorher nicht sichtbar, ob ein Trade durch
        # Stop-Loss, Take-Profit, Max-Holding-Time, ein gegenlaeufiges
        # Strategie-Signal oder den Kill Switch geschlossen wurde.
        # Long/Short-Aufschluesselung bewusst NICHT als eigener Counter -
        # sgr.trades.executed/_winning/_losing tragen bereits ein
        # "side"-Label (siehe record_trade_executed), ein zusaetzlicher
        # Counter waere eine redundante Metrik-Definition.
        self.trade_exit_reason = counter(
            "sgr.trading.exit_reason", "Closed trades broken down by exit reason"
        )

        # Strategy Metrics
        self.strategy_signals = counter(
            "sgr.strategy.signals_generated", "Trading signals generated"
        )
        self.strategy_signals_rejected = counter(
            "sgr.strategy.signals_rejected", "Trading signals rejected before reaching risk"
        )
        self.strategy_evaluations = counter(
            "sgr.strategy.evaluations", "Strategy Engine evaluation cycles (per symbol)"
        )
        self.strategy_win_rate = gauge("sgr.strategy.win_rate_pct", "Strategy win rate", "%")

        # Aktuell klassifiziertes Marktregime pro Symbol (siehe
        # sgr/strategy/regime_classifier.py "regime_detector_v1"). Wert =
        # Rang (siehe REGIME_RANK unten) fuer Sortierung/Heatmaps in
        # Grafana, regime-Name zusaetzlich als Label fuer direktes Filtern.
        self.market_regime = gauge(
            "sgr.market.regime", "Currently classified market regime per symbol"
        )

        # Strategy Validation Metrics (Schritt 6: Sharpe/Return/Drawdown aus
        # StrategyValidationRunner duerfen nicht nur geloggt werden - siehe
        # MonitoringEngine._collect(), das diese Werte vorher nur per
        # log.debug ausgab, nie als Metrik. Quelle: StrategyEntry.
        # last_validation_result (sgr/strategy/registry.py), gefuellt von
        # StrategyValidationRunner.mark_validated().
        self.active_strategies_count = gauge(
            "sgr.strategy.active_count",
            "Number of currently activated (validated, is_active) strategies",
        )
        self.strategy_validation_status = gauge(
            "sgr.strategy.validation_status",
            "Go-live gate result per strategy (1 = can_go_live, "
            "0 = not approved for paper/live activation)",
        )
        self.strategy_sharpe_ratio = gauge(
            "sgr.strategy.backtest_sharpe_ratio",
            "Sharpe ratio from the most recent validation backtest",
        )
        self.strategy_total_return = gauge(
            "sgr.strategy.backtest_total_return_pct",
            "Total return from the most recent validation backtest",
        )
        self.strategy_max_drawdown = gauge(
            "sgr.strategy.backtest_max_drawdown_pct",
            "Max drawdown from the most recent validation backtest",
        )
        self.strategy_backtest_trades = gauge(
            "sgr.strategy.backtest_total_trades",
            "Number of trades in the most recent validation backtest",
        )

        # Futures Grid Metrics (siehe sgr/execution/grid_controller.py,
        # sgr/risk/grid_risk.py). Eine Zeitreihe pro (exchange, symbol,
        # strategy, direction, trading_mode)-Kombination - tenant kommt
        # wie bei allen SGRMetrics-Instrumenten automatisch dazu
        # (_TenantScopedInstrument), damit das Grafana-Dashboard nach
        # Exchange/Produkt/Strategie/Tenant filtern kann (siehe
        # Aufgabenstellung: "Pionex / Futures Grid / Gordon" etc.).
        self.futures_grid_active = gauge(
            "sgr.futures_grid.active", "1 wenn dieses Grid aktuell aktiv ist, sonst 0"
        )
        self.futures_grid_exposure = gauge(
            "sgr.futures_grid.exposure_usd", "Aktuelle Netto-Notional-Exposure des Grids"
        )
        self.futures_grid_pnl = gauge(
            "sgr.futures_grid.pnl_usd", "Realisierter PnL des Grids (Grid Capture - Fees - Funding)"
        )
        self.futures_grid_funding_cost = gauge(
            "sgr.futures_grid.funding_cost_usd", "Kumulierte Funding-Kosten des Grids"
        )
        self.futures_grid_orders = gauge(
            "sgr.futures_grid.orders", "Anzahl aktuell offener Grid-Level-Orders"
        )
        self.futures_grid_fills = counter(
            "sgr.futures_grid.fills", "Anzahl ausgefuehrter Grid-Level-Fills"
        )
        self.futures_grid_liquidation_distance = gauge(
            "sgr.futures_grid.liquidation_distance_pct",
            "Geschaetzte relative Distanz zum Liquidationspreis (0-1)",
        )
        self.futures_grid_edge = gauge(
            "sgr.futures_grid.edge", "Zusammengesetzter Edge-Score der Grid-Strategie (0-1)"
        )
        self.futures_grid_regime_score = gauge(
            "sgr.futures_grid.regime_score", "GridSuitabilityScore.composite fuer aktuelles Regime"
        )

        # Market Data Metrics
        self.candles_received = counter(
            "sgr.market_data.candles_received", "OHLCV candles received"
        )

        # System Metrics (gleicher Doppel-Suffix-Grund wie bei trades_total
        # oben: "_total" im OTel-Instrumentnamen + automatischer
        # Counter-Suffix des Exporters).
        self.api_requests_total = counter("sgr.api.requests", "Total API requests")
        self.api_errors_total = counter("sgr.api.errors", "API errors")

        log.info("metrics.sgr_metrics_initialized", tenant_id=self._tenant_id)


def _resolve_tenant_id() -> str:
    """Liest tenant_id aus der globalen Config (sgr.core.config.get_config()
    .tenant_id, dasselbe Feld, das WorkerMetricsPublisher bereits fuer den
    Redis-Snapshot-Key nutzt - siehe sgr/api/main.py Instanziierungsstelle).
    Fail-safe: liefert "default", falls Config noch nicht initialisiert ist
    (z.B. in Unit-Tests ohne vollen App-Kontext) oder tenant_id nicht
    gesetzt ist - konsistent mit WorkerMetricsPublisher's eigenem
    "config.tenant_id or 'default'"-Fallback."""
    try:
        config = get_config()
        return config.tenant_id or "default"
    except Exception:
        return "default"


# Singleton instance
_metrics_instance: SGRMetrics | None = None


def get_metrics() -> SGRMetrics:
    """Returns the global SGR metrics instance."""
    global _metrics_instance
    if _metrics_instance is None:
        _metrics_instance = SGRMetrics()
    return _metrics_instance


def record_portfolio_snapshot(
    portfolio_value: Decimal,
    cash: Decimal,
    daily_pnl: Decimal,
    daily_pnl_pct: float,
) -> None:
    """Records portfolio state."""
    m = get_metrics()
    m.portfolio_value.set(float(portfolio_value), {"status": "live"})
    m.portfolio_cash.set(float(cash), {"status": "live"})
    m.daily_pnl.set(float(daily_pnl), {"status": "live"})
    m.daily_pnl_pct.set(daily_pnl_pct, {"status": "live"})


def record_risk_snapshot(
    portfolio_heat: float,
    max_drawdown_pct: float,
    leverage: float,
    open_positions: int,
    var_95_pct: float,
) -> None:
    """Records risk metrics."""
    m = get_metrics()
    m.portfolio_heat.set(portfolio_heat, {"status": "live"})
    m.max_drawdown.set(max_drawdown_pct, {"status": "live"})
    m.leverage.set(leverage, {"status": "live"})
    m.open_positions_count.set(open_positions, {"status": "live"})
    m.var_95.set(var_95_pct, {"status": "live"})


def record_trade_executed(
    side: str,
    pnl: Decimal,
    winning: bool,
    cumulative_realized_pnl: Decimal | None = None,
    exit_reason: str | None = None,
) -> None:
    """Records trade execution.

    cumulative_realized_pnl: laufende Summe aller realisierten PnL
    dieses Tenants (siehe PortfolioEngine._record_trade - Summe ueber
    self._trade_history). Optional (Default None = Gauge wird nicht
    angefasst), damit bestehende Aufrufer/Tests ohne diesen Wert
    unveraendert funktionieren.

    exit_reason: siehe ExitReason (sgr/core/types.py) - optional aus
    demselben Grund (bestehende Aufrufer/Tests ohne dieses Feld bleiben
    unveraendert funktionsfaehig).
    """
    m = get_metrics()
    m.trades_total.add(1, {"side": side})
    if winning:
        m.trades_winning.add(1, {"side": side})
    else:
        m.trades_losing.add(1, {"side": side})
    if cumulative_realized_pnl is not None:
        m.realized_pnl.set(float(cumulative_realized_pnl))
    if exit_reason:
        m.trade_exit_reason.add(1, {"exit_reason": exit_reason, "side": side})


def record_signal_generated(strategy_name: str, direction: str, confidence: float) -> None:
    """Records signal generation."""
    m = get_metrics()
    m.strategy_signals.add(1, {"strategy": strategy_name, "direction": direction})


def record_signal_rejected(symbol: str, reason: str) -> None:
    """Records a signal that never reached the Risk Engine (z.B.
    widerspruechliche Strategie-Signale, Konfidenz unter Schwelle)."""
    m = get_metrics()
    m.strategy_signals_rejected.add(1, {"symbol": symbol, "reason": reason})


def record_strategy_evaluation(symbol: str) -> None:
    """Records one StrategyEngine.process() Durchlauf fuer ein Symbol -
    beantwortet 'wie viele Symbole werden tatsaechlich kontinuierlich
    evaluiert' unabhaengig davon, ob ein Signal entstand."""
    m = get_metrics()
    m.strategy_evaluations.add(1, {"symbol": symbol})


def record_market_regime(symbol: str, regime: str, rank: int) -> None:
    """Records the currently classified market regime for a symbol
    (siehe sgr/strategy/regime_classifier.py)."""
    m = get_metrics()
    m.market_regime.set(float(rank), {"symbol": symbol, "regime": regime})


def record_candle_received(symbol: str, timeframe: str) -> None:
    """Records candle reception."""
    m = get_metrics()
    m.candles_received.add(1, {"symbol": symbol, "timeframe": timeframe})


def record_position_snapshot(
    symbol: str,
    side: str,
    trading_mode: str,
    exchange: str,
    size: float,
    exposure_usd: float,
    leverage: float,
    unrealized_pnl_usd: float,
    entry_price_usd: float = 0.0,
    current_price_usd: float = 0.0,
    stop_loss_price_usd: float = 0.0,
    take_profit_price_usd: float = 0.0,
    margin_usd: float = 0.0,
    holding_seconds: float = 0.0,
) -> None:
    """Records the current state of a single open position.

    Aufrufer: MonitoringEngine._collect() - einmal pro offener Position
    und Sammel-Intervall. Fuettert das Asset/Position-Breakdown-Panel im
    Grafana-Dashboard (symbol/side/trading_mode/exchange als Labels,
    tenant automatisch via _TenantScopedInstrument).

    entry_price_usd/current_price_usd/stop_loss_price_usd/
    take_profit_price_usd/margin_usd/holding_seconds sind optional
    (Default 0.0) statt Pflichtfelder, damit der bestehende Aufruf-/
    Zero-Reset-Pfad (siehe MonitoringEngine._collect_position_metrics -
    eine geschlossene Position wird explizit auf 0 gesetzt) unveraendert
    funktioniert, ohne an jeder Stelle alle Werte mitschleppen zu muessen.
    """
    m = get_metrics()
    labels = {
        "symbol": symbol,
        "side": side,
        "trading_mode": trading_mode,
        "exchange": exchange,
    }
    m.position_size.set(size, labels)
    m.position_exposure.set(exposure_usd, labels)
    m.position_leverage.set(leverage, labels)
    m.position_unrealized_pnl.set(unrealized_pnl_usd, labels)
    m.position_entry_price.set(entry_price_usd, labels)
    m.position_current_price.set(current_price_usd, labels)
    m.position_stop_loss_price.set(stop_loss_price_usd, labels)
    m.position_take_profit_price.set(take_profit_price_usd, labels)
    m.position_margin_usd.set(margin_usd, labels)
    m.position_holding_seconds.set(holding_seconds, labels)


def record_futures_grid_snapshot(
    exchange: str,
    symbol: str,
    strategy: str,
    direction: str,
    trading_mode: str,
    is_active: bool,
    exposure_usd: float,
    pnl_usd: float,
    funding_cost_usd: float,
    open_orders: int,
    liquidation_distance_pct: float | None = None,
    edge_score: float | None = None,
    regime_score: float | None = None,
) -> None:
    """
    Records the current state of one Futures Grid instance. Aufrufer:
    ein periodischer Monitoring-Zyklus (analog zu
    MonitoringEngine._collect_position_metrics()) ueber
    sgr.execution.grid_controller.GridController.active_grids().
    """
    m = get_metrics()
    labels = {
        "exchange": exchange,
        "symbol": symbol,
        "strategy": strategy,
        "direction": direction,
        "trading_mode": trading_mode,
    }
    m.futures_grid_active.set(1.0 if is_active else 0.0, labels)
    m.futures_grid_exposure.set(exposure_usd, labels)
    m.futures_grid_pnl.set(pnl_usd, labels)
    m.futures_grid_funding_cost.set(funding_cost_usd, labels)
    m.futures_grid_orders.set(float(open_orders), labels)
    if liquidation_distance_pct is not None:
        m.futures_grid_liquidation_distance.set(liquidation_distance_pct, labels)
    if edge_score is not None:
        m.futures_grid_edge.set(edge_score, labels)
    if regime_score is not None:
        m.futures_grid_regime_score.set(regime_score, labels)


def record_futures_grid_fill(exchange: str, symbol: str, strategy: str, direction: str) -> None:
    """Records one executed grid-level fill (siehe GridController._fill_level())."""
    m = get_metrics()
    m.futures_grid_fills.add(
        1, {"exchange": exchange, "symbol": symbol, "strategy": strategy, "direction": direction}
    )


def record_asset_universe_snapshot(entries: list[Any]) -> None:
    """Records one gauge row per discovered market (siehe
    sgr/market_data/asset_universe.py AssetUniverseEntry/RANK).

    entries: list[AssetUniverseEntry] - als `list[Any]` typisiert, um
    einen Importzyklus zu vermeiden (asset_universe.py importiert
    bereits aus sgr.exchanges.base; ein Rueckimport von
    sgr.monitoring.metrics dorthin ist nicht noetig, hier reicht
    strukturelle Nutzung der bekannten Attribute).
    """
    from sgr.market_data.asset_universe import RANK

    m = get_metrics()
    for entry in entries:
        market = entry.market
        labels = {
            "exchange": market.exchange_id.value,
            "symbol": market.symbol,
            "base_asset": market.base_asset,
            "quote_asset": market.quote_asset,
            "market_type": market.market_type,
            "status": entry.status,
        }
        m.asset_universe_status.set(float(RANK.get(entry.status, 0)), labels)
