"""Monte Carlo simulation: N runs with derived seeds + trajectory statistics."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from dtb.common.seeding import derive_seed
from dtb.domain.model import EnterpriseModel
from dtb.domain.scenario import Scenario
from dtb.simulation.adapters import AdapterRegistry
from dtb.simulation.engine import SimControl, SimulationConfig, simulate
from dtb.simulation.result import SimulationResult


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _step_value(series: list[tuple[float, float]], t: float) -> float:
    """Trajectories are step functions between switching moments."""
    value = series[0][1] if series else 0.0
    for st, sv in series:
        if st <= t + 1e-9:
            value = sv
        else:
            break
    return value


@dataclass
class MonteCarloResult:
    runs: int
    master_seed: int
    provenance: dict[str, Any]
    grid: list[float] = field(default_factory=list)
    # per KPI: {"mean": [...], "std": [...], "p5": [...], ..., "ci_low": [...], "ci_high": [...]}
    kpi_stats: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    final_kpis: dict[str, dict[str, float]] = field(default_factory=dict)
    goal_achievement_probability: dict[str, float] = field(default_factory=dict)
    goal_metric_details: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    run_digests: list[str] = field(default_factory=list)
    failed_runs: int = 0
    sample_result: dict[str, Any] | None = None  # one full run for drill-down

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "master_seed": self.master_seed,
            "provenance": self.provenance,
            "grid": self.grid,
            "kpi_stats": self.kpi_stats,
            "final_kpis": self.final_kpis,
            "goal_achievement_probability": self.goal_achievement_probability,
            "goal_metric_details": self.goal_metric_details,
            "run_digests": self.run_digests,
            "failed_runs": self.failed_runs,
            "sample_result": self.sample_result,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MonteCarloResult":
        return cls(**data)


def wrap_single(result: SimulationResult, grid_points: int = 60) -> MonteCarloResult:
    """Present a single run in MonteCarloResult form (degenerate statistics),
    so forecasts and comparisons work uniformly for kind=single simulations."""
    horizon = result.provenance.horizon_hours
    grid = [round(horizon * i / grid_points, 6) for i in range(grid_points + 1)]
    mc = MonteCarloResult(
        runs=1,
        master_seed=result.provenance.seed,
        provenance=result.provenance.to_dict(),
        grid=grid,
        run_digests=[result.content_digest()],
        sample_result=result.to_dict(),
    )
    for kid, series in result.kpi_series.items():
        values = [_step_value(series, t) for t in grid]
        rounded = [round(v, 6) for v in values]
        mc.kpi_stats[kid] = {
            "mean": rounded, "std": [0.0] * len(grid),
            "p5": rounded, "p25": rounded, "p50": rounded, "p75": rounded, "p95": rounded,
            "ci_low": rounded, "ci_high": rounded,
        }
        final = result.final_kpis.get(kid, 0.0)
        mc.final_kpis[kid] = {
            "mean": final, "std": 0.0, "min": final, "max": final,
            "p5": final, "p50": final, "p95": final,
        }
    for gid, data in result.goal_achievement.items():
        mc.goal_achievement_probability[gid] = 1.0 if data.get("achieved") else 0.0
        mc.goal_metric_details[gid] = [
            {
                "kpi_id": metric["kpi_id"],
                "target": metric["target"],
                "comparison": metric["comparison"],
                "probability": 1.0 if metric["achieved"] else 0.0,
                "expected_value": metric["value"],
                "p5": metric["value"],
                "p95": metric["value"],
            }
            for metric in data.get("metrics", [])
        ]
    return mc


def run_monte_carlo(
    model: EnterpriseModel,
    scenario: Scenario,
    runs: int | None = None,
    master_seed: int | None = None,
    grid_points: int = 60,
    registry: AdapterRegistry | None = None,
    config: SimulationConfig | None = None,
    control: SimControl | None = None,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> MonteCarloResult:
    n_runs = int(runs if runs is not None else scenario.monte_carlo_runs)
    seed0 = int(master_seed if master_seed is not None else (scenario.seed or 0))
    horizon = float(scenario.horizon_hours or model.enterprise.planning_horizon_hours)
    grid = [round(horizon * i / grid_points, 6) for i in range(grid_points + 1)]

    results: list[SimulationResult] = []
    failed = 0
    for i in range(n_runs):
        if control is not None and not control.checkpoint():
            break
        run_seed = seed0 if n_runs == 1 else derive_seed(seed0, "mc_run", i)
        result = simulate(model, scenario, seed=run_seed, registry=registry, config=config)
        if result.status == "failed":
            failed += 1
            if failed == n_runs:  # every run failed — surface the error
                raise RuntimeError(f"all Monte Carlo runs failed: {result.error}")
        else:
            results.append(result)
        if progress_cb:
            progress_cb(
                {
                    "phase": "monte_carlo",
                    "run": i + 1,
                    "runs": n_runs,
                    "progress": (i + 1) / n_runs,
                }
            )

    mc = MonteCarloResult(
        runs=len(results),
        master_seed=seed0,
        provenance=results[0].provenance.to_dict() if results else {},
        grid=grid,
        failed_runs=failed,
        run_digests=[r.content_digest() for r in results],
        sample_result=results[0].to_dict() if results else None,
    )
    if not results:
        return mc

    kpi_ids = sorted({k for r in results for k in r.kpi_series})
    z95 = 1.959964
    for kid in kpi_ids:
        stats: dict[str, list[float]] = {
            k: [] for k in ("mean", "std", "p5", "p25", "p50", "p75", "p95", "ci_low", "ci_high")
        }
        for t in grid:
            values = sorted(_step_value(r.kpi_series.get(kid, []), t) for r in results)
            n = len(values)
            mean = sum(values) / n
            var = sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
            std = math.sqrt(var)
            sem = std / math.sqrt(n) if n else 0.0
            stats["mean"].append(round(mean, 6))
            stats["std"].append(round(std, 6))
            stats["p5"].append(round(_quantile(values, 0.05), 6))
            stats["p25"].append(round(_quantile(values, 0.25), 6))
            stats["p50"].append(round(_quantile(values, 0.50), 6))
            stats["p75"].append(round(_quantile(values, 0.75), 6))
            stats["p95"].append(round(_quantile(values, 0.95), 6))
            stats["ci_low"].append(round(mean - z95 * sem, 6))
            stats["ci_high"].append(round(mean + z95 * sem, 6))
        mc.kpi_stats[kid] = stats
        finals = sorted(r.final_kpis.get(kid, 0.0) for r in results)
        n = len(finals)
        mean = sum(finals) / n
        var = sum((v - mean) ** 2 for v in finals) / (n - 1) if n > 1 else 0.0
        mc.final_kpis[kid] = {
            "mean": round(mean, 6),
            "std": round(math.sqrt(var), 6),
            "min": round(finals[0], 6),
            "max": round(finals[-1], 6),
            "p5": round(_quantile(finals, 0.05), 6),
            "p50": round(_quantile(finals, 0.50), 6),
            "p95": round(_quantile(finals, 0.95), 6),
        }

    goal_ids = sorted({g for r in results for g in r.goal_achievement})
    for gid in goal_ids:
        achieved = sum(
            1 for r in results if r.goal_achievement.get(gid, {}).get("achieved", False)
        )
        mc.goal_achievement_probability[gid] = round(achieved / len(results), 4)
        # per-metric detail from the runs
        metric_acc: dict[int, dict[str, Any]] = {}
        for r in results:
            for idx, metric in enumerate(r.goal_achievement.get(gid, {}).get("metrics", [])):
                slot = metric_acc.setdefault(
                    idx,
                    {
                        "kpi_id": metric["kpi_id"],
                        "target": metric["target"],
                        "comparison": metric["comparison"],
                        "values": [],
                        "achieved": 0,
                    },
                )
                slot["values"].append(metric["value"])
                slot["achieved"] += 1 if metric["achieved"] else 0
        details = []
        for idx in sorted(metric_acc):
            slot = metric_acc[idx]
            values = sorted(slot["values"])
            details.append(
                {
                    "kpi_id": slot["kpi_id"],
                    "target": slot["target"],
                    "comparison": slot["comparison"],
                    "probability": round(slot["achieved"] / len(results), 4),
                    "expected_value": round(sum(values) / len(values), 6),
                    "p5": round(_quantile(values, 0.05), 6),
                    "p95": round(_quantile(values, 0.95), 6),
                }
            )
        mc.goal_metric_details[gid] = details
    return mc
