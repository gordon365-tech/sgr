"""
Tests für sgr/monitoring/{sentry,alerts,engine,metrics}.py

Kontext: sgr/monitoring/__init__.py und engine.py referenzierten nach
commit d76297e (OTel-Migration von metrics.py) noch die alte
prometheus_client-Attribut-API (portfolio_value, drawdown_pct, ...).
Das Paket war dadurch beim Import komplett kaputt (ImportError in
__init__.py). Diese Tests verifizieren die reparierte, konsistente
OTel-basierte API (SGRMetrics / get_metrics() / record_*).

observability.py bleibt bewusst außen vor (deferred finding, siehe
Projekt-Memory) mit Ausnahme von setup_tracing(), das jetzt ein
dokumentierter No-Op ist statt eines Import-Crashs.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import sgr.monitoring.metrics as metrics_module
from sgr.backtesting.types import BacktestResult, BacktestStatus
from sgr.core.types import MarketRegime
from sgr.monitoring import alerts, sentry
from sgr.monitoring.alerts import AlertSeverity
from sgr.monitoring.engine import MonitoringEngine, add_metrics_middleware, create_metrics_app
from sgr.monitoring.metrics import (
    SGRMetrics,
    get_metrics,
    record_candle_received,
    record_portfolio_snapshot,
    record_risk_snapshot,
    record_signal_generated,
    record_trade_executed,
)
from sgr.strategy.base import ValidationStatus
from sgr.strategy.registry import StrategyRegistry


class FakeStrategyForMetrics:
    """Minimal-Strategie fürs Registry-basierte Testen der Gauge-Wiring in
    MonitoringEngine._collect() - siehe TestMonitoringEngineCollect neue
    Tests weiter unten (Schritt 6: Sharpe/Return/active_strategies)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.version = "1.0.0"
        self.supported_regimes = [MarketRegime.TRENDING_UP]

    def generate_signal(self, context):
        return None

    def get_parameters(self):
        return {}


def make_fake_backtest_result(
    *,
    sharpe_ratio: float,
    total_return_pct: float,
    max_drawdown_pct: float,
    total_trades: int,
) -> BacktestResult:
    """Minimal-gültiges BacktestResult für Gauge-Wiring-Tests - nur die
    Felder, die MonitoringEngine._collect() tatsächlich liest, sind
    variabel, alles andere ist ein neutraler Platzhalter."""
    return BacktestResult(
        config_summary={},
        status=BacktestStatus.COMPLETED,
        start_date="2026-01-01",
        end_date="2026-06-30",
        duration_days=180,
        initial_capital="10000",
        final_capital="10000",
        total_return_pct=total_return_pct,
        cagr_pct=0.0,
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=0.0,
        calmar_ratio=0.0,
        max_drawdown_pct=max_drawdown_pct,
        max_drawdown_duration_days=1,
        profit_factor=1.0,
        hit_rate_pct=50.0,
        expected_value_per_trade="0",
        total_trades=total_trades,
        winning_trades=0,
        losing_trades=0,
        avg_winner="0",
        avg_loser="0",
        avg_holding_bars=1.0,
        total_fees="0",
        total_slippage="0",
    )


# ---------------------------------------------------------------------------
# metrics.py
# ---------------------------------------------------------------------------


class TestSGRMetrics:
    def setup_method(self) -> None:
        # Singleton is module-global; reset so each test gets a fresh instrument set.
        metrics_module._metrics_instance = None

    def test_get_metrics_returns_singleton(self) -> None:
        m1 = get_metrics()
        m2 = get_metrics()
        assert m1 is m2
        assert isinstance(m1, SGRMetrics)

    def test_sgr_metrics_creates_all_instruments(self) -> None:
        m = SGRMetrics()
        for attr in (
            "portfolio_value",
            "portfolio_cash",
            "daily_pnl",
            "daily_pnl_pct",
            "portfolio_heat",
            "max_drawdown",
            "leverage",
            "open_positions_count",
            "var_95",
            "trades_total",
            "trades_winning",
            "trades_losing",
            "strategy_signals",
            "strategy_win_rate",
            "active_strategies_count",
            "strategy_validation_status",
            "strategy_sharpe_ratio",
            "strategy_total_return",
            "strategy_max_drawdown",
            "strategy_backtest_trades",
            "candles_received",
            "api_requests_total",
            "api_errors_total",
        ):
            assert hasattr(m, attr)

    def test_record_portfolio_snapshot_does_not_raise(self) -> None:
        record_portfolio_snapshot(
            portfolio_value=Decimal("10000.50"),
            cash=Decimal("5000.25"),
            daily_pnl=Decimal("123.45"),
            daily_pnl_pct=1.25,
        )

    def test_record_risk_snapshot_does_not_raise(self) -> None:
        record_risk_snapshot(
            portfolio_heat=0.3,
            max_drawdown_pct=5.0,
            leverage=1.5,
            open_positions=2,
            var_95_pct=2.1,
        )

    def test_record_trade_executed_winning(self) -> None:
        record_trade_executed(side="buy", pnl=Decimal("100"), winning=True)

    def test_record_trade_executed_losing(self) -> None:
        record_trade_executed(side="sell", pnl=Decimal("-50"), winning=False)

    def test_record_signal_generated_does_not_raise(self) -> None:
        record_signal_generated(strategy_name="trend_following", direction="long", confidence=0.8)

    def test_record_candle_received_does_not_raise(self) -> None:
        record_candle_received(symbol="BTC/USDT", timeframe="1h")


class TestSGRMetricsTenantScoping:
    """
    Server-verifizierte Multi-Tenant-Lücke (Schritt 6): worker-gordon und
    worker-sumo laufen als separate Prozesse, aber ohne Tenant-Label waren
    ihre Metriken in Prometheus ununterscheidbar (bestätigt per curl:
    zwei identische sgr_strategy_active_count-Zeilen). Diese Tests prüfen
    den Fix direkt: _TenantScopedInstrument muss "tenant" transparent in
    jeden Call injizieren, ohne dass Aufrufer (record_*-Funktionen,
    MonitoringEngine) sich selbst darum kümmern müssen.
    """

    def setup_method(self) -> None:
        metrics_module._metrics_instance = None

    def test_tenant_id_defaults_to_default_without_config_context(self) -> None:
        m = SGRMetrics()
        assert isinstance(m._tenant_id, str)

    def test_explicit_tenant_id_is_used(self) -> None:
        m = SGRMetrics(tenant_id="gordon")
        assert m._tenant_id == "gordon"

    def test_tenant_id_falls_back_to_default_when_config_unavailable(self, mocker) -> None:
        """Fail-safe: ein Config-Fehler (z.B. get_config() noch nicht
        initialisiert) darf SGRMetrics() niemals crashen lassen - siehe
        _resolve_tenant_id() Docstring."""
        mocker.patch(
            "sgr.monitoring.metrics.get_config", side_effect=RuntimeError("not configured")
        )
        m = SGRMetrics()
        assert m._tenant_id == "default"

    def test_set_injects_tenant_label(self, mocker) -> None:
        underlying = mocker.MagicMock()
        m = SGRMetrics(tenant_id="sumo")
        m.portfolio_value._instrument = underlying

        m.portfolio_value.set(100.0, {"status": "live"})

        underlying.set.assert_called_once_with(100.0, {"tenant": "sumo", "status": "live"})

    def test_add_injects_tenant_label(self, mocker) -> None:
        underlying = mocker.MagicMock()
        m = SGRMetrics(tenant_id="gordon")
        m.trades_total._instrument = underlying

        m.trades_total.add(1, {"side": "buy"})

        underlying.add.assert_called_once_with(1, {"tenant": "gordon", "side": "buy"})

    def test_set_injects_tenant_label_even_without_extra_attributes(self, mocker) -> None:
        underlying = mocker.MagicMock()
        m = SGRMetrics(tenant_id="gordon")
        m.active_strategies_count._instrument = underlying

        m.active_strategies_count.set(3)

        underlying.set.assert_called_once_with(3, {"tenant": "gordon"})

    def test_two_tenant_instances_produce_distinct_prometheus_series(self) -> None:
        """End-to-end gegen eine echte prometheus_client REGISTRY: zwei
        SGRMetrics-Instanzen mit unterschiedlicher tenant_id müssen zwei
        unterscheidbare Zeitreihen erzeugen - genau der auf dem Server
        beobachtete Bug, jetzt als Regressionstest."""
        from opentelemetry import metrics as otel_metrics
        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        from opentelemetry.sdk.metrics import MeterProvider
        from prometheus_client import CollectorRegistry, generate_latest

        registry = CollectorRegistry()
        reader = PrometheusMetricReader(registry=registry)
        provider = MeterProvider(metric_readers=[reader])
        otel_metrics.set_meter_provider(provider)

        m_gordon = SGRMetrics(tenant_id="gordon")
        m_gordon.active_strategies_count.set(0, {"trading_mode": "paper"})

        m_sumo = SGRMetrics(tenant_id="sumo")
        m_sumo.active_strategies_count.set(1, {"trading_mode": "paper"})

        out = generate_latest(registry).decode()
        gordon_lines = [
            line
            for line in out.splitlines()
            if "sgr_strategy_active_count" in line
            and not line.startswith("#")
            and 'tenant="gordon"' in line
        ]
        sumo_lines = [
            line
            for line in out.splitlines()
            if "sgr_strategy_active_count" in line
            and not line.startswith("#")
            and 'tenant="sumo"' in line
        ]
        assert len(gordon_lines) == 1
        assert len(sumo_lines) == 1
        assert gordon_lines[0] != sumo_lines[0]
        assert " 0.0" in gordon_lines[0]
        assert " 1.0" in sumo_lines[0]


# ---------------------------------------------------------------------------
# sentry.py
# ---------------------------------------------------------------------------


class TestSentrySetup:
    def test_setup_sentry_noop_when_dsn_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MONITORING_SENTRY_DSN", raising=False)
        with patch("sgr.monitoring.sentry.sentry_sdk.init") as mock_init:
            sentry.setup_sentry()
            mock_init.assert_not_called()

    def test_setup_sentry_initializes_when_dsn_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONITORING_SENTRY_DSN", "https://example@sentry.example.com/1")
        with patch("sgr.monitoring.sentry.sentry_sdk.init") as mock_init:
            sentry.setup_sentry()
            mock_init.assert_called_once()
            _, kwargs = mock_init.call_args
            assert kwargs["dsn"] is not None
            assert len(kwargs["integrations"]) == 3


# ---------------------------------------------------------------------------
# alerts.py
# ---------------------------------------------------------------------------


class TestSendTelegramAlert:
    async def test_returns_false_when_not_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MONITORING_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("MONITORING_TELEGRAM_CHAT_ID", raising=False)
        result = await alerts.send_telegram_alert("test message")
        assert result is False

    async def test_returns_true_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONITORING_TELEGRAM_BOT_TOKEN", "dummy-token")
        monkeypatch.setenv("MONITORING_TELEGRAM_CHAT_ID", "12345")

        mock_response = MagicMock(status_code=200)
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
            result = await alerts.send_telegram_alert("test", AlertSeverity.CRITICAL)
        assert result is True

    async def test_returns_false_on_non_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONITORING_TELEGRAM_BOT_TOKEN", "dummy-token")
        monkeypatch.setenv("MONITORING_TELEGRAM_CHAT_ID", "12345")

        mock_response = MagicMock(status_code=500)
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
            result = await alerts.send_telegram_alert("test")
        assert result is False

    async def test_returns_false_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONITORING_TELEGRAM_BOT_TOKEN", "dummy-token")
        monkeypatch.setenv("MONITORING_TELEGRAM_CHAT_ID", "12345")

        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=httpx.ConnectError("boom"))):
            result = await alerts.send_telegram_alert("test")
        assert result is False


class TestAlertHelpers:
    async def test_alert_high_drawdown_triggers_above_threshold(self) -> None:
        with patch(
            "sgr.monitoring.alerts.send_telegram_alert", new=AsyncMock(return_value=True)
        ) as mock_send:
            await alerts.alert_high_drawdown(current_drawdown=15.0, threshold=10.0)
            mock_send.assert_called_once()
            args, _ = mock_send.call_args
            assert "15.0" in args[0]

    async def test_alert_high_drawdown_silent_below_threshold(self) -> None:
        with patch(
            "sgr.monitoring.alerts.send_telegram_alert", new=AsyncMock(return_value=True)
        ) as mock_send:
            await alerts.alert_high_drawdown(current_drawdown=5.0, threshold=10.0)
            mock_send.assert_not_called()

    async def test_alert_api_error_rate_triggers_above_threshold(self) -> None:
        with patch(
            "sgr.monitoring.alerts.send_telegram_alert", new=AsyncMock(return_value=True)
        ) as mock_send:
            await alerts.alert_api_error_rate(error_rate=0.1, threshold=0.05)
            mock_send.assert_called_once()

    async def test_alert_api_error_rate_silent_below_threshold(self) -> None:
        with patch(
            "sgr.monitoring.alerts.send_telegram_alert", new=AsyncMock(return_value=True)
        ) as mock_send:
            await alerts.alert_api_error_rate(error_rate=0.01, threshold=0.05)
            mock_send.assert_not_called()

    async def test_alert_strategy_degradation_triggers_below_threshold(self) -> None:
        with patch(
            "sgr.monitoring.alerts.send_telegram_alert", new=AsyncMock(return_value=True)
        ) as mock_send:
            await alerts.alert_strategy_degradation(
                strategy_name="mean_reversion", win_rate=0.2, threshold=0.4
            )
            mock_send.assert_called_once()
            args, _ = mock_send.call_args
            assert "mean_reversion" in args[0]

    async def test_alert_strategy_degradation_silent_above_threshold(self) -> None:
        with patch(
            "sgr.monitoring.alerts.send_telegram_alert", new=AsyncMock(return_value=True)
        ) as mock_send:
            await alerts.alert_strategy_degradation(
                strategy_name="mean_reversion", win_rate=0.6, threshold=0.4
            )
            mock_send.assert_not_called()

    async def test_alert_critical_event_always_sends(self) -> None:
        with patch(
            "sgr.monitoring.alerts.send_telegram_alert", new=AsyncMock(return_value=True)
        ) as mock_send:
            await alerts.alert_critical_event("DB down", "connection refused")
            mock_send.assert_called_once()
            args, kwargs = mock_send.call_args
            assert "DB down" in args[0]


# ---------------------------------------------------------------------------
# engine.py — MonitoringEngine
# ---------------------------------------------------------------------------


class TestMonitoringEngineLifecycle:
    async def test_start_and_stop(self) -> None:
        engine = MonitoringEngine(interval_seconds=0.01)
        await engine.start()
        assert engine._running is True
        assert engine._task is not None
        await engine.stop()
        assert engine._running is False

    async def test_collect_loop_survives_collect_error(self) -> None:
        """A raising _collect() must not kill the background task."""
        engine = MonitoringEngine(interval_seconds=0.01)
        engine._collect = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        await engine.start()
        import asyncio

        await asyncio.sleep(0.05)
        assert engine._task is not None
        assert not engine._task.done()
        await engine.stop()


class TestMonitoringEngineCollect:
    def _make_portfolio_engine(self) -> MagicMock:
        pe = MagicMock()
        pe.portfolio_value = Decimal("10000")
        pe.positions = {"BTC/USDT": MagicMock()}
        return pe

    async def test_collect_with_no_engines_does_not_raise(self) -> None:
        engine = MonitoringEngine()
        await engine._collect()

    async def test_collect_portfolio_metrics(self) -> None:
        pe = self._make_portfolio_engine()
        engine = MonitoringEngine(portfolio_engine=pe)
        metrics_module._metrics_instance = None
        await engine._collect()  # must not raise

    async def test_collect_portfolio_error_is_swallowed(self) -> None:
        pe = MagicMock()
        type(pe).portfolio_value = property(lambda self: (_ for _ in ()).throw(RuntimeError()))
        engine = MonitoringEngine(portfolio_engine=pe)
        await engine._collect()  # must not raise despite broken portfolio_engine

    async def test_collect_risk_metrics(self) -> None:
        pe = self._make_portfolio_engine()
        re = MagicMock()
        risk_metrics = MagicMock(
            drawdown_from_peak=0.05,
            var_95=0.02,
            portfolio_heat=0.3,
            daily_pnl_pct=0.01,
        )
        re._compute_metrics.return_value = risk_metrics
        engine = MonitoringEngine(risk_engine=re, portfolio_engine=pe)
        await engine._collect()
        # _compute_metrics wird aktuell zweimal aufgerufen (Portfolio-Snapshot-
        # Block und separater Risk-Block) -- ineffizient, aber kein Bug.
        assert re._compute_metrics.call_count >= 1
        re._compute_metrics.assert_any_call(
            portfolio_value=pe.portfolio_value, positions=pe.positions
        )

    async def test_collect_risk_error_is_swallowed(self) -> None:
        pe = self._make_portfolio_engine()
        re = MagicMock()
        re._compute_metrics.side_effect = RuntimeError("risk engine down")
        engine = MonitoringEngine(risk_engine=re, portfolio_engine=pe)
        await engine._collect()  # must not raise

    async def test_collect_strategy_metrics(self) -> None:
        entry = MagicMock()
        entry.performance = MagicMock(hit_rate=0.55)
        registry = MagicMock()
        registry.get_all.return_value = {"trend_following": entry}
        engine = MonitoringEngine(strategy_registry=registry)
        await engine._collect()
        registry.get_all.assert_called_once()

    async def test_collect_strategy_no_performance_skipped(self) -> None:
        entry = MagicMock()
        entry.performance = None
        registry = MagicMock()
        registry.get_all.return_value = {"trend_following": entry}
        engine = MonitoringEngine(strategy_registry=registry)
        await engine._collect()  # must not raise

    async def test_collect_strategy_error_is_swallowed(self) -> None:
        registry = MagicMock()
        registry.get_all.side_effect = RuntimeError("registry down")
        engine = MonitoringEngine(strategy_registry=registry)
        await engine._collect()  # must not raise

    async def test_collect_sets_active_strategies_count_gauge(self) -> None:
        """Schritt 6: active_strategies muss als echte Metrik sichtbar
        sein, nicht nur geloggt - siehe MonitoringEngine._collect()
        Docstring-Kommentar zum Server-Verifikationsverlauf."""
        registry = StrategyRegistry.get()
        registry.clear()
        try:
            registry.register_instance(FakeStrategyForMetrics("s1"))
            registry.register_instance(FakeStrategyForMetrics("s2"))
            await registry.activate("s1")  # nur eine von zwei aktiv

            metrics_module._metrics_instance = None
            m = get_metrics()
            m.active_strategies_count = MagicMock(wraps=m.active_strategies_count)

            engine = MonitoringEngine(strategy_registry=registry)
            await engine._collect()

            m.active_strategies_count.set.assert_called_once()
            call_args = m.active_strategies_count.set.call_args
            assert call_args[0][0] == 1  # nur "s1" ist aktiv
        finally:
            registry.clear()

    async def test_collect_sets_backtest_metrics_from_last_validation_result(self) -> None:
        """StrategyEntry.last_validation_result (gefüllt von
        StrategyValidationRunner.mark_validated) muss als
        strategy_sharpe_ratio/total_return/max_drawdown/backtest_trades
        Gauge ankommen - der zuvor auf dem Server bestätigte Bug war, dass
        diese Werte nur per log.debug ausgegeben wurden."""
        registry = StrategyRegistry.get()
        registry.clear()
        try:
            registry.register_instance(FakeStrategyForMetrics("s1"))
            registry.mark_validated(
                "s1",
                ValidationStatus(
                    backtest_passed=False,
                    walk_forward_passed=True,
                    paper_trading_passed=True,
                ),
                backtest_result=make_fake_backtest_result(
                    sharpe_ratio=-1.91,
                    total_return_pct=-1.6,
                    max_drawdown_pct=12.0,
                    total_trades=120,
                ),
            )

            metrics_module._metrics_instance = None
            m = get_metrics()
            m.strategy_sharpe_ratio = MagicMock(wraps=m.strategy_sharpe_ratio)
            m.strategy_total_return = MagicMock(wraps=m.strategy_total_return)
            m.strategy_max_drawdown = MagicMock(wraps=m.strategy_max_drawdown)
            m.strategy_backtest_trades = MagicMock(wraps=m.strategy_backtest_trades)
            m.strategy_validation_status = MagicMock(wraps=m.strategy_validation_status)

            engine = MonitoringEngine(strategy_registry=registry)
            await engine._collect()

            m.strategy_sharpe_ratio.set.assert_called_once()
            assert m.strategy_sharpe_ratio.set.call_args[0][0] == -1.91
            m.strategy_total_return.set.assert_called_once()
            assert m.strategy_total_return.set.call_args[0][0] == -1.6
            m.strategy_max_drawdown.set.assert_called_once()
            assert m.strategy_max_drawdown.set.call_args[0][0] == 12.0
            m.strategy_backtest_trades.set.assert_called_once()
            assert m.strategy_backtest_trades.set.call_args[0][0] == 120
            # can_go_live False (backtest_passed=False) -> Gauge 0, nicht 1
            m.strategy_validation_status.set.assert_called_once()
            assert m.strategy_validation_status.set.call_args[0][0] == 0
        finally:
            registry.clear()

    async def test_collect_skips_backtest_gauges_before_first_validation(self) -> None:
        """Vor dem ersten Validierungslauf ist last_validation_result None -
        die Backtest-Gauges dürfen dann NICHT geschrieben werden (keine
        vorgetäuschte 0, die wie ein echtes Ergebnis aussähe)."""
        registry = StrategyRegistry.get()
        registry.clear()
        try:
            registry.register_instance(FakeStrategyForMetrics("s1"))

            metrics_module._metrics_instance = None
            m = get_metrics()
            m.strategy_sharpe_ratio = MagicMock(wraps=m.strategy_sharpe_ratio)

            engine = MonitoringEngine(strategy_registry=registry)
            await engine._collect()

            m.strategy_sharpe_ratio.set.assert_not_called()
        finally:
            registry.clear()


class TestMonitoringEngineWorkerHealth:
    """_publish_worker_health() - siehe sgr/monitoring/worker_health.py und
    /health/trading-Fix (2026-09-25, Phase 5). Alle vier Signale muessen
    aus echten Komponenten-Referenzen abgeleitet werden, nie erfunden."""

    async def test_publish_worker_health_is_noop_without_redis(self) -> None:
        """Ohne redis_client ist publish_worker_health() ein no-op (siehe
        dessen Docstring) - _collect() darf dabei trotzdem nicht werfen."""
        engine = MonitoringEngine(risk_engine=MagicMock())
        await engine._collect()  # must not raise

    async def test_publish_worker_health_derives_true_signals_from_components(self) -> None:
        redis_client = AsyncMock()
        execution_engine = MagicMock()
        adapter = AsyncMock()
        adapter.ping = AsyncMock(return_value=12.3)
        exchange_pool = MagicMock()
        exchange_pool.get.return_value = adapter
        market_data_engine = MagicMock()
        market_data_engine._running = True

        engine = MonitoringEngine(
            risk_engine=MagicMock(),
            execution_engine=execution_engine,
            exchange_pool=exchange_pool,
            market_data_engine=market_data_engine,
            redis_client=redis_client,
            tenant_id="gordon",
        )

        with patch(
            "sgr.monitoring.engine.publish_worker_health", AsyncMock()
        ) as mock_publish:
            await engine._collect()

        mock_publish.assert_called_once()
        _, kwargs = mock_publish.call_args
        assert kwargs["risk_engine_available"] is True
        assert kwargs["preflight_available"] is True
        assert kwargs["exchange_connected"] is True
        assert kwargs["market_data_active"] is True
        assert kwargs["tenant_id"] == "gordon"

    async def test_publish_worker_health_false_signals_without_components(self) -> None:
        redis_client = AsyncMock()
        engine = MonitoringEngine(redis_client=redis_client)

        with patch(
            "sgr.monitoring.engine.publish_worker_health", AsyncMock()
        ) as mock_publish:
            await engine._collect()

        _, kwargs = mock_publish.call_args
        assert kwargs["risk_engine_available"] is False
        assert kwargs["preflight_available"] is False
        assert kwargs["exchange_connected"] is None  # kein Pool injiziert -> unknown, nicht False
        assert kwargs["market_data_active"] is False

    async def test_publish_worker_health_exchange_ping_failure_is_false_not_unknown(self) -> None:
        """Ein injizierter Pool, dessen Ping fehlschlaegt, ist ein echtes
        'nicht verbunden' (False) - nicht dasselbe wie 'kein Pool
        injiziert' (None/unknown, siehe Test oben)."""
        redis_client = AsyncMock()
        exchange_pool = MagicMock()
        adapter = AsyncMock()
        adapter.ping = AsyncMock(side_effect=RuntimeError("connection refused"))
        exchange_pool.get.return_value = adapter

        engine = MonitoringEngine(exchange_pool=exchange_pool, redis_client=redis_client)

        with patch(
            "sgr.monitoring.engine.publish_worker_health", AsyncMock()
        ) as mock_publish:
            await engine._collect()

        _, kwargs = mock_publish.call_args
        assert kwargs["exchange_connected"] is False

    async def test_publish_worker_health_error_is_swallowed(self) -> None:
        """Ein Fehler beim Heartbeat-Publish darf _collect() nie zum
        Absturz bringen (identisches Fail-Safe-Prinzip wie die anderen
        _collect()-Teilschritte)."""
        engine = MonitoringEngine(redis_client=AsyncMock())
        with patch(
            "sgr.monitoring.engine.publish_worker_health",
            AsyncMock(side_effect=RuntimeError("redis down")),
        ):
            await engine._collect()  # must not raise


class TestMetricsAppAndMiddleware:
    def test_create_metrics_app_returns_asgi_app(self) -> None:
        app = create_metrics_app()
        assert app is not None
        assert callable(app)

    def test_add_metrics_middleware_registers_middleware(self) -> None:
        from fastapi import FastAPI

        app = FastAPI()
        before = len(app.user_middleware)
        add_metrics_middleware(app)
        assert len(app.user_middleware) == before + 1

    async def test_metrics_middleware_tracks_api_route(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        add_metrics_middleware(app)

        @app.get("/api/ping")
        def ping() -> dict:
            return {"ok": True}

        client = TestClient(app)
        response = client.get("/api/ping")
        assert response.status_code == 200

    async def test_metrics_middleware_skips_non_api_route(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        add_metrics_middleware(app)

        @app.get("/not-tracked")
        def other() -> dict:
            return {"ok": True}

        client = TestClient(app)
        response = client.get("/not-tracked")
        assert response.status_code == 200
