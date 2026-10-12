"""Фаза 2: управляющее действие как объект, субъект с полномочиями."""

import pytest

from dtb.domain import EffectKind, EventEffect, Scenario
from dtb.domain.control import ControlActionDef, ControlLoop, Subject
from dtb.domain.scenario import ControlAction
from dtb.simulation import simulate
from tests.conftest import build_control_model

SCN = Scenario(id="s", name="s", enterprise_id="ctl", seed=11)


def model_with(**action_fields):
    """Модель с политикой, которая всегда применяет действие add_line."""
    model = build_control_model(horizon=120.0, timer_period=20.0, rule="true")
    action = model.control_actions["add_line"]
    for key, value in action_fields.items():
        setattr(action, key, value)
    return model


def applied_times(result, name="add_line"):
    return [
        entry["t"]
        for entry in result.event_log
        if entry["type"] == "control_action_applied" and entry["name"] == name
    ]


def logged(result, kind):
    return [entry for entry in result.event_log if entry["type"] == kind]


# ------------------------------------------------------- задержка активации --


def test_activation_delay_postpones_effect():
    """Решение принято в МР, эффект наступает через activation_delay."""
    result = simulate(model_with(activation_delay=5.0), SCN, seed=11)
    scheduled = logged(result, "control_action_scheduled")
    assert scheduled, "действие должно быть сначала отложено"
    assert scheduled[0]["details"]["activates_at"] == pytest.approx(
        scheduled[0]["t"] + 5.0
    )
    assert applied_times(result)[0] == pytest.approx(scheduled[0]["t"] + 5.0)


def test_zero_delay_applies_immediately():
    result = simulate(model_with(activation_delay=0.0), SCN, seed=11)
    assert not logged(result, "control_action_scheduled")
    assert applied_times(result)[0] == 0.0


def test_delayed_action_counts_as_decision_for_cooldown():
    """Отложенное действие уже израсходовало решение: антидребезг работает."""
    model = model_with(activation_delay=10.0)
    model.policies["pol"].cooldown_hours = 60.0
    result = simulate(model, SCN, seed=11)
    firings = result.metrics["policy_firings"]
    assert firings == 3  # МР t=0..120 через 20 ч, антидребезг пускает 0, 60, 120
    assert len(logged(result, "control_action_scheduled")) == firings


# ------------------------------------------------- срок действия и откат --


def test_term_limits_resource_effect():
    """Срок обратимого действия ограничивает эффект по ёмкости ресурса."""
    model = model_with(term=12.0)
    model.control_actions["add_line"].effect.duration = None
    model.policies["pol"].cooldown_hours = 100.0
    result = simulate(model, SCN, seed=11)
    change = logged(result, "resource_capacity_change")[0]
    assert change["details"]["until"] == pytest.approx(change["t"] + 12.0)
    assert result.resource_stats["line"]["final_capacity"] == 1  # срок истёк


def test_irreversible_action_ignores_term():
    """Необратимое действие остаётся навсегда, срок к нему не применяется."""
    model = model_with(term=12.0, reversible=False)
    model.control_actions["add_line"].effect.duration = None
    model.policies["pol"].cooldown_hours = 500.0
    result = simulate(model, SCN, seed=11)
    change = logged(result, "resource_capacity_change")[0]
    assert change["details"]["until"] is None
    assert result.resource_stats["line"]["final_capacity"] == 2


def test_reversible_parameter_change_is_rolled_back():
    """Обратимое изменение параметра возвращается к прежнему значению."""
    model = build_control_model(horizon=100.0, timer_period=50.0, rule="true")
    model.control_actions["boost"] = ControlActionDef(
        id="boost", name="Ускорение потока", loop=ControlLoop.REGULATION,
        effect=EventEffect(
            kind=EffectKind.SET_PARAMETER,
            parameter_path="event_kind:arrive.stream.rate_per_hour", value=2.0,
        ),
        term=10.0, reversible=True,
    )
    model.policies["pol"].action_ref = "boost"
    model.policies["pol"].cooldown_hours = 90.0
    result = simulate(model, SCN, seed=11)
    reverted = logged(result, "control_action_reverted")
    assert reverted and reverted[0]["details"]["value"] == pytest.approx(0.5)
    assert reverted[0]["t"] == pytest.approx(10.0)


# ---------------------------------------------------- вероятность успеха --


def test_success_probability_can_fail_action():
    """Действие с вероятностью успеха 0 не применяется, но фиксируется."""
    result = simulate(model_with(success_probability=0.0), SCN, seed=11)
    assert logged(result, "control_action_failed")
    assert not applied_times(result)
    assert result.resource_stats["line"]["final_capacity"] == 1


def test_success_probability_is_reproducible_substream():
    """Розыгрыш успеха идёт из субпотока: одинаковый seed — одинаковый исход."""
    a = simulate(model_with(success_probability=0.5), SCN, seed=11)
    b = simulate(model_with(success_probability=0.5), SCN, seed=11)
    assert a.content_digest() == b.content_digest()
    assert len(logged(a, "control_action_failed")) == len(logged(b, "control_action_failed"))
    # часть попыток удалась, часть нет — розыгрыш действительно работает
    assert logged(a, "control_action_failed") and applied_times(a)


def test_different_seeds_give_different_outcomes():
    a = simulate(model_with(success_probability=0.5), SCN, seed=11)
    b = simulate(model_with(success_probability=0.5), SCN, seed=12345)
    assert len(logged(a, "control_action_failed")) != len(
        logged(b, "control_action_failed")
    ) or a.content_digest() != b.content_digest()


# ---------------------------------------------------- полномочия субъекта --


def test_action_without_subject_is_rejected():
    """Действие требует полномочий, субъект не задан — отклонено."""
    result = simulate(model_with(min_authority_level=2), SCN, seed=11)
    assert result.rejected_actions
    assert result.rejected_actions[0]["reason"] == "no_subject"
    assert not applied_times(result)
    assert result.metrics["rejected_actions"] == len(result.rejected_actions)


def test_insufficient_authority_is_rejected():
    model = model_with(min_authority_level=3, subject_ref="junior")
    model.subjects["junior"] = Subject(id="junior", name="Мастер", authority_level=1)
    result = simulate(model, SCN, seed=11)
    assert result.rejected_actions[0]["reason"] == "insufficient_authority"
    assert result.rejected_actions[0]["authority_level"] == 1
    assert result.rejected_actions[0]["required_authority"] == 3


def test_sufficient_authority_applies():
    model = model_with(min_authority_level=2, subject_ref="boss")
    model.subjects["boss"] = Subject(id="boss", name="Директор", authority_level=3)
    result = simulate(model, SCN, seed=11)
    assert not result.rejected_actions
    assert applied_times(result)
    assert logged(result, "control_action_applied")[0]["details"]["subject"] == "boss"


def test_policy_subject_overrides_action_subject():
    """Субъект политики важнее субъекта по умолчанию у действия."""
    model = model_with(min_authority_level=3, subject_ref="junior")
    model.subjects["junior"] = Subject(id="junior", name="Мастер", authority_level=1)
    model.subjects["boss"] = Subject(id="boss", name="Директор", authority_level=3)
    model.policies["pol"].subject_ref = "boss"
    result = simulate(model, SCN, seed=11)
    assert not result.rejected_actions
    assert logged(result, "control_action_applied")[0]["details"]["subject"] == "boss"


def test_unknown_subject_is_rejected():
    result = simulate(model_with(subject_ref="ghost"), SCN, seed=11)
    assert result.rejected_actions[0]["reason"] == "unknown_subject"


def test_scenario_action_carries_subject():
    """Полномочия проверяются и для действия, привязанного ко времени."""
    model = build_control_model(with_policy=False, with_decision_point=False)
    model.control_actions["add_line"].min_authority_level = 2
    model.subjects["junior"] = Subject(id="junior", name="Мастер", authority_level=1)
    scenario = SCN.model_copy(deep=True)
    scenario.control_actions = [
        ControlAction(id="c", time=30.0, action_ref="add_line", subject_ref="junior")
    ]
    result = simulate(model, scenario, seed=11)
    assert result.rejected_actions[0]["reason"] == "insufficient_authority"
    assert result.rejected_actions[0]["source"] == "scenario"


# ------------------------------------------------------------- стоимость --


def test_costs_are_accumulated_by_action_and_loop():
    """Разовая стоимость — за попытку, повременная — за часы действия."""
    model = model_with(one_time_cost=1000.0, recurring_cost=10.0, term=20.0)
    model.control_actions["add_line"].effect.duration = None
    model.policies["pol"].cooldown_hours = 100.0
    result = simulate(model, SCN, seed=11)
    costs = result.control_costs
    assert costs["one_time"] == pytest.approx(2000.0)  # решения в t=0 и t=100
    assert costs["recurring"] == pytest.approx(2 * 20.0 * 10.0)  # два срока по 20 ч
    assert costs["total"] == pytest.approx(costs["one_time"] + costs["recurring"])
    assert costs["by_loop"]["regulation"] == pytest.approx(costs["total"])
    assert costs["by_action"]["add_line"]["attempts"] == 2
    assert result.metrics["control_cost_total"] == pytest.approx(costs["total"])


def test_failed_attempt_still_costs_one_time():
    """Неудачная попытка списывает разовую стоимость, но не повременную."""
    model = model_with(one_time_cost=500.0, recurring_cost=10.0,
                       success_probability=0.0)
    result = simulate(model, SCN, seed=11)
    assert result.control_costs["one_time"] > 0
    assert result.control_costs["recurring"] == 0.0


def test_rejected_action_costs_nothing():
    """Отклонённое по полномочиям действие не стоит ничего."""
    model = model_with(one_time_cost=500.0, min_authority_level=5)
    result = simulate(model, SCN, seed=11)
    assert result.rejected_actions
    assert result.control_costs["total"] == 0.0


def test_open_ended_recurring_cost_runs_to_horizon():
    """Бессрочное действие тарифицируется до конца горизонта."""
    model = model_with(recurring_cost=10.0, reversible=False)
    model.control_actions["add_line"].effect.duration = None
    model.policies["pol"].cooldown_hours = 1000.0
    result = simulate(model, SCN, seed=11)
    assert result.control_costs["recurring"] == pytest.approx(120.0 * 10.0)
