"""
Validierung von monitoring/grafana/dashboards/sgr-trading.json.

Hintergrund (Schritt 6): das Dashboard-JSON ist reine Konfiguration ohne
Python-Import-Pfad, wird also von keinem der üblichen Testläufe automatisch
erfasst. Diese Tests laufen als normale pytest-Tests, damit ein zukünftiger
struktureller Fehler (kaputtes JSON, überlappende Panels, eine Query gegen
eine Metrik, die im Code gar nicht mehr existiert) beim regulären
`pytest`-Lauf auffällt statt erst beim manuellen Öffnen in Grafana.

Deckt NICHT ab: ob die Panels in Grafana tatsächlich korrekt rendern (das
wäre ein E2E-Test gegen eine laufende Grafana-Instanz, siehe RUNBOOK für
die manuelle/curl-basierte Serververifikation) - nur die statische
JSON-Struktur und die Konsistenz zwischen referenzierten Metriknamen und
tatsächlich im Code definierten Metriken.
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
    / "sgr-trading.json"
)

METRICS_PY_PATH = (
    Path(__file__).resolve().parents[2] / "sgr" / "monitoring" / "metrics.py"
)

# OTel-Metriknamen (sgr.strategy.backtest_sharpe_ratio) werden von
# PrometheusMetricReader zu Prometheus-Namen mit Unterstrichen und
# "sgr_"-Präfix konvertiert (sgr_strategy_backtest_sharpe_ratio) - siehe
# tatsächlich beobachtete /metrics-Ausgabe auf dem Server.
_OTEL_NAME_RE = re.compile(r'"(sgr\.[a-z_.]+)"')
_PROMQL_METRIC_RE = re.compile(r"\b(sgr_[a-z_]+)\b")


def _otel_to_prometheus_name(otel_name: str) -> str:
    return otel_name.replace(".", "_")


@pytest.fixture(scope="module")
def dashboard() -> dict:
    with open(DASHBOARD_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def known_metric_names() -> set[str]:
    """Alle in sgr/monitoring/metrics.py per gauge()/counter() definierten
    Metriknamen, umgewandelt ins Prometheus-Textformat-Schema."""
    content = METRICS_PY_PATH.read_text()
    otel_names = _OTEL_NAME_RE.findall(content)
    return {_otel_to_prometheus_name(n) for n in otel_names}


class TestDashboardJsonStructure:
    def test_dashboard_file_exists(self) -> None:
        assert DASHBOARD_PATH.exists()

    def test_dashboard_is_valid_json(self, dashboard: dict) -> None:
        assert isinstance(dashboard, dict)

    def test_dashboard_has_title_and_uid(self, dashboard: dict) -> None:
        assert dashboard["title"]
        assert dashboard["uid"]

    def test_dashboard_has_panels(self, dashboard: dict) -> None:
        assert len(dashboard["panels"]) > 0

    def test_panel_ids_are_unique(self, dashboard: dict) -> None:
        ids = [p["id"] for p in dashboard["panels"]]
        assert len(ids) == len(set(ids)), f"Duplicate panel ids: {ids}"

    def test_every_panel_has_a_title(self, dashboard: dict) -> None:
        for p in dashboard["panels"]:
            assert p.get("title"), f"Panel {p.get('id')} has no title"

    def test_every_panel_has_at_least_one_target(self, dashboard: dict) -> None:
        for p in dashboard["panels"]:
            assert p.get("targets"), f"Panel {p['title']!r} has no targets"

    def test_no_panels_overlap_in_grid(self, dashboard: dict) -> None:
        """Zwei Panels dürfen sich in Grafanas 24-Spalten-Grid nicht
        überlappen (x/y/w/h) - ein per-JSON-Layout-Fehler, der visuell erst
        beim Öffnen des Dashboards auffallen würde."""
        panels = dashboard["panels"]
        overlaps = []
        for i, p1 in enumerate(panels):
            for p2 in panels[i + 1 :]:
                g1, g2 = p1["gridPos"], p2["gridPos"]
                y_overlap = g1["y"] < g2["y"] + g2["h"] and g2["y"] < g1["y"] + g1["h"]
                x_overlap = g1["x"] < g2["x"] + g2["w"] and g2["x"] < g1["x"] + g1["w"]
                if y_overlap and x_overlap:
                    overlaps.append((p1["title"], p2["title"]))
        assert overlaps == [], f"Overlapping panels: {overlaps}"

    def test_no_panel_exceeds_grid_width(self, dashboard: dict) -> None:
        """Grafana-Grid ist 24 Spalten breit - x + w darf das nie
        überschreiten."""
        for p in dashboard["panels"]:
            g = p["gridPos"]
            assert g["x"] + g["w"] <= 24, f"Panel {p['title']!r} exceeds grid width"


class TestDashboardMetricReferencesExist:
    """
    Prüft, dass jede sgr_*-Metrik, die eine Panel-Query referenziert, auch
    tatsächlich in sgr/monitoring/metrics.py definiert ist. Verhindert
    stille Drift: ein zukünftiges Umbenennen/Entfernen einer Metrik im Code
    würde sonst erst beim Öffnen des Dashboards als leeres Panel auffallen.

    Deckt bewusst nur sgr.*-Metriken (metrics.py/SGRMetrics) ab, nicht die
    raw-prometheus_client-Metriken aus trading_metrics.py (andere
    Namenskonvention, aktuell nicht vom Dashboard referenziert).
    """

    def test_all_referenced_metrics_exist_in_code(
        self, dashboard: dict, known_metric_names: set[str]
    ) -> None:
        referenced = set()
        for p in dashboard["panels"]:
            for t in p.get("targets", []):
                expr = t.get("expr", "")
                referenced.update(_PROMQL_METRIC_RE.findall(expr))

        # Nur die sgr_strategy_*/sgr_portfolio_*/sgr_risk_*/sgr_trades_*
        # OTel-Metriken pruefen - sgr_trades_winning/losing sind Counter
        # (kein .total-Suffix im Namen, siehe metrics.py: "sgr.trades.winning").
        unknown = referenced - known_metric_names
        assert unknown == set(), (
            f"Dashboard references metrics not defined in metrics.py: {unknown}. "
            f"Known: {sorted(known_metric_names)}"
        )

    def test_known_metric_names_is_non_empty(self, known_metric_names: set[str]) -> None:
        """Sanity-Check fuer die Fixture selbst - waere leer, wuerde der
        vorige Test triviell (immer) durchfallen oder (schlimmer) triviell
        durchlaufen, falls known_metric_names leer UND referenced leer ist."""
        assert len(known_metric_names) > 10

    def test_strategy_validation_metrics_from_step6_are_referenced(
        self, dashboard: dict
    ) -> None:
        """Die in Schritt 6 neu hinzugefügten Gauges (active_count,
        validation_status, backtest_sharpe_ratio, ...) müssen tatsächlich
        von mindestens einem Panel verwendet werden - sonst wäre der ganze
        Dashboard-Update-Zweck verfehlt."""
        all_exprs = " ".join(
            t.get("expr", "")
            for p in dashboard["panels"]
            for t in p.get("targets", [])
        )
        for metric in (
            "sgr_strategy_active_count",
            "sgr_strategy_validation_status",
            "sgr_strategy_backtest_sharpe_ratio",
            "sgr_strategy_backtest_total_return_pct",
            "sgr_strategy_backtest_max_drawdown_pct",
            "sgr_strategy_backtest_total_trades",
        ):
            assert metric in all_exprs, f"{metric} is not referenced by any panel"


class TestDashboardTenantSeparation:
    """
    Schritt 6 (Server-verifizierter Bug + Fix, siehe vorheriger Commit):
    beide Tenants (Gordon, Sumo) müssen im Dashboard sauber getrennt
    sichtbar sein, nicht als eine kollabierte Zeitreihe.
    """

    GORDON_UUID = "a47d994d-35cc-4619-83bb-86fd0cb48447"
    SUMO_UUID = "144f7bb0-113d-4c66-b5d3-7a34ab1d551a"

    def test_portfolio_value_panel_has_separate_gordon_and_sumo_targets(
        self, dashboard: dict
    ) -> None:
        panel = next(p for p in dashboard["panels"] if p["title"] == "Portfolio Value Over Time")
        exprs = [t["expr"] for t in panel["targets"]]
        assert any(self.GORDON_UUID in e for e in exprs)
        assert any(self.SUMO_UUID in e for e in exprs)

    def test_active_strategies_are_split_per_tenant(self, dashboard: dict) -> None:
        titles = {p["title"] for p in dashboard["panels"]}
        assert "Active Strategies - Gordon" in titles
        assert "Active Strategies - Sumo" in titles

    def test_no_panel_mixes_both_tenant_uuids_in_a_single_target_expr(
        self, dashboard: dict
    ) -> None:
        """Jede einzelne Query darf höchstens einen Tenant filtern - sonst
        würde die Query wieder beide Zeitreihen kollabieren, statt sie
        als separate, benannte Linien darzustellen."""
        for p in dashboard["panels"]:
            for t in p.get("targets", []):
                expr = t.get("expr", "")
                has_gordon = self.GORDON_UUID in expr
                has_sumo = self.SUMO_UUID in expr
                assert not (has_gordon and has_sumo), (
                    f"Target {t.get('refId')} in panel {p['title']!r} mixes both "
                    f"tenant UUIDs in one query: {expr!r}"
                )

    def test_tenant_split_targets_have_distinguishing_legend_names(
        self, dashboard: dict
    ) -> None:
        """Für Panels, die BEIDE Tenants gemeinsam zeigen (z.B. zwei Linien
        in einem Zeitreihen-Panel), muss legendFormat erkennbar machen,
        welche Linie zu welchem Tenant gehört - sonst zeigt die Legende
        nur die rohe UUID. Panels, die pro Tenant bereits vollständig
        getrennt sind (z.B. 'Active Strategies - Gordon' als eigenes
        Panel), tragen die Unterscheidung schon im Panel-Titel und sind
        hier ausgenommen."""
        for p in dashboard["panels"]:
            targets = p.get("targets", [])
            tenants_in_panel = {
                self.GORDON_UUID if self.GORDON_UUID in t.get("expr", "") else None
                for t in targets
            } | {
                self.SUMO_UUID if self.SUMO_UUID in t.get("expr", "") else None
                for t in targets
            }
            tenants_in_panel.discard(None)
            if len(tenants_in_panel) < 2:
                continue  # Panel zeigt nur einen (oder keinen) Tenant - kein Mix zu klären

            for t in targets:
                expr = t.get("expr", "")
                legend = t.get("legendFormat", "")
                if self.GORDON_UUID in expr:
                    assert "Gordon" in legend, (
                        f"Gordon-filtered target in {p['title']!r} (multi-tenant panel) "
                        f"has no 'Gordon' in legendFormat: {legend!r}"
                    )
                if self.SUMO_UUID in expr:
                    assert "Sumo" in legend, (
                        f"Sumo-filtered target in {p['title']!r} (multi-tenant panel) "
                        f"has no 'Sumo' in legendFormat: {legend!r}"
                    )
