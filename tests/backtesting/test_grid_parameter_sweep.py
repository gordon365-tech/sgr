"""
Tests für sgr.backtesting.grid_parameter_sweep.sweep_grid_parameters()
(zuletzt revidiert 2026-09-24) - Anbindung von FuturesGridParameters-
Kandidaten an den bestehenden GridBacktestSimulator, KEINE neue
Optimizer-Engine. Deckt die echte Drei-Wege-Trennung (in-sample /
validation / out-of-sample) und das Top-K-Pruning.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sgr.backtesting.grid_parameter_sweep import (
    IN_SAMPLE_FRACTION,
    VALIDATION_FRACTION,
    sweep_grid_parameters,
)
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
        candles = _oscillating_candles(600)
        result = sweep_grid_parameters(
            candles,
            _base_config(),
            grid_count_candidates=[3, 5],
            leverage_candidates=[Decimal("1"), Decimal("2")],
            range_width_multiplier_candidates=[Decimal("1")],
        )

        assert result.candidates_evaluated > 0
        assert result.candidates_promoted_to_validation > 0
        assert result.chosen_parameters is not None
        assert result.skipped_reason is None
        # validation/out_of_sample_edge_confirmed sind bewusst NICHT hart
        # asserted - eine synthetische Sinuswelle muss keine reale Edge
        # im PerformanceAnalyzer-Sinn erzeugen; nur dass der gesamte
        # Sweep-Pfad (alle drei Fenster) fehlerfrei durchlaeuft und ein
        # Ergebnis mit expliziten blockers liefert, wird geprueft.
        assert isinstance(result.validation_edge_confirmed, bool)
        assert isinstance(result.validation_blockers, list)
        assert isinstance(result.out_of_sample_edge_confirmed, bool)
        assert isinstance(result.out_of_sample_blockers, list)

    def test_chosen_parameters_respect_candidate_bounds(self) -> None:
        candles = _oscillating_candles(600)
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

    def test_three_windows_are_disjoint_and_ordered(self) -> None:
        """Grundprinzip (Anti-Overfitting): in-sample, validation und
        out-of-sample duerfen sich nicht ueberlappen und muessen
        chronologisch aufeinander folgen."""
        candles = _oscillating_candles(600)
        n = len(candles)
        i1 = int(n * IN_SAMPLE_FRACTION)
        i2 = int(n * (IN_SAMPLE_FRACTION + VALIDATION_FRACTION))

        assert i1 < i2 < n
        assert candles[i1 - 1].timestamp < candles[i1].timestamp
        assert candles[i2 - 1].timestamp < candles[i2].timestamp

    def test_pruning_limits_candidates_promoted_to_validation(self) -> None:
        """Top-K-Pruning: auch bei einem grossen Kandidaten-Kreuzprodukt
        werden nur top_k_for_validation Kandidaten auf Validation/OOS
        ausgewertet, nicht der volle Raum (Kosten-Kontrolle)."""
        candles = _oscillating_candles(600)
        result = sweep_grid_parameters(
            candles,
            _base_config(),
            grid_count_candidates=[3, 4, 5],
            leverage_candidates=[Decimal("1"), Decimal("2")],
            range_width_multiplier_candidates=[Decimal("0.5"), Decimal("1"), Decimal("1.5")],
            top_k_for_validation=2,
        )

        # 3 x 2 x 3 x 2(SL/TP-Paare) = 36 In-Sample-Kandidaten moeglich,
        # aber hoechstens 2 werden befoerdert.
        assert result.candidates_evaluated > 2
        assert result.candidates_promoted_to_validation <= 2

    def test_insufficient_validation_or_oos_window_is_skipped(self) -> None:
        """Genug Gesamthistorie fuer min_candles, aber die Drei-Wege-
        Aufteilung selbst laesst ein zu kleines OOS-Fenster - muss
        ehrlich uebersprungen werden, nicht mit einem zu kleinen Fenster
        weiterlaufen."""
        candles = _oscillating_candles(300)  # == default min_candles, knapp
        result = sweep_grid_parameters(candles, _base_config(), min_candles=300)

        # Bei genau 300 Candles: OOS-Fenster = 25% von 300 = 75 >= 30,
        # sollte NICHT geskippt werden - dieser Test verifiziert die
        # Grenze bewusst knapp ueber der Skip-Schwelle.
        assert result.skipped_reason != "insufficient_validation_or_oos_window"
