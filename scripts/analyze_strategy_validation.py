#!/usr/bin/env python3
"""
Aggregierte Failure-/Gate-/Strategy-Comparison-Analyse fuer einen bereits
abgeschlossenen Symbol-Strategy-Validation-Batch (Autonomous-Strategy-
Universe-Rollout, Phase 3-9).

WICHTIG: liest AUSSCHLIESSLICH bereits persistierte Ergebnisse aus
strategy_symbol_validations. Startet KEINEN neuen Backtest, keinen neuen
Exchange-Zugriff. Reine Datenbank-Auswertung.

Es existiert in diesem Repository kein sgr.cli-Package (per grep
verifiziert, 2026-09-15) - dieses Skript folgt deshalb bewusst demselben
bestehenden Muster wie scripts/run_symbol_strategy_validation.py
(argparse + async main), statt eine neue, nicht vorhandene CLI-
Architektur zu erfinden.

Usage:
    python scripts/analyze_strategy_validation.py \\
        --batch-id full_universe_20260915b \\
        --output-json /tmp/sgr-718-validation-analysis.json \\
        --output-md /tmp/sgr-718-validation-analysis.md

Gate-Schwellen exakt aus sgr/backtesting/types.py::BacktestResult.
is_acceptable uebernommen (Sharpe>=1.0, PF>=1.3, MaxDD<=20%,
HitRate>=40%, Trades>=30) - keine eigenen, abweichenden Schwellen
erfunden.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from sgr.strategy.symbol_validation_runner import NO_STRATEGY_SENTINEL

# Identisch zu sgr/backtesting/types.py::BacktestResult.is_acceptable -
# NICHT abweichend neu definiert.
GATES: dict[str, Any] = {
    "sharpe_gate": lambda m: (m.get("sharpe_ratio") or 0.0) >= 1.0,
    "profit_factor_gate": lambda m: (m.get("profit_factor") or 0.0) >= 1.3,
    "max_drawdown_gate": lambda m: (m.get("max_drawdown_pct") or 999.0) <= 20.0,
    "hit_rate_gate": lambda m: (m.get("hit_rate_pct") or 0.0) >= 40.0,
    "trade_count_gate": lambda m: (m.get("total_trades") or 0) >= 30,
    "walk_forward_gate": lambda m: bool(m.get("wf_is_consistent")),
}

# Die fuenf Backtest-Gates (alles ausser walk_forward_gate) - separat
# benannt, weil "alle Backtest-Gates bestanden, aber WF gescheitert" eine
# eigene, oft nachgefragte Kategorie ist (siehe Phase 5/8 der Task-
# Vorgabe: "Strategien, die im Backtest gut aussehen, aber systematisch
# im Walk Forward scheitern").
BACKTEST_GATES = (
    "sharpe_gate",
    "profit_factor_gate",
    "max_drawdown_gate",
    "hit_rate_gate",
    "trade_count_gate",
)


def _passes_all_backtest_gates(m: dict[str, Any]) -> bool:
    return all(GATES[g](m) for g in BACKTEST_GATES)


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


async def _load_rows(batch_id: str) -> list[dict[str, Any]]:
    from sgr.core.repositories import StrategySymbolValidationRepository

    repo = StrategySymbolValidationRepository()
    return await repo.get_all_for_batch(batch_id=batch_id)


def _is_candidate_row(row: dict[str, Any]) -> bool:
    """Eine echte Pro-Strategie-Backtest-Zeile, keine Symbol-Level-
    Sentinel-Zeile (INSUFFICIENT_DATA/INVALID_DATA/kein Regime-Match/
    TECHNICAL_FAILURE - siehe NO_STRATEGY_SENTINEL in
    symbol_validation_runner.py)."""
    return bool(row["strategy"] != NO_STRATEGY_SENTINEL)


def analyze(rows: list[dict[str, Any]]) -> dict[str, Any]:
    final_rows = [r for r in rows if r["is_best_for_symbol"]]
    candidate_rows = [r for r in rows if _is_candidate_row(r)]
    sentinel_final_rows = [r for r in final_rows if r["strategy"] == NO_STRATEGY_SENTINEL]

    total_symbols = len({r["symbol"] for r in rows})

    # ------------------------------------------------------------------
    # Phase 3: finaler Status pro Symbol (bereits bekannt, hier erneut
    # aus den Rohdaten abgeleitet statt uebernommen - Ehrlichkeits-
    # Gegenprobe).
    # ------------------------------------------------------------------
    final_status_counts = dict(Counter(r["status"] for r in final_rows))

    # ------------------------------------------------------------------
    # Phase 3+5+6: pro Strategie.
    # ------------------------------------------------------------------
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in candidate_rows:
        by_strategy[r["strategy"]].append(r)

    strategy_stats: dict[str, Any] = {}
    for name, strat_rows in sorted(by_strategy.items()):
        metrics_list = [r["metrics"] for r in strat_rows if r["metrics"]]
        gate_results = {
            gate: [fn(m) for m in metrics_list] for gate, fn in GATES.items()
        }
        backtest_pass = sum(1 for m in metrics_list if _passes_all_backtest_gates(m))
        wf_pass = sum(1 for m in metrics_list if GATES["walk_forward_gate"](m))
        final_wins = [r for r in final_rows if r["strategy"] == name]
        final_active = sum(1 for r in final_wins if r["status"] == "active")

        sharpes = [m["sharpe_ratio"] for m in metrics_list if m.get("sharpe_ratio") is not None]
        returns = [
            m["total_return_pct"] for m in metrics_list if m.get("total_return_pct") is not None
        ]
        drawdowns = [
            m["max_drawdown_pct"]
            for m in metrics_list
            if m.get("max_drawdown_pct") is not None
        ]
        trades = [m["total_trades"] for m in metrics_list if m.get("total_trades") is not None]
        robustness = [
            r["robustness_score"] for r in strat_rows if r["robustness_score"] is not None
        ]

        best_row = max(metrics_list, key=lambda m: m.get("sharpe_ratio", -999), default=None)
        worst_row = min(metrics_list, key=lambda m: m.get("sharpe_ratio", 999), default=None)

        strategy_stats[name] = {
            "tested_symbols": len(strat_rows),
            "backtest_pass": backtest_pass,
            "backtest_fail": len(metrics_list) - backtest_pass,
            "walk_forward_pass": wf_pass,
            "walk_forward_fail": len(metrics_list) - wf_pass,
            "backtest_pass_but_wf_fail": sum(
                1
                for m in metrics_list
                if _passes_all_backtest_gates(m) and not GATES["walk_forward_gate"](m)
            ),
            "final_symbol_wins": len(final_wins),
            "final_active": final_active,
            "gate_pass_counts": {g: sum(vals) for g, vals in gate_results.items()},
            "gate_pass_rate_pct": {
                g: round(100 * sum(vals) / len(vals), 1) if vals else None
                for g, vals in gate_results.items()
            },
            "median_sharpe": _median(sharpes),
            "median_return_pct": _median(returns),
            "median_max_drawdown_pct": _median(drawdowns),
            "median_trade_count": _median(trades),
            "median_robustness_score": _median(robustness),
            "best_result": {
                "sharpe_ratio": best_row.get("sharpe_ratio"),
                "total_return_pct": best_row.get("total_return_pct"),
                "total_trades": best_row.get("total_trades"),
            }
            if best_row
            else None,
            "worst_result": {
                "sharpe_ratio": worst_row.get("sharpe_ratio"),
                "total_return_pct": worst_row.get("total_return_pct"),
                "total_trades": worst_row.get("total_trades"),
            }
            if worst_row
            else None,
        }

    # ------------------------------------------------------------------
    # Phase 5: aggregierte Gate-Analyse ueber ALLE Kandidaten-Zeilen
    # (unabhaengig von Strategie) - "welches Gate filtert am meisten".
    # ------------------------------------------------------------------
    all_metrics = [r["metrics"] for r in candidate_rows if r["metrics"]]
    gate_summary: dict[str, Any] = {}
    for gate, fn in GATES.items():
        results = [fn(m) for m in all_metrics]
        passed = sum(results)
        total = len(results)
        gate_summary[gate] = {
            "total_candidates": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate_pct": round(100 * passed / total, 1) if total else None,
        }

    # Primary vs. secondary Failure: fuer jede fehlgeschlagene Zeile,
    # welche(s) Gate(s) genau scheitern. "primary" = Zeilen, bei denen
    # GENAU EIN Gate scheitert (eindeutig zuordenbarer Grund); "multi"
    # = mehrere Gates scheitern gleichzeitig (kein einzelnes Gate ist
    # hier "die" Ursache).
    exclusive_failure_counts: Counter[str] = Counter()
    multi_failure_rows = 0
    fully_passed_backtest_gates_rows = 0
    for m in all_metrics:
        failed_gates = [g for g, fn in GATES.items() if g != "walk_forward_gate" and not fn(m)]
        if not failed_gates:
            fully_passed_backtest_gates_rows += 1
        elif len(failed_gates) == 1:
            exclusive_failure_counts[failed_gates[0]] += 1
        else:
            multi_failure_rows += 1

    dominant_gate = max(gate_summary.items(), key=lambda kv: kv[1]["failed"])[0]

    # ------------------------------------------------------------------
    # Phase 4: Failure-Reason-Verteilung - reale, tatsaechlich
    # gespeicherte failure_reason-Strings, keine erfundenen Kategorien.
    # ------------------------------------------------------------------
    def _reason_bucket(reason: str | None) -> str:
        """Buendelt die freitextigen 'N blocker(s)'-Meldungen nach
        Blocker-Anzahl - die exakten Strings unterscheiden sich pro
        Zeile nur in dieser Zahl (siehe BacktestingEngine._make_decision()),
        eine Buendelung macht die Verteilung lesbar ohne den
        tatsaechlichen Text zu veraendern."""
        if reason is None:
            return "(none)"
        if "blocker(s)" in reason:
            # Buendelt "N blocker(s)" auf den generischen Satz, ohne die
            # exakte Blocker-Anzahl - diese wird bereits separat und
            # praeziser ueber die Gate Analysis (welches Gate genau
            # scheitert) ausgewiesen.
            return reason.rsplit(".", 2)[0] + "."
        return reason

    candidate_reason_counts = Counter(
        _reason_bucket(r["failure_reason"]) for r in candidate_rows if r["failure_reason"]
    )
    sentinel_reason_counts = Counter(
        r["failure_reason"] for r in sentinel_final_rows if r["failure_reason"]
    )
    all_reason_counts = candidate_reason_counts + sentinel_reason_counts
    total_reasons = sum(all_reason_counts.values())
    dominant_reason = (
        max(all_reason_counts.items(), key=lambda kv: kv[1]) if all_reason_counts else (None, 0)
    )

    failure_reason_symbols: dict[str, set[str]] = defaultdict(set)
    failure_reason_strategies: dict[str, set[str]] = defaultdict(set)
    for r in candidate_rows:
        if r["failure_reason"]:
            bucket = _reason_bucket(r["failure_reason"])
            failure_reason_symbols[bucket].add(r["symbol"])
            failure_reason_strategies[bucket].add(r["strategy"])
    for r in sentinel_final_rows:
        if r["failure_reason"]:
            failure_reason_symbols[r["failure_reason"]].add(r["symbol"])

    # ------------------------------------------------------------------
    # Phase 7: Market-Regime-Korrelation - nur, wenn regime_profile
    # tatsaechlich befuellt ist (bei Data-Quality-Ablehnungen leer).
    # ------------------------------------------------------------------
    regime_correlation: dict[str, Any] = {}
    for name, strat_rows in sorted(by_strategy.items()):
        by_regime: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in strat_rows:
            regime = (r.get("regime_profile") or {}).get("dominant_regime")
            if regime:
                by_regime[regime].append(r)
        regime_correlation[name] = {
            regime: {
                "symbol_count": len(rs),
                "median_sharpe": _median(
                    [
                        r["metrics"]["sharpe_ratio"]
                        for r in rs
                        if r["metrics"].get("sharpe_ratio") is not None
                    ]
                ),
                "backtest_pass_count": sum(
                    1 for r in rs if r["metrics"] and _passes_all_backtest_gates(r["metrics"])
                ),
            }
            for regime, rs in sorted(by_regime.items())
        }

    return {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "total_symbols": total_symbols,
        "total_candidate_rows": len(candidate_rows),
        "total_final_rows": len(final_rows),
        "final_status_counts": final_status_counts,
        "strategy_comparison": strategy_stats,
        "gate_analysis": {
            "gates": gate_summary,
            "dominant_gate_by_failure_count": dominant_gate,
            "exclusive_single_gate_failures": dict(exclusive_failure_counts),
            "multi_gate_failures": multi_failure_rows,
            "passed_all_backtest_gates_rows": fully_passed_backtest_gates_rows,
        },
        "failure_reason_analysis": {
            "total_reason_occurrences": total_reasons,
            "dominant_reason": {"reason": dominant_reason[0], "count": dominant_reason[1]},
            "distribution": [
                {
                    "reason": reason,
                    "count": count,
                    "percentage_pct": (
                        round(100 * count / total_reasons, 1) if total_reasons else 0.0
                    ),
                    "affected_symbols": len(failure_reason_symbols.get(reason, set())),
                    "affected_strategies": sorted(failure_reason_strategies.get(reason, set())),
                }
                for reason, count in all_reason_counts.most_common()
            ],
        },
        "regime_correlation": regime_correlation,
    }


def render_markdown(analysis: dict[str, Any], batch_id: str) -> str:
    lines: list[str] = []
    a = analysis
    ga = a["gate_analysis"]
    fra = a["failure_reason_analysis"]

    lines.append(f"# SGR Strategy Validation Analysis — batch `{batch_id}`")
    lines.append(f"\nGenerated: {a['generated_at']}")
    lines.append(
        f"\nSource: {a['total_symbols']} symbols, {a['total_candidate_rows']} candidate rows, "
        f"{a['total_final_rows']} final rows (all already persisted — no new backtesting "
        "performed for this report)."
    )

    lines.append("\n## Final Status Distribution")
    lines.append("\n| Status | Count |\n|---|---|")
    for status, count in sorted(a["final_status_counts"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {status} | {count} |")

    lines.append("\n## Gate Analysis")
    lines.append(
        "\n| Gate | Total Candidates | Passed | Failed | Pass Rate |\n|---|---|---|---|---|"
    )
    for gate, g in ga["gates"].items():
        lines.append(
            f"| {gate} | {g['total_candidates']} | {g['passed']} | {g['failed']} | "
            f"{g['pass_rate_pct']}% |"
        )
    lines.append(f"\n**Dominant gate by failure count:** `{ga['dominant_gate_by_failure_count']}`")
    lines.append(
        "\n**Rows where exactly one gate is the sole failure (unambiguous primary cause):**"
    )
    for gate, count in sorted(
        ga["exclusive_single_gate_failures"].items(), key=lambda kv: -kv[1]
    ):
        lines.append(f"- {gate}: {count}")
    lines.append(
        "\n**Rows failing multiple gates simultaneously (no single primary cause):** "
        f"{ga['multi_gate_failures']}"
    )
    lines.append(
        "\n**Rows passing ALL backtest gates (Sharpe/PF/DD/HitRate/Trades) — only "
        f"Walk-Forward could still block these:** {ga['passed_all_backtest_gates_rows']}"
    )

    lines.append("\n## Failure Reason Analysis")
    lines.append(
        f"\n**Dominant reason:** {fra['dominant_reason']['reason']} "
        f"({fra['dominant_reason']['count']} occurrences)"
    )
    lines.append(
        "\n| Reason | Count | % | Affected Symbols | Affected Strategies |\n|---|---|---|---|---|"
    )
    for item in fra["distribution"][:20]:
        strategies = ", ".join(item["affected_strategies"]) or "-"
        lines.append(
            f"| {item['reason']} | {item['count']} | {item['percentage_pct']}% | "
            f"{item['affected_symbols']} | {strategies} |"
        )

    lines.append("\n## Strategy Comparison")
    lines.append(
        "\n| Strategy | Tested | Backtest Pass | WF Pass | Backtest-Pass-but-WF-Fail | "
        "Final Wins | Active | Median Sharpe | Median Return % | Median MaxDD % | Median Trades |"
        "\n|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for name, s in a["strategy_comparison"].items():
        lines.append(
            f"| {name} | {s['tested_symbols']} | {s['backtest_pass']} | "
            f"{s['walk_forward_pass']} | {s['backtest_pass_but_wf_fail']} | "
            f"{s['final_symbol_wins']} | {s['final_active']} | {s['median_sharpe']} | "
            f"{s['median_return_pct']} | {s['median_max_drawdown_pct']} | "
            f"{s['median_trade_count']} |"
        )

    lines.append("\n## Market Regime Correlation")
    lines.append("(per strategy, dominant regime observed on the symbol)")
    for name, regimes in a["regime_correlation"].items():
        lines.append(f"\n### {name}")
        lines.append("\n| Regime | Symbols | Backtest Pass | Median Sharpe |\n|---|---|---|---|")
        for regime, r in regimes.items():
            lines.append(
                f"| {regime} | {r['symbol_count']} | {r['backtest_pass_count']} | "
                f"{r['median_sharpe']} |"
            )

    return "\n".join(lines) + "\n"


async def _main(batch_id: str, output_json: str, output_md: str) -> int:
    from sgr.core.database import close_db, init_db

    await init_db()
    try:
        rows = await _load_rows(batch_id)
        if not rows:
            print(f"No rows found for batch_id={batch_id!r} - nothing to analyze.")
            return 1

        analysis = analyze(rows)
        analysis["batch_id"] = batch_id

        with open(output_json, "w") as f:
            json.dump(analysis, f, indent=2, default=str)
        print(f"Wrote JSON analysis to {output_json}")

        md = render_markdown(analysis, batch_id)
        with open(output_md, "w") as f:
            f.write(md)
        print(f"Wrote Markdown report to {output_md}")

        return 0
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--output-json", default="/tmp/sgr-718-validation-analysis.json")
    parser.add_argument("--output-md", default="/tmp/sgr-718-validation-analysis.md")
    args = parser.parse_args()

    exit_code = asyncio.run(_main(args.batch_id, args.output_json, args.output_md))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
