import pytest

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


# ----------------------------------- парные разности с доверительным интервалом --


def test_paired_difference_detects_shift():
    """Парная разность находит систематический сдвиг даже при большом разбросе."""
    from dtb.analysis.comparison import paired_difference

    a = [100.0, 150.0, 200.0, 120.0, 180.0, 90.0, 210.0, 160.0]
    b = [v + 10.0 for v in a]  # общий шум одинаков, сдвиг +10
    stats = paired_difference(a, b)
    assert stats["mean"] == pytest.approx(10.0)
    assert stats["std"] == pytest.approx(0.0)
    assert stats["significant"] is True
    assert stats["share_better"] == 1.0
    assert stats["n_pairs"] == 8


def test_paired_difference_not_significant_when_noise_dominates():
    from dtb.analysis.comparison import paired_difference

    a = [100.0, 100.0, 100.0, 100.0, 100.0]
    b = [110.0, 90.0, 105.0, 95.0, 100.0]
    stats = paired_difference(a, b)
    assert stats["mean"] == pytest.approx(0.0)
    assert stats["significant"] is False
    assert stats["ci_low"] < 0 < stats["ci_high"]


def test_paired_difference_single_pair_is_degenerate():
    from dtb.analysis.comparison import paired_difference

    stats = paired_difference([1.0], [2.0])
    assert stats["n_pairs"] == 1
    assert stats["mean"] == 1.0
    assert stats["significant"] is False  # по одному прогону вывода нет
    assert paired_difference([], [])["n_pairs"] == 0


def test_t_quantile_table():
    from dtb.analysis.comparison import t_quantile_975

    assert t_quantile_975(1) == pytest.approx(12.706)
    assert t_quantile_975(10) == pytest.approx(2.228)
    assert t_quantile_975(500) == pytest.approx(1.959964)  # нормальное приближение


def test_comparison_pairs_monte_carlo_runs_by_seed(small_model, base_scenario):
    """Сравнение сопрягает прогоны по сидам: общие случайные числа работают."""
    from dtb.analysis.comparison import compare_results
    from dtb.analysis.montecarlo import run_monte_carlo

    better = base_scenario.model_copy(deep=True)
    better.id = "more_capacity"
    better.resource_capacity_overrides = {"line": 4}
    a = run_monte_carlo(small_model, base_scenario, runs=6, master_seed=777)
    b = run_monte_carlo(small_model, better, runs=6, master_seed=777)
    assert a.run_seeds == b.run_seeds  # общие случайные числа
    report = compare_results([("base", a), ("more_capacity", b)])
    paired = report["paired_differences"]["revenue"][0]
    assert paired["label"] == "more_capacity"
    assert paired["paired_by_seed"] is True
    assert paired["n_pairs"] == 6
    assert paired["ci_low"] <= paired["mean"] <= paired["ci_high"]
    # больше мощности — не меньше выручки на каждом сопряжённом прогоне
    assert paired["share_worse"] == 0.0


def test_comparison_reports_paired_npv(small_model, base_scenario):
    from dtb.analysis.comparison import compare_results
    from dtb.analysis.montecarlo import run_monte_carlo
    from dtb.domain.economics import (
        EconomicBasis,
        EconomicDirection,
        EconomicItem,
        EconomicKind,
    )

    small_model.economic_items["revenue"] = EconomicItem(
        id="revenue", name="Выручка", article="выручка",
        kind=EconomicKind.ONE_TIME, basis=EconomicBasis.UTILITY_PRODUCED,
        ref="money", rate=1.0, direction=EconomicDirection.INCOME,
    )
    small_model.economic_items["line"] = EconomicItem(
        id="line", name="Линия", article="содержание",
        kind=EconomicKind.RATE, basis=EconomicBasis.RESOURCE_CAPACITY,
        ref="line", rate=50.0,
    )
    wide = base_scenario.model_copy(deep=True)
    wide.id = "wide"
    wide.resource_capacity_overrides = {"line": 4}
    a = run_monte_carlo(small_model, base_scenario, runs=5, master_seed=3)
    b = run_monte_carlo(small_model, wide, runs=5, master_seed=3)
    report = compare_results([("base", a), ("wide", b)])
    assert report["paired_npv"]
    row = report["paired_npv"][0]
    assert row["label"] == "wide" and row["n_pairs"] == 5
    assert report["economics"] and report["economics"][0]["npv"] is not None
    assert report["confidence_level"] == 0.95
