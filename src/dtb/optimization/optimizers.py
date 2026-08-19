"""Optimizers: grid search + random search, behind a plugin registry."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from dtb.analysis.montecarlo import run_monte_carlo
from dtb.common.seeding import substream
from dtb.domain.model import EnterpriseModel
from dtb.domain.scenario import Scenario
from dtb.optimization.problem import OptimizationProblem
from dtb.simulation.adapters import AdapterRegistry
from dtb.simulation.engine import SimControl


@dataclass
class OptimizationResult:
    status: str = "completed"  # completed | cancelled | infeasible
    best: dict[str, Any] | None = None
    evaluations: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "best": self.best,
            "evaluations": self.evaluations,
            "provenance": self.provenance,
        }


class Optimizer(Protocol):
    name: str

    def optimize(
        self,
        model: EnterpriseModel,
        base_scenario: Scenario,
        problem: OptimizationProblem,
        registry: AdapterRegistry | None = None,
        control: SimControl | None = None,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> OptimizationResult: ...


class _EvaluatingOptimizer:
    """Shared evaluation loop: subclasses supply candidate assignments."""

    name = "base"

    def _candidates(
        self, problem: OptimizationProblem
    ) -> list[dict[str, float]]:  # pragma: no cover — abstract
        raise NotImplementedError

    def optimize(
        self,
        model: EnterpriseModel,
        base_scenario: Scenario,
        problem: OptimizationProblem,
        registry: AdapterRegistry | None = None,
        control: SimControl | None = None,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> OptimizationResult:
        for var in problem.variables:
            var.validate_path()
        objective = problem.build_objective()
        candidates = self._candidates(problem)[: problem.max_evaluations]
        result = OptimizationResult(
            provenance={
                "optimizer": self.name,
                "base_scenario_id": base_scenario.id,
                "objective": objective.describe(),
                "seed": problem.seed,
                "runs_per_evaluation": problem.runs_per_evaluation,
                "candidates": len(candidates),
            }
        )
        best_score: float | None = None
        for index, assignment in enumerate(candidates):
            if control is not None and not control.checkpoint():
                result.status = "cancelled"
                break
            scenario = base_scenario.model_copy(deep=True)
            scenario.id = f"{base_scenario.id}::opt{index}"
            for var in problem.variables:
                var.apply(scenario, assignment[var.name])
            mc = run_monte_carlo(
                model,
                scenario,
                runs=problem.runs_per_evaluation,
                master_seed=problem.seed,  # common random numbers across candidates
                registry=registry,
            )
            feasible = all(c.is_satisfied(mc) for c in problem.constraints)
            score = objective.evaluate(mc)
            evaluation = {
                "index": index,
                "assignment": assignment,
                "score": round(score, 6),
                "feasible": feasible,
                "final_kpis": {k: v.get("mean") for k, v in mc.final_kpis.items()},
                "goal_probabilities": mc.goal_achievement_probability,
            }
            result.evaluations.append(evaluation)
            if feasible and (best_score is None or score > best_score):
                best_score = score
                result.best = evaluation
            if progress_cb:
                progress_cb(
                    {
                        "phase": "optimization",
                        "evaluation": index + 1,
                        "total": len(candidates),
                        "progress": (index + 1) / len(candidates),
                        "best_score": best_score,
                    }
                )
        if result.status == "completed" and result.best is None:
            result.status = "infeasible"
        return result


class GridSearchOptimizer(_EvaluatingOptimizer):
    """Exhaustive search over the cartesian product of variable values."""

    name = "grid_search"

    def _candidates(self, problem: OptimizationProblem) -> list[dict[str, float]]:
        names = [v.name for v in problem.variables]
        return [
            dict(zip(names, combo))
            for combo in itertools.product(*(v.values for v in problem.variables))
        ]


class RandomSearchOptimizer(_EvaluatingOptimizer):
    """Uniform random sampling of the grid — for larger spaces."""

    name = "random_search"

    def __init__(self, samples: int = 30):
        self.samples = samples

    def _candidates(self, problem: OptimizationProblem) -> list[dict[str, float]]:
        rng = substream(problem.seed, "random_search")
        out = []
        for _ in range(min(self.samples, problem.max_evaluations)):
            out.append({v.name: rng.choice(v.values) for v in problem.variables})
        return out


_REGISTRY: dict[str, Callable[[], Optimizer]] = {
    "grid_search": GridSearchOptimizer,
    "random_search": RandomSearchOptimizer,
}


def register_optimizer(name: str, factory: Callable[[], Optimizer]) -> None:
    """Plugin hook for future optimizers (DP over МП steps, evolutionary, RL…)."""
    _REGISTRY[name] = factory


def get_optimizer(name: str) -> Optimizer:
    factory = _REGISTRY.get(name)
    if factory is None:
        raise KeyError(f"unknown optimizer {name!r}; available: {sorted(_REGISTRY)}")
    return factory()
