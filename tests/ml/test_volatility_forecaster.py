"""
Tests für sgr.ml.volatility_forecaster.VolatilityForecaster.

Coverage-Ziel: 82% -> hoch.

Teststrategie: Da `arch` und `scipy` in dieser Umgebung installiert
sind, wird fit()/predict() über echte GARCH(1,1)-Schätzung getestet
statt gemockt - ein echter Maximum-Likelihood-Fit auf synthetischen
Returns deckt die tatsächliche `arch`-Integration ab. Der EWMA-
Fallback-Pfad (kein `arch` installiert) wird gezielt über
`sys.modules`-Patching der `arch`-Bibliothek getriggert.

Hinweis Bugfix: Diese Tests deckten einen echten Produktionsfehler in
fit() auf: res.conditional_volatility war in der installierten
`arch`-Version (8.0.0) ein numpy.ndarray statt eines pandas.Series,
wodurch `.iloc[-1]` mit AttributeError crashte - JEDER echte GARCH-Fit
schlug fehl. Siehe Commit-Message für Details zum Fix
(np.asarray(...)[-1] statt .iloc[-1]).
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from sgr.ml.types import ModelStatus
from sgr.ml.volatility_forecaster import VolatilityForecaster


def _make_returns(n: int = 500, seed: int = 42) -> np.ndarray:
    """Synthetische, aber realistische Log-Returns mit Volatility Clustering."""
    rng = np.random.default_rng(seed)
    returns = np.zeros(n)
    vol = 0.01
    for i in range(n):
        vol = 0.9 * vol + 0.1 * abs(returns[i - 1]) if i > 0 else vol
        returns[i] = rng.normal(0, vol)
    return returns


# ---------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------


class TestInitialState:
    def test_not_fitted_initially(self) -> None:
        forecaster = VolatilityForecaster()
        assert forecaster.is_fitted is False
        assert forecaster.metadata.status == ModelStatus.UNTRAINED


# ---------------------------------------------------------------------
# fit() - GARCH path
# ---------------------------------------------------------------------


class TestFitGarch:
    def test_fit_raises_with_too_few_returns(self) -> None:
        forecaster = VolatilityForecaster()
        with pytest.raises(ValueError, match="at least 30 returns"):
            forecaster.fit(np.array([0.01] * 10))

    def test_fit_succeeds_and_sets_metadata(self) -> None:
        forecaster = VolatilityForecaster()
        params = forecaster.fit(_make_returns())

        assert forecaster.is_fitted is True
        assert forecaster.metadata.status == ModelStatus.PRODUCTION
        assert forecaster.metadata.trained_at is not None
        assert "omega" in params
        assert "alpha" in params
        assert "beta" in params
        assert "persistence" in params

    def test_fit_forces_stationarity_when_non_stationary(self) -> None:
        forecaster = VolatilityForecaster()

        class _FakeParams(dict):
            def get(self, key, default=None):
                return {"omega": 0.01, "alpha[1]": 0.7, "beta[1]": 0.6}.get(key, default)

        fake_result = type(
            "FakeResult",
            (),
            {
                "params": _FakeParams(),
                "conditional_volatility": np.array([1.0, 2.0]),
            },
        )()
        fake_model = type("FakeModel", (), {"fit": lambda self, **kw: fake_result})()

        with patch("arch.arch_model", return_value=fake_model):
            params = forecaster.fit(_make_returns())

        # alpha + beta = 1.3 >= 1.0 -> forced to the stationary constants.
        assert params["alpha"] == 0.09
        assert params["beta"] == 0.89

    def test_fit_falls_back_to_ewma_when_arch_missing(self) -> None:
        forecaster = VolatilityForecaster()
        with patch.dict("sys.modules", {"arch": None}):
            params = forecaster.fit(_make_returns())

        assert forecaster.is_fitted is True
        assert params.get("fallback") == "ewma"


# ---------------------------------------------------------------------
# _fit_ewma() - direct
# ---------------------------------------------------------------------


class TestFitEwma:
    def test_fit_ewma_sets_stationary_params(self) -> None:
        forecaster = VolatilityForecaster()
        params = forecaster._fit_ewma(_make_returns())

        assert forecaster.is_fitted is True
        assert forecaster.metadata.status == ModelStatus.PRODUCTION
        assert params["alpha"] + params["beta"] < 1.0


# ---------------------------------------------------------------------
# predict()
# ---------------------------------------------------------------------


class TestPredict:
    def test_predict_unfitted_returns_fallback(self) -> None:
        forecaster = VolatilityForecaster()
        forecast = forecaster.predict("BTC/USDT", "1h", horizon_bars=5)

        assert forecast.model_version == "fallback"
        assert forecast.predicted_volatility_pct == 2.0

    def test_predict_fitted_returns_real_forecast(self) -> None:
        forecaster = VolatilityForecaster()
        forecaster.fit(_make_returns())

        forecast = forecaster.predict("BTC/USDT", "1h", horizon_bars=5)

        assert forecast.model_version == VolatilityForecaster.VERSION
        assert forecast.predicted_volatility_pct >= 0
        assert forecast.lower_bound_pct >= 0
        assert forecast.upper_bound_pct >= forecast.lower_bound_pct
        assert forecast.garch_alpha == forecaster._alpha
        assert forecast.garch_beta == forecaster._beta

    def test_predict_respects_custom_confidence_level(self) -> None:
        forecaster = VolatilityForecaster()
        forecaster.fit(_make_returns())

        narrow = forecaster.predict("BTC/USDT", "1h", horizon_bars=5, confidence_level=0.50)
        wide = forecaster.predict("BTC/USDT", "1h", horizon_bars=5, confidence_level=0.99)

        narrow_width = narrow.upper_bound_pct - narrow.lower_bound_pct
        wide_width = wide.upper_bound_pct - wide.lower_bound_pct
        assert wide_width >= narrow_width

    def test_predict_lower_bound_never_negative(self) -> None:
        forecaster = VolatilityForecaster()
        forecaster.fit(_make_returns())

        forecast = forecaster.predict("BTC/USDT", "1h", horizon_bars=1, confidence_level=0.999)

        assert forecast.lower_bound_pct >= 0.0


# ---------------------------------------------------------------------
# update()
# ---------------------------------------------------------------------


class TestUpdate:
    def test_update_noop_when_not_fitted(self) -> None:
        forecaster = VolatilityForecaster()
        forecaster.update(0.05)  # Should not raise.
        assert forecaster.is_fitted is False

    def test_update_adjusts_variance_when_fitted(self) -> None:
        forecaster = VolatilityForecaster()
        forecaster.fit(_make_returns())

        variance_before = forecaster._last_variance
        forecaster.update(0.10)

        assert forecaster._last_residual == pytest.approx(0.01)
        assert forecaster._last_variance != variance_before
