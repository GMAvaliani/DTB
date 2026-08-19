"""End-to-end acceptance test (Section 44 of the master specification).

A new user can: create an enterprise → define a goal → create SEDs → create
utilities → link them → define resources → define external events → import
data → create a scenario → run the event-driven simulation → see state change →
see SED results → get a forecast → compare two scenarios → see resource
influence → get bottlenecks → run a simple optimization → see the causal /
knowledge graph → save a model version → re-run with the same seed and get a
reproducible result.
"""

import time

import pytest
from fastapi.testclient import TestClient

from dtb.api import create_app
from dtb.persistence import Database


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DTB_API_KEYS", raising=False)
    app = create_app(Database(url=f"sqlite:///{tmp_path}/e2e.db"))
    return TestClient(app)


def wait(client, path, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = client.get(path).json()
        if record["status"] not in ("pending", "running", "paused"):
            return record
        time.sleep(0.2)
    raise TimeoutError(path)


def test_full_acceptance_chain(client):
    # 1. создать предприятие
    assert client.post("/api/enterprises", json={
        "id": "workshop", "name": "Мастерская", "planning_horizon_hours": 500,
    }).status_code == 201

    # 4. создать полезности (перед СЭД, чтобы ссылки были валидны)
    for utility in [
        {"id": "request", "name": "Заявка", "kind": "information"},
        {"id": "timber", "name": "Доска", "kind": "material", "unit": "шт"},
        {"id": "chair", "name": "Стул", "kind": "product", "unit": "шт"},
        {"id": "cash", "name": "Деньги", "kind": "money", "unit": "руб"},
    ]:
        assert client.post("/api/enterprises/workshop/entities/utility_type",
                           json=utility).status_code == 200

    # KPI + 2. определить цель
    client.post("/api/enterprises/workshop/entities/kpi",
                json={"id": "cash_in", "name": "Выручка", "expression": "produced('cash')"})
    assert client.post("/api/enterprises/workshop/entities/goal", json={
        "id": "g_cash", "name": "Заработать 30000",
        "target_metrics": [{"kpi_id": "cash_in", "target_value": 30000, "comparison": ">="}],
    }).status_code == 200

    # 6. задать ресурсы
    client.post("/api/enterprises/workshop/entities/resource",
                json={"id": "master", "name": "Мастер", "kind": "personnel", "capacity": 1})

    # 7. создать внешние события
    client.post("/api/enterprises/workshop/entities/event_kind", json={
        "id": "client_request", "name": "Заявка клиента", "category": "external",
        "stream": {"kind": "poisson", "rate_per_hour": 0.05},
        "effects": [{"kind": "add_utility", "utility_type_id": "request",
                     "quantity_expression": "1"}],
    })

    # 3+5. создать СЭДы и связать их с полезностями (цепочка make → sell)
    assert client.post("/api/enterprises/workshop/entities/sed", json={
        "id": "make", "name": "Изготовление", "goal_id": "g_cash",
        "inputs": [{"utility_type_id": "request", "quantity": 1},
                   {"utility_type_id": "timber", "quantity": 3}],
        "outputs": [{"utility_type_id": "chair", "quantity": 1}],
        "resources": [{"resource_id": "master", "quantity": 1}],
        "duration": {"kind": "triangular", "low": 5, "high": 15, "mode": 8},
        "triggers": [{"kind": "on_event", "event_kind_id": "client_request"}],
    }).status_code == 200
    client.post("/api/enterprises/workshop/entities/sed", json={
        "id": "sell", "name": "Продажа", "goal_id": "g_cash",
        "inputs": [{"utility_type_id": "chair", "quantity": 1}],
        "outputs": [{"utility_type_id": "cash", "quantity": 2500}],
        "duration": {"kind": "fixed", "value": 2},
        "triggers": [{"kind": "on_sed_completed", "sed_id": "make"}],
    })

    # 8. импортировать исходные данные (начальное состояние через CSV)
    csv = "utility_type_id,quantity\ntimber,200\n"
    r = client.post("/api/enterprises/workshop/import/csv?sheet=initial_state",
                    files={"file": ("init.csv", csv)})
    assert r.json()["ok"], r.json()

    # модель валидна
    assert client.get("/api/enterprises/workshop/validation").json()["ok_to_simulate"]

    # 9. создать сценарии (базовый + рост спроса)
    base_id = client.post("/api/scenarios", json={
        "id": "ws_base", "name": "База", "enterprise_id": "workshop",
        "seed": 42, "monte_carlo_runs": 8,
    }).json()["id"]
    boost_id = client.post("/api/scenarios", json={
        "id": "ws_boost", "name": "Рост спроса", "enterprise_id": "workshop",
        "seed": 42, "monte_carlo_runs": 8,
        "event_rate_multipliers": {"client_request": 2.0},
    }).json()["id"]

    # 10. запустить event-driven симуляцию (Монте-Карло)
    sims = {}
    for scenario_id in (base_id, boost_id):
        r = client.post("/api/simulations", json={
            "scenario_id": scenario_id, "kind": "monte_carlo", "runs": 8, "seed": 42,
        })
        assert r.status_code == 202
        sims[scenario_id] = r.json()["id"]
    for sim_id in sims.values():
        assert wait(client, f"/api/simulations/{sim_id}")["status"] == "completed"

    # 11. увидеть изменение состояния предприятия
    state = client.get(f"/api/simulations/{sims[base_id]}/state").json()
    assert state["final_kpis"]["cash_in"] > 0
    timeline = client.get(f"/api/simulations/{sims[base_id]}/timeline").json()
    series = timeline["kpi_series"]["cash_in"]
    assert series[0][1] == 0 and series[-1][1] > 0  # state evolved over time

    # 12. увидеть результаты СЭД
    assert timeline["sed_stats"]["make"]["completed"] > 0
    assert timeline["sed_stats"]["sell"]["completed"] > 0

    # 13. получить forecast
    forecast = client.get(f"/api/forecasts?scenario_id={base_id}").json()
    assert forecast["kpis"]["cash_in"]["expected_final"] > 0
    probability = forecast["goals"]["g_cash"]["achievement_probability"]
    assert 0.0 <= probability <= 1.0

    # 14. сравнить два сценария / 15. увидеть влияние ресурсов
    cmp = client.get(
        f"/api/simulations/compare?ids={sims[base_id]},{sims[boost_id]}").json()
    assert cmp["kpis"]["cash_in"][1]["delta_vs_baseline"] > 0
    assert forecast["resource_influence"][0]["resource_id"] == "master"

    # 16. получить список bottlenecks
    bottlenecks = client.get(f"/api/simulations/{sims[boost_id]}/bottlenecks").json()
    assert any(b["type"] in ("missing_resource", "resource_saturation") for b in bottlenecks)

    # 17. выполнить простую оптимизацию
    r = client.post("/api/optimizations", json={
        "scenario_id": boost_id,
        "variables": [{"name": "masters", "path": "resource:master", "values": [1, 2]}],
        "objective_kpi_id": "cash_in", "runs_per_evaluation": 3, "seed": 42,
    })
    assert r.status_code == 202
    opt = wait(client, f"/api/optimizations/{r.json()['id']}")
    assert opt["status"] == "completed"
    assert opt["result"]["best"]["assignment"]["masters"] == 2

    # 18. увидеть causal/knowledge graph
    graph = client.get("/api/enterprises/workshop/graph").json()
    ids = {n["id"] for n in graph["nodes"]}
    assert {"sed:make", "sed:sell", "utility_type:cash", "goal:g_cash"} <= ids
    impact = client.get("/api/enterprises/workshop/graph/impact/resource:master").json()
    assert any(chain[-1] == "goal:g_cash" for chain in impact["chains"])

    # 19. сохранить версию модели
    version = client.post("/api/enterprises/workshop/versions",
                          json={"comment": "acceptance"}).json()
    assert version["version"] >= 1

    # 20. воспроизводимость: тот же seed → идентичный результат
    digests = []
    for _ in range(2):
        sim = client.post("/api/simulations", json={
            "scenario_id": base_id, "kind": "single", "seed": 777,
        }).json()["id"]
        wait(client, f"/api/simulations/{sim}")
        result = client.get(f"/api/simulations/{sim}/results").json()["result"]
        result["provenance"].pop("started_at")
        result["metrics"].pop("wall_time_seconds")
        import json as jsonlib

        digests.append(jsonlib.dumps(result, sort_keys=True))
    assert digests[0] == digests[1]
