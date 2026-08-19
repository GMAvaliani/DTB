import pytest

from dtb.domain import Scenario, UtilityType
from dtb.persistence import Database, diff_snapshots
from tests.conftest import build_small_model


@pytest.fixture()
def db(tmp_path):
    return Database(url=f"sqlite:///{tmp_path}/test.db")


def test_model_roundtrip(db):
    model = build_small_model()
    db.save_model(model)
    loaded = db.load_model("acme")
    assert loaded.content_hash() == model.content_hash()
    assert db.list_enterprises()[0]["id"] == "acme"


def test_versioning_and_diff(db):
    model = build_small_model()
    db.save_model(model)
    db.save_version(model, author="tester", comment="v1")
    model.enterprise.model_version = 2
    model.utility_types["extra"] = UtilityType(id="extra", name="Extra")
    model.resources["line"].capacity = 9
    db.save_model(model)
    db.save_version(model, author="tester", comment="v2")

    versions = db.list_versions("acme")
    assert [v["version"] for v in versions] == [2, 1]
    diff = diff_snapshots(
        db.get_version_snapshot("acme", 1), db.get_version_snapshot("acme", 2)
    )
    assert diff["collections"]["utility_types"]["added"] == ["extra"]
    changed = diff["collections"]["resources"]["changed"][0]
    assert changed["id"] == "line"
    assert any(c["field"] == "capacity" and c["to"] == 9 for c in changed["changes"])


def test_scenarios_and_simulations(db):
    model = build_small_model()
    db.save_model(model)
    scenario = Scenario(id="s1", name="S1", enterprise_id="acme")
    db.save_scenario(scenario)
    assert db.load_scenario("s1").name == "S1"
    db.create_simulation("sim1", "acme", "s1", "single", label="test")
    db.update_simulation("sim1", status="completed", result={"kind": "single"}, finished=True)
    record = db.get_simulation("sim1")
    assert record["status"] == "completed"
    assert record["finished_at"]
    assert db.list_simulations("acme")[0]["id"] == "sim1"


def test_audit_and_series(db):
    db.audit("tester", "entity_created", "acme", "sed", "x", details={"a": 1})
    entries = db.audit_entries("acme")
    assert entries[0]["action"] == "entity_created"
    db.append_series("acme", "actual", "revenue", [(0, 1), (1, 2)], source="t")
    db.append_series("acme", "actual", "revenue", [(0, 5)], source="t")  # replace
    rows = db.get_series("acme", "actual", "revenue")
    assert [(r["t"], r["value"]) for r in rows] == [(0, 5)]


def test_delete_enterprise_cascades(db):
    model = build_small_model()
    db.save_model(model)
    db.save_scenario(Scenario(id="s1", name="S1", enterprise_id="acme"))
    db.save_version(model)
    assert db.delete_enterprise("acme")
    assert db.load_model("acme") is None
    assert db.list_scenarios("acme") == []
    assert db.list_versions("acme") == []
