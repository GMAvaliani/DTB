from dtb.analysis import analyze_bottlenecks, build_forecast, compare_results, run_monte_carlo
from dtb.analysis.montecarlo import wrap_single
from dtb.domain import Scenario, SeriesPoint
from dtb.simulation import simulate
from tests.conftest import build_small_model


def test_monte_carlo_statistics(small_model, base_scenario):
    mc = run_monte_carlo(small_model, base_scenario, runs=12, master_seed=5)
    assert mc.runs == 12
    stats = mc.kpi_stats["revenue"]
    assert len(stats["mean"]) == len(mc.grid)
    final = mc.final_kpis["revenue"]
    assert final["p5"] <= final["p50"] <= final["p95"]
    assert final["min"] <= final["mean"] <= final["max"]
    assert stats["ci_low"][-1] <= stats["mean"][-1] <= stats["ci_high"][-1]
    assert 0.0 <= mc.goal_achievement_probability["g1"] <= 1.0
    assert mc.provenance["seed"] is not None


def test_goal_probability_reacts_to_target(small_model, base_scenario):
    small_model.goals["g1"].target_metrics[0].target_value = 1e12
    mc = run_monte_carlo(small_model, base_scenario, runs=6, master_seed=5)
    assert mc.goal_achievement_probability["g1"] == 0.0


def test_forecast_report(small_model, base_scenario):
    small_model.plans["revenue"] = [SeriesPoint(t=240, value=999999)]
    small_model.actuals["revenue"] = [SeriesPoint(t=-24, value=100)]
    mc = run_monte_carlo(small_model, base_scenario, runs=6, master_seed=5)
    fc = build_forecast(small_model, mc, "base")
    kpi = fc["kpis"]["revenue"]
    assert kpi["plan_final"] == 999999
    assert kpi["deviation_from_plan"] is not None and kpi["deviation_from_plan"] < 0
    assert kpi["current_actual"] == 100
    assert fc["goals"]["g1"]["achievement_probability"] is not None
    assert isinstance(fc["bottlenecks"], list)


def test_bottlenecks_from_starved_resource(small_model, base_scenario):
    small_model.resources["line"].capacity = 0.5  # cannot even hold one unit
    small_model.seds["production"].resources[0].quantity = 1
    result = simulate(small_model, base_scenario, seed=1)
    findings = analyze_bottlenecks(result, small_model)
    assert any(f["type"] == "missing_resource" and f["resource_id"] == "line" for f in findings)


def test_wrap_single_matches_final_values(small_model, base_scenario):
    result = simulate(small_model, base_scenario, seed=3)
    mc = wrap_single(result)
    assert mc.runs == 1
    assert mc.final_kpis["revenue"]["mean"] == result.final_kpis["revenue"]
    assert mc.goal_achievement_probability["g1"] in (0.0, 1.0)


def test_compare_results(small_model, base_scenario):
    mc_a = run_monte_carlo(small_model, base_scenario, runs=5, master_seed=5)
    boosted = Scenario(id="boost", name="boost", enterprise_id="acme",
                       event_rate_multipliers={"customer_order": 2.0})
    mc_b = run_monte_carlo(small_model, boosted, runs=5, master_seed=5)
    cmp = compare_results([("base", mc_a), ("boost", mc_b)])
    rows = cmp["kpis"]["revenue"]
    assert rows[0]["delta_vs_baseline"] == 0.0
    assert rows[1]["delta_vs_baseline"] > 0  # doubled demand → more revenue
    assert cmp["baseline"] == "base"
