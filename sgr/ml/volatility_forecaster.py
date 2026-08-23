"""
SGR Volatility Forecaster
==========================
Prognostiziert Volatilität für die nächsten N Bars.

Modell: GARCH(1,1) mit optionalem LSTM-Residual-Korrekturfaktor

GARCH(1,1) Grundidee:
    σ²(t) = ω + α * ε²(t-1) + β * σ²(t-1)
    ω: langfristige Varianz (Unconditional Variance)
    α: Reaktion auf neue Informationen (ARCH-Term)
    β: Persistenz der Volatilität (GARCH-Term)
    α + β < 1: Stationarität (Voraussetzung)

Warum GARCH?
    - Interpretierbar: alle Parameter haben klare Bedeutung
    - Gut für Crypto: modelliert Volatility Clustering
    - Konfidenz-Intervalle: direkt aus Modell berechenbar
    - Industriestandard: für Risk Management seit 1986

Einschränkungen:
    - Normalverteilungs-Annahme für Residuen (Crypto: Fat Tails!)
    - Kein Regime-Switching (stationär)
    → Erweiterung: GJR-GARCH für asymmetrische Effekte
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import numpy as np

from sgr.core.logging import get_logger
from sgr.ml.types import ModelMetadata, ModelStatus, ModelType, VolatilityForecast

log = get_logger(__name__)


class VolatilityForecaster:
    """
    GARCH(1,1) Volatilitätsprognose.

    Usage:
        forecaster = VolatilityForecaster()
        forecaster.fit(returns)
        forecast = forecaster.predict("BTC/USDT", "1h", horizon=5)
    """

    VERSION = "1.0.0"

    def __init__(self) -> None:
        self._omega: float = 0.0
        self._alpha: float = 0.1
        self._beta: float = 0.8
        self._long_run_var: float = 0.0
        self._last_variance: float = 0.0
        self._last_residual: float = 0.0
        self._fitted = False
        self._metadata = ModelMetadata(
            model_id=str(uuid4()),
            model_type=ModelType.VOLATILITY_FORECASTER,
            version=self.VERSION,
            status=ModelStatus.UNTRAINED,
        )

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    @property
    def metadata(self) -> ModelMetadata:
        return self._metadata

    def fit(self, returns: np.ndarray) -> dict[str, float]:
        """
        Schätzt GARCH(1,1) Parameter via Maximum Likelihood.

        Args:
            returns: Array von Log-Returns (z.B. np.diff(np.log(prices)))

        Returns:
            dict mit geschätzten Parametern und Gütekriterien
        """
        if len(returns) < 30:
            raise ValueError(f"Need at least 30 returns, got {len(returns)}")

        try:
            from arch import arch_model

            am = arch_model(
                returns * 100,  # arch erwartet Prozent-Returns
                vol="Garch",
                p=1,
                q=1,
                dist="Normal",
                rescale=False,
            )
            res = am.fit(disp="off", show_warning=False)

            self._omega = float(res.params.get("omega", 0.01))
            self._alpha = float(res.params.get("alpha[1]", 0.1))
            self._beta = float(res.params.get("beta[1]", 0.8))

            # Stationaritäts-Check
            persistence = self._alpha + self._beta
            if persistence >= 1.0:
                log.warning(
                    "volatility_forecaster.non_stationary",
                    persistence=persistence,
                    note="Forcing stationarity",
                )
                self._alpha = 0.09
                self._beta = 0.89

            # Langfristige Varianz: ω / (1 - α - β)
            denom = 1 - self._alpha - self._beta
            self._long_run_var = self._omega / denom if denom > 0 else self._omega

            # Letzte bekannte Varianz und Residual
            conditional_vol = res.conditional_volatility
            self._last_variance = float(conditional_vol.iloc[-1]) ** 2 / 10000
            self._last_residual = float(returns[-1]) ** 2

            self._fitted = True
            self._metadata.status = ModelStatus.PRODUCTION
            self._metadata.trained_at = datetime.now(tz=UTC)
            self._metadata.training_samples = len(returns)

            params = {
                "omega": self._omega,
                "alpha": self._alpha,
                "beta": self._beta,
                "persistence": persistence,
                "long_run_volatility_pct": float(np.sqrt(self._long_run_var) * 100),
            }
            self._metadata.hyperparameters = params

            log.info(
                "volatility_forecaster.fitted",
                alpha=f"{self._alpha:.4f}",
                beta=f"{self._beta:.4f}",
                persistence=f"{persistence:.4f}",
                lr_vol=f"{np.sqrt(self._long_run_var) * 100:.2f}%",
            )
            return params

        except ImportError:
            log.warning(
                "volatility_forecaster.arch_not_available", note="Using simple EWMA fallback"
            )
            return self._fit_ewma(returns)

    def _fit_ewma(self, returns: np.ndarray) -> dict[str, float]:
        """
        EWMA-Fallback wenn arch nicht installiert.
        Einfacher aber weniger präzise.
        """
        # Stationaritaet (alpha + beta < 1.0) darf nicht von exakter
        # IEEE-754-Komplementaritaet zwischen alpha und beta abhaengen:
        # JEDES Paar (1-x) + x kann durch unabhaengiges Runden von
        # Subtraktion und Addition exakt 1.0 ergeben, unabhaengig vom
        # gewaehlten x (z.B. bei x=0.90: 1.0-0.90 -> 0.09999999999999998,
        # aber (1.0-0.90)+0.90 rundet wieder auf exakt 1.0). Deshalb hier
        # ein expliziter numerischer Sicherheitsabstand (margin) statt
        # reiner Komplementaer-Subtraktion.
        lambda_decay = 0.90
        stationarity_margin = 0.01
        var = float(np.var(returns))
        for r in returns:
            var = lambda_decay * var + (1 - lambda_decay) * r**2

        self._beta = lambda_decay
        self._alpha = max(1.0 - lambda_decay - stationarity_margin, 0.0)
        self._omega = var * self._alpha
        self._long_run_var = var
        self._last_variance = var
        self._last_residual = float(returns[-1]) ** 2
        self._fitted = True
        self._metadata.status = ModelStatus.PRODUCTION

        return {"alpha": self._alpha, "beta": self._beta, "fallback": "ewma"}

    def predict(
        self,
        symbol: str,
        timeframe: str,
        horizon_bars: int = 5,
        confidence_level: float = 0.95,
    ) -> VolatilityForecast:
        """
        Prognostiziert Volatilität für nächste `horizon_bars` Bars.

        Args:
            symbol: Trading-Paar
            timeframe: Bar-Länge
            horizon_bars: Wie viele Bars voraus
            confidence_level: Für Konfidenz-Intervalle (0.95 = 95% CI)

        Returns:
            VolatilityForecast mit Punkt-Schätzung und Intervallen
        """
        if not self._fitted:
            return self._fallback_forecast(symbol, timeframe, horizon_bars)

        # Multi-Step GARCH Forecast
        forecasted_variances = []
        var_t = self._last_variance
        eps_sq = self._last_residual

        for _ in range(horizon_bars):
            var_next = self._omega + self._alpha * eps_sq + self._beta * var_t
            forecasted_variances.append(var_next)
            eps_sq = var_next  # E[ε²] = σ² für zukünftige Steps
            var_t = var_next

        # H-Step Variance: kumulativ für Gesamthorizont
        horizon_var = sum(forecasted_variances)
        horizon_vol_pct = float(np.sqrt(horizon_var)) * 100

        # Konfidenz-Intervall (Normalverteilung: z * σ)
        from scipy import stats  # type: ignore[import-untyped]

        z = stats.norm.ppf((1 + confidence_level) / 2)
        lower = horizon_vol_pct * (1 - (z - 1) * 0.2)
        upper = horizon_vol_pct * (1 + (z - 1) * 0.2)

        log.debug(
            "volatility_forecaster.predicted",
            symbol=symbol,
            horizon=horizon_bars,
            vol_pct=f"{horizon_vol_pct:.3f}%",
        )

        return VolatilityForecast(
            symbol=symbol,
            timeframe=timeframe,
            horizon_bars=horizon_bars,
            predicted_volatility_pct=round(horizon_vol_pct, 4),
            lower_bound_pct=round(max(lower, 0.0), 4),
            upper_bound_pct=round(upper, 4),
            garch_alpha=self._alpha,
            garch_beta=self._beta,
            model_version=self.VERSION,
            predicted_at=datetime.now(tz=UTC),
        )

    def update(self, new_return: float) -> None:
        """
        Online-Update nach jedem neuen Return.
        Hält Modell aktuell ohne volles Retraining.
        """
        if not self._fitted:
            return
        self._last_residual = new_return**2
        self._last_variance = (
            self._omega + self._alpha * self._last_residual + self._beta * self._last_variance
        )

    def _fallback_forecast(
        self,
        symbol: str,
        timeframe: str,
        horizon_bars: int,
    ) -> VolatilityForecast:
        """Konservative Fallback-Schätzung wenn nicht fitted."""
        return VolatilityForecast(
            symbol=symbol,
            timeframe=timeframe,
            horizon_bars=horizon_bars,
            predicted_volatility_pct=2.0,  # Konservative Schätzung
            lower_bound_pct=1.0,
            upper_bound_pct=4.0,
            garch_alpha=0.1,
            garch_beta=0.8,
            model_version="fallback",
            predicted_at=datetime.now(tz=UTC),
        )
