import time

import pytest
from fastapi.testclient import TestClient

from dtb.api import create_app
from dtb.persistence import Database


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DTB_API_KEYS", raising=False)
    app = create_app(Database(url=f"sqlite:///{tmp_path}/api.db"))
    return TestClient(app)


def wait_done(client, sim_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/simulations/{sim_id}").json()["status"]
        if status not in ("pending", "running", "paused"):
            return status
        time.sleep(0.2)
    raise TimeoutError


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["auth"] == "open-dev-mode"


def test_enterprise_crud_and_entities(client):
    r = client.post("/api/enterprises", json={"id": "acme", "name": "ACME"})
    assert r.status_code == 201
    assert client.post("/api/enterprises", json={"id": "acme", "name": "x"}).status_code == 409

    r = client.post("/api/enterprises/acme/entities/utility_type",
                    json={"id": "m", "name": "Money", "kind": "money"})
    assert r.status_code == 200 and r.json()["created"]
    r = client.post("/api/enterprises/acme/entities/utility_type",
                    json={"id": "m", "name": "Money2", "kind": "money"})
    assert not r.json()["created"]  # update
    assert client.get("/api/enterprises/acme/entities/utility_type/m").json()["name"] == "Money2"
    # invalid entity → 422 with pydantic details
    r = client.post("/api/enterprises/acme/entities/utility_type",
                    json={"id": "x", "name": "X", "kind": "not_a_kind"})
    assert r.status_code == 422
    # unknown type → 404
    assert client.get("/api/enterprises/acme/entities/wat").status_code == 404
    assert client.delete("/api/enterprises/acme/entities/utility_type/m").json()["deleted"] == "m"
    # audit recorded
    actions = [a["action"] for a in client.get("/api/audit?enterprise_id=acme").json()]
    assert "entity_created" in actions and "entity_deleted" in actions


def test_simulation_gate_blocks_invalid_model(client):
    client.post("/api/demo")
    # break the model: SED referencing missing utility
    client.post("/api/enterprises/demo_plant/entities/sed", json={
        "id": "broken", "name": "broken",
        "inputs": [{"utility_type_id": "ghost", "quantity": 1}],
        "triggers": [{"kind": "at_time", "at": 0}],
    })
    r = client.post("/api/simulations", json={"scenario_id": "demo_baseline", "kind": "single"})
    assert r.status_code == 422
    assert "model_validation" in r.json()["detail"]


def test_single_simulation_flow(client):
    client.post("/api/demo")
    r = client.post("/api/simulations",
                    json={"scenario_id": "demo_baseline", "kind": "single", "seed": 42})
    assert r.status_code == 202
    sim_id = r.json()["id"]
    assert wait_done(client, sim_id) == "completed"
    state = client.get(f"/api/simulations/{sim_id}/state").json()
    assert state["final_kpis"]["revenue"] > 0
    timeline = client.get(f"/api/simulations/{sim_id}/timeline").json()
    assert timeline["kpi_series"]["revenue"]
    events = client.get(f"/api/simulations/{sim_id}/events").json()
    assert events["event_log"]
    provenance = client.get(f"/api/simulations/{sim_id}").json()["provenance"]
    assert provenance["seed"] == 42
    bottlenecks = client.get(f"/api/simulations/{sim_id}/bottlenecks").json()
    assert isinstance(bottlenecks, list)


def test_sse_stream_terminates(client):
    client.post("/api/demo")
    sim_id = client.post("/api/simulations",
                         json={"scenario_id": "demo_baseline", "kind": "single", "seed": 1}).json()["id"]
    saw_done = False
    with client.stream("GET", f"/api/simulations/{sim_id}/stream") as response:
        for line in response.iter_lines():
            if line.startswith("data:") and '"done": true' in line:
                saw_done = True
                break
    assert saw_done


def test_whatif_and_scenario_validation(client):
    client.post("/api/demo")
    r = client.post("/api/scenarios/demo_baseline/whatif",
                    json={"name": "wf", "resource_capacity_overrides": {"production_line": 3}})
    assert r.status_code == 201
    derived = client.get(f"/api/scenarios/{r.json()['id']}").json()
    assert derived["base_scenario_id"] == "demo_baseline"
    assert derived["resource_capacity_overrides"]["production_line"] == 3
    r = client.post("/api/scenarios/demo_baseline/whatif",
                    json={"name": "bad", "resource_capacity_overrides": {"ghost": 3}})
    assert r.status_code == 422


def test_graph_endpoints(client):
    client.post("/api/demo")
    graph = client.get("/api/enterprises/demo_plant/graph").json()
    assert graph["stats"]["nodes"]["sed"] == 9
    impact = client.get(
        "/api/enterprises/demo_plant/graph/impact/resource:production_line").json()
    assert any(c[-1].startswith("goal:") or c[-1].startswith("kpi:") for c in impact["chains"])
    # custom node + relation
    r = client.post("/api/enterprises/demo_plant/graph/entities",
                    json={"id": "custom:reg", "type": "custom", "label": "Регулятор"})
    assert r.status_code == 200
    r = client.post("/api/enterprises/demo_plant/graph/relations",
                    json={"id": "cr1", "source": "custom:reg",
                          "target": "event_kind:customer_order", "type": "AFFECTS"})
    assert r.status_code == 200
    graph = client.get("/api/enterprises/demo_plant/graph").json()
    assert any(n["id"] == "custom:reg" for n in graph["nodes"])
    r = client.post("/api/enterprises/demo_plant/graph/relations",
                    json={"id": "cr2", "source": "custom:reg", "target": "nope", "type": "AFFECTS"})
    assert r.status_code == 422


def test_versions_flow(client):
    client.post("/api/demo")
    v = client.post("/api/enterprises/demo_plant/versions", json={"comment": "before"}).json()
    client.post("/api/enterprises/demo_plant/entities/resource",
                json={"id": "new_res", "name": "Новый", "capacity": 2})
    diff = client.get(
        f"/api/enterprises/demo_plant/versions-diff?from={v['version']}&to=0").json()
    assert "new_res" in diff["collections"]["resources"]["added"]


def test_auth_roles_and_tenant_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("DTB_API_KEYS", "vkey:viewer,ekey:editor,akey:admin,skey:editor:tenant_a")
    app = create_app(Database(url=f"sqlite:///{tmp_path}/auth.db"))
    client = TestClient(app)
    # no key → 401
    assert client.get("/api/enterprises").status_code == 401
    # viewer can read, cannot write
    assert client.get("/api/enterprises", headers={"X-API-Key": "vkey"}).status_code == 200
    assert client.post("/api/enterprises", headers={"X-API-Key": "vkey"},
                       json={"id": "a", "name": "A"}).status_code == 403
    # editor can write
    assert client.post("/api/enterprises", headers={"X-API-Key": "ekey"},
                       json={"id": "tenant_a", "name": "A"}).status_code == 201
    assert client.post("/api/enterprises", headers={"X-API-Key": "ekey"},
                       json={"id": "tenant_b", "name": "B"}).status_code == 201
    # scoped key sees only its enterprise
    assert client.get("/api/enterprises/tenant_a",
                      headers={"X-API-Key": "skey"}).status_code == 200
    assert client.get("/api/enterprises/tenant_b",
                      headers={"X-API-Key": "skey"}).status_code == 403
    # delete requires admin
    assert client.delete("/api/enterprises/tenant_b",
                         headers={"X-API-Key": "ekey"}).status_code == 403
    assert client.delete("/api/enterprises/tenant_b",
                         headers={"X-API-Key": "akey"}).status_code == 200


def test_control_loop_entities_and_run_endpoints(client):
    """Фаза 1: сущности контура в API, журналы МР/политик/наблюдений в прогоне."""
    client.post("/api/demo")
    # сущности контура доступны через общий маршрут /entities/{etype}
    for etype, expected in [
        ("subject", "ceo"),
        ("control_action", "overtime_shift"),
        ("policy", "pol_overtime"),
        ("decision_point", "dp_shift"),
        ("observation", "obs_line_load"),
    ]:
        listed = client.get(f"/api/enterprises/demo_plant/entities/{etype}").json()
        assert expected in {item["id"] for item in listed}, etype
    counts = client.get("/api/enterprises/demo_plant").json()["counts"]
    assert counts["policy"] == 3 and counts["decision_point"] == 4

    # создание и удаление политики
    r = client.post("/api/enterprises/demo_plant/entities/policy", json={
        "id": "pol_test", "name": "Тестовая", "loop": "regulation",
        "rule": "true", "action_ref": "overtime_shift",
    })
    assert r.status_code == 200 and r.json()["created"]
    assert client.delete(
        "/api/enterprises/demo_plant/entities/policy/pol_test"
    ).json()["deleted"] == "pol_test"
    # политика без действия → 422 на валидации pydantic
    assert client.post("/api/enterprises/demo_plant/entities/policy",
                       json={"id": "bad", "name": "Без действия"}).status_code == 422

    sim_id = client.post("/api/simulations", json={
        "scenario_id": "demo_baseline", "kind": "single", "seed": 20250819
    }).json()["id"]
    assert wait_done(client, sim_id) == "completed"

    decisions = client.get(f"/api/simulations/{sim_id}/decisions").json()
    assert decisions["decision_moments"] > 0
    assert decisions["decision_moments"] < decisions["switching_moments"]
    assert decisions["decision_log"][0]["triggers"]

    trace = client.get(f"/api/simulations/{sim_id}/policy-trace").json()
    assert trace["policy_firings"] > 0
    assert set(trace["by_loop"]) == {"regulation", "organization", "development"}
    only = client.get(
        f"/api/simulations/{sim_id}/policy-trace?policy_id=pol_overtime"
    ).json()
    assert {e["policy"] for e in only["policy_trace"]} == {"pol_overtime"}

    error = client.get(f"/api/simulations/{sim_id}/observation-error").json()
    assert error["observations"]["obs_line_load"]["reads"] > 0
    assert error["observations"]["obs_line_load"]["mean_abs_error"] > 0


def test_twin_versus_pure_observation_difference(client):
    """Цена неполноты наблюдений выводится сравнением двух прогонов."""
    client.post("/api/demo")
    ids = []
    for scenario in ("demo_baseline", "demo_pure_observation"):
        sim_id = client.post("/api/simulations", json={
            "scenario_id": scenario, "kind": "single", "seed": 20250819
        }).json()["id"]
        assert wait_done(client, sim_id) == "completed"
        ids.append(sim_id)
    twin, pure = (
        client.get(f"/api/simulations/{i}/observation-error").json() for i in ids
    )
    assert twin["observation_mode"] == "per_observation"
    assert pure["observation_mode"] == "pure_simulation"
    assert twin["observations"]["obs_prod_queue"]["mean_abs_error"] > 0
    assert pure["observations"]["obs_prod_queue"]["mean_abs_error"] == 0
    compare = client.get(f"/api/simulations/compare?ids={ids[0]},{ids[1]}").json()
    assert compare


def test_rejected_actions_endpoint(client):
    """Фаза 2: отклонённые по полномочиям действия и стоимость управления в API."""
    client.post("/api/demo")
    # поднимаем требование полномочий выше уровня начальника смены
    action = client.get(
        "/api/enterprises/demo_plant/entities/control_action/overtime_shift"
    ).json()
    action["min_authority_level"] = 5
    assert client.post(
        "/api/enterprises/demo_plant/entities/control_action", json=action
    ).status_code == 200
    sim_id = client.post("/api/simulations", json={
        "scenario_id": "demo_baseline", "kind": "single", "seed": 20250819
    }).json()["id"]
    assert wait_done(client, sim_id) == "completed"
    body = client.get(f"/api/simulations/{sim_id}/rejected-actions").json()
    assert body["by_reason"]["insufficient_authority"] > 0
    assert all(e["action"] == "overtime_shift" for e in body["rejected_actions"])
    costs = body["control_costs"]
    assert costs["total"] > 0
    assert "overtime_shift" not in costs["by_action"]  # отклонённое не стоит ничего
    assert set(costs["by_loop"]) <= {"regulation", "organization", "development"}
