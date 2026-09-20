"""
Validierung von monitoring/grafana/dashboards/sgr-futures-grid.json - siehe
tests/unit/test_grafana_dashboard.py Modul-Docstring fuer die Begruendung
(kaputtes JSON/ueberlappende Panels/verwaiste Metriknamen sollen im
regulaeren pytest-Lauf auffallen, nicht erst beim manuellen Oeffnen in
Grafana). Wiederverwendet dieselbe Namens-Uebersetzungslogik
(OTel-Instrumentname -> tatsaechlich exportierter Prometheus-Name).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

DASHBOARD_PATH = (
    Path(__file__).resolve().parents[2]
    / "monitoring"
    / "grafana"
    / "dashboards"
    / "sgr-futures-grid.json"
)
METRICS_PY_PATH = Path(__file__).resolve().parents[2] / "sgr" / "monitoring" / "metrics.py"

_OTEL_INSTRUMENT_RE = re.compile(r'(gauge|counter)\(\s*"(sgr\.[a-z_.]+)"')
_PROMQL_METRIC_RE = re.compile(r"\b(sgr_[a-z_]+)\b")


def _known_metric_names() -> set[str]:
    content = METRICS_PY_PATH.read_text()
    names = set()
    for kind, otel_name in _OTEL_INSTRUMENT_RE.findall(content):
        prom_name = otel_name.replace(".", "_")
        if kind == "counter":
            prom_name += "_total"
        names.add(prom_name)
    return names


@pytest.fixture(scope="module")
def dashboard() -> dict:
    with open(DASHBOARD_PATH) as f:
        return json.load(f)


def _content_panels(dashboard: dict) -> list[dict]:
    return [p for p in dashboard["panels"] if p.get("type") != "row"]


def _all_exprs(dashboard: dict) -> list[str]:
    return [t.get("expr", "") for p in _content_panels(dashboard) for t in p.get("targets", [])]


class TestFuturesGridDashboardStructure:
    def test_dashboard_file_exists(self) -> None:
        assert DASHBOARD_PATH.exists()

    def test_dashboard_is_valid_json(self, dashboard: dict) -> None:
        assert isinstance(dashboard, dict)

    def test_has_title_and_uid(self, dashboard: dict) -> None:
        assert dashboard["title"]
        assert dashboard["uid"]

    def test_panel_ids_are_unique(self, dashboard: dict) -> None:
        ids = [p["id"] for p in dashboard["panels"]]
        assert len(ids) == len(set(ids))

    def test_every_content_panel_has_title_and_target(self, dashboard: dict) -> None:
        for p in _content_panels(dashboard):
            assert p.get("title")
            assert p.get("targets")

    def test_no_panels_overlap_in_grid(self, dashboard: dict) -> None:
        panels = dashboard["panels"]
        overlaps = []
        for i, p1 in enumerate(panels):
            for p2 in panels[i + 1 :]:
                g1, g2 = p1["gridPos"], p2["gridPos"]
                y_overlap = g1["y"] < g2["y"] + g2["h"] and g2["y"] < g1["y"] + g1["h"]
                x_overlap = g1["x"] < g2["x"] + g2["w"] and g2["x"] < g1["x"] + g1["w"]
                if y_overlap and x_overlap:
                    overlaps.append((p1["title"], p2["title"]))
        assert overlaps == []

    def test_no_panel_exceeds_grid_width(self, dashboard: dict) -> None:
        for p in dashboard["panels"]:
            g = p["gridPos"]
            assert g["x"] + g["w"] <= 24

    def test_no_duplicate_panel_titles(self, dashboard: dict) -> None:
        titles = [p["title"] for p in _content_panels(dashboard)]
        assert len(titles) == len(set(titles))


class TestFuturesGridDashboardMetricsExist:
    def test_all_referenced_metrics_are_defined_in_code(self, dashboard: dict) -> None:
        known = _known_metric_names()
        referenced = set()
        for expr in _all_exprs(dashboard):
            referenced.update(_PROMQL_METRIC_RE.findall(expr))

        unknown = referenced - known
        assert unknown == set(), f"Dashboard references undefined metrics: {unknown}"

    def test_dashboard_uses_exchange_product_strategy_tenant_variables(
        self, dashboard: dict
    ) -> None:
        var_names = {v["name"] for v in dashboard["templating"]["list"]}
        assert {"exchange", "symbol", "strategy", "tenant"} <= var_names
