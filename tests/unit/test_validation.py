from dtb.domain import (
    DurationKind,
    DurationModel,
    Goal,
    Scenario,
    SEDDefinition,
    TargetMetric,
    TriggerSpec,
    TriggerKind,
    UtilityFlow,
)
from dtb.validation import data_quality_report, validate_model, validate_scenario
from tests.conftest import build_small_model


def test_clean_model_passes(small_model):
    report = validate_model(small_model)
    assert report.ok_to_simulate
    assert not report.critical


def test_broken_references_are_critical(small_model):
    small_model.seds["bad"] = SEDDefinition(
        id="bad", name="bad",
        inputs=[UtilityFlow(utility_type_id="ghost", quantity=1)],
        triggers=[TriggerSpec(kind=TriggerKind.ON_EVENT, event_kind_id="no_event")],
    )
    report = validate_model(small_model)
    assert not report.ok_to_simulate
    codes = {i.code for i in report.critical}
    assert "broken_ref" in codes


def test_goal_hierarchy_cycle_detected(small_model):
    small_model.goals["g1"].parent_id = "g2"
    small_model.goals["g2"] = Goal(id="g2", name="g2", parent_id="g1")
    report = validate_model(small_model)
    assert any(i.code == "hierarchy_cycle" for i in report.critical)


def test_zero_duration_cycle_detected(small_model):
    small_model.seds["a"] = SEDDefinition(
        id="a", name="a", duration=DurationModel(kind=DurationKind.FIXED, value=0),
        triggers=[TriggerSpec(kind=TriggerKind.ON_SED_COMPLETED, sed_id="b")],
    )
    small_model.seds["b"] = SEDDefinition(
        id="b", name="b", duration=DurationModel(kind=DurationKind.FIXED, value=0),
        triggers=[TriggerSpec(kind=TriggerKind.ON_SED_COMPLETED, sed_id="a")],
    )
    report = validate_model(small_model)
    assert any(i.code == "zero_duration_cycle" for i in report.critical)


def test_invalid_kpi_expression_critical(small_model):
    small_model.kpis["bad"] = small_model.kpis["revenue"].model_copy(
        update={"id": "bad", "expression": "import os"}
    )
    report = validate_model(small_model)
    assert any(i.code == "invalid_expression" for i in report.critical)


def test_unreachable_sed_warning(small_model):
    small_model.seds["orphan"] = SEDDefinition(id="orphan", name="orphan")
    report = validate_model(small_model)
    assert any(i.code == "sed_unreachable" for i in report.issues)
    assert report.ok_to_simulate  # warning, not critical


def test_scenario_validation(small_model):
    bad = Scenario(id="s", name="s", enterprise_id="acme",
                   resource_capacity_overrides={"ghost": 1})
    report = validate_scenario(small_model, bad)
    assert not report.ok_to_simulate


def test_data_quality_flags_unsourced_consumption(small_model):
    small_model.initial_state.utility_stocks.pop("material")
    dq = data_quality_report(small_model)
    assert any(
        i["check"] == "broken_references" and i["entity_id"] == "material"
        for i in dq["issues"]
    )
    assert dq["score"] < 100


# ------------------------------------------------- контур управления (фаза 1) --


def _with_action(model):
    from dtb.domain import EffectKind, EventEffect
    from dtb.domain.control import ControlActionDef

    model.control_actions["add_line"] = ControlActionDef(
        id="add_line", name="Линия",
        effect=EventEffect(
            kind=EffectKind.RESOURCE_CAPACITY_DELTA, resource_id="line", delta=1
        ),
    )
    return model


def test_policy_with_unknown_action_is_critical(small_model):
    from dtb.domain.control import Policy

    small_model.policies["p"] = Policy(
        id="p", name="p", rule="true", action_ref="ghost_action"
    )
    report = validate_model(small_model)
    assert any(
        i.code == "broken_ref" and i.entity_type == "policy" for i in report.critical
    )


def test_control_action_with_unknown_resource_is_critical(small_model):
    from dtb.domain import EffectKind, EventEffect
    from dtb.domain.control import ControlActionDef

    small_model.control_actions["bad"] = ControlActionDef(
        id="bad", name="bad",
        effect=EventEffect(
            kind=EffectKind.RESOURCE_CAPACITY_DELTA, resource_id="ghost", delta=1
        ),
    )
    assert not validate_model(small_model).ok_to_simulate


def test_decision_point_with_unknown_event_is_critical(small_model):
    from dtb.domain.control import DecisionPoint, DecisionPointKind

    small_model.decision_points["dp"] = DecisionPoint(
        id="dp", name="dp", kind=DecisionPointKind.EVENT, event_ref="ghost_event"
    )
    assert not validate_model(small_model).ok_to_simulate


def test_observation_with_unknown_target_is_critical(small_model):
    from dtb.domain.control import Observation, ObservationTarget

    small_model.observations["o"] = Observation(
        id="o", name="o", target_kind=ObservationTarget.RESOURCE, target_ref="ghost"
    )
    assert not validate_model(small_model).ok_to_simulate


def test_policy_without_decision_point_is_warning(small_model):
    from dtb.domain.control import Policy

    _with_action(small_model)
    small_model.policies["p"] = Policy(
        id="p", name="p", rule="true", action_ref="add_line"
    )
    report = validate_model(small_model)
    assert report.ok_to_simulate
    assert any(i.code == "policy_never_invoked" for i in report.issues)


def test_insufficient_authority_is_warning(small_model):
    from dtb.domain.control import Subject

    _with_action(small_model)
    small_model.subjects["s"] = Subject(id="s", name="s", authority_level=1)
    small_model.control_actions["add_line"].min_authority_level = 3
    small_model.control_actions["add_line"].subject_ref = "s"
    report = validate_model(small_model)
    assert any(i.code == "insufficient_authority" for i in report.issues)
    assert report.ok_to_simulate


def test_policy_invalid_condition_is_critical(small_model):
    from dtb.domain.control import Policy

    _with_action(small_model)
    small_model.policies["p"] = Policy(
        id="p", name="p", rule="waiting_count('production') >=", action_ref="add_line"
    )
    assert not validate_model(small_model).ok_to_simulate


def test_scenario_action_ref_must_exist(small_model):
    from dtb.domain.scenario import ControlAction

    scenario = Scenario(
        id="s", name="s", enterprise_id="acme",
        control_actions=[ControlAction(id="c", time=1.0, action_ref="ghost")],
    )
    assert not validate_scenario(small_model, scenario).ok_to_simulate


def test_rbac_mismatch_is_warning(small_model):
    """Субъект с ролью viewer не должен применять действия (фаза 2)."""
    from dtb.domain.control import Policy, Subject

    _with_action(small_model)
    small_model.subjects["s"] = Subject(
        id="s", name="s", authority_level=3, role_ref="viewer"
    )
    small_model.policies["p"] = Policy(
        id="p", name="p", rule="true", action_ref="add_line", subject_ref="s"
    )
    report = validate_model(small_model)
    assert any(i.code == "rbac_mismatch" for i in report.issues)
    assert report.ok_to_simulate


def test_unknown_rbac_role_rejected():
    from dtb.domain.control import Subject

    import pytest

    with pytest.raises(ValueError, match="роль"):
        Subject(id="s", name="s", role_ref="superuser")


def test_action_requiring_authority_without_subject_is_warning(small_model):
    _with_action(small_model)
    small_model.control_actions["add_line"].min_authority_level = 2
    report = validate_model(small_model)
    assert any(i.code == "no_subject" for i in report.issues)


def test_irreversible_action_with_term_is_warning(small_model):
    _with_action(small_model)
    small_model.control_actions["add_line"].reversible = False
    small_model.control_actions["add_line"].term = 24.0
    report = validate_model(small_model)
    assert any(i.code == "irreversible_term" for i in report.issues)


def test_activation_delay_beyond_term_is_warning(small_model):
    _with_action(small_model)
    small_model.control_actions["add_line"].activation_delay = 48.0
    small_model.control_actions["add_line"].term = 24.0
    report = validate_model(small_model)
    assert any(i.code == "delay_exceeds_term" for i in report.issues)


def test_scenario_subject_must_exist(small_model):
    from dtb.domain.scenario import ControlAction

    _with_action(small_model)
    scenario = Scenario(
        id="s", name="s", enterprise_id="acme",
        control_actions=[
            ControlAction(id="c", time=1.0, action_ref="add_line", subject_ref="ghost")
        ],
    )
    assert not validate_scenario(small_model, scenario).ok_to_simulate


# ------------------------------------------------- экономика (фаза 3) --


def test_economic_item_broken_ref_is_critical(small_model):
    from dtb.domain.economics import EconomicBasis, EconomicItem

    small_model.economic_items["bad"] = EconomicItem(
        id="bad", name="bad", basis=EconomicBasis.RESOURCE_CAPACITY, ref="ghost", rate=1.0
    )
    assert not validate_model(small_model).ok_to_simulate


def test_warmup_longer_than_horizon_is_critical(small_model):
    small_model.economics.warmup_hours = small_model.enterprise.planning_horizon_hours
    assert not validate_model(small_model).ok_to_simulate


def test_long_warmup_is_warning(small_model):
    small_model.economics.warmup_hours = (
        small_model.enterprise.planning_horizon_hours * 0.75
    )
    report = validate_model(small_model)
    assert report.ok_to_simulate
    assert any(i.code == "warmup_long" for i in report.issues)


def test_economics_without_income_is_warning(small_model):
    from dtb.domain.economics import EconomicBasis, EconomicItem

    small_model.economic_items["rent"] = EconomicItem(
        id="rent", name="Аренда", basis=EconomicBasis.RESOURCE_CAPACITY,
        ref="line", rate=10.0,
    )
    report = validate_model(small_model)
    assert any(i.code == "no_income_items" for i in report.issues)
    assert report.ok_to_simulate


def test_scenario_warmup_validated(small_model):
    bad = Scenario(id="s", name="s", enterprise_id="acme", warmup_hours=10_000.0)
    assert not validate_scenario(small_model, bad).ok_to_simulate


# ------------------------------------------------- календари (фаза 5) --


def test_unknown_resource_calendar_is_critical(small_model):
    small_model.resources["line"].calendar_ref = "ghost"
    assert not validate_model(small_model).ok_to_simulate


def test_unknown_default_calendar_is_critical(small_model):
    small_model.enterprise.default_calendar_ref = "ghost"
    assert not validate_model(small_model).ok_to_simulate


def test_calendar_without_shifts_is_info(small_model):
    from dtb.domain.calendars import Calendar

    small_model.calendars["always"] = Calendar(id="always", name="Круглосуточно")
    report = validate_model(small_model)
    assert any(i.code == "calendar_always_on" for i in report.issues)
    assert report.ok_to_simulate


def test_exception_beyond_horizon_is_warning(small_model):
    from dtb.domain.calendars import Calendar, CalendarException

    small_model.calendars["stop"] = Calendar(
        id="stop", name="Остановка",
        exceptions=[CalendarException(from_hour=10_000.0, to_hour=10_024.0)],
    )
    report = validate_model(small_model)
    assert any(i.code == "exception_outside_horizon" for i in report.issues)
