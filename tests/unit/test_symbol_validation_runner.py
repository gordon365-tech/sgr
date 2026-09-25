"""
Tests fuer sgr.strategy.symbol_validation_runner.SymbolStrategyValidationRunner.

BacktestingEngine.run_full_validation() (netzwerk-/DB-lastig) und
StrategySymbolValidationRepository (echte DB) werden gemockt - das
Regime-/Data-Quality-Detail ist bereits separat getestet
(test_regime_profile.py, test_data_quality.py); hier steht die
Orchestrierung im Fokus: Resume/Idempotenz, Fehlerisolation pro
Symbol, Best-Strategy-Auswahl, korrekte Statuswerte.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from sgr.backtesting.data_quality import DataQualityResult, DataQualityStatus
from sgr.backtesting.engine import FullValidationReport
from sgr.backtesting.types import BacktestResult, BacktestStatus, WalkForwardResult
from sgr.core.types import MarketRegime
from sgr.strategy.regime_profile import RegimeProfile
from sgr.strategy.registry import StrategyRegistry
from sgr.strategy.symbol_validation_runner import (
    NO_STRATEGY_SENTINEL,
    SymbolStrategyValidationRunner,
    SymbolValidationStatus,
)


class FakeStrategy:
    def __init__(self, name: str) -> None:
        self.name = name
        self.supported_regimes = []

    def get_parameters(self):
        from sgr.strategy.base import StrategyParameters

        return StrategyParameters(name=self.name, version="1.0.0", params={})


@dataclass
class _FakeOptParams:
    threshold: float = 1.0


class FakeStrategyWithParams:
    """Im Unterschied zu FakeStrategy: hat ein _params-Attribut UND
    folgt exakt demselben Konstruktor-Muster wie jede der 5 echten
    Strategien - __init__(self, params: XParams | None = None), name
    als KLASSEN-Attribut, nicht als Konstruktor-Parameter (siehe
    sgr/strategy/mean_reversion.py etc.). Fuer Tests der Parameter-
    Optimierungs-Integration in _validate_symbol():
    parameter_optimizer.build_trial_strategy() ruft strategy_class(
    trial_params) auf und setzt instance.name danach separat - ein
    Test-Double mit einer abweichenden Konstruktor-Signatur (z.B. name
    als erstes Positionsargument) wuerde diesen Aufruf falsch binden."""

    name = "weak_strategy"
    version = "1.0.0"
    supported_regimes: list = []

    def __init__(self, params: _FakeOptParams | None = None) -> None:
        self._params = params or _FakeOptParams()

    def generate_signal(self, context):
        return None

    def get_parameters(self):
        from sgr.strategy.base import StrategyParameters

        return StrategyParameters(
            name=self.name, version=self.version, params={"threshold": self._params.threshold}
        )

    def validate_context(self, context) -> bool:
        return True


class FakeRepo:
    def __init__(self, processed: set[str] | None = None) -> None:
        self.processed = processed or set()
        self.upserts: list[dict] = []
        self.cleared_best_calls: list[dict] = []

    async def get_processed_symbols(self, *, batch_id: str) -> set[str]:
        return set(self.processed)

    async def upsert(self, **kwargs) -> None:
        self.upserts.append(kwargs)

    async def clear_best_flag(self, **kwargs) -> None:
        self.cleared_best_calls.append(kwargs)

    async def get_strategy_distribution(self, *, batch_id=None) -> dict[str, int]:
        return {}

    async def get_active(self, *, batch_id=None) -> list[dict]:
        return []


def _profile(distribution: dict[str, int]) -> RegimeProfile:
    return RegimeProfile(
        dominant_regime=MarketRegime.TRENDING_UP,
        distribution=distribution,
        n_samples=5,
    )


def _quality_ok() -> DataQualityResult:
    return DataQualityResult(
        status=DataQualityStatus.OK,
        n_candles=1200,
        first_timestamp="2026-01-01T00:00:00+00:00",
        last_timestamp="2026-06-01T00:00:00+00:00",
        gap_count=0,
        missing_bars=0,
        missing_ratio=0.0,
    )


def _backtest(sharpe: float, trades: int = 60) -> BacktestResult:
    return BacktestResult(
        config_summary={},
        status=BacktestStatus.COMPLETED,
        start_date="2026-01-01",
        end_date="2026-06-01",
        duration_days=150,
        initial_capital="10000",
        final_capital="11000",
        total_return_pct=10.0,
        cagr_pct=20.0,
        sharpe_ratio=sharpe,
        sortino_ratio=sharpe,
        calmar_ratio=1.0,
        max_drawdown_pct=5.0,
        max_drawdown_duration_days=3,
        profit_factor=1.5,
        hit_rate_pct=50.0,
        expected_value_per_trade="10",
        total_trades=trades,
        winning_trades=trades // 2,
        losing_trades=trades // 2,
        avg_winner="50",
        avg_loser="30",
        avg_holding_bars=10.0,
        total_fees="10",
        total_slippage="5",
        trades=[],
        go_live_eligible=sharpe >= 1.0,
        go_live_blockers=[],
    )


def _wf(consistent: bool = True) -> WalkForwardResult:
    return WalkForwardResult(
        n_splits=6,
        split_results=[],
        is_consistent=consistent,
        consistency_score=0.8,
        in_sample_sharpe=1.5,
        out_of_sample_sharpe=1.2,
        degradation_factor=0.8,
        recommendation="PASS",
    )


def _report(sharpe: float, consistent: bool = True) -> FullValidationReport:
    backtest = _backtest(sharpe)
    return FullValidationReport(
        strategy_names=["fake"],
        symbols=["BTC/USDT"],
        timeframe="1h",
        start_date="2026-01-01",
        end_date="2026-06-01",
        backtest=backtest,
        walk_forward=_wf(consistent),
        go_live_decision="GO" if sharpe >= 1.0 and consistent else "NO-GO",
    )


@pytest.fixture(autouse=True)
def _fresh_registry():
    registry = StrategyRegistry.get()
    registry.clear()
    yield
    registry.clear()


class TestDataQualityGate:
    async def test_insufficient_data_skips_backtesting_entirely(self, monkeypatch) -> None:
        repo = FakeRepo()
        runner = SymbolStrategyValidationRunner(repo=repo)

        monkeypatch.setattr(
            "sgr.backtesting.data_loader.BacktestDataLoader.load_public_history",
            AsyncMock(return_value=[]),
        )
        insufficient = DataQualityResult(
            status=DataQualityStatus.INSUFFICIENT_DATA,
            n_candles=10,
            first_timestamp=None,
            last_timestamp=None,
            gap_count=0,
            missing_bars=0,
            missing_ratio=0.0,
            issues=["Only 10 candles available"],
        )
        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.assess_data_quality",
            lambda candles, timeframe: insufficient,
        )
        engine_mock = AsyncMock()
        runner._engine = engine_mock

        outcome = await runner._validate_symbol("XYZ/USDT", "batch-1", [])

        assert outcome.status == SymbolValidationStatus.INSUFFICIENT_DATA
        engine_mock.run_full_validation.assert_not_called()
        assert repo.upserts[0]["strategy"] == NO_STRATEGY_SENTINEL
        assert repo.upserts[0]["status"] == "insufficient_data"


class TestNoRegimeMatch:
    async def test_no_candidate_strategies_gives_no_valid_strategy(self, monkeypatch) -> None:
        repo = FakeRepo()
        runner = SymbolStrategyValidationRunner(repo=repo)

        monkeypatch.setattr(
            "sgr.backtesting.data_loader.BacktestDataLoader.load_public_history",
            AsyncMock(return_value=[object()] * 1200),
        )
        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.assess_data_quality",
            lambda candles, timeframe: _quality_ok(),
        )
        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.build_regime_profile",
            lambda candles: _profile({}),
        )
        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.select_candidate_strategies",
            lambda profile, strategies: [],
        )
        engine_mock = AsyncMock()
        runner._engine = engine_mock

        outcome = await runner._validate_symbol(
            "XYZ/USDT", "batch-1", [FakeStrategy("trend_following_v1")]
        )

        assert outcome.status == SymbolValidationStatus.NO_VALID_STRATEGY
        engine_mock.run_full_validation.assert_not_called()


class TestBestStrategySelection:
    async def test_picks_the_higher_scoring_strategy_as_active(self, monkeypatch) -> None:
        repo = FakeRepo()
        runner = SymbolStrategyValidationRunner(repo=repo)

        monkeypatch.setattr(
            "sgr.backtesting.data_loader.BacktestDataLoader.load_public_history",
            AsyncMock(return_value=[object()] * 1200),
        )
        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.assess_data_quality",
            lambda candles, timeframe: _quality_ok(),
        )
        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.build_regime_profile",
            lambda candles: _profile({"trending_up": 5}),
        )
        weak = FakeStrategy("weak_strategy")
        strong = FakeStrategy("strong_strategy")
        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.select_candidate_strategies",
            lambda profile, strategies: [weak, strong],
        )

        async def fake_run_full_validation(*, strategy_names, **kwargs):
            name = strategy_names[0]
            sharpe = 1.1 if name == "weak_strategy" else 2.5
            return _report(sharpe)

        runner._engine.run_full_validation = fake_run_full_validation

        outcome = await runner._validate_symbol("BTC/USDT", "batch-1", [weak, strong])

        assert outcome.status == SymbolValidationStatus.ACTIVE
        assert outcome.best_strategy == "strong_strategy"

        best_upserts = [u for u in repo.upserts if u.get("is_best_for_symbol")]
        assert len(best_upserts) == 1
        assert best_upserts[0]["strategy"] == "strong_strategy"
        assert len(repo.cleared_best_calls) == 1

        # Regressionstest: die "als beste Strategie markieren"-upsert()
        # muss die ECHTEN Metriken/Data-Quality der Gewinner-Strategie
        # tragen, nicht leere Defaults. upsert() macht ON CONFLICT DO
        # UPDATE ueber ALLE Spalten der (symbol, exchange, timeframe,
        # strategy, batch_id)-Zeile - ohne explizite Weitergabe wuerde
        # dieser zweite Aufruf fuer dieselbe Zeile (strong_strategy) die
        # bereits im Loop persistierten echten Werte mit {} ueberschreiben.
        assert best_upserts[0]["metrics"], "best-upsert metrics must not be empty"
        assert best_upserts[0]["metrics"]["sharpe_ratio"] == 2.5
        assert best_upserts[0]["data_quality"], "best-upsert data_quality must not be empty"
        assert best_upserts[0]["regime_profile"], "best-upsert regime_profile must not be empty"
        # Regressionstest (gefunden 2026-09-15 bei der Failure-/Gate-
        # Analyse ueber den echten 718-Symbol-Batch): identische Bug-
        # Klasse wie oben, aber fuer robustness_score - fehlte in der
        # finalen upsert() und wurde dadurch auf NULL zurueckgesetzt.
        # Live bestaetigt: alle 670 "no_valid_strategy"-Endergebnisse
        # des bereits abgeschlossenen 718-Symbol-Batches hatten dadurch
        # robustness_score=NULL, obwohl im Loop korrekt berechnet.
        assert best_upserts[0]["robustness_score"] is not None, (
            "best-upsert robustness_score must not be wiped to None"
        )

    async def test_no_candidate_passes_gates_gives_no_valid_strategy_not_active(
        self, monkeypatch
    ) -> None:
        repo = FakeRepo()
        runner = SymbolStrategyValidationRunner(repo=repo)

        monkeypatch.setattr(
            "sgr.backtesting.data_loader.BacktestDataLoader.load_public_history",
            AsyncMock(return_value=[object()] * 1200),
        )
        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.assess_data_quality",
            lambda candles, timeframe: _quality_ok(),
        )
        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.build_regime_profile",
            lambda candles: _profile({"ranging": 5}),
        )
        weak = FakeStrategy("weak_strategy")
        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.select_candidate_strategies",
            lambda profile, strategies: [weak],
        )

        async def fake_run_full_validation(*, strategy_names, **kwargs):
            return _report(sharpe=0.1, consistent=False)  # klar unter den Gates

        runner._engine.run_full_validation = fake_run_full_validation

        outcome = await runner._validate_symbol("BTC/USDT", "batch-1", [weak])

        assert outcome.status == SymbolValidationStatus.NO_VALID_STRATEGY


class TestParameterOptimizationIntegration:
    """Deckt die Verdrahtung in _validate_symbol() ab (nicht die
    Optimierungslogik selbst - die ist in test_parameter_optimizer.py
    getestet): wird bei einem erfolgreichen Optimierungsergebnis
    tatsaechlich eine Trial-Instanz mit den optimierten Parametern
    validiert, statt der Default-Instanz? Werden die optimierten
    Parameter (nicht die Defaults) persistiert? Wird der Trial-Eintrag
    danach wieder aus der Registry entfernt?"""

    async def test_optimized_params_are_used_and_persisted_when_optimization_succeeds(
        self, monkeypatch
    ) -> None:
        from sgr.strategy.parameter_optimizer import OptimizationResult

        repo = FakeRepo()
        runner = SymbolStrategyValidationRunner(repo=repo)
        registry = StrategyRegistry.get()
        registry.clear()

        monkeypatch.setattr(
            "sgr.backtesting.data_loader.BacktestDataLoader.load_public_history",
            AsyncMock(return_value=[object()] * 1200),
        )
        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.assess_data_quality",
            lambda candles, timeframe: _quality_ok(),
        )
        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.build_regime_profile",
            lambda candles: _profile({"trending_up": 5}),
        )
        strategy = FakeStrategyWithParams()
        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.select_candidate_strategies",
            lambda profile, strategies: [strategy],
        )

        async def fake_optimize(**kwargs):
            return OptimizationResult(
                strategy_name="weak_strategy",
                symbol="BTC/USDT",
                performed=True,
                best_overrides={"threshold": 42.0},
                candidates_tried=9,
                validation_window=("2026-01-01T00:00:00", "2026-06-01T00:00:00"),
            )

        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.optimize_strategy_parameters", fake_optimize
        )

        seen_strategy_names: list[str] = []
        registry_size_during_call: list[int] = []

        async def fake_run_full_validation(*, strategy_names, **kwargs):
            seen_strategy_names.append(strategy_names[0])
            registry_size_during_call.append(len(registry.get_all()))
            return _report(2.5)

        runner._engine.run_full_validation = fake_run_full_validation

        try:
            outcome = await runner._validate_symbol("BTC/USDT", "batch-1", [strategy])
        finally:
            registry.clear()

        # Die Trial-Instanz (nicht "weak_strategy" selbst) wurde
        # tatsaechlich validiert - Beweis, dass optimize_* wirklich in
        # den Ablauf eingreift statt nur ein totes Ergebnis zu liefern.
        assert seen_strategy_names[0] != "weak_strategy"
        assert seen_strategy_names[0].startswith("weak_strategy__optimized_")
        # Der Trial-Eintrag existierte WAEHREND des Aufrufs ...
        assert registry_size_during_call[0] == 1
        # ... und wurde danach wieder entfernt (kein Leck relativ zum
        # Ausgangszustand).
        assert set(registry.get_all().keys()) - {"weak_strategy"} == set()

        best_upserts = [u for u in repo.upserts if u.get("is_best_for_symbol")]
        assert len(best_upserts) == 1
        # Persistiert werden die OPTIMIERTEN Parameter (42.0), nicht der
        # Default (1.0) - siehe _FakeOptParams.threshold Default.
        assert best_upserts[0]["parameters"]["threshold"] == 42.0
        assert best_upserts[0]["metrics"]["optimization"]["performed"] is True
        assert best_upserts[0]["metrics"]["optimization"]["best_overrides"] == {"threshold": 42.0}
        assert outcome.status == SymbolValidationStatus.ACTIVE

    async def test_default_params_used_when_optimization_not_performed(self, monkeypatch) -> None:
        """Fallback-Pfad: wenn optimize_strategy_parameters() performed=
        False liefert (z.B. zu wenig Historie), muss die Default-
        Instanz validiert werden - unveraendertes Verhalten."""
        from sgr.strategy.parameter_optimizer import OptimizationResult

        repo = FakeRepo()
        runner = SymbolStrategyValidationRunner(repo=repo)
        registry = StrategyRegistry.get()
        registry.clear()

        monkeypatch.setattr(
            "sgr.backtesting.data_loader.BacktestDataLoader.load_public_history",
            AsyncMock(return_value=[object()] * 1200),
        )
        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.assess_data_quality",
            lambda candles, timeframe: _quality_ok(),
        )
        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.build_regime_profile",
            lambda candles: _profile({"trending_up": 5}),
        )
        strategy = FakeStrategyWithParams()
        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.select_candidate_strategies",
            lambda profile, strategies: [strategy],
        )

        async def fake_optimize(**kwargs):
            return OptimizationResult(
                strategy_name="weak_strategy",
                symbol="BTC/USDT",
                performed=False,
                skip_reason="insufficient_history_for_optimization_split",
            )

        monkeypatch.setattr(
            "sgr.strategy.symbol_validation_runner.optimize_strategy_parameters", fake_optimize
        )

        seen_strategy_names: list[str] = []

        async def fake_run_full_validation(*, strategy_names, **kwargs):
            seen_strategy_names.append(strategy_names[0])
            return _report(2.5)

        runner._engine.run_full_validation = fake_run_full_validation

        try:
            await runner._validate_symbol("BTC/USDT", "batch-1", [strategy])
        finally:
            registry.clear()

        assert seen_strategy_names == ["weak_strategy"]

        best_upserts = [u for u in repo.upserts if u.get("is_best_for_symbol")]
        assert best_upserts[0]["parameters"]["threshold"] == 1.0  # Default, nicht optimiert
        assert best_upserts[0]["metrics"]["optimization"]["performed"] is False


class TestBatchResumeAndIsolation:
    async def test_resume_skips_already_processed_symbols(self, monkeypatch) -> None:
        repo = FakeRepo(processed={"BTC/USDT"})
        runner = SymbolStrategyValidationRunner(repo=repo)

        called_with: list[str] = []

        async def fake_validate_symbol(symbol, batch_id, candidates):
            called_with.append(symbol)
            from sgr.strategy.symbol_validation_runner import SymbolValidationOutcome

            return SymbolValidationOutcome(symbol=symbol, status=SymbolValidationStatus.ACTIVE)

        monkeypatch.setattr(runner, "_validate_symbol", fake_validate_symbol)
        monkeypatch.setattr(runner, "_publish_summary_metrics", AsyncMock())

        await runner.run_batch(["BTC/USDT", "ETH/USDT"], batch_id="batch-1", resume=True)

        assert called_with == ["ETH/USDT"]

    async def test_no_resume_reprocesses_everything(self, monkeypatch) -> None:
        repo = FakeRepo(processed={"BTC/USDT"})
        runner = SymbolStrategyValidationRunner(repo=repo)

        called_with: list[str] = []

        async def fake_validate_symbol(symbol, batch_id, candidates):
            called_with.append(symbol)
            from sgr.strategy.symbol_validation_runner import SymbolValidationOutcome

            return SymbolValidationOutcome(symbol=symbol, status=SymbolValidationStatus.ACTIVE)

        monkeypatch.setattr(runner, "_validate_symbol", fake_validate_symbol)
        monkeypatch.setattr(runner, "_publish_summary_metrics", AsyncMock())

        await runner.run_batch(["BTC/USDT", "ETH/USDT"], batch_id="batch-1", resume=False)

        assert set(called_with) == {"BTC/USDT", "ETH/USDT"}

    async def test_one_symbol_failure_does_not_abort_the_batch(self, monkeypatch) -> None:
        repo = FakeRepo()
        runner = SymbolStrategyValidationRunner(repo=repo)

        async def fake_validate_symbol(symbol, batch_id, candidates):
            if symbol == "BROKEN/USDT":
                raise RuntimeError("exchange timeout")
            from sgr.strategy.symbol_validation_runner import SymbolValidationOutcome

            return SymbolValidationOutcome(symbol=symbol, status=SymbolValidationStatus.ACTIVE)

        monkeypatch.setattr(runner, "_validate_symbol", fake_validate_symbol)
        monkeypatch.setattr(runner, "_publish_summary_metrics", AsyncMock())

        summary = await runner.run_batch(
            ["BTC/USDT", "BROKEN/USDT", "ETH/USDT"], batch_id="batch-1", resume=False
        )

        assert summary.total == 3
        assert summary.by_status["technical_failure"] == 1
        assert summary.by_status["active"] == 2
        assert "BROKEN/USDT" in summary.technical_failures


class TestSanitizeForJsonb:
    """Regression test (2026-09-15, echter Live-Fund): Postgres JSONB
    lehnt Infinity/NaN ab (im Unterschied zu Python's json-Modul) -
    PerformanceAnalyzer._sortino() liefert legitim float('inf') bei
    Backtests ohne verlierende Bar."""

    def test_infinity_becomes_none(self) -> None:
        from sgr.strategy.symbol_validation_runner import _sanitize_for_jsonb

        assert _sanitize_for_jsonb(float("inf")) is None
        assert _sanitize_for_jsonb(float("-inf")) is None

    def test_nan_becomes_none(self) -> None:
        from sgr.strategy.symbol_validation_runner import _sanitize_for_jsonb

        assert _sanitize_for_jsonb(float("nan")) is None

    def test_finite_float_is_unchanged(self) -> None:
        from sgr.strategy.symbol_validation_runner import _sanitize_for_jsonb

        assert _sanitize_for_jsonb(1.5) == 1.5

    def test_recurses_into_nested_dicts_and_lists(self) -> None:
        from sgr.strategy.symbol_validation_runner import _sanitize_for_jsonb

        value = {"a": float("inf"), "b": [1.0, float("nan"), {"c": float("-inf")}]}
        result = _sanitize_for_jsonb(value)
        assert result == {"a": None, "b": [1.0, None, {"c": None}]}
