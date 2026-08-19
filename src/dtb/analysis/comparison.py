"""Scenario comparison: n-way diff of simulation / Monte-Carlo outcomes."""

from __future__ import annotations

from typing import Any

from dtb.analysis.montecarlo import MonteCarloResult


def compare_results(
    entries: list[tuple[str, MonteCarloResult]],
    baseline_label: str | None = None,
) -> dict[str, Any]:
    """entries: [(label, MonteCarloResult)] — first entry is baseline unless given."""
    if not entries:
        return {"kpis": {}, "goals": {}, "entries": []}
    labels = [label for label, _ in entries]
    baseline = baseline_label if baseline_label in labels else labels[0]
    base_mc = dict(entries)[baseline]

    kpi_ids = sorted({k for _, mc in entries for k in mc.final_kpis})
    kpis: dict[str, Any] = {}
    for kid in kpi_ids:
        base_mean = base_mc.final_kpis.get(kid, {}).get("mean")
        rows = []
        for label, mc in entries:
            final = mc.final_kpis.get(kid, {})
            mean = final.get("mean")
            delta = (
                round(mean - base_mean, 6)
                if mean is not None and base_mean is not None
                else None
            )
            rel = (
                round(delta / abs(base_mean), 4)
                if delta is not None and base_mean not in (None, 0)
                else None
            )
            rows.append(
                {
                    "label": label,
                    "mean": mean,
                    "p5": final.get("p5"),
                    "p95": final.get("p95"),
                    "delta_vs_baseline": delta,
                    "relative_delta": rel,
                    "series_mean": mc.kpi_stats.get(kid, {}).get("mean", []),
                    "grid": mc.grid,
                }
            )
        kpis[kid] = rows

    goal_ids = sorted({g for _, mc in entries for g in mc.goal_achievement_probability})
    goals: dict[str, Any] = {}
    for gid in goal_ids:
        base_p = base_mc.goal_achievement_probability.get(gid)
        goals[gid] = [
            {
                "label": label,
                "probability": mc.goal_achievement_probability.get(gid),
                "delta_vs_baseline": (
                    round(mc.goal_achievement_probability.get(gid, 0) - base_p, 4)
                    if base_p is not None
                    else None
                ),
            }
            for label, mc in entries
        ]

    return {
        "baseline": baseline,
        "entries": [
            {"label": label, "runs": mc.runs, "provenance": mc.provenance}
            for label, mc in entries
        ],
        "kpis": kpis,
        "goals": goals,
    }
