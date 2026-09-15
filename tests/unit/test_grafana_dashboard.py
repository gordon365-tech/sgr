"""
Validierung von monitoring/grafana/dashboards/sgr-trading.json.

Hintergrund (Grafana-Observability-Audit): das Dashboard-JSON ist reine
Konfiguration ohne Python-Import-Pfad, wird also von keinem der üblichen
Testläufe automatisch erfasst. Diese Tests laufen als normale pytest-Tests,
damit ein zukünftiger struktureller Fehler (kaputtes JSON, überlappende
Panels, eine Query gegen eine Metrik, die im Code gar nicht mehr existiert)
beim regulären `pytest`-Lauf auffällt statt erst beim manuellen Öffnen in
Grafana.

Deckt NICHT ab: ob die Panels in Grafana tatsächlich korrekt rendern (das
wäre ein E2E-Test gegen eine laufende Grafana-Instanz) - nur die statische
JSON-Struktur und die Konsistenz zwischen referenzierten Metriknamen und
tatsächlich im Code definierten Metriken.

Root-Cause-Fund beim Vorgänger dieser Datei: die alte `known_metric_names`-
Fixture wandelte OTel-Instrumentnamen ("sgr.portfolio.value_usd") NAIV per
Punkt->Unterstrich in einen Prometheus-Namen um, ohne zu beruecksichtigen,
dass der PrometheusMetricReader zusaetzlich IMMER den `unit`-Wert als
Namens-Suffix anhaengt (empirisch am exportierten /metrics-Text der
sgr-api verifiziert) und an jeden Counter automatisch "_total" anhaengt.
Das alte Dashboard-JSON referenzierte deshalb Namen wie
"sgr_portfolio_value_usd", waehrend der tatsaechlich exportierte Name
"sgr_portfolio_value_usd_USD" lautete - der Test selbst haette diese
Divergenz nie auffangen koennen, weil er dieselbe falsche Annahme codierte
wie das Dashboard. sgr/monitoring/metrics.py wurde daraufhin so
korrigiert, dass kein Instrument mehr ein redundantes `unit=` traegt (die
Einheit steht bereits im letzten Namensteil) - diese Datei bildet die
tatsaechliche PrometheusMetricReader-Namenslogik (Punkt->Unterstrich +
"_total" fuer Counter) jetzt explizit nach, statt sie zu ignorieren.
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

METRICS_PY_PATH = Path(__file__).resolve().parents[2] / "sgr" / "monitoring" / "metrics.py"
TRADING_METRICS_PY_PATH = (
    Path(__file__).resolve().parents[2] / "sgr" / "monitoring" / "trading_metrics.py"
)

GORDON_UUID = "a47d994d-35cc-4619-83bb-86fd0cb48447"
SUMO_UUID = "144f7bb0-113d-4c66-b5d3-7a34ab1d551a"

# OTel gauge()/counter() Aufrufe in metrics.py: "sgr.foo.bar" als erstes Argument.
_OTEL_INSTRUMENT_RE = re.compile(r'(gauge|counter)\(\s*"(sgr\.[a-z_.]+)"')
# Rohe prometheus_client-Metriken in trading_metrics.py: Counter(/Gauge(/
# Histogram(/Info( mit "sgr_foo_bar" als erstes Argument.
_RAW_INSTRUMENT_RE = re.compile(r'(Counter|Gauge|Histogram|Info)\(\s*\n?\s*"(sgr_[a-z_]+)"')
_PROMQL_METRIC_RE = re.compile(r"\b(sgr_[a-z_]+)\b")


def _otel_to_prometheus_names(content: str) -> set[str]:
    """Wendet die tatsaechliche PrometheusMetricReader-Namenslogik an:
    Punkt->Unterstrich, und fuer Counter zusaetzlich "_total" (empirisch
    verifiziert - siehe Moduldocstring)."""
    names = set()
    for kind, otel_name in _OTEL_INSTRUMENT_RE.findall(content):
        prom_name = otel_name.replace(".", "_")
        if kind == "counter":
            prom_name += "_total"
        names.add(prom_name)
    return names


def _raw_prometheus_names(content: str) -> set[str]:
    return {name for _kind, name in _RAW_INSTRUMENT_RE.findall(content)}


@pytest.fixture(scope="module")
def dashboard() -> dict:
    with open(DASHBOARD_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def known_metric_names() -> set[str]:
    """Alle Metriknamen, die tatsaechlich im Code definiert werden (OTel via
    metrics.py + rohe prometheus_client-Metriken via trading_metrics.py),
    im tatsaechlich exportierten Prometheus-Textformat-Schema."""
    otel_names = _otel_to_prometheus_names(METRICS_PY_PATH.read_text())
    raw_names = _raw_prometheus_names(TRADING_METRICS_PY_PATH.read_text())
    return otel_names | raw_names


def _all_panels(dashboard: dict) -> list[dict]:
    """Content-Panels (ohne 'row'-Marker-Panels, die selbst keine Targets
    tragen)."""
    return [p for p in dashboard["panels"] if p.get("type") != "row"]


def _all_exprs(dashboard: dict) -> list[str]:
    return [t.get("expr", "") for p in _all_panels(dashboard) for t in p.get("targets", [])]


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

    def test_every_content_panel_has_at_least_one_target(self, dashboard: dict) -> None:
        for p in _all_panels(dashboard):
            assert p.get("targets"), f"Panel {p['title']!r} has no targets"

    def test_no_panels_overlap_in_grid(self, dashboard: dict) -> None:
        """Zwei Panels dürfen sich in Grafanas 24-Spalten-Grid nicht
        überlappen (x/y/w/h) - ein Layout-Fehler, der visuell erst beim
        Öffnen des Dashboards auffallen würde."""
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


class TestDashboardIsFocusedNotBloated:
    """Anforderung: EIN uebersichtliches Dashboard, keine 30-40 kleinen
    Panels (siehe Task-Vorgabe 'Grafana Design')."""

    def test_content_panel_count_is_reasonable(self, dashboard: dict) -> None:
        content_panels = _all_panels(dashboard)
        assert 0 < len(content_panels) <= 20, (
            f"Expected a focused dashboard (<=20 content panels), got {len(content_panels)}"
        )

    def test_no_duplicate_panel_titles(self, dashboard: dict) -> None:
        titles = [p["title"] for p in _all_panels(dashboard)]
        assert len(titles) == len(set(titles)), f"Duplicate panel titles: {titles}"


class TestDashboardRequiredSections:
    """Die vier fachlich geforderten Bereiche (System Health, Portfolio,
    Risk, Asset/Position) muessen als erkennbare Abschnitte vorhanden
    sein."""

    def _row_titles(self, dashboard: dict) -> list[str]:
        return [p["title"] for p in dashboard["panels"] if p.get("type") == "row"]

    def test_system_health_section_exists(self, dashboard: dict) -> None:
        assert any("System Health" in t for t in self._row_titles(dashboard))

    def test_portfolio_section_exists(self, dashboard: dict) -> None:
        assert any("Portfolio" in t for t in self._row_titles(dashboard))

    def test_risk_section_exists(self, dashboard: dict) -> None:
        assert any("Risk" in t for t in self._row_titles(dashboard))

    def test_asset_position_section_exists(self, dashboard: dict) -> None:
        assert any("Asset" in t or "Position" in t for t in self._row_titles(dashboard))

    def test_system_health_section_is_the_first_row(self, dashboard: dict) -> None:
        """Kill Switch & Co. muessen 'sofort erkennbar' sein - der System-
        Health-Bereich muss daher oben stehen, nicht irgendwo im Dashboard."""
        rows = [p for p in dashboard["panels"] if p.get("type") == "row"]
        rows_sorted = sorted(rows, key=lambda p: p["gridPos"]["y"])
        assert "System Health" in rows_sorted[0]["title"]


class TestKillSwitchIsProminent:
    def test_kill_switch_panel_exists(self, dashboard: dict) -> None:
        titles = {p["title"] for p in dashboard["panels"]}
        assert any("Kill Switch" in t for t in titles)

    def test_kill_switch_panel_is_near_the_top(self, dashboard: dict) -> None:
        panel = next(p for p in dashboard["panels"] if "Kill Switch" in p["title"])
        assert panel["gridPos"]["y"] <= 2

    def test_kill_switch_query_targets_both_tenants(self, dashboard: dict) -> None:
        panel = next(p for p in dashboard["panels"] if "Kill Switch" in p["title"])
        exprs = " ".join(t.get("expr", "") for t in panel["targets"])
        assert GORDON_UUID in exprs
        assert SUMO_UUID in exprs

    def test_kill_switch_uses_the_real_metric_name(self, dashboard: dict) -> None:
        panel = next(p for p in dashboard["panels"] if "Kill Switch" in p["title"])
        exprs = " ".join(t.get("expr", "") for t in panel["targets"])
        assert "sgr_kill_switch_active" in exprs


class TestDashboardMetricReferencesExist:
    """
    Prüft, dass jede sgr_*-Metrik, die eine Panel-Query referenziert, auch
    tatsächlich im Code definiert ist (metrics.py ODER trading_metrics.py) -
    unter dem Namen, der tatsaechlich am /metrics-Endpoint exportiert wird.
    Verhindert stille Drift: ein zukünftiges Umbenennen/Entfernen einer
    Metrik im Code würde sonst erst beim Öffnen des Dashboards als leeres
    Panel auffallen.
    """

    def test_all_referenced_metrics_exist_in_code(
        self, dashboard: dict, known_metric_names: set[str]
    ) -> None:
        referenced: set[str] = set()
        for expr in _all_exprs(dashboard):
            referenced.update(_PROMQL_METRIC_RE.findall(expr))

        unknown = referenced - known_metric_names
        assert unknown == set(), (
            f"Dashboard references metrics not defined in code: {unknown}. "
            f"Known: {sorted(known_metric_names)}"
        )

    def test_known_metric_names_is_non_empty(self, known_metric_names: set[str]) -> None:
        """Sanity-Check fuer die Fixture selbst - waere leer, wuerde der
        vorige Test trivial (immer) durchfallen oder (schlimmer) trivial
        durchlaufen, falls known_metric_names leer UND referenced leer
        ist."""
        assert len(known_metric_names) > 10

    def test_no_panel_uses_the_double_suffixed_broken_names(self, dashboard: dict) -> None:
        """Regressionsschutz fuer genau den Bug, der dieses Audit ausgeloest
        hat: sgr_portfolio_value_usd_usd, sgr_risk_max_drawdown_pct_percent
        etc. duerfen NIE wieder in einer Panel-Query auftauchen."""
        broken_suffixes = ("_usd_usd", "_usd_USD", "_pct_percent", "_count_count")
        for expr in _all_exprs(dashboard):
            for suffix in broken_suffixes:
                assert suffix not in expr, f"Broken double-suffix {suffix!r} in query: {expr!r}"


class TestDashboardNoFakeOrDeadMetrics:
    """Anforderung: nur Metriken darstellen, die tatsaechlich beschrieben
    werden - keine bekanntermassen toten/nie inkrementierten Metriken."""

    # Definiert in trading_metrics.py, aber nirgends im Code inkrementiert/
    # gesetzt (siehe Audit-Report) - duerfen nicht im Dashboard landen.
    DEAD_METRICS = (
        "sgr_portfolio_drawdown",
        "sgr_portfolio_heat",
        "sgr_active_positions_count",
        "sgr_risk_checks_total",
        "sgr_risk_reduced_total",
        "sgr_reconciliation_runs_total",
        "sgr_reconciliation_failures_total",
        "sgr_reconciliation_discrepancies_found",
        "sgr_execution_latency_seconds",
        "sgr_exchange_latency_seconds",
        "sgr_exchange_timeout_total",
        "sgr_build_info",
    )

    def test_no_dead_metric_is_referenced(self, dashboard: dict) -> None:
        referenced: set[str] = set()
        for expr in _all_exprs(dashboard):
            referenced.update(_PROMQL_METRIC_RE.findall(expr))
        dead_used = referenced & set(self.DEAD_METRICS)
        assert dead_used == set(), f"Dashboard references dead/never-set metrics: {dead_used}"


class TestDashboardTenantSeparation:
    """
    Beide Tenants (Gordon, Sumo) müssen im Dashboard sauber getrennt
    sichtbar sein, nicht als eine kollabierte Zeitreihe.
    """

    def test_no_panel_mixes_both_tenant_uuids_in_a_single_target_expr(
        self, dashboard: dict
    ) -> None:
        """Jede einzelne Query darf höchstens einen Tenant filtern - sonst
        würde die Query wieder beide Zeitreihen kollabieren, statt sie als
        separate, benannte Linien darzustellen."""
        for p in _all_panels(dashboard):
            for t in p.get("targets", []):
                expr = t.get("expr", "")
                has_gordon = GORDON_UUID in expr
                has_sumo = SUMO_UUID in expr
                assert not (has_gordon and has_sumo), (
                    f"Target {t.get('refId')} in panel {p['title']!r} mixes both "
                    f"tenant UUIDs in one query: {expr!r}"
                )

    def test_tenant_split_targets_have_distinguishing_legend_names(self, dashboard: dict) -> None:
        """Für Panels, die BEIDE Tenants gemeinsam zeigen (zwei Linien/Tiles
        in einem Panel), muss legendFormat erkennbar machen, welche Linie
        zu welchem Tenant gehört."""
        for p in _all_panels(dashboard):
            targets = p.get("targets", [])
            tenants_in_panel = {
                GORDON_UUID if GORDON_UUID in t.get("expr", "") else None for t in targets
            } | {SUMO_UUID if SUMO_UUID in t.get("expr", "") else None for t in targets}
            tenants_in_panel.discard(None)
            if len(tenants_in_panel) < 2:
                continue

            for t in targets:
                expr = t.get("expr", "")
                legend = t.get("legendFormat", "")
                if GORDON_UUID in expr:
                    assert "Gordon" in legend, (
                        f"Gordon-filtered target in {p['title']!r} has no 'Gordon' in "
                        f"legendFormat: {legend!r}"
                    )
                if SUMO_UUID in expr:
                    assert "Sumo" in legend, (
                        f"Sumo-filtered target in {p['title']!r} has no 'Sumo' in "
                        f"legendFormat: {legend!r}"
                    )

    def test_position_table_is_tenant_aware_via_label_not_hardcoded_split(
        self, dashboard: dict
    ) -> None:
        """Die Asset/Position-Tabelle zeigt beide Tenants ueber das
        'tenant'-Label (dynamisch, per Variable filterbar) statt ueber zwei
        hartcodierte Panels - siehe Task-Vorgabe 'keine manuelle Pflege pro
        Asset/Tenant'."""
        panel = next(p for p in dashboard["panels"] if p["title"] == "Position Breakdown")
        exprs = " ".join(t.get("expr", "") for t in panel["targets"])
        assert "tenant" in exprs
        assert GORDON_UUID not in exprs
        assert SUMO_UUID not in exprs


class TestDashboardDynamicAssets:
    """
    Anforderung: Assets duerfen NICHT auf BTCUSDT/ETHUSDT hartcodiert sein -
    neue Symbole muessen automatisch erscheinen, ueber Prometheus-Label-
    Queries bzw. Grafana-Variablen.
    """

    HARDCODED_SYMBOL_PATTERNS = ("BTCUSDT", "ETHUSDT", "BTC/USDT", "ETH/USDT")

    def test_no_query_hardcodes_a_specific_trading_symbol(self, dashboard: dict) -> None:
        for expr in _all_exprs(dashboard):
            for pattern in self.HARDCODED_SYMBOL_PATTERNS:
                assert pattern not in expr, f"Hardcoded symbol {pattern!r} in query: {expr!r}"

    def test_symbol_template_variable_exists_and_is_query_driven(self, dashboard: dict) -> None:
        variables = dashboard["templating"]["list"]
        symbol_var = next((v for v in variables if v["name"] == "symbol"), None)
        assert symbol_var is not None, "No 'symbol' template variable defined"
        assert symbol_var["type"] == "query"
        assert "label_values" in symbol_var["definition"]
        assert symbol_var["includeAll"] is True

    def test_position_table_uses_the_symbol_variable(self, dashboard: dict) -> None:
        panel = next(p for p in dashboard["panels"] if p["title"] == "Position Breakdown")
        exprs = " ".join(t.get("expr", "") for t in panel["targets"])
        assert "$symbol" in exprs

    def test_symbol_variable_is_not_sourced_from_position_metrics(self, dashboard: dict) -> None:
        """Regressionsschutz fuer den urspruenglichen Audit-Fund: $symbol
        war leer, weil es aus sgr_position_size gespeist wurde - einer
        Metrik, die NUR bei offenen Positionen ueberhaupt Zeitreihen hat.
        $symbol muss stattdessen aus sgr_asset_universe_status kommen,
        die unabhaengig von offenen Positionen das gesamte entdeckte
        Marktuniversum abbildet (siehe sgr/market_data/asset_universe.py)."""
        variables = dashboard["templating"]["list"]
        symbol_var = next(v for v in variables if v["name"] == "symbol")
        assert "sgr_asset_universe_status" in symbol_var["definition"]
        assert "sgr_position_size" not in symbol_var["definition"]

    def test_tenant_variable_offers_gordon_and_sumo_by_name(self, dashboard: dict) -> None:
        """Task-Vorgabe: der Benutzer muss mit wenigen Klicks zwischen
        All/Gordon/Sumo wechseln koennen - ein reines label_values()-Query
        wuerde nur die rohen Tenant-UUIDs als Text zeigen."""
        variables = dashboard["templating"]["list"]
        tenant_var = next(v for v in variables if v["name"] == "tenant")
        option_texts = {o["text"] for o in tenant_var["options"]}
        assert "Gordon" in option_texts
        assert "Sumo" in option_texts
        assert tenant_var["includeAll"] is True


class TestPositionBreakdownColumns:
    """Task-Vorgabe #8: Position Breakdown soll mindestens Tenant,
    Exchange, Symbol, Position Size, Entry Price, Current Price, Leverage,
    Unrealized PnL zeigen - nur Spalten mit tatsaechlich vorhandenen
    Daten, keine Dummy-Werte."""

    REQUIRED_METRICS = (
        "sgr_position_size",
        "sgr_position_exposure_usd",
        "sgr_position_leverage",
        "sgr_position_unrealized_pnl_usd",
        "sgr_position_entry_price_usd",
        "sgr_position_current_price_usd",
    )

    def test_all_required_position_metrics_are_queried(self, dashboard: dict) -> None:
        panel = next(p for p in dashboard["panels"] if p["title"] == "Position Breakdown")
        exprs = " ".join(t.get("expr", "") for t in panel["targets"])
        for metric in self.REQUIRED_METRICS:
            assert metric in exprs, f"Position Breakdown does not query {metric}"

    def test_entry_and_current_price_columns_are_labeled(self, dashboard: dict) -> None:
        panel = next(p for p in dashboard["panels"] if p["title"] == "Position Breakdown")
        organize = next(t for t in panel["transformations"] if t["id"] == "organize")
        rename = organize["options"]["renameByName"]
        assert "Entry Price (USD)" in rename.values()
        assert "Current Price (USD)" in rename.values()


class TestDashboardPanelTypesAreAppropriate:
    def test_kill_switch_is_a_stat_panel(self, dashboard: dict) -> None:
        panel = next(p for p in dashboard["panels"] if "Kill Switch" in p["title"])
        assert panel["type"] == "stat"

    def test_position_breakdown_is_a_table(self, dashboard: dict) -> None:
        panel = next(p for p in dashboard["panels"] if p["title"] == "Position Breakdown")
        assert panel["type"] == "table"

    def test_no_panel_type_text_or_fake_static_content(self, dashboard: dict) -> None:
        """Keine 'text'-Panels mit hartcodierten Werten - siehe Task-Vorgabe
        'keine Fake Values / statische Demo-Werte'."""
        for p in dashboard["panels"]:
            assert p.get("type") != "text"

    def test_every_content_panel_target_references_a_real_metric_or_builtin(
        self, dashboard: dict
    ) -> None:
        """Jede Query muss entweder eine sgr_*-Metrik oder das eingebaute
        Prometheus-'up'-Signal referenzieren - kein Panel mit rein
        statischem/konstantem Ausdruck."""
        for p in _all_panels(dashboard):
            for t in p.get("targets", []):
                expr = t.get("expr", "")
                assert re.search(r"\bsgr_[a-z_]+\b", expr) or "up{" in expr, (
                    f"Target {t.get('refId')} in {p['title']!r} has no real metric "
                    f"reference: {expr!r}"
                )
