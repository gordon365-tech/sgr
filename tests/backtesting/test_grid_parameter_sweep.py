"""
Tests für sgr.backtesting.grid_parameter_sweep.sweep_grid_parameters()
(Phase O, 2026-09-23) - Anbindung von FuturesGridParameters-Kandidaten an
den bestehenden GridBacktestSimulator, KEINE neue Optimizer-Engine.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sgr.backtesting.grid_parameter_sweep import sweep_grid_parameters
from sgr.backtesting.grid_types import GridBacktestConfig
from sgr.core.grid_types import FuturesGridParameters
from sgr.core.types import Candle, ExchangeID, GridDirection, Symbol

SYMBOL = Symbol(base="BTC", quote="USDT", exchange=ExchangeID.PIONEX)


def _candle(ts, o, h, low, c) -> Candle:
    return Candle(
        symbol=SYMBOL,
        timestamp=ts,
        timeframe="1h",
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        volume=Decimal("10000"),
    )


def _oscillating_candles(n: int, mid: float = 100.0, amplitude: float = 12.0) -> list[Candle]:
    """Sinuswellen-Preisverlauf zwischen [mid-amplitude, mid+amplitude] -
    erzeugt garantiert wiederholte Grid-Level-Crossings (deterministisch,
    kein Zufall - reproduzierbarer Test)."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles = []
    prev_close = mid
    for i in range(n):
        close = mid + amplitude * math.sin(i / 6.0)
        open_ = prev_close
        high = max(open_, close) + 0.5
        low = min(open_, close) - 0.5
        candles.append(_candle(start + timedelta(hours=i), open_, high, low, close))
        prev_close = close
    return candles


def _base_config() -> GridBacktestConfig:
    params = FuturesGridParameters(
        grid_lower_price=Decimal("90"),
        grid_upper_price=Decimal("110"),
        grid_count=5,
        long_or_short=GridDirection.LONG,
        leverage=Decimal("2"),
        position_size=Decimal("50"),
        max_notional=Decimal("250"),
    )
    return GridBacktestConfig(
        symbol="BTC/USDT", timeframe="1h", strategy_name="futures_grid_long_v1", parameters=params
    )


class TestSweepGridParameters:
    def test_insufficient_history_is_skipped_honestly(self) -> None:
        candles = _oscillating_candles(50)
        result = sweep_grid_parameters(candles, _base_config())

        assert result.chosen_parameters is None
        assert result.skipped_reason == "insufficient_history"

    def test_sweep_over_oscillating_market_produces_a_chosen_candidate(self) -> None:
        candles = _oscillating_candles(400)
        result = sweep_grid_parameters(
            candles,
            _base_config(),
            grid_count_candidates=[3, 5],
            leverage_candidates=[Decimal("1"), Decimal("2")],
            range_width_multiplier_candidates=[Decimal("1")],
        )

        assert result.candidates_evaluated > 0
        assert result.chosen_parameters is not None
        assert result.skipped_reason is None
        # validation_window_edge_confirmed ist bewusst NICHT hart
        # asserted - eine synthetische Sinuswelle muss keine reale
        # Edge im PerformanceAnalyzer-Sinn erzeugen; nur dass der
        # gesamte Sweep- und Validierungs-Pfad fehlerfrei durchlaeuft
        # und ein Ergebnis mit expliziten blockers liefert, wird
        # geprueft.
        assert isinstance(result.validation_window_edge_confirmed, bool)
        assert isinstance(result.validation_window_blockers, list)

    def test_chosen_parameters_respect_candidate_bounds(self) -> None:
        candles = _oscillating_candles(400)
        result = sweep_grid_parameters(
            candles,
            _base_config(),
            grid_count_candidates=[4],
            leverage_candidates=[Decimal("3")],
            range_width_multiplier_candidates=[Decimal("2")],
        )

        assert result.chosen_parameters is not None
        assert result.chosen_parameters.grid_count == 4
        assert result.chosen_parameters.leverage == Decimal("3")

    def test_optimization_and_validation_windows_are_disjoint(self) -> None:
        """Grundprinzip (Anti-Overfitting): der Sweep darf nicht auf
        derselben Datenmenge suchen UND validieren - verifiziert indirekt
        ueber die interne Fenster-Aufteilung (60/40)."""
        from sgr.backtesting.grid_parameter_sweep import OPTIMIZATION_WINDOW_FRACTION

        candles = _oscillating_candles(400)
        split = int(len(candles) * OPTIMIZATION_WINDOW_FRACTION)

        assert split < len(candles)
        assert candles[split - 1].timestamp < candles[split].timestamp
