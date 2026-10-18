"""API версии 0.2: проекты (EVM), финансовая отчётность, мониторинг, смета."""

import io
import time

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from dtb.api import create_app
from dtb.persistence import Database


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DTB_API_KEYS", raising=False)
    app = create_app(Database(url=f"sqlite:///{tmp_path}/api.db"))
    return TestClient(app)


def wait_done(client, sim_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/simulations/{sim_id}").json()["status"]
        if status not in ("pending", "running", "paused"):
            return status
        time.sleep(0.2)
    raise TimeoutError


@pytest.fixture()
def farm_sim(client):
    r = client.post("/api/demo?which=farm")
    assert r.status_code == 201
    assert r.json()["enterprise_id"] == "demo_farm"
    r = client.post("/api/simulations",
                    json={"scenario_id": "farm_status", "kind": "single", "seed": 7})
    assert r.status_code == 202, r.text
    sim_id = r.json()["id"]
    assert wait_done(client, sim_id) == "completed"
    return sim_id


def test_demo_default_is_plant_and_all_creates_both(client):
    r = client.post("/api/demo")
    assert r.json()["enterprise_id"] == "demo_plant"
    r = client.post("/api/demo?which=all")
    assert [e["enterprise_id"] for e in r.json()["enterprises"]] == ["demo_plant", "demo_farm"]
    assert client.post("/api/demo?which=nope").status_code == 400


def test_project_status_and_baseline(client):
    client.post("/api/demo?which=farm")
    projects = client.get("/api/enterprises/demo_farm/projects").json()
    farm = next(p for p in projects if p["id"] == "farm")
    assert farm["work_packages"] > 100 and farm["with_baseline"] == farm["work_packages"]
    status = client.get("/api/enterprises/demo_farm/projects/farm/evm-status").json()
    ind = status["indicators"]
    assert ind["ev"] == pytest.approx(7_978_260.12, abs=0.01)
    assert ind["ac"] == pytest.approx(11_741_655.32, abs=0.01)
    assert 0 < ind["spi"] < 1 and 0 < ind["cpi"] < 1
    assert status["materials_ahead"] > 3_000_000
    assert status["eac_adjusted"] < ind["eac"]
    assert client.get("/api/enterprises/demo_farm/projects/x/evm-status").status_code == 404
    r = client.post("/api/enterprises/demo_farm/projects/baseline")
    assert r.status_code == 200
    assert r.json()["projects"]["farm"]["unscheduled"] == []


def test_finance_reports(client, farm_sim):
    flow = client.get(f"/api/simulations/{farm_sim}/reports/cash-flow?period=month").json()
    assert flow["rows"] and set(flow["net_by_section"]) == {"operating", "investing", "financing"}
    balance = client.get(f"/api/simulations/{farm_sim}/reports/cash-balance").json()
    assert balance[-1]["balance"] == pytest.approx(flow["closing_balance"], abs=0.05)
    week = client.get(f"/api/simulations/{farm_sim}/reports/cash-flow?period=week&until=240").json()
    assert week["until_hours"] == 240
    assert client.get(f"/api/simulations/{farm_sim}/reports/cash-flow?period=decade").status_code == 400
    income = client.get(f"/api/simulations/{farm_sim}/reports/income?period=quarter").json()
    assert income["totals"]["revenue"] > 0
    assert income["totals"]["capital_expenditure"] > 0
    gap = client.get(f"/api/simulations/{farm_sim}/reports/cash-gap").json()
    assert "min_balance" in gap and "monte_carlo" in gap
    calendar = client.get(
        f"/api/simulations/{farm_sim}/reports/payment-calendar?from_hours=0&days=10").json()
    assert len(calendar["rows"]) <= 10
    dash = client.get(f"/api/simulations/{farm_sim}/reports/dashboard?at=720").json()
    assert dash["at_hours"] == 720 and "farm" in dash["projects"]
    variance = client.get(
        f"/api/simulations/{farm_sim}/reports/variance?plan={farm_sim}").json()
    assert all(row["variance_net"] == 0 for row in variance["rows"])
    assert client.get("/api/reports/recommended").json()


def test_evm_and_alerts(client, farm_sim):
    evm = client.get(f"/api/simulations/{farm_sim}/reports/evm").json()
    farm = evm["projects"]["farm"]
    assert farm["mode"] == "status" and farm["time_offset"] == 2160
    assert farm["series"]["ev"][-1] >= farm["series"]["ev"][0]
    assert evm["monte_carlo"]["farm"]["runs"] == 1
    packages = client.get(f"/api/simulations/{farm_sim}/reports/evm/farm/packages").json()
    assert any(p["status"] == "done" for p in packages)
    alerts = client.get(f"/api/simulations/{farm_sim}/alerts").json()
    assert alerts["monitoring"]["raised"] >= 1
    assert alerts["reporting_lag"]["mean_lag_days"] > 0


def _estimate_workbook() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Смета: тестовый объект"])
    ws.append(["Наименование работ", "Обозн.", "Объем", "Материал ед.", "Материал всего",
               "Работа ед.", "Работа всего", "Доставка", "Аренда техники", "Примечание",
               "Объем", "Материал всего", "Работа всего"])
    ws.append(["Склад", None, None, None, None, None, None, None, None, None])
    ws.append(["Фундамент", None, None, None, None, None, None, None, None, None])
    ws.append(["Бетонирование", "м3", 10, 8000, 80000, 5000, 50000, 0, 0, None, 5, 40000, 25000])
    ws.append(["Армирование", "тн", 2, 90000, 180000, 70000, 140000, 0, 0, None, 0, 0, 0])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_import_estimate(client):
    client.post("/api/enterprises", json={"id": "acme", "name": "ACME"})
    r = client.post(
        "/api/enterprises/acme/import/estimate?project_id=wh&project_name=Склад",
        files={"file": ("e.xlsx", _estimate_workbook(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["work_packages"] == 2
    assert body["summary"]["bac"] == pytest.approx(450_000)
    status = client.get("/api/enterprises/acme/projects/wh/evm-status").json()
    assert status["indicators"]["ev"] == pytest.approx(65_000)
    bad = client.post("/api/enterprises/acme/import/estimate?project_id=x",
                      files={"file": ("e.xlsx", b"not an xlsx", "application/octet-stream")})
    assert bad.status_code == 400
