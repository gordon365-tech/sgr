"""
SGR Symbol Strategy Validation Runner
========================================
Orchestriert Backtest+Walk-Forward-Validierung fuer JEDES dynamisch
entdeckte Symbol im Asset Universe (Autonomous-Strategy-Universe-
Rollout) - im Unterschied zu StrategyValidationRunner (validation_runner.py),
der GLOBAL pro Strategie-Name gegen ein festes Symbol-Paar (BTC/ETH)
validiert und in StrategyRegistry (ein Flag pro Strategie) eintraegt.

Architektur-Entscheidung (siehe StrategySymbolValidationModel Docstring
in sgr/core/database.py): dieser Runner AENDERT NICHT die bestehende
StrategyRegistry-Semantik. Er persistiert eine NEUE, additive
pro-(Symbol, Timeframe, Strategie)-Tabelle. Die Produktions-Integration
(Phase 12) liegt in sgr/strategy/symbol_gate.py - ein rein lesender
Zusatzfilter in StrategyEngine.process(), der bestehende Kandidaten
(bereits global aktive Strategien + Regime-Filter) zusaetzlich gegen
diese Tabelle prueft, WENN fuer das Symbol bereits ein Ergebnis
existiert. Kein Ergebnis vorhanden = bestehendes Verhalten bleibt
unveraendert (kein Blockieren durch fehlende Batch-Daten).

Bewusst NICHT gebaut (dokumentierte Scope-Entscheidung, siehe
sgr/strategy/strategy_scoring.py Docstring): eine generische
Parameter-Grid-Suche ueber alle 5 Strategie-Parameter-Dataclasses.
TradingStrategy/StrategyParameters (sgr/strategy/base.py) haben keinen
generischen "aus StrategyParameters neu konstruieren"-Weg - jede
Strategie nimmt ihre eigene, strategie-spezifische Params-Dataclass im
Konstruktor entgegen. Eine echte Grid-Suche dafuer waere eine
eigenstaendige Erweiterung jeder der 5 Strategie-Klassen (mehrere neue
Parameter-Varianten-Konstruktoren) - das Robustness-Kriterium "Stabilitaet
bei leicht veraenderten Parametern" wird stattdessen ueber die bereits
vorhandenen Walk-Forward-Splits + Trade-Verteilungsanalyse abgedeckt
(siehe strategy_scoring._robustness_score).

Datenquelle Symbol-Universum: KEINE hartcodierte Liste (Phase 2) -
discover_binance_universe() nutzt exakt dieselben, bereits produktiv
genutzten Bausteine wie AssetUniverseEngine (discover_binance_markets +
classify_asset_status, sgr/market_data/asset_universe.py), mit einer
eigenen, temporaeren oeffentlichen Binance-Verbindung (analog
BacktestDataLoader.load_public_history() - kein API-Key noetig, reine
Marktdaten-Lesezugriffe, unabhaengig davon ob ein Worker-Prozess laeuft).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sgr.backtesting.data_quality import DataQualityStatus, assess_data_quality
from sgr.backtesting.engine import BacktestingEngine
from sgr.core.logging import get_logger
from sgr.core.repositories import StrategySymbolValidationRepository
from sgr.core.types import ExchangeID
from sgr.strategy.parameter_optimizer import (
    OptimizationResult,
    build_trial_strategy,
    optimize_strategy_parameters,
)
from sgr.strategy.regime_profile import build_regime_profile, select_candidate_strategies
from sgr.strategy.registry import StrategyRegistry
from sgr.strategy.strategy_scoring import score_validation

log = get_logger(__name__)

DEFAULT_TIMEFRAME = "1h"
DEFAULT_LOOKBACK_DAYS = 180


class SymbolValidationStatus(StrEnum):
    INSUFFICIENT_DATA = "insufficient_data"
    INVALID_DATA = "invalid_data"
    NO_VALID_STRATEGY = "no_valid_strategy"
    VALIDATED_BUT_NOT_ACTIVE = "validated_but_not_active"
    ACTIVE = "active"
    TECHNICAL_FAILURE = "technical_failure"


# Sentinel-Strategiename fuer Zeilen, die scheitern BEVOR ueberhaupt
# eine konkrete Strategie getestet wurde (Data-Quality-Gate oder
# "kein Regime-Match") - die DB-Spalte strategy ist NOT NULL, ein
# Symbol-Level-Ergebnis braucht trotzdem eine Zeile (Phase 11: JEDES
# Symbol bekommt eine Entscheidung, auch ein negatives Ergebnis).
NO_STRATEGY_SENTINEL = "__none__"


def _sanitize_for_jsonb(value: Any) -> Any:
    """Postgres JSONB (RFC 8259) hat kein NaN/Infinity - im Unterschied
    zu Python's eigenem json-Modul, das beides standardmaessig
    (nicht-standardkonform) serialisiert. PerformanceAnalyzer._sortino()
    liefert bewusst float('inf'), wenn ein Backtest keine einzige
    verlierende Bar hatte (siehe dortigen Docstring) - ein legitimer,
    nicht seltener Fall (z.B. 0-Trade-Ergebnisse wie breakout_v1 auf
    ETH/USDT, live beobachtet). Ohne diese Sanitisierung schlaegt JEDER
    solche INSERT fehl (asyncpg.InvalidTextRepresentationError: Token
    'Infinity' is invalid) - der gesamte Validierungslauf fuer dieses
    Symbol wuerde als TECHNICAL_FAILURE verloren gehen, obwohl das
    Backtest-Ergebnis selbst gueltig ist. inf/-inf/nan werden zu None,
    nicht zu einer erfundenen Zahl - "kein sinnvoller Wert" bleibt
    ehrlich sichtbar statt einen falschen Wert vorzutaeuschen."""
    import math

    if isinstance(value, float):
        return None if (math.isinf(value) or math.isnan(value)) else value
    if isinstance(value, dict):
        return {k: _sanitize_for_jsonb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_for_jsonb(v) for v in value]
    return value


@dataclass
class SymbolValidationOutcome:
    symbol: str
    status: SymbolValidationStatus
    best_strategy: str | None = None
    score: float | None = None
    reason: str = ""


@dataclass
class BatchSummary:
    batch_id: str
    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    technical_failures: dict[str, str] = field(default_factory=dict)
    outcomes: list[SymbolValidationOutcome] = field(default_factory=list)

    def record(self, outcome: SymbolValidationOutcome) -> None:
        self.total += 1
        self.by_status[outcome.status.value] = self.by_status.get(outcome.status.value, 0) + 1
        self.outcomes.append(outcome)
        if outcome.status == SymbolValidationStatus.TECHNICAL_FAILURE:
            self.technical_failures[outcome.symbol] = outcome.reason


async def discover_binance_universe(min_status_rank: str = "tradable") -> list[str]:
    """
    Ermittelt dynamisch alle aktuell TRADABLE+ USDT-Binance-Perpetuals -
    dieselbe Klassifikations-Kaskade wie AssetUniverseEngine (siehe
    sgr/market_data/asset_universe.py DISCOVERED/SUPPORTED/TRADABLE/
    SUBSCRIBED/ACTIVE).

    Verwendet BEWUSST keinen SGR-CCXTBaseAdapter (BinanceAdapter):
    dessen connect() verifiziert Credentials per Balance-Abfrage (Auth
    zwingend erforderlich) und schaltet im PAPER-Modus per
    set_sandbox_mode(True) auf Binance TESTNET um (siehe
    CCXTBaseAdapter.connect() Docstring) - beides waere fuer eine reine,
    credential-lose Markt-Discovery falsch (Testnet hat ein anderes,
    viel kleineres Symbol-Universum als Mainnet). Stattdessen exakt
    dasselbe Muster wie BacktestDataLoader._load_public_history_with_ccxt_id():
    ein bare oeffentlicher ccxt-Client ohne Auth, ohne Sandbox-Modus,
    direkt gegen die echte Mainnet-API.

    Returns: settle-freie kanonische Symbolform ("BTC/USDT", siehe
    Symbol.ccxt_symbol) - identisch zur Form, die
    BacktestDataLoader.load_public_history() erwartet.
    """
    import ccxt.async_support as ccxt_async

    from sgr.exchanges.base import MarketInfo
    from sgr.market_data.asset_universe import RANK, classify_asset_status

    public_client = ccxt_async.binance({"enableRateLimit": True, "timeout": 30_000})
    try:
        await public_client.load_markets()
        raw_markets = public_client.markets or {}
    finally:
        await public_client.close()

    # Identische Extraktionslogik wie CCXTBaseAdapter.discover_markets()
    # (sgr/exchanges/ccxt_base.py) - hier dupliziert statt importiert,
    # weil jene Methode eine verbundene Adapter-Instanz voraussetzt
    # (self._require_connected()), die wir aus obigem Grund bewusst
    # nicht herstellen.
    markets: list[MarketInfo] = []
    for symbol, market in raw_markets.items():
        if "/" not in symbol:
            continue
        base = market.get("base")
        quote = market.get("quote")
        if not base or not quote:
            continue
        precision = market.get("precision") or {}
        limits = market.get("limits") or {}
        amount_limits = limits.get("amount") or {}
        cost_limits = limits.get("cost") or {}
        created_raw = market.get("created")
        listed_at = (
            datetime.fromtimestamp(created_raw / 1000, tz=UTC)
            if isinstance(created_raw, (int, float))
            else None
        )
        amount_precision_raw = precision.get("amount")
        price_precision_raw = precision.get("price")
        min_amount_raw = amount_limits.get("min")
        min_notional_raw = cost_limits.get("min")
        markets.append(
            MarketInfo(
                exchange_id=ExchangeID.BINANCE,
                symbol=f"{base}/{quote}",
                base_asset=base,
                quote_asset=quote,
                market_type=str(market.get("type") or "unknown"),
                active=bool(market.get("active")),
                discovered_at=datetime.now(tz=UTC),
                contract=bool(market.get("contract")),
                linear=market.get("linear"),
                settle=market.get("settle"),
                amount_precision=(
                    int(amount_precision_raw) if amount_precision_raw is not None else None
                ),
                price_precision=(
                    int(price_precision_raw) if price_precision_raw is not None else None
                ),
                min_amount=(Decimal(str(min_amount_raw)) if min_amount_raw is not None else None),
                min_notional=(
                    Decimal(str(min_notional_raw)) if min_notional_raw is not None else None
                ),
                listed_at=listed_at,
            )
        )

    threshold = RANK[min_status_rank]
    symbols: list[str] = []
    for market in markets:
        entry = classify_asset_status(
            market,
            expected_market_type="swap",
            execution_supported=True,
            subscribed_symbols=set(),
            has_active_strategy=False,
        )
        if RANK.get(entry.status, 0) >= threshold:
            symbols.append(market.symbol)

    log.info("symbol_validation.universe_discovered", count=len(symbols))
    return sorted(set(symbols))


class SymbolStrategyValidationRunner:
    """Fuehrt den vollstaendigen Batch (Phase 2-11) fuer eine Liste von
    Symbolen aus. Siehe Modul-Docstring fuer die Architektur-
    Entscheidungen."""

    def __init__(
        self,
        *,
        repo: StrategySymbolValidationRepository | None = None,
        exchange_id: ExchangeID = ExchangeID.BINANCE,
        timeframe: str = DEFAULT_TIMEFRAME,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ) -> None:
        self._repo = repo or StrategySymbolValidationRepository()
        self._exchange_id = exchange_id
        self._timeframe = timeframe
        self._lookback_days = lookback_days
        self._engine = BacktestingEngine()

    async def run_batch(
        self,
        symbols: list[str],
        *,
        batch_id: str | None = None,
        resume: bool = True,
    ) -> BatchSummary:
        batch_id = batch_id or f"batch_{datetime.now(tz=UTC).strftime('%Y%m%d')}"
        summary = BatchSummary(batch_id=batch_id)

        already_processed: set[str] = set()
        if resume:
            already_processed = await self._repo.get_processed_symbols(batch_id=batch_id)
            if already_processed:
                log.info(
                    "symbol_validation.batch_resuming",
                    batch_id=batch_id,
                    already_processed=len(already_processed),
                    remaining=len(symbols) - len(already_processed & set(symbols)),
                )

        registry = StrategyRegistry.get()
        candidate_strategies = [entry.strategy for entry in registry.get_all().values()]

        for symbol in symbols:
            if resume and symbol in already_processed:
                continue
            try:
                outcome = await self._validate_symbol(symbol, batch_id, candidate_strategies)
            except Exception as e:
                # Phase 17: ein Fehler bei EINEM Symbol darf den Batch
                # nicht abbrechen - isolieren, dokumentieren, weiter.
                log.error(
                    "symbol_validation.symbol_technical_failure",
                    symbol=symbol,
                    error=str(e),
                )
                outcome = SymbolValidationOutcome(
                    symbol=symbol,
                    status=SymbolValidationStatus.TECHNICAL_FAILURE,
                    reason=str(e)[:500],
                )
                try:
                    await self._repo.upsert(
                        symbol=symbol,
                        exchange=self._exchange_id.value,
                        timeframe=self._timeframe,
                        strategy=NO_STRATEGY_SENTINEL,
                        status=outcome.status.value,
                        batch_id=batch_id,
                        failure_reason=outcome.reason,
                        is_best_for_symbol=True,
                    )
                except Exception as persist_error:
                    log.error(
                        "symbol_validation.persist_failed_after_technical_failure",
                        symbol=symbol,
                        error=str(persist_error),
                    )
            summary.record(outcome)
            self._update_metrics(outcome)

        await self._publish_summary_metrics(batch_id, summary)

        log.info(
            "symbol_validation.batch_completed",
            batch_id=batch_id,
            total=summary.total,
            by_status=summary.by_status,
        )
        return summary

    async def _publish_summary_metrics(self, batch_id: str, summary: BatchSummary) -> None:
        """Setzt die Aggregat-Gauges (Phase 13) am Ende eines Batches -
        siehe record_strategy_universe_summary() Docstring."""
        try:
            from sgr.monitoring.trading_metrics import record_strategy_universe_summary

            strategy_distribution = await self._repo.get_strategy_distribution(batch_id=batch_id)
            active_rows = await self._repo.get_active(batch_id=batch_id)

            avg_by_strategy: dict[str, dict[str, float]] = {}
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in active_rows:
                grouped.setdefault(row["strategy"], []).append(row)
            for strategy, rows in grouped.items():
                metrics_list = [r["metrics"] for r in rows if r.get("metrics")]
                if not metrics_list:
                    continue
                avg_by_strategy[strategy] = {
                    "score": sum(r.get("score") or 0.0 for r in rows) / len(rows),
                    "sharpe": sum(m.get("sharpe_ratio", 0.0) for m in metrics_list)
                    / len(metrics_list),
                    "return_pct": sum(m.get("total_return_pct", 0.0) for m in metrics_list)
                    / len(metrics_list),
                    "drawdown_pct": sum(m.get("max_drawdown_pct", 0.0) for m in metrics_list)
                    / len(metrics_list),
                }

            record_strategy_universe_summary(
                status_counts=summary.by_status,
                strategy_distribution=strategy_distribution,
                avg_metrics_by_strategy=avg_by_strategy,
            )
        except Exception as e:
            log.warning("symbol_validation.summary_metrics_failed", error=str(e))

    async def _validate_symbol(
        self,
        symbol: str,
        batch_id: str,
        candidate_strategies: list[Any],
    ) -> SymbolValidationOutcome:
        from sgr.backtesting.data_loader import BacktestDataLoader

        # Singleton - dieselbe Instanz wie die, die run_batch() bereits
        # fuer candidate_strategies nutzt (siehe StrategyRegistry.get()
        # Docstring). Hier erneut geholt statt durchgereicht, um die
        # bestehende Methodensignatur/den bestehenden Aufrufer in
        # run_batch() nicht aendern zu muessen.
        registry = StrategyRegistry.get()
        end_date = datetime.now(tz=UTC)
        start_date = end_date - timedelta(days=self._lookback_days)

        loader = BacktestDataLoader()
        candles = await loader.load_public_history(
            symbol=symbol,
            timeframe=self._timeframe,
            start=start_date,
            end=end_date,
            exchange_id=self._exchange_id,
        )

        quality = assess_data_quality(candles, self._timeframe)
        if not quality.passed:
            status = (
                SymbolValidationStatus.INSUFFICIENT_DATA
                if quality.status == DataQualityStatus.INSUFFICIENT_DATA
                else SymbolValidationStatus.INVALID_DATA
            )
            await self._repo.upsert(
                symbol=symbol,
                exchange=self._exchange_id.value,
                timeframe=self._timeframe,
                strategy=NO_STRATEGY_SENTINEL,
                status=status.value,
                batch_id=batch_id,
                data_quality=quality.to_dict(),
                failure_reason="; ".join(quality.issues[:5]) or status.value,
                is_best_for_symbol=True,
            )
            return SymbolValidationOutcome(
                symbol=symbol, status=status, reason=quality.issues[0] if quality.issues else ""
            )

        profile = build_regime_profile(candles)
        candidates = select_candidate_strategies(profile, candidate_strategies)

        if not candidates:
            await self._repo.upsert(
                symbol=symbol,
                exchange=self._exchange_id.value,
                timeframe=self._timeframe,
                strategy=NO_STRATEGY_SENTINEL,
                status=SymbolValidationStatus.NO_VALID_STRATEGY.value,
                batch_id=batch_id,
                data_quality=quality.to_dict(),
                regime_profile=profile.to_dict(),
                failure_reason="No registered strategy supports the observed regime(s)",
                is_best_for_symbol=True,
            )
            return SymbolValidationOutcome(
                symbol=symbol,
                status=SymbolValidationStatus.NO_VALID_STRATEGY,
                reason="No regime-matched candidate strategies",
            )

        best_strategy_name: str | None = None
        best_score = -1.0
        best_passes_gates = False
        # BUG (gefunden bei der Review): die abschliessende "als beste
        # Strategie markieren"-upsert() unten schreibt dieselbe Zeile
        # (symbol, exchange, timeframe, strategy, batch_id) ein zweites
        # Mal - StrategySymbolValidationRepository.upsert() macht ein
        # ON CONFLICT DO UPDATE ueber ALLE Spalten, nicht nur die
        # explizit uebergebenen. Ohne die hier gesammelten Werte wuerde
        # dieser zweite Aufruf metrics/parameters (Default {}) ueber die
        # bereits im Loop persistierten echten Werte schreiben - die
        # ACTIVE-Zeile haette dann leere Metriken, obwohl der Backtest
        # tatsaechlich welche geliefert hat.
        best_metrics: dict[str, Any] = {}
        best_parameters: dict[str, Any] = {}
        # BUG (gefunden 2026-09-15 bei der Failure-/Gate-Analyse ueber
        # den echten 718-Symbol-Batch): identische Bug-Klasse wie oben
        # bei metrics/parameters - robustness_score wurde in der finalen
        # "als beste Strategie markieren"-upsert() NICHT mitgegeben und
        # damit vom ON CONFLICT DO UPDATE auf NULL zurueckgesetzt. Live
        # bestaetigt: mean_reversion_v1 zeigte dadurch fuer ALLE 591
        # betroffenen Symbole robustness_score=NULL in der finalen Zeile,
        # obwohl der Wert im Loop korrekt berechnet wurde.
        best_robustness_score: float | None = None

        for strategy in candidates:
            validation_started_at = datetime.now(tz=UTC)

            # Parameter-Optimierung VOR der eigentlichen Validierung
            # (Phase-8-Folgearbeit, siehe sgr/strategy/parameter_optimizer.py
            # Modul-Docstring fuer die Anti-Overfitting-Begruendung: die
            # Suche laeuft nur auf einem frueheren Teilfenster, die
            # anschliessende Validierung NUR auf dem disjunkten Rest).
            # type(strategy._params) statt einer separaten name->Klasse-
            # Mapping: jede der 5 registrierten Strategien folgt identisch
            # dem Muster __init__(self, params: XParams | None = None),
            # Zugriff auf das "private" Feld ist hier bewusst pragmatisch
            # statt einer zweiten, driftanfaelligen Mapping-Quelle.
            # hasattr-Wache: eine Strategie-Instanz ohne dieses Attribut
            # (z.B. ein Test-Double, das TradingStrategy minimal erfuellt)
            # wird einfach nicht optimiert, statt hart zu scheitern -
            # optimize_strategy_parameters() selbst wuerde denselben Fall
            # (kein definierter Suchraum) ohnehin genauso behandeln.
            opt_result = (
                await optimize_strategy_parameters(
                    engine=self._engine,
                    registry=registry,
                    strategy_name=strategy.name,
                    strategy_class=type(strategy),
                    params_class=type(strategy._params),  # noqa: SLF001
                    symbol=symbol,
                    timeframe=self._timeframe,
                    full_start=start_date,
                    full_end=end_date,
                    exchange_id=self._exchange_id,
                )
                if hasattr(strategy, "_params")
                else OptimizationResult(
                    strategy_name=strategy.name,
                    symbol=symbol,
                    performed=False,
                    skip_reason="strategy_has_no_params_attribute",
                )
            )

            run_strategy_names = [strategy.name]
            run_start, run_end = start_date, end_date
            used_parameters = dict(strategy.get_parameters().params)
            trial_name: str | None = None

            if opt_result.performed and opt_result.best_overrides:
                assert opt_result.validation_window is not None
                run_start = datetime.fromisoformat(opt_result.validation_window[0])
                run_end = datetime.fromisoformat(opt_result.validation_window[1])
                trial_name = f"{strategy.name}__optimized_{uuid4().hex[:6]}"
                trial_instance = build_trial_strategy(
                    type(strategy),
                    type(strategy._params),
                    opt_result.best_overrides,
                    trial_name,  # noqa: SLF001
                )
                registry.register_instance(trial_instance)
                run_strategy_names = [trial_name]
                used_parameters = dict(trial_instance.get_parameters().params)

            try:
                report = await self._engine.run_full_validation(
                    strategy_names=run_strategy_names,
                    symbols=[symbol],
                    timeframe=self._timeframe,
                    start_date=run_start,
                    end_date=run_end,
                    exchange_pool=None,
                    exchange_id=self._exchange_id,
                    initial_capital=Decimal("10000"),
                    run_walk_forward=True,
                    run_monte_carlo=False,
                )
            finally:
                if trial_name is not None:
                    registry.unregister(trial_name)

            try:
                from sgr.monitoring.trading_metrics import record_strategy_validation_duration

                record_strategy_validation_duration(
                    (datetime.now(tz=UTC) - validation_started_at).total_seconds()
                )
            except Exception as e:
                log.warning("symbol_validation.duration_metric_failed", error=str(e))

            result = score_validation(report.backtest, report.walk_forward)

            metrics = {
                "optimization": opt_result.to_dict(),
                "total_return_pct": report.backtest.total_return_pct,
                "sharpe_ratio": report.backtest.sharpe_ratio,
                "sortino_ratio": report.backtest.sortino_ratio,
                "calmar_ratio": report.backtest.calmar_ratio,
                "max_drawdown_pct": report.backtest.max_drawdown_pct,
                "profit_factor": report.backtest.profit_factor,
                "hit_rate_pct": report.backtest.hit_rate_pct,
                "total_trades": report.backtest.total_trades,
                "winning_trades": report.backtest.winning_trades,
                "losing_trades": report.backtest.losing_trades,
                "avg_winner": report.backtest.avg_winner,
                "avg_loser": report.backtest.avg_loser,
                "wf_is_consistent": report.walk_forward.is_consistent
                if report.walk_forward
                else False,
                "wf_consistency_score": report.walk_forward.consistency_score
                if report.walk_forward
                else 0.0,
                "wf_oos_sharpe": report.walk_forward.out_of_sample_sharpe
                if report.walk_forward
                else 0.0,
                "wf_degradation_factor": report.walk_forward.degradation_factor
                if report.walk_forward
                else 0.0,
                "go_live_decision": report.go_live_decision,
                "score_components": result.components,
            }

            symbol_status = (
                SymbolValidationStatus.VALIDATED_BUT_NOT_ACTIVE
                if result.passes_gates
                else SymbolValidationStatus.NO_VALID_STRATEGY
            )

            await self._repo.upsert(
                symbol=symbol,
                exchange=self._exchange_id.value,
                timeframe=self._timeframe,
                strategy=strategy.name,
                status=symbol_status.value,
                batch_id=batch_id,
                parameters=used_parameters,
                metrics=_sanitize_for_jsonb(metrics),
                data_quality=quality.to_dict(),
                regime_profile=profile.to_dict(),
                score=result.score,
                robustness_score=result.robustness_score,
                failure_reason=report.decision_summary if not result.passes_gates else None,
            )

            if result.score > best_score:
                best_score = result.score
                best_strategy_name = strategy.name
                best_passes_gates = result.passes_gates
                best_metrics = metrics
                best_parameters = used_parameters
                best_robustness_score = result.robustness_score

        final_status = (
            SymbolValidationStatus.ACTIVE
            if best_passes_gates
            else SymbolValidationStatus.NO_VALID_STRATEGY
        )

        if best_strategy_name is not None:
            await self._repo.clear_best_flag(
                symbol=symbol,
                exchange=self._exchange_id.value,
                timeframe=self._timeframe,
                batch_id=batch_id,
            )
            await self._repo.upsert(
                symbol=symbol,
                exchange=self._exchange_id.value,
                timeframe=self._timeframe,
                strategy=best_strategy_name,
                status=final_status.value,
                batch_id=batch_id,
                score=best_score,
                is_best_for_symbol=True,
                # siehe Kommentar oben bei best_metrics/best_parameters/
                # best_robustness_score - ohne diese wuerde dieser
                # Aufruf die bereits im Loop persistierten echten Werte
                # mit leeren Defaults ueberschreiben (ON CONFLICT DO
                # UPDATE trifft ALLE Spalten).
                metrics=_sanitize_for_jsonb(best_metrics),
                parameters=best_parameters,
                robustness_score=best_robustness_score,
                data_quality=quality.to_dict(),
                regime_profile=profile.to_dict(),
            )

        return SymbolValidationOutcome(
            symbol=symbol,
            status=final_status,
            best_strategy=best_strategy_name,
            score=best_score if best_score >= 0 else None,
        )

    def _update_metrics(self, outcome: SymbolValidationOutcome) -> None:
        try:
            from sgr.monitoring.trading_metrics import (
                record_strategy_validation,
            )

            record_strategy_validation(
                status=outcome.status.value,
                strategy=outcome.best_strategy or "none",
            )
        except Exception as e:
            log.warning("symbol_validation.metrics_record_failed", error=str(e))
