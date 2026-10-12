from dtb.domain import Enterprise, EnterpriseModel
from dtb.integration import (
    MockERPConnector,
    export_graph_json,
    export_model_json,
    export_workbook,
    import_csv,
    import_model_json,
    import_workbook,
    sync_connector,
)
from dtb.validation import validate_model
from tests.conftest import build_small_model


def test_excel_roundtrip_preserves_model(small_model):
    data = export_workbook(small_model)
    target = EnterpriseModel(enterprise=Enterprise(id="copy", name="Copy"))
    report = import_workbook(target, data)
    assert report.ok, report.errors
    assert set(target.seds) == set(small_model.seds)
    assert set(target.utility_types) == set(small_model.utility_types)
    material_flow = next(
        f for f in target.seds["production"].inputs if f.utility_type_id == "material"
    )
    assert material_flow.quantity == 5
    assert target.seds["production"].duration.kind.value == "triangular"
    assert target.goals["g1"].target_metrics[0].target_value == 1000
    assert target.initial_state.utility_stocks["material"] == 500
    assert validate_model(target).ok_to_simulate


def test_excel_roundtrip_demo_model():
    from dtb.demo import build_demo_model

    demo = build_demo_model()
    data = export_workbook(demo)
    target = EnterpriseModel(enterprise=Enterprise(id="copy", name="Copy"))
    report = import_workbook(target, data)
    assert report.ok, report.errors
    assert set(target.seds) == set(demo.seds)
    # branches survive (QC pass/fail) and effects survive
    assert len(target.seds["quality_control"].outcome_branches) == 2
    assert target.event_kinds["equipment_failure"].effects[0].delta == -1
    assert validate_model(target).ok_to_simulate


def test_csv_import_with_row_errors(small_model):
    csv_text = (
        "id,name,kind,unit\n"
        "widget,Widget,product,шт\n"
        ",NoId,product,шт\n"
    )
    report = import_csv(small_model, "utility_types", csv_text)
    assert report.created.get("utility_type") == 1
    assert len(report.errors) == 1
    assert report.errors[0]["row"] == 3


def test_csv_sed_compact_syntax(small_model):
    csv_text = (
        "id,name,inputs,outputs,duration,triggers,priority\n"
        'pack,Packing,"product:1","money:5",fixed:2,"on_sed_completed:production",50\n'
    )
    report = import_csv(small_model, "seds", csv_text)
    assert report.ok, report.errors
    sed = small_model.seds["pack"]
    assert sed.inputs[0].utility_type_id == "product"
    assert sed.duration.value == 2
    assert sed.triggers[0].sed_id == "production"


def test_json_roundtrip(small_model):
    text = export_model_json(small_model)
    model, report = import_model_json(text)
    assert report.ok
    assert model.content_hash() == small_model.content_hash()


def test_json_import_rejects_id_mismatch(small_model):
    other = build_small_model()
    other.enterprise.id = "other"
    _, report = import_model_json(export_model_json(other), existing=small_model)
    assert not report.ok


def test_json_import_reports_validation_errors():
    model, report = import_model_json('{"enterprise": {"name": 5}}')
    assert model is None
    assert report.errors


def test_graph_json_export(small_model):
    import json

    data = json.loads(export_graph_json(small_model))
    assert {n["id"] for n in data["nodes"]} >= {"sed:production", "utility_type:money"}
    assert any(r["type"] == "PRODUCES" for r in data["relations"])


def test_mock_erp_sync_normalizes(small_model):
    report = sync_connector(small_model, MockERPConnector())
    assert report.ok or not report.errors
    # material stock only lands for utilities described in the model
    assert "emp_ivanova" in small_model.employees
    assert "eq_line_1" in small_model.equipment
    assert small_model.erp_objects  # raw atomic objects stored
    assert any(m.entity_type == "employee" for m in small_model.erp_mappings)
    assert small_model.initial_state.source == "erp:mock_1c"
    # unknown utility types are warned about, not silently created
    assert any("raw_material" in w["message"] for w in report.warnings)


def test_excel_roundtrip_control_loop():
    """Контур управления переносится через Excel без потерь (фаза 1, ТЗ 0.4)."""
    from dtb.demo import build_demo_model

    demo = build_demo_model()
    target = EnterpriseModel(enterprise=Enterprise(id="copy", name="Copy"))
    report = import_workbook(target, export_workbook(demo))
    assert report.ok, report.errors
    assert set(target.subjects) == set(demo.subjects)
    assert set(target.control_actions) == set(demo.control_actions)
    assert set(target.policies) == set(demo.policies)
    assert set(target.decision_points) == set(demo.decision_points)
    assert set(target.observations) == set(demo.observations)
    action = target.control_actions["second_shift_crew"]
    assert action.loop.value == "organization"
    assert action.effect.resource_id == "production_line"
    assert action.activation_delay == 48
    assert action.one_time_cost == demo.control_actions['second_shift_crew'].one_time_cost
    assert action.term == 336
    assert target.control_actions["third_line"].reversible is False
    assert target.control_actions["third_line"].success_probability == 0.85
    # экономика (фаза 3)
    assert set(target.economic_items) == set(demo.economic_items)
    assert target.economics.discount_rate_annual == demo.economics.discount_rate_annual
    assert target.economics.currency == demo.economics.currency
    item = target.economic_items["revenue_money"]
    assert item.kind.value == "one_time" and item.direction.value == "income"
    assert item.basis.value == "utility_produced" and item.ref == "money"
    assert target.economic_items["wip_cost"].basis.value == "wip"
    assert target.policies["pol_overtime"].cooldown_hours == 48
    assert target.policies["pol_overtime"].action_ref == "overtime_shift"
    dp = target.decision_points["dp_shift"]
    assert dp.kind.value == "timer" and dp.timer_period == 24 and dp.timer_start == 8
    assert target.decision_points["dp_failure"].event_ref == "equipment_failure"
    obs = target.observations["obs_line_load"]
    assert obs.estimator.value == "smooth" and obs.delay == 8 and obs.relative_error == 0.1
    assert target.subjects["ceo"].authority_level == 3
    assert validate_model(target).ok_to_simulate


def test_csv_policy_rules_compact_syntax(small_model):
    """Правила политики читаются из компактной записи ``условие -> действие @ величина``."""
    from dtb.integration import import_csv

    small_model.control_actions.clear()
    import_csv(
        small_model,
        "control_actions",
        'id,name,effect\n'
        'add_line,Линия,"{""kind"": ""resource_capacity_delta"", '
        '""resource_id"": ""line"", ""delta"": 1}"\n',
    )
    report = import_csv(
        small_model,
        "policies",
        "id,name,loop,rules,priority\n"
        'p1,Расширение,organization,"stock(\'material\') < 50 -> add_line @ 2; '
        'true -> add_line",20\n',
    )
    assert report.ok, report.errors
    policy = small_model.policies["p1"]
    assert policy.loop.value == "organization"
    assert len(policy.rules) == 2
    assert policy.rules[0].condition == "stock('material') < 50"
    assert policy.rules[0].action_ref == "add_line"
    assert policy.rules[0].magnitude_expression == "2"
    assert policy.rules[1].magnitude_expression is None
