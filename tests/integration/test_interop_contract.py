"""Фаза 7: контракт стыковки движков — модель + политика + сиды → результат."""

import pytest

from dtb.demo import build_demo_model
from dtb.interop import (
    CONTRACT_VERSION,
    REQUIRED_RUN_FIELDS,
    CallableEngineAdapter,
    ContractError,
    ContractRequest,
    DTBEngineAdapter,
    certify,
    compare_engines,
    validate_request,
    validate_response,
)
from tests.conftest import build_small_model


@pytest.fixture()
def request_small():
    return ContractRequest(
        model=build_small_model(horizon=120.0).to_snapshot(),
        scenario={"horizon_hours": 120.0},
        seeds=[11, 12, 13],
        options={"trajectory_points": 12},
    )


# ---------------------------------------------------------- наш движок --


def test_dtb_engine_satisfies_the_contract(request_small):
    report = certify(DTBEngineAdapter(), request_small)
    assert report["passed"], [c for c in report["checks"] if not c["passed"]]
    assert report["contract_version"] == CONTRACT_VERSION
    assert {c["id"] for c in report["checks"]} >= {
        "responds", "schema", "runs_per_seed", "economics_numeric",
        "reproducible", "seed_sensitive",
    }


def test_response_shape(request_small):
    response = DTBEngineAdapter().run(request_small)
    assert not validate_response(response, request_small)
    assert len(response["runs"]) == 3
    run = response["runs"][0]
    for key in REQUIRED_RUN_FIELDS:
        assert key in run
    grid = run["trajectory"]["grid"]
    assert len(grid) == 13 and grid[0] == 0.0 and grid[-1] == 120.0
    for series in run["trajectory"]["kpis"].values():
        assert len(series) == len(grid)
    assert set(run["logs"]) == {
        "events", "decisions", "policies", "lookahead", "rejected_actions"
    }
    assert run["seed"] in request_small.seeds


def test_policies_can_be_passed_beside_the_model():
    """Контур управления передаётся отдельно от модели — это и есть «политика»."""
    from dtb.domain import EffectKind, EventEffect
    from dtb.domain.control import (
        ControlActionDef,
        DecisionPoint,
        DecisionPointKind,
        Policy,
    )

    model = build_small_model(horizon=200.0)
    # делаем модель ограниченной по мощности, чтобы решение политики было видно
    model.resources["line"].capacity = 1
    model.event_kinds["customer_order"].stream.rate_per_hour = 0.6
    model.control_actions["add_line"] = ControlActionDef(
        id="add_line", name="Линия",
        effect=EventEffect(
            kind=EffectKind.RESOURCE_CAPACITY_DELTA, resource_id="line", delta=1
        ),
    )
    model.decision_points["dp"] = DecisionPoint(
        id="dp", name="Регламент", kind=DecisionPointKind.TIMER, timer_period=24.0
    )
    snapshot = model.to_snapshot()
    without = DTBEngineAdapter().run(
        ContractRequest(model=snapshot, scenario={"horizon_hours": 200.0}, seeds=[7])
    )
    policy = Policy(
        id="pol", name="Регулирование", rule="waiting_count('production') >= 1",
        action_ref="add_line",
    )
    with_policy = DTBEngineAdapter().run(
        ContractRequest(
            model=snapshot, scenario={"horizon_hours": 200.0}, seeds=[7],
            policies=[policy.model_dump(mode="json")],
        )
    )
    assert without["runs"][0]["metrics"]["policy_firings"] == 0
    assert with_policy["runs"][0]["metrics"]["policy_firings"] > 0
    assert (
        with_policy["runs"][0]["final_kpis"]["revenue"]
        != without["runs"][0]["final_kpis"]["revenue"]
    )


def test_contract_run_is_reproducible(request_small):
    first = DTBEngineAdapter().run(request_small)
    second = DTBEngineAdapter().run(request_small)
    assert [r["content_digest"] for r in first["runs"]] == [
        r["content_digest"] for r in second["runs"]
    ]


# ------------------------------------------------------ проверка запроса --


def test_request_validation():
    with pytest.raises(ContractError, match="model"):
        validate_request({"seeds": [1]})
    with pytest.raises(ContractError, match="версия"):
        validate_request(
            {"contract_version": "9.0", "model": {"enterprise": {"id": "x"}}}
        )
    with pytest.raises(ContractError, match="seeds"):
        validate_request({"model": {"enterprise": {"id": "x"}}, "seeds": []})
    with pytest.raises(ContractError, match="не целое"):
        validate_request({"model": {"enterprise": {"id": "x"}}, "seeds": ["a"]})
    validate_request({"model": {"enterprise": {"id": "x"}}, "seeds": [1]})


def test_unknown_request_fields_are_ignored(request_small):
    """Адаптер игнорирует незнакомые поля, а не падает: контракт растёт аддитивно."""
    payload = request_small.to_dict()
    payload["future_field"] = {"whatever": 1}
    restored = ContractRequest.from_dict(payload)
    response = DTBEngineAdapter().run(restored)
    assert not validate_response(response, restored)


# ------------------------------------------- нарушения контракта находятся --


def broken_engine(drop: str):
    """Движок, роняющий одно обязательное поле ответа."""

    def fn(payload):
        request = ContractRequest.from_dict(payload)
        response = DTBEngineAdapter().run(request)
        if drop == "runs":
            response["runs"] = []
        elif drop == "economics":
            for run in response["runs"]:
                run.pop("economics")
        elif drop == "engine":
            response.pop("engine")
        elif drop == "version":
            response["contract_version"] = "9.9"
        elif drop == "grid":
            for run in response["runs"]:
                run["trajectory"]["grid"] = run["trajectory"]["grid"][:-3]
        elif drop == "seeds":
            for run in response["runs"]:
                run["seed"] = 999
        return response

    return CallableEngineAdapter(fn, name=f"broken:{drop}", validate=False)


@pytest.mark.parametrize(
    "drop,expected",
    [
        ("runs", "runs"),
        ("economics", "economics"),
        ("engine", "engine.name"),
        ("version", "версия"),
        ("grid", "длина"),
        ("seeds", "сиды"),
    ],
)
def test_contract_violations_are_detected(request_small, drop, expected):
    adapter = broken_engine(drop)
    problems = validate_response(adapter.run(request_small), request_small)
    assert problems, f"нарушение {drop} не обнаружено"
    assert any(expected in problem for problem in problems), problems
    report = certify(adapter, request_small, check_reproducibility=False)
    assert report["passed"] is False


def test_validating_adapter_raises_on_violation(request_small):
    def fn(payload):
        response = DTBEngineAdapter().run(ContractRequest.from_dict(payload))
        for run in response["runs"]:
            run.pop("economics")
        return response

    adapter = CallableEngineAdapter(fn, name="strict")
    with pytest.raises(ContractError, match="нарушает контракт"):
        adapter.run(request_small)


def test_nonreproducible_engine_is_caught(request_small):
    """Движок, выдающий разные результаты на одном сиде, проверку не проходит."""
    state = {"calls": 0}

    def fn(payload):
        state["calls"] += 1
        request = ContractRequest.from_dict(payload)
        shifted = ContractRequest(
            model=request.model, scenario=request.scenario,
            seeds=[s + state["calls"] for s in request.seeds],
            options=request.options,
        )
        response = DTBEngineAdapter().run(shifted)
        for run, seed in zip(response["runs"], request.seeds):
            run["seed"] = seed  # сид подменён: ответ выдаёт себя за запрошенный
        return response

    report = certify(CallableEngineAdapter(fn, name="drifting", validate=False),
                     request_small)
    assert report["passed"] is False
    reproducible = next(c for c in report["checks"] if c["id"] == "reproducible")
    assert reproducible["passed"] is False


def test_unknown_capability_is_reported(request_small):
    adapter = CallableEngineAdapter(
        lambda payload: DTBEngineAdapter().run(ContractRequest.from_dict(payload)),
        name="odd", capabilities=["trajectory", "telepathy"],
    )
    report = certify(adapter, request_small, check_reproducibility=False)
    known = next(c for c in report["checks"] if c["id"] == "capabilities_known")
    assert known["passed"] is False
    assert "telepathy" in known["detail"]


# ------------------------------------------------------ сравнение движков --


def test_compare_engines_on_common_seeds():
    """Разность двух движков считается на одних данных и одних сидах."""
    model = build_demo_model().to_snapshot()
    request = ContractRequest(
        model=model, scenario={"horizon_hours": 240.0}, seeds=[11, 12, 13],
        options={"trajectory_points": 8},
    )

    def without_control_loop(payload):
        inner = ContractRequest.from_dict(payload)
        stripped = dict(inner.model)
        stripped["policies"] = {}
        stripped["decision_points"] = {}
        response = DTBEngineAdapter().run(
            ContractRequest(
                model=stripped, scenario=inner.scenario, seeds=inner.seeds,
                options=inner.options,
            )
        )
        response["engine"] = {
            "name": "no_control", "version": "0.1",
            "capabilities": ["trajectory", "economics"],
        }
        return response

    report = compare_engines(
        DTBEngineAdapter(),
        CallableEngineAdapter(without_control_loop, name="no_control"),
        request,
    )
    assert report["paired_seeds"] == [11, 12, 13]
    assert report["shared_capabilities"] == ["economics", "trajectory"]
    assert report["npv"]["n_pairs"] == 3
    # отключение контура управления меняет экономику, и разность значима
    assert report["npv"]["mean"] != 0.0
    assert "revenue" in report["kpis"]
    assert report["kpis"]["revenue"]["left_mean"] is not None


def test_compare_identical_engines_shows_no_difference(request_small):
    report = compare_engines(DTBEngineAdapter(), DTBEngineAdapter(), request_small)
    assert report["npv"]["mean"] == 0.0
    assert report["npv"]["significant"] is False
    for stats in report["kpis"].values():
        assert stats["mean"] == 0.0


# --------------------------------------------------------------- API --


def test_interop_api_endpoints(tmp_path, monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from dtb.api import create_app
    from dtb.persistence import Database

    monkeypatch.delenv("DTB_API_KEYS", raising=False)
    client = TestClient(create_app(Database(url=f"sqlite:///{tmp_path}/interop.db")))
    client.post("/api/demo")

    contract = client.get("/api/interop/contract").json()
    assert contract["contract_version"] == CONTRACT_VERSION
    assert "reproducible" in contract["this_engine"]["capabilities"]

    run = client.post("/api/interop/run", json={
        "enterprise_id": "demo_plant", "scenario_id": "demo_baseline",
        "seeds": [11, 12], "horizon_hours": 168.0, "trajectory_points": 8,
    }).json()
    assert run["summary"] == {"runs": 2, "failed": 0}
    assert len(run["runs"][0]["trajectory"]["grid"]) == 9

    report = client.post("/api/interop/certify", json={
        "enterprise_id": "demo_plant", "seeds": [1, 2], "horizon_hours": 120.0,
    }).json()
    assert report["passed"] is True and report["failed"] == 0

    snapshot = client.get("/api/enterprises/demo_plant/model").json()
    raw = client.post("/api/interop/run-raw", json={
        "model": snapshot, "scenario": {"horizon_hours": 72.0}, "seeds": [5],
        "options": {"trajectory_points": 4, "include_logs": False},
    }).json()
    assert raw["runs"][0]["seed"] == 5
    assert "logs" not in raw["runs"][0]

    assert client.post("/api/interop/run-raw", json={"seeds": [1]}).status_code == 422
    assert client.post("/api/interop/run", json={
        "enterprise_id": "ghost", "seeds": [1],
    }).status_code == 404
    assert time.time() > 0  # прогоны синхронные: отдельного ожидания не нужно
