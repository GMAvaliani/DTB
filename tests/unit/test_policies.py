"""Фаза 1.2 и 1.4: политики трёх контуров, замыкание контура, связь с действием."""

import pytest

from dtb.domain import EffectKind, EventEffect, Scenario
from dtb.domain.control import (
    ControlActionDef,
    ControlLoop,
    DecisionPoint,
    DecisionPointKind,
    Policy,
    PolicyRule,
)
from dtb.domain.scenario import ControlAction
from dtb.simulation import simulate
from tests.conftest import build_control_model

SCN = Scenario(id="s", name="s", enterprise_id="ctl", seed=11)


def test_policy_applies_action_only_when_condition_true():
    """Политика применяет действие только при истинном условии правила."""
    never = simulate(build_control_model(rule="false"), SCN, seed=11)
    always = simulate(build_control_model(rule="true"), SCN, seed=11)
    assert never.metrics["policy_firings"] == 0
    assert always.metrics["policy_firings"] > 0
    # условие проверялось в обоих случаях — вызовы политики были
    assert len(never.policy_trace) == len(always.policy_trace) > 0
    assert all(entry["fired"] is False for entry in never.policy_trace)


def test_policy_action_changes_state_and_next_decision_sees_it():
    """Действие политики изменяет состояние, и следующий МР видит изменение."""
    model = build_control_model(horizon=60.0, timer_period=10.0, rule="utilization('line') >= 0")
    model.control_actions["add_line"].effect = EventEffect(
        kind=EffectKind.ADD_UTILITY, utility_type_id="order", quantity_expression="5"
    )
    model.policies["pol"].rule = "stock('order') < 4"
    result = simulate(model, SCN, seed=11)
    fired = [entry for entry in result.policy_trace if entry["fired"]]
    assert fired, "политика должна была сработать хотя бы раз"
    # после пополнения запаса условие перестаёт выполняться на следующем МР
    order_of_entries = [entry["fired"] for entry in result.policy_trace]
    assert False in order_of_entries, "контур замкнулся: состояние изменилось и условие сбросилось"


def test_closed_loop_differs_from_open_loop_scenario():
    """Замкнутый контур даёт траекторию, отличную от разомкнутого сценария."""
    closed = simulate(build_control_model(rule="waiting_count('work') >= 2"), SCN, seed=11)
    # разомкнутый: то же действие, но привязанное ко времени, без политики
    open_model = build_control_model(with_policy=False, with_decision_point=False)
    open_scn = SCN.model_copy(deep=True)
    open_scn.control_actions = [
        ControlAction(id="ca", time=50.0, action_ref="add_line", label="по расписанию")
    ]
    opened = simulate(open_model, open_scn, seed=11)
    assert closed.final_kpis["done"] != opened.final_kpis["done"]
    assert closed.metrics["policy_firings"] > 0
    assert opened.metrics["policy_firings"] == 0


def test_policy_trace_is_reproducible():
    """Два прогона с одним seed дают побитово одинаковый policy-trace."""
    a = simulate(build_control_model(), SCN, seed=11)
    b = simulate(build_control_model(), SCN, seed=11)
    assert a.policy_trace == b.policy_trace
    assert a.decision_log == b.decision_log
    assert a.content_digest() == b.content_digest()


def test_loops_differ_by_firing_frequency():
    """Контуры разнесены по горизонту: regulation чаще organization чаще development."""
    model = build_control_model(horizon=720.0, with_policy=False, with_decision_point=False)
    periods = {
        ControlLoop.REGULATION: 4.0,
        ControlLoop.ORGANIZATION: 48.0,
        ControlLoop.DEVELOPMENT: 240.0,
    }
    for loop, period in periods.items():
        model.policies[loop.value] = Policy(
            id=loop.value, name=loop.value, loop=loop, rule="true", action_ref="add_line"
        )
        model.decision_points[loop.value] = DecisionPoint(
            id=loop.value, name=loop.value, kind=DecisionPointKind.TIMER,
            timer_period=period, policy_ref=loop.value,
        )
    result = simulate(model, SCN, seed=11)
    fired = {}
    for entry in result.policy_trace:
        if entry["fired"]:
            fired[entry["loop"]] = fired.get(entry["loop"], 0) + 1
    assert fired["regulation"] > fired["organization"] > fired["development"]


def test_policy_ref_restricts_decision_point():
    model = build_control_model(horizon=100.0, rule="true")
    model.policies["other"] = Policy(
        id="other", name="Другая", rule="true", action_ref="add_line"
    )
    model.decision_points["dp"].policy_ref = "pol"
    result = simulate(model, SCN, seed=11)
    assert {entry["policy"] for entry in result.policy_trace} == {"pol"}


def test_cooldown_limits_firing_rate():
    frequent = simulate(build_control_model(rule="true", cooldown=None), SCN, seed=11)
    throttled = simulate(build_control_model(rule="true", cooldown=50.0), SCN, seed=11)
    assert throttled.metrics["policy_firings"] < frequent.metrics["policy_firings"]


def test_policy_rules_branches_pick_first_match():
    model = build_control_model(horizon=60.0, with_policy=False)
    model.control_actions["other"] = ControlActionDef(
        id="other", name="Другое",
        effect=EventEffect(
            kind=EffectKind.ADD_UTILITY, utility_type_id="order", quantity_expression="1"
        ),
    )
    model.policies["multi"] = Policy(
        id="multi", name="С ветвями",
        rules=[
            PolicyRule(condition="false", action_ref="add_line"),
            PolicyRule(condition="true", action_ref="other"),
        ],
    )
    result = simulate(model, SCN, seed=11)
    applied = [e for e in result.policy_trace if e["fired"]]
    assert applied and all(e["action"] == "other" for e in applied)


def test_magnitude_expression_overrides_effect_value():
    model = build_control_model(horizon=40.0, with_policy=False, timer_period=10.0)
    model.policies["mag"] = Policy(
        id="mag", name="С величиной",
        rules=[
            PolicyRule(
                condition="true", action_ref="add_line", magnitude_expression="3"
            )
        ],
    )
    result = simulate(model, SCN, seed=11)
    changes = [
        entry for entry in result.event_log if entry["type"] == "resource_capacity_change"
    ]
    assert changes and all(entry["details"]["delta"] == 3.0 for entry in changes)


# ------------------------------------------------------------------ фаза 1.4 --


def test_time_bound_action_still_works():
    """Действие, привязанное ко времени, работает как раньше (регресс)."""
    model = build_control_model(with_policy=False, with_decision_point=False)
    scenario = SCN.model_copy(deep=True)
    scenario.control_actions = [
        ControlAction(
            id="ca", time=30.0,
            effect=EventEffect(
                kind=EffectKind.RESOURCE_CAPACITY_DELTA, resource_id="line", delta=2
            ),
        )
    ]
    result = simulate(model, scenario, seed=11)
    assert any(entry["type"] == "control_action" for entry in result.event_log)
    assert result.resource_stats["line"]["final_capacity"] == 3


def test_action_reference_from_scenario_uses_catalogue():
    model = build_control_model(with_policy=False, with_decision_point=False)
    scenario = SCN.model_copy(deep=True)
    scenario.control_actions = [ControlAction(id="ca", time=30.0, action_ref="add_line")]
    result = simulate(model, scenario, seed=11)
    assert any(
        entry["type"] == "control_action_applied" for entry in result.event_log
    )


def test_policy_bound_action_does_not_fire_without_decision_moment():
    """Действие, привязанное к политике, не срабатывает без МР."""
    model = build_control_model(with_decision_point=False, rule="true")
    result = simulate(model, SCN, seed=11)
    assert result.metrics["policy_firings"] == 0
    assert not any(
        entry["type"] == "control_action_applied" for entry in result.event_log
    )


def test_policy_wins_over_scheduled_action_at_same_moment():
    """При конфликте приоритет у политики: действие не дублируется."""
    model = build_control_model(horizon=60.0, timer_period=20.0, rule="true")
    scenario = SCN.model_copy(deep=True)
    scenario.control_actions = [
        ControlAction(id="ca", time=20.0, action_ref="add_line", label="дубль")
    ]
    result = simulate(model, scenario, seed=11)
    skipped = [e for e in result.event_log if e["type"] == "control_action_skipped"]
    assert skipped and skipped[0]["details"]["reason"] == "policy_priority"


def test_policy_requires_action_or_rules():
    with pytest.raises(ValueError):
        Policy(id="bad", name="Без действия")
