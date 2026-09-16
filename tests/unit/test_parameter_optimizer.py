"""
Tests fuer sgr.strategy.parameter_optimizer.

Kernfragen, die diese Tests beantworten muessen (siehe Modul-Docstring
von parameter_optimizer.py fuer die vollstaendige Begruendung):
    1. Wird bei zu wenig Historie ehrlich uebersprungen statt erzwungen?
    2. Ist das Optimization-Fenster IMMER strikt vor dem Validation-
       Fenster und disjunkt (die eigentliche Anti-Overfitting-Garantie)?
    3. Wird bei JEDEM Kandidaten (Erfolg UND Fehler) der Trial-Eintrag
       wieder aus der Registry entfernt (keine Leichen)?
    4. Wird tatsaechlich der Sharpe-beste QUALIFIZIERENDE Kandidat
       gewaehlt, nicht z.B. einer mit zu wenigen Trades?
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sgr.backtesting.engine import FullValidationReport
from sgr.backtesting.types import BacktestResult, BacktestStatus
from sgr.core.types import ExchangeID
from sgr.strategy.base import StrategyParameters
from sgr.strategy.parameter_optimizer import (
    MIN_CANDLES_FOR_OPTIMIZATION,
    OPTIMIZATION_WINDOW_FRACTION,
    PARAMETER_SEARCH_SPACE,
    _param_grid,
    build_trial_strategy,
    optimize_strategy_parameters,
)
from sgr.strategy.registry import StrategyRegistry


@dataclass
class FakeParams:
    threshold_a: float = 10.0
    min_confidence: float = 0.55


class FakeOptStrategy:
    name = "fake_opt_strategy"
    version = "1.0.0"
    supported_regimes: list = []

    def __init__(self, params: FakeParams | None = None) -> None:
        self._params = params or FakeParams()

    def generate_signal(self, context):
        return None

    def get_parameters(self) -> StrategyParameters:
        return StrategyParameters(
            name=self.name,
            version=self.version,
            params={
                "threshold_a": self._params.threshold_a,
                "min_confidence": self._params.min_confidence,
            },
        )

    def validate_context(self, context) -> bool:
        return True


def _backtest(sharpe: float, trades: int) -> BacktestResult:
    return BacktestResult(
        config_summary={},
        status=BacktestStatus.COMPLETED,
        start_date="2026-01-01",
        end_date="2026-06-01",
        duration_days=150,
        initial_capital="10000",
        final_capital="11000",
        total_return_pct=1.0,
        cagr_pct=2.0,
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


def _report(sharpe: float, trades: int) -> FullValidationReport:
    return FullValidationReport(
        strategy_names=["fake"],
        symbols=["BTC/USDT"],
        timeframe="1h",
        start_date="2026-01-01",
        end_date="2026-06-01",
        backtest=_backtest(sharpe, trades),
        walk_forward=None,
    )


class _FakeEngine:
    """Reiht vorkonfigurierte Ergebnisse ab, ein Ergebnis pro
    run_full_validation()-Aufruf, in Aufrufreihenfolge - der
    Kandidaten-Grid wird deterministisch in _param_grid()-Reihenfolge
    durchlaufen."""

    def __init__(self, results: list[FullValidationReport | Exception]) -> None:
        self._results = list(results)
        self.calls: list[dict] = []

    async def run_full_validation(self, **kwargs):
        self.calls.append(kwargs)
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _fresh_registry() -> StrategyRegistry:
    registry = StrategyRegistry.get()
    registry.clear()
    return registry


class TestParamGrid:
    def test_cartesian_product_size(self) -> None:
        space = {"a": [1, 2, 3], "b": [10, 20, 30]}
        combos = _param_grid(space)
        assert len(combos) == 9

    def test_every_combination_present(self) -> None:
        space = {"a": [1, 2], "b": [10, 20]}
        combos = _param_grid(space)
        assert {"a": 1, "b": 10} in combos
        assert {"a": 1, "b": 20} in combos
        assert {"a": 2, "b": 10} in combos
        assert {"a": 2, "b": 20} in combos

    def test_all_five_real_strategies_have_a_search_space(self) -> None:
        """Regressionsschutz: die 5 im Autonomous-Strategy-Universe-
        Rollout validierten Strategien muessen alle einen Suchraum
        haben, sonst wird eine davon stillschweigend nie optimiert."""
        for name in [
            "mean_reversion_v1",
            "trend_following_v1",
            "breakout_v1",
            "momentum_v1",
            "volatility_adjusted_momentum_v1",
        ]:
            assert name in PARAMETER_SEARCH_SPACE
            assert len(_param_grid(PARAMETER_SEARCH_SPACE[name])) == 9


class TestBuildTrialStrategy:
    def test_overrides_merge_with_defaults(self) -> None:
        instance = build_trial_strategy(
            FakeOptStrategy, FakeParams, {"threshold_a": 99.0}, "trial_1"
        )
        assert instance.name == "trial_1"
        assert instance._params.threshold_a == 99.0
        assert instance._params.min_confidence == 0.55  # unveraendert, Default

    def test_real_strategy_class_reflection_works(self) -> None:
        """Sanity-Check mit einer ECHTEN Strategie (nicht nur dem Fake) -
        stellt sicher, dass type(strategy._params) + dataclasses.replace
        tatsaechlich mit den realen *Params-Dataclasses funktioniert."""
        from sgr.strategy.mean_reversion import MeanReversionParams, MeanReversionStrategy

        base = MeanReversionStrategy()
        instance = build_trial_strategy(
            type(base), type(base._params), {"rsi_oversold": 20.0}, "trial_mr"
        )
        assert isinstance(instance._params, MeanReversionParams)
        assert instance._params.rsi_oversold == 20.0
        assert instance._params.rsi_overbought == 65.0  # unveraendert, Default
        assert instance.name == "trial_mr"


class TestOptimizeStrategyParameters:
    async def test_skips_when_no_search_space_defined(self) -> None:
        registry = _fresh_registry()
        try:
            engine = _FakeEngine([])
            result = await optimize_strategy_parameters(
                engine=engine,
                registry=registry,
                strategy_name="strategy_without_a_space",
                strategy_class=FakeOptStrategy,
                params_class=FakeParams,
                symbol="BTC/USDT",
                timeframe="1h",
                full_start=datetime(2026, 1, 1, tzinfo=UTC),
                full_end=datetime(2026, 6, 1, tzinfo=UTC),
                exchange_id=ExchangeID.BINANCE,
            )
            assert result.performed is False
            assert result.skip_reason == "no_search_space_defined_for_strategy"
            assert engine.calls == []
        finally:
            registry.clear()

    async def test_skips_when_insufficient_history(self, monkeypatch) -> None:
        registry = _fresh_registry()
        try:
            monkeypatch.setitem(
                PARAMETER_SEARCH_SPACE, "fake_opt_strategy", {"threshold_a": [1.0, 2.0, 3.0]}
            )
            engine = _FakeEngine([])
            short_span = datetime(2026, 1, 1, tzinfo=UTC)
            result = await optimize_strategy_parameters(
                engine=engine,
                registry=registry,
                strategy_name="fake_opt_strategy",
                strategy_class=FakeOptStrategy,
                params_class=FakeParams,
                symbol="BTC/USDT",
                timeframe="1h",
                full_start=short_span,
                full_end=short_span + timedelta(days=5),  # weit unter MIN_CANDLES_FOR_OPTIMIZATION
                exchange_id=ExchangeID.BINANCE,
            )
            assert result.performed is False
            assert result.skip_reason == "insufficient_history_for_optimization_split"
            assert engine.calls == []
        finally:
            registry.clear()

    async def test_optimization_and_validation_windows_are_disjoint_and_ordered(
        self, monkeypatch
    ) -> None:
        """Die zentrale Anti-Overfitting-Garantie: validation_window darf
        sich NIE mit optimization_window ueberlappen, und optimization
        muss zeitlich VOR validation liegen."""
        registry = _fresh_registry()
        try:
            monkeypatch.setitem(
                PARAMETER_SEARCH_SPACE, "fake_opt_strategy", {"threshold_a": [1.0, 2.0, 3.0]}
            )
            full_start = datetime(2026, 1, 1, tzinfo=UTC)
            days_needed = int(MIN_CANDLES_FOR_OPTIMIZATION / 24) + 10  # 1h-Timeframe-Annahme
            full_end = full_start + timedelta(days=days_needed)

            engine = _FakeEngine([_report(1.0, 30) for _ in range(3)])
            result = await optimize_strategy_parameters(
                engine=engine,
                registry=registry,
                strategy_name="fake_opt_strategy",
                strategy_class=FakeOptStrategy,
                params_class=FakeParams,
                symbol="BTC/USDT",
                timeframe="1h",
                full_start=full_start,
                full_end=full_end,
                exchange_id=ExchangeID.BINANCE,
            )

            assert result.performed is True
            assert result.optimization_window is not None
            assert result.validation_window is not None
            opt_start, opt_end = (datetime.fromisoformat(t) for t in result.optimization_window)
            val_start, val_end = (datetime.fromisoformat(t) for t in result.validation_window)

            assert opt_start == full_start
            assert opt_end == val_start  # direkt aneinander, keine Lücke, keine Ueberlappung
            assert val_end == full_end
            assert opt_end <= val_start
            span_days = (full_end - full_start).days
            expected_opt_days = int(span_days * OPTIMIZATION_WINDOW_FRACTION)
            assert (opt_end - opt_start).days == expected_opt_days
        finally:
            registry.clear()

    async def test_registry_has_no_leftover_trial_entries_after_success(self, monkeypatch) -> None:
        registry = _fresh_registry()
        try:
            monkeypatch.setitem(
                PARAMETER_SEARCH_SPACE, "fake_opt_strategy", {"threshold_a": [1.0, 2.0, 3.0]}
            )
            full_start = datetime(2026, 1, 1, tzinfo=UTC)
            full_end = full_start + timedelta(days=int(MIN_CANDLES_FOR_OPTIMIZATION / 24) + 10)
            engine = _FakeEngine([_report(1.0, 30) for _ in range(3)])

            entries_before = set(registry.get_all().keys())
            await optimize_strategy_parameters(
                engine=engine,
                registry=registry,
                strategy_name="fake_opt_strategy",
                strategy_class=FakeOptStrategy,
                params_class=FakeParams,
                symbol="BTC/USDT",
                timeframe="1h",
                full_start=full_start,
                full_end=full_end,
                exchange_id=ExchangeID.BINANCE,
            )
            entries_after = set(registry.get_all().keys())

            assert entries_after == entries_before
        finally:
            registry.clear()

    async def test_registry_has_no_leftover_trial_entries_after_candidate_error(
        self, monkeypatch
    ) -> None:
        """Ein technischer Fehler bei EINEM Kandidaten darf keinen
        Trial-Eintrag in der Registry zuruecklassen (finally-Cleanup)."""
        registry = _fresh_registry()
        try:
            monkeypatch.setitem(
                PARAMETER_SEARCH_SPACE, "fake_opt_strategy", {"threshold_a": [1.0, 2.0, 3.0]}
            )
            full_start = datetime(2026, 1, 1, tzinfo=UTC)
            full_end = full_start + timedelta(days=int(MIN_CANDLES_FOR_OPTIMIZATION / 24) + 10)
            results: list = [_report(1.0, 30), RuntimeError("boom"), _report(2.0, 30)]
            engine = _FakeEngine(results)

            entries_before = set(registry.get_all().keys())
            result = await optimize_strategy_parameters(
                engine=engine,
                registry=registry,
                strategy_name="fake_opt_strategy",
                strategy_class=FakeOptStrategy,
                params_class=FakeParams,
                symbol="BTC/USDT",
                timeframe="1h",
                full_start=full_start,
                full_end=full_end,
                exchange_id=ExchangeID.BINANCE,
            )
            entries_after = set(registry.get_all().keys())

            assert entries_after == entries_before
            assert result.candidates_tried == 3
            assert any(
                c.disqualified_reason and "technical_error" in c.disqualified_reason
                for c in result.candidates
            )
        finally:
            registry.clear()

    async def test_picks_the_qualifying_candidate_with_highest_sharpe(self, monkeypatch) -> None:
        registry = _fresh_registry()
        try:
            monkeypatch.setitem(
                PARAMETER_SEARCH_SPACE, "fake_opt_strategy", {"threshold_a": [1.0, 2.0, 3.0]}
            )
            full_start = datetime(2026, 1, 1, tzinfo=UTC)
            full_end = full_start + timedelta(days=int(MIN_CANDLES_FOR_OPTIMIZATION / 24) + 10)
            # 3 Kandidaten (nur "threshold_a" variiert, min_confidence
            # nicht im Suchraum dieses Tests): Sharpes 0.5, 2.5 (bester,
            # qualifiziert), 5.0 (bester ROH-Wert, aber disqualifiziert
            # wegen zu weniger Trades) - der Gewinner MUSS 2.5 sein.
            engine = _FakeEngine(
                [_report(0.5, 30), _report(2.5, 30), _report(5.0, 2)]
            )

            result = await optimize_strategy_parameters(
                engine=engine,
                registry=registry,
                strategy_name="fake_opt_strategy",
                strategy_class=FakeOptStrategy,
                params_class=FakeParams,
                symbol="BTC/USDT",
                timeframe="1h",
                full_start=full_start,
                full_end=full_end,
                exchange_id=ExchangeID.BINANCE,
            )

            assert result.performed is True
            assert result.best_overrides == {"threshold_a": 2.0}
            assert result.candidates_tried == 3
        finally:
            registry.clear()

    async def test_all_candidates_below_min_trades_yields_no_selection(self, monkeypatch) -> None:
        registry = _fresh_registry()
        try:
            monkeypatch.setitem(
                PARAMETER_SEARCH_SPACE, "fake_opt_strategy", {"threshold_a": [1.0, 2.0, 3.0]}
            )
            full_start = datetime(2026, 1, 1, tzinfo=UTC)
            full_end = full_start + timedelta(days=int(MIN_CANDLES_FOR_OPTIMIZATION / 24) + 10)
            engine = _FakeEngine([_report(1.0, 1), _report(2.0, 3), _report(3.0, 0)])

            result = await optimize_strategy_parameters(
                engine=engine,
                registry=registry,
                strategy_name="fake_opt_strategy",
                strategy_class=FakeOptStrategy,
                params_class=FakeParams,
                symbol="BTC/USDT",
                timeframe="1h",
                full_start=full_start,
                full_end=full_end,
                exchange_id=ExchangeID.BINANCE,
            )

            assert result.performed is True
            assert result.best_overrides == {}
            assert result.skip_reason == "no_candidate_reached_minimum_trade_count"
        finally:
            registry.clear()
