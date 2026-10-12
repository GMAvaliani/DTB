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


# ------------------------ покоординатный подбор порогов политик (фаза 6) --


def build_threshold_model(horizon: float = 200.0):
    """Модель с политикой, порог которой определяет, когда расширять ресурс."""
    from dtb.domain.control import ControlLoop, Policy
    from dtb.domain.economics import (
        EconomicBasis,
        EconomicDirection,
        EconomicItem,
        EconomicKind,
    )
    from tests.conftest import build_control_model

    model = build_control_model(horizon=horizon, with_policy=False, timer_period=10.0)
    model.policies["pol"] = Policy(
        id="pol", name="Регулирование", loop=ControlLoop.REGULATION,
        rule="waiting_count('work') >= threshold('queue')", action_ref="add_line",
        thresholds={"queue": 3.0}, cooldown_hours=24.0,
    )
    model.control_actions["add_line"].one_time_cost = 500.0
    model.economic_items["fee"] = EconomicItem(
        id="fee", name="Выручка", article="выручка", kind=EconomicKind.ONE_TIME,
        basis=EconomicBasis.SED_COMPLETED, ref="work", rate=300.0,
        direction=EconomicDirection.INCOME,
    )
    return model


def test_coordinate_search_tunes_policy_threshold():
    """Покоординатный поиск подбирает порог политики по NPV."""
    from dtb.domain import Scenario
    from dtb.optimization import DecisionVariable, OptimizationProblem, get_optimizer

    model = build_threshold_model()
    scenario = Scenario(id="base", name="base", enterprise_id="ctl", seed=11)
    problem = OptimizationProblem(
        variables=[
            DecisionVariable(name="queue", path="policy:pol.queue",
                             values=[1, 2, 3, 5, 8, 12])
        ],
        objective_expression="npv()", runs_per_evaluation=3, seed=11,
    )
    result = get_optimizer("coordinate_search").optimize(model, scenario, problem)
    assert result.status == "completed"
    assert result.best is not None
    assert result.best["assignment"]["queue"] in (1, 2, 3, 5, 8, 12)
    # лучший кандидат действительно лучший среди оценённых
    assert result.best["score"] == max(e["score"] for e in result.evaluations)
    # каждая точка решётки оценена не более одного раза (кеш работает)
    assert result.provenance["evaluations"] == len(problem.variables[0].values)
    assert result.provenance["trace"][0]["pass"] == 1


def test_coordinate_search_is_cheaper_than_grid_on_two_variables():
    """Две переменные: покоординатный поиск обходится меньшим числом оценок."""
    from dtb.domain import Scenario
    from dtb.optimization import DecisionVariable, OptimizationProblem, get_optimizer

    model = build_threshold_model(horizon=120.0)
    scenario = Scenario(id="base", name="base", enterprise_id="ctl", seed=11)
    variables = [
        DecisionVariable(name="queue", path="policy:pol.queue", values=[1, 2, 3, 5]),
        DecisionVariable(name="line", path="resource:line", values=[1, 2, 3, 4]),
    ]
    problem = OptimizationProblem(
        variables=variables, objective_expression="npv()",
        runs_per_evaluation=2, seed=11,
    )
    coordinate = get_optimizer("coordinate_search").optimize(model, scenario, problem)
    grid = get_optimizer("grid_search").optimize(model, scenario, problem)
    assert coordinate.provenance["evaluations"] < len(grid.evaluations)
    assert coordinate.best is not None and grid.best is not None
    # найденное решение не хуже среднего по решётке
    grid_scores = sorted(e["score"] for e in grid.evaluations)
    assert coordinate.best["score"] >= grid_scores[len(grid_scores) // 2]


def test_coordinate_search_is_reproducible():
    from dtb.domain import Scenario
    from dtb.optimization import DecisionVariable, OptimizationProblem, get_optimizer

    model = build_threshold_model(horizon=120.0)
    scenario = Scenario(id="base", name="base", enterprise_id="ctl", seed=11)
    problem = OptimizationProblem(
        variables=[
            DecisionVariable(name="queue", path="policy:pol.queue", values=[1, 3, 6])
        ],
        objective_kpi_id="done", runs_per_evaluation=2, seed=11,
    )
    first = get_optimizer("coordinate_search").optimize(model, scenario, problem)
    second = get_optimizer("coordinate_search").optimize(model, scenario, problem)
    assert [e["score"] for e in first.evaluations] == [
        e["score"] for e in second.evaluations
    ]
    assert first.best["assignment"] == second.best["assignment"]


def test_policy_path_requires_threshold_name():
    from dtb.optimization import DecisionVariable

    var = DecisionVariable(name="bad", path="policy:pol", values=[1.0])
    var.validate_path()  # префикс допустим
    from dtb.domain import Scenario

    with pytest.raises(ValueError, match="threshold"):
        var.apply(Scenario(id="s", name="s", enterprise_id="ctl"), 1.0)


def test_quantile_objective_is_more_cautious_than_mean():
    """Нижний квантиль выбирает устойчивое решение, а не удачное в среднем."""
    from dtb.analysis.montecarlo import MonteCarloResult
    from dtb.optimization import KPIObjective

    mc = MonteCarloResult(runs=5, master_seed=1, provenance={})
    mc.final_kpis["revenue"] = {"mean": 100.0}
    mc.run_finals["revenue"] = [10.0, 50.0, 100.0, 140.0, 200.0]
    mean_objective = KPIObjective("revenue")
    cautious = KPIObjective("revenue", quantile=0.1)
    assert mean_objective.evaluate(mc) == pytest.approx(100.0)
    assert cautious.evaluate(mc) == pytest.approx(26.0)  # 10% квантиль выборки
    assert cautious.describe()["quantile"] == 0.1


def test_quantile_in_expression_objective_and_constraint():
    from dtb.analysis.montecarlo import MonteCarloResult
    from dtb.optimization import Constraint, ExpressionObjective

    mc = MonteCarloResult(runs=4, master_seed=1, provenance={})
    mc.final_kpis["revenue"] = {"mean": 100.0}
    mc.run_finals["revenue"] = [40.0, 80.0, 120.0, 160.0]
    mc.run_npv = [10.0, 20.0, 30.0, 40.0]
    objective = ExpressionObjective("kpi_quantile('revenue', 0.25) + npv()")
    assert objective.evaluate(mc) == pytest.approx(70.0 + 25.0)
    assert Constraint(expression="kpi_quantile('revenue', 0.25) >= 70").is_satisfied(mc)
    assert not Constraint(expression="npv() >= 100").is_satisfied(mc)
