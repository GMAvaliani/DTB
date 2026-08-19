"""Forecast — the twin's main output (View 3).

Combines fact (imported actuals), plan and the Monte-Carlo forecast into one
report per KPI/goal: expected trajectory with confidence band, deviation from
plan and target, probability of goal achievement, and evidence-backed causes
(bottlenecks) of deviations.
"""

from __future__ import annotations

from typing import Any

from dtb.analysis.bottlenecks import analyze_bottlenecks
from dtb.analysis.montecarlo import MonteCarloResult
from dtb.domain.model import EnterpriseModel
from dtb.simulation.result import SimulationResult


def build_forecast(
    model: EnterpriseModel,
    mc: MonteCarloResult,
    scenario_id: str | None = None,
) -> dict[str, Any]:
    sample = (
        SimulationResult.from_dict(mc.sample_result) if mc.sample_result else None
    )
    bottlenecks = analyze_bottlenecks(sample, model) if sample else []

    kpi_reports: dict[str, Any] = {}
    for kpi_id, kpi in model.kpis.items():
        stats = mc.kpi_stats.get(kpi_id)
        finals = mc.final_kpis.get(kpi_id, {})
        plan_series = [{"t": p.t, "value": p.value} for p in model.plans.get(kpi_id, [])]
        actual_series = [{"t": p.t, "value": p.value} for p in model.actuals.get(kpi_id, [])]
        current_actual = actual_series[-1]["value"] if actual_series else None
        plan_final = plan_series[-1]["value"] if plan_series else None
        expected_final = finals.get("mean")
        deviation_from_plan = (
            round(expected_final - plan_final, 6)
            if expected_final is not None and plan_final is not None
            else None
        )
        kpi_reports[kpi_id] = {
            "name": kpi.name or kpi_id,
            "unit": kpi.unit,
            "direction": kpi.direction.value,
            "current_actual": current_actual,
            "actual_series": actual_series,
            "plan_series": plan_series,
            "forecast": {
                "grid": mc.grid,
                "mean": stats["mean"] if stats else [],
                "p5": stats["p5"] if stats else [],
                "p95": stats["p95"] if stats else [],
                "ci_low": stats["ci_low"] if stats else [],
                "ci_high": stats["ci_high"] if stats else [],
            },
            "expected_final": expected_final,
            "final_distribution": finals,
            "plan_final": plan_final,
            "deviation_from_plan": deviation_from_plan,
        }

    goal_reports: dict[str, Any] = {}
    for goal_id, goal in model.goals.items():
        if goal_id not in mc.goal_achievement_probability and not goal.target_metrics:
            continue
        goal_reports[goal_id] = {
            "name": goal.name or goal_id,
            "priority": goal.priority,
            "achievement_probability": mc.goal_achievement_probability.get(goal_id),
            "metrics": mc.goal_metric_details.get(goal_id, []),
        }

    # resource influence: utilization + capacity from the sample run
    resource_influence = []
    if sample:
        for res_id, stats in sorted(
            sample.resource_stats.items(), key=lambda p: -p[1].get("utilization", 0)
        ):
            resource = model.resources.get(res_id)
            resource_influence.append(
                {
                    "resource_id": res_id,
                    "name": resource.name if resource else res_id,
                    "utilization": stats.get("utilization", 0.0),
                    "peak_allocated": stats.get("peak_allocated", 0.0),
                    "capacity": resource.capacity if resource else None,
                }
            )

    return {
        "scenario_id": scenario_id,
        "provenance": mc.provenance,
        "runs": mc.runs,
        "kpis": kpi_reports,
        "goals": goal_reports,
        "bottlenecks": bottlenecks[:20],
        "resource_influence": resource_influence,
        "deviation_causes": [b["evidence"] for b in bottlenecks[:5]],
    }
