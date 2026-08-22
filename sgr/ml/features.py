"""
SGR ML Feature Extractor
=========================
Konvertiert FeatureSet → ML-ready Feature-Vektoren.

Verantwortlichkeiten:
    - Feature-Selektion (welche Indikatoren für welches Modell)
    - Normalisierung (Z-Score, Min-Max)
    - Missing-Value-Handling (Imputation mit Median)
    - Feature-Namen für SHAP-Explainability

Design:
    Jedes Modell bekommt eine eigene Feature-Gruppe.
    Regime Detector: technische Indikatoren + Marktstruktur
    Volatility: ATR, BB-Width, Return-Varianz
    Strategy Selector: alle Features + Regime + Performance-History

Temporal Split:
    Beim Training IMMER zeitlich splitten (nicht zufällig).
    Zufälliger Split = Look-Ahead Bias bei Zeitreihen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from sgr.core.logging import get_logger
from sgr.market_data.types import FeatureSet

log = get_logger(__name__)


@dataclass
class FeatureMatrix:
    """ML-ready Feature Matrix mit Namen für Explainability."""

    X: np.ndarray  # Shape: (n_samples, n_features)
    feature_names: list[str]  # Länge: n_features
    timestamps: list[Any]  # Länge: n_samples

    def __post_init__(self) -> None:
        assert self.X.shape[1] == len(self.feature_names), (
            f"Feature count mismatch: {self.X.shape[1]} != {len(self.feature_names)}"
        )


# Feature-Gruppen pro Modell
REGIME_FEATURES = [
    "rsi_14",
    "rsi_7",
    "macd_histogram",
    "macd_line",
    "adx_14",
    "di_plus",
    "di_minus",
    "atr_pct",
    "bb_position",
    "bb_width",
    "volume_ratio",
    "returns_1",
    "returns_5",
    "returns_10",
    "returns_20",
    "obv",
]

VOLATILITY_FEATURES = [
    "atr_pct",
    "bb_width",
    "returns_1",
    "returns_5",
    "returns_10",
    "volume_ratio",
    "adx_14",
]

STRATEGY_SELECTOR_FEATURES = REGIME_FEATURES + [
    "order_imbalance_5",
    "order_imbalance_10",
    "bid_ask_spread_pct",
]


class FeatureExtractor:
    """
    Extrahiert Feature-Vektoren aus FeatureSet-Listen.
    Stateful: fitted Normalisierungs-Parameter werden gespeichert.
    """

    def __init__(self, feature_names: list[str]) -> None:
        self._feature_names = feature_names
        self._means: np.ndarray | None = None
        self._stds: np.ndarray | None = None
        self._medians: np.ndarray | None = None
        self._fitted = False

    @property
    def feature_names(self) -> list[str]:
        return self._feature_names

    def extract_single(self, features: FeatureSet) -> np.ndarray:
        """Extrahiert Feature-Vektor aus einem FeatureSet."""
        row = self._extract_row(features)
        return np.array(row, dtype=np.float64)

    def fit_transform(self, feature_sets: list[FeatureSet]) -> FeatureMatrix:
        """
        Fit Normalisierung auf Trainings-Daten und transformiert.
        Nur auf Trainings-Split aufrufen (nie auf Test-Split).
        """
        X_raw = self._build_matrix(feature_sets)

        # Imputation: NaN → Spalten-Median
        self._medians = np.nanmedian(X_raw, axis=0)
        for j in range(X_raw.shape[1]):
            mask = np.isnan(X_raw[:, j])
            X_raw[mask, j] = self._medians[j]

        # Z-Score Normalisierung
        self._means = np.mean(X_raw, axis=0)
        self._stds = np.std(X_raw, axis=0)
        self._stds[self._stds < 1e-8] = 1.0  # Division by zero vermeiden (float tolerance)

        X_norm = (X_raw - self._means) / self._stds
        self._fitted = True

        return FeatureMatrix(
            X=X_norm,
            feature_names=self._feature_names,
            timestamps=[fs.timestamp for fs in feature_sets],
        )

    def transform(self, feature_sets: list[FeatureSet]) -> FeatureMatrix:
        """Transformiert neue Daten mit gespeicherter Normalisierung."""
        if not self._fitted:
            raise RuntimeError("FeatureExtractor not fitted. Call fit_transform first.")

        X_raw = self._build_matrix(feature_sets)

        # Imputation mit Trainings-Medians
        for j in range(X_raw.shape[1]):
            mask = np.isnan(X_raw[:, j])
            X_raw[mask, j] = self._medians[j]  # type: ignore[index]

        X_norm = (X_raw - self._means) / self._stds  # type: ignore[operator]

        return FeatureMatrix(
            X=X_norm,
            feature_names=self._feature_names,
            timestamps=[fs.timestamp for fs in feature_sets],
        )

    def transform_single(self, features: FeatureSet) -> np.ndarray:
        """Transformiert einzelnes FeatureSet (für Live-Inference)."""
        matrix = self.transform([features])
        return matrix.X[0]

    def _build_matrix(self, feature_sets: list[FeatureSet]) -> np.ndarray:
        rows = [self._extract_row(fs) for fs in feature_sets]
        return np.array(rows, dtype=np.float64)

    def _extract_row(self, fs: FeatureSet) -> list[float]:
        """Extrahiert einen Feature-Vektor aus einem FeatureSet."""
        ind = fs.indicators
        ob = fs.orderbook

        def _get(name: str) -> float:
            # Indikatoren
            val = getattr(ind, name, None)
            if val is not None:
                return float(val)

            # Returns
            if name == "returns_1":
                return float(fs.returns_1) if fs.returns_1 is not None else float("nan")
            if name == "returns_5":
                return float(fs.returns_5) if fs.returns_5 is not None else float("nan")
            if name == "returns_10":
                return float(fs.returns_10) if fs.returns_10 is not None else float("nan")
            if name == "returns_20":
                return float(fs.returns_20) if fs.returns_20 is not None else float("nan")

            # Orderbook Features
            if ob is not None:
                if name == "order_imbalance_5":
                    return float(ob.order_imbalance_5)
                if name == "order_imbalance_10":
                    return float(ob.order_imbalance_10)
                if name == "bid_ask_spread_pct":
                    return float(ob.bid_ask_spread_pct)

            return float("nan")

        return [_get(name) for name in self._feature_names]

    def get_params(self) -> dict:
        """Serialisiert Normalisierungs-Parameter für Persistence."""
        if not self._fitted:
            return {}
        return {
            "feature_names": self._feature_names,
            "means": self._means.tolist(),  # type: ignore[union-attr]
            "stds": self._stds.tolist(),  # type: ignore[union-attr]
            "medians": self._medians.tolist(),  # type: ignore[union-attr]
        }

    @classmethod
    def from_params(cls, params: dict) -> FeatureExtractor:
        """Stellt FeatureExtractor aus gespeicherten Parametern wieder her."""
        extractor = cls(params["feature_names"])
        extractor._means = np.array(params["means"])
        extractor._stds = np.array(params["stds"])
        extractor._medians = np.array(params["medians"])
        extractor._fitted = True
        return extractor
