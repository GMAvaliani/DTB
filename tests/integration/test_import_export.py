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
