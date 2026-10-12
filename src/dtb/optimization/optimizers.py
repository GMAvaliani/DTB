"""Оптимизаторы: полный перебор, случайный поиск и покоординатный спуск.

Покоординатный поиск нужен там, где перебор невозможен: подбор порогов
политик контура управления. Переменные перебираются по очереди, каждая — по
своей решётке значений при фиксированных остальных; проход повторяется, пока
находится улучшение. Оценки кешируются, поэтому повторные комбинации не
считаются заново, а все кандидаты сравниваются на общих случайных числах.
"""

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


class _Evaluation:
    """Оценка одного набора значений переменных на Монте-Карло."""

    def __init__(
        self,
        model: EnterpriseModel,
        base_scenario: Scenario,
        problem: OptimizationProblem,
        registry: AdapterRegistry | None,
    ):
        self.model = model
        self.base_scenario = base_scenario
        self.problem = problem
        self.registry = registry
        self.objective = problem.build_objective()
        self._cache: dict[tuple, dict[str, Any]] = {}
        self.count = 0

    @staticmethod
    def key(assignment: dict[str, float]) -> tuple:
        return tuple(sorted(assignment.items()))

    def evaluate(self, assignment: dict[str, float], label: str) -> dict[str, Any]:
        cached = self._cache.get(self.key(assignment))
        if cached is not None:
            return cached
        scenario = self.base_scenario.model_copy(deep=True)
        scenario.id = f"{self.base_scenario.id}::{label}"
        for var in self.problem.variables:
            var.apply(scenario, assignment[var.name])
        mc = run_monte_carlo(
            self.model,
            scenario,
            runs=self.problem.runs_per_evaluation,
            master_seed=self.problem.seed,  # общие случайные числа для кандидатов
            registry=self.registry,
        )
        evaluation = {
            "index": self.count,
            "assignment": dict(assignment),
            "score": round(self.objective.evaluate(mc), 6),
            "feasible": all(c.is_satisfied(mc) for c in self.problem.constraints),
            "final_kpis": {k: v.get("mean") for k, v in mc.final_kpis.items()},
            "goal_probabilities": mc.goal_achievement_probability,
        }
        self.count += 1
        self._cache[self.key(assignment)] = evaluation
        return evaluation


class CoordinateSearchOptimizer:
    """Покоординатный спуск по решёткам значений переменных.

    Для подбора порогов политик: пространство — произведение решёток, но
    зависимость обычно почти разделима, поэтому последовательный подбор по
    одной переменной находит хорошее решение за линейное, а не
    экспоненциальное число оценок. Обход детерминирован: переменные в порядке
    задания, значения — в порядке решётки, при равенстве счёта удерживается
    текущее значение.
    """

    name = "coordinate_search"

    def __init__(self, passes: int = 3):
        self.passes = passes

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
        evaluator = _Evaluation(model, base_scenario, problem, registry)
        result = OptimizationResult(
            provenance={
                "optimizer": self.name,
                "base_scenario_id": base_scenario.id,
                "objective": evaluator.objective.describe(),
                "seed": problem.seed,
                "runs_per_evaluation": problem.runs_per_evaluation,
                "passes": self.passes,
                "quantile": problem.quantile,
                "variables": [
                    {"name": v.name, "path": v.path, "values": v.values}
                    for v in problem.variables
                ],
            }
        )
        # старт — середина решётки каждой переменной: ни край, ни случайность
        current = {
            v.name: v.values[len(v.values) // 2] for v in problem.variables
        }
        start = evaluator.evaluate(current, "start")
        result.evaluations.append(start)
        best = start if start["feasible"] else None
        best_score = start["score"] if start["feasible"] else None
        trace: list[dict[str, Any]] = []
        for pass_index in range(self.passes):
            improved = False
            for var in problem.variables:
                if control is not None and not control.checkpoint():
                    result.status = "cancelled"
                    result.provenance["trace"] = trace
                    return result
                if evaluator.count >= problem.max_evaluations:
                    break
                for value in var.values:
                    if value == current[var.name]:
                        continue
                    if evaluator.count >= problem.max_evaluations:
                        break
                    trial = dict(current)
                    trial[var.name] = value
                    evaluation = evaluator.evaluate(
                        trial, f"p{pass_index}_{var.name}_{value:g}"
                    )
                    if evaluation not in result.evaluations:
                        result.evaluations.append(evaluation)
                    if not evaluation["feasible"]:
                        continue
                    if best_score is None or evaluation["score"] > best_score + 1e-12:
                        best_score = evaluation["score"]
                        best = evaluation
                        current = trial
                        improved = True
                if progress_cb:
                    progress_cb(
                        {
                            "phase": "optimization",
                            "evaluation": evaluator.count,
                            "total": problem.max_evaluations,
                            "progress": min(
                                1.0, evaluator.count / max(1, problem.max_evaluations)
                            ),
                            "best_score": best_score,
                            "pass": pass_index + 1,
                        }
                    )
            trace.append(
                {
                    "pass": pass_index + 1,
                    "assignment": dict(current),
                    "score": best_score,
                    "improved": improved,
                    "evaluations": evaluator.count,
                }
            )
            if not improved:
                break  # ни одна координата не улучшает — локальный оптимум
        result.best = best
        result.provenance["trace"] = trace
        result.provenance["evaluations"] = evaluator.count
        if result.status == "completed" and result.best is None:
            result.status = "infeasible"
        return result


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
    "coordinate_search": CoordinateSearchOptimizer,
}


def register_optimizer(name: str, factory: Callable[[], Optimizer]) -> None:
    """Plugin hook for future optimizers (DP over МП steps, evolutionary, RL…)."""
    _REGISTRY[name] = factory


def get_optimizer(name: str) -> Optimizer:
    factory = _REGISTRY.get(name)
    if factory is None:
        raise KeyError(f"unknown optimizer {name!r}; available: {sorted(_REGISTRY)}")
    return factory()
