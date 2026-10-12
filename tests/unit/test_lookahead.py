"""Фаза 6: просмотр вперёд с клонированием состояния и подбор порогов политик."""

import pytest

from dtb.domain import Scenario
from dtb.domain.control import (
    ControlLoop,
    Lookahead,
    LookaheadCriterion,
    Policy,
)
from dtb.domain.economics import (
    EconomicBasis,
    EconomicDirection,
    EconomicItem,
    EconomicKind,
)
from dtb.simulation import simulate
from tests.conftest import build_control_model

SCN = Scenario(id="s", name="s", enterprise_id="ctl", seed=11)


def build_lookahead_model(horizon: float = 120.0, **spec_kwargs):
    model = build_control_model(horizon=horizon, timer_period=20.0, rule="true")
    model.policies["pol"].lookahead = Lookahead(
        horizon=spec_kwargs.pop("lookahead_horizon", 40.0),
        objective=spec_kwargs.pop("objective", "completed('work')"),
        **spec_kwargs,
    )
    return model


# ----------------------------------------------------------- просмотр вперёд --


def test_lookahead_compares_candidates_against_doing_nothing():
    """В журнале видно и оценку «ничего не делать», и оценку действия."""
    result = simulate(build_lookahead_model(), SCN, seed=11)
    assert result.lookahead_log
    entry = result.lookahead_log[0]
    actions = {score["action"] for score in entry["scores"]}
    assert None in actions and "add_line" in actions
    assert entry["gain_vs_noop"] == pytest.approx(
        next(s["score"] for s in entry["scores"] if s["action"] == "add_line")
        - next(s["score"] for s in entry["scores"] if s["action"] is None)
    )
    assert entry["fired"] is True
    assert entry["horizon"] == 40.0


def test_lookahead_rejects_action_without_gain():
    """Если действие не улучшает критерий, политика его не применяет."""
    model = build_lookahead_model(objective="stock('order')")
    # запас заказов от расширения линии только убывает: действие невыгодно
    model.policies["pol"].lookahead.maximize = True
    result = simulate(model, SCN, seed=11)
    assert result.lookahead_log
    assert all(entry["fired"] is False for entry in result.lookahead_log)
    assert result.metrics["policy_firings"] == 0
    assert not any(
        e["type"] == "control_action_applied" for e in result.event_log
    )


def test_min_gain_filters_marginal_actions():
    """Порог выгоды отсекает действия, выигрыш которых мал."""
    without = simulate(build_lookahead_model(), SCN, seed=11)
    with_threshold = simulate(build_lookahead_model(min_gain=1000.0), SCN, seed=11)
    assert without.metrics["policy_firings"] > 0
    assert with_threshold.metrics["policy_firings"] == 0


def test_lookahead_is_reproducible():
    a = simulate(build_lookahead_model(runs=3), SCN, seed=11)
    b = simulate(build_lookahead_model(runs=3), SCN, seed=11)
    assert a.content_digest() == b.content_digest()
    assert a.lookahead_log == b.lookahead_log


def test_lookahead_does_not_disturb_the_main_run():
    """Клон не влияет на основной прогон: без просмотра траектория та же."""
    plain = simulate(
        build_control_model(horizon=120.0, timer_period=20.0, rule="true"), SCN, seed=11
    )
    # просмотр с единственным кандидатом и нулевым порогом принимает то же решение
    looked = simulate(build_lookahead_model(), SCN, seed=11)
    assert looked.final_kpis["done"] == plain.final_kpis["done"]
    assert looked.metrics["np_events"] == plain.metrics["np_events"]


def test_lookahead_runs_several_futures():
    single = simulate(build_lookahead_model(runs=1), SCN, seed=11)
    multiple = simulate(build_lookahead_model(runs=4), SCN, seed=11)
    assert len(single.lookahead_log[0]["scores"][0]["samples"]) == 1
    assert len(multiple.lookahead_log[0]["scores"][0]["samples"]) == 4


def test_criterion_worst_is_more_cautious_than_best():
    """Критерий «худший исход» не оптимистичнее критерия «лучший исход»."""
    worst = simulate(
        build_lookahead_model(runs=4, criterion=LookaheadCriterion.WORST), SCN, seed=11
    )
    best = simulate(
        build_lookahead_model(runs=4, criterion=LookaheadCriterion.BEST), SCN, seed=11
    )
    worst_score = worst.lookahead_log[0]["scores"][-1]["score"]
    best_score = best.lookahead_log[0]["scores"][-1]["score"]
    assert worst_score <= best_score


def test_quantile_criterion_between_worst_and_mean():
    runs = 5
    quantile = simulate(
        build_lookahead_model(
            runs=runs, criterion=LookaheadCriterion.QUANTILE, quantile=0.2
        ),
        SCN, seed=11,
    )
    worst = simulate(
        build_lookahead_model(runs=runs, criterion=LookaheadCriterion.WORST),
        SCN, seed=11,
    )
    mean = simulate(
        build_lookahead_model(runs=runs, criterion=LookaheadCriterion.MEAN),
        SCN, seed=11,
    )
    q_score = quantile.lookahead_log[0]["scores"][-1]["score"]
    assert worst.lookahead_log[0]["scores"][-1]["score"] <= q_score
    assert q_score <= mean.lookahead_log[0]["scores"][-1]["score"]


def test_lookahead_can_optimize_npv():
    """Критерий просмотра может быть экономическим: NPV короткого прогона."""
    model = build_lookahead_model(objective="npv", lookahead_horizon=60.0)
    model.economic_items["fee"] = EconomicItem(
        id="fee", name="Выручка", article="выручка", kind=EconomicKind.ONE_TIME,
        basis=EconomicBasis.SED_COMPLETED, ref="work", rate=400.0,
        direction=EconomicDirection.INCOME,
    )
    model.control_actions["add_line"].one_time_cost = 100.0
    result = simulate(model, SCN, seed=11)
    assert result.lookahead_log
    scores = result.lookahead_log[0]["scores"]
    assert any(abs(s["score"]) > 0 for s in scores)
    assert result.economics["npv"] != 0.0


def test_lookahead_explicit_candidates():
    model = build_lookahead_model()
    model.control_actions["add_two"] = model.control_actions["add_line"].model_copy(
        deep=True, update={"id": "add_two", "name": "Две линии"}
    )
    model.control_actions["add_two"].effect.delta = 2
    model.policies["pol"].lookahead.candidates = ["add_line", "add_two"]
    result = simulate(model, SCN, seed=11)
    entry = result.lookahead_log[0]
    assert {s["action"] for s in entry["scores"]} == {None, "add_line", "add_two"}
    # выбранное действие — лучшее по критерию
    best = max(
        (s for s in entry["scores"] if s["action"]), key=lambda s: s["score"]
    )
    assert entry["action"] == best["action"]


def test_lookahead_nested_inside_rollout_is_disabled():
    """Внутри прогона-просмотра политика не запускает новый просмотр."""
    model = build_lookahead_model(lookahead_horizon=60.0, runs=2)
    result = simulate(model, SCN, seed=11)
    # каждая запись журнала относится к реальному МР, а не к вложенному
    moments = {entry["t"] for entry in result.lookahead_log}
    assert len(moments) == len(result.lookahead_log)


def test_cooldown_applies_to_lookahead_decisions():
    model = build_lookahead_model()
    model.policies["pol"].cooldown_hours = 60.0
    result = simulate(model, SCN, seed=11)
    fired = [e["t"] for e in result.lookahead_log if e["fired"]]
    # антидребезг пускает решения в 0 и 60; в 120 горизонт исчерпан, и просмотр
    # не видит выигрыша — действие не применяется
    assert fired == [0.0, 60.0]
    last = result.lookahead_log[-1]
    assert last["t"] == 120.0 and last["gain_vs_noop"] == 0.0


# ------------------------------------------------------------- пороги политик --


def test_threshold_is_read_from_policy():
    model = build_control_model(horizon=100.0, timer_period=10.0, with_policy=False)
    model.policies["pol"] = Policy(
        id="pol", name="Регулирование", loop=ControlLoop.REGULATION,
        rule="waiting_count('work') >= threshold('queue')", action_ref="add_line",
        thresholds={"queue": 2.0},
    )
    low = simulate(model, SCN, seed=11)
    model.policies["pol"].thresholds["queue"] = 20.0
    high = simulate(model, SCN, seed=11)
    assert low.metrics["policy_firings"] > high.metrics["policy_firings"]


def test_missing_threshold_is_reported_not_crashing():
    model = build_control_model(horizon=60.0, timer_period=10.0, with_policy=False)
    model.policies["pol"] = Policy(
        id="pol", name="pol", rule="waiting_count('work') >= threshold('ghost')",
        action_ref="add_line",
    )
    result = simulate(model, SCN, seed=11)
    assert result.status == "completed"
    assert all(entry["fired"] is False for entry in result.policy_trace)
    assert any("ghost" in (entry.get("error") or "") for entry in result.policy_trace)


def test_threshold_override_through_scenario():
    """Порог переопределяется параметром сценария — так его подбирает оптимизатор."""
    model = build_control_model(horizon=100.0, timer_period=10.0, with_policy=False)
    model.policies["pol"] = Policy(
        id="pol", name="pol", rule="waiting_count('work') >= threshold('queue')",
        action_ref="add_line", thresholds={"queue": 2.0},
    )
    scenario = SCN.model_copy(deep=True)
    scenario.parameter_overrides["policy:pol.thresholds.queue"] = 25.0
    tight = simulate(model, SCN, seed=11)
    loose = simulate(model, scenario, seed=11)
    assert loose.metrics["policy_firings"] < tight.metrics["policy_firings"]
    # исходная модель не изменена переопределением
    assert model.policies["pol"].thresholds["queue"] == 2.0
