import pytest

from dtb.domain.kpi import KPIDirection
from dtb.optimization import (
    Constraint,
    DecisionVariable,
    ExpressionObjective,
    GridSearchOptimizer,
    OptimizationProblem,
    get_optimizer,
    register_optimizer,
)
from tests.conftest import build_small_model


def test_grid_search_finds_better_capacity(small_model, base_scenario):
    # line capacity limits throughput → more capacity → more revenue
    small_model.event_kinds["customer_order"].stream.rate_per_hour = 0.6
    problem = OptimizationProblem(
        variables=[DecisionVariable(name="cap", path="resource:line", values=[1, 4])],
        objective_kpi_id="revenue",
        runs_per_evaluation=3,
        seed=7,
    )
    result = GridSearchOptimizer().optimize(small_model, base_scenario, problem)
    assert result.status == "completed"
    assert len(result.evaluations) == 2
    assert result.best["assignment"]["cap"] == 4
    scores = {e["assignment"]["cap"]: e["score"] for e in result.evaluations}
    assert scores[4] > scores[1]
    assert result.provenance["optimizer"] == "grid_search"


def test_constraints_make_candidates_infeasible(small_model, base_scenario):
    problem = OptimizationProblem(
        variables=[DecisionVariable(name="cap", path="resource:line", values=[1])],
        objective_kpi_id="revenue",
        constraints=[Constraint(expression="kpi('revenue') > 1e15")],
        runs_per_evaluation=2,
        seed=7,
    )
    result = GridSearchOptimizer().optimize(small_model, base_scenario, problem)
    assert result.status == "infeasible"
    assert result.best is None


def test_minimize_direction(small_model, base_scenario):
    problem = OptimizationProblem(
        variables=[DecisionVariable(name="rate", path="event_rate:customer_order",
                                    values=[0.5, 2.0])],
        objective_kpi_id="revenue",
        direction=KPIDirection.MINIMIZE,
        runs_per_evaluation=2,
        seed=7,
    )
    result = GridSearchOptimizer().optimize(small_model, base_scenario, problem)
    assert result.best["assignment"]["rate"] == 0.5  # fewer orders → less revenue


def test_expression_objective():
    obj = ExpressionObjective("kpi('revenue') - 100 * (1 - prob('g1'))")
    class FakeMC:
        final_kpis = {"revenue": {"mean": 500.0}}
        goal_achievement_probability = {"g1": 0.5}
    assert obj.evaluate(FakeMC()) == 500.0 - 50.0


def test_invalid_variable_path_rejected(small_model, base_scenario):
    problem = OptimizationProblem(
        variables=[DecisionVariable(name="x", path="bogus:line", values=[1])],
        objective_kpi_id="revenue",
    )
    with pytest.raises(ValueError):
        GridSearchOptimizer().optimize(small_model, base_scenario, problem)


def test_optimizer_plugin_registry():
    assert get_optimizer("grid_search").name == "grid_search"
    assert get_optimizer("random_search").name == "random_search"

    class Custom:
        name = "custom"
        def optimize(self, *a, **kw):  # pragma: no cover
            raise NotImplementedError

    register_optimizer("custom", Custom)
    assert get_optimizer("custom").name == "custom"
    with pytest.raises(KeyError):
        get_optimizer("nope")
