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
