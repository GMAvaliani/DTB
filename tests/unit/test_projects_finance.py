"""Версия 0.2: EVM, исполнение проектов, деньги по времени, мониторинг."""

import io

import pytest
from openpyxl import Workbook

from dtb.analysis.evm import build_baseline, evm_status
from dtb.analysis.finance import (
    cash_balance_series,
    cash_flow_statement,
    cash_gap_report,
    dashboard,
    income_statement,
    reporting_lag,
)
from dtb.analysis.montecarlo import run_monte_carlo
from dtb.domain import (
    DurationKind,
    DurationModel,
    Enterprise,
    EnterpriseModel,
    Resource,
    ResourceRequirement,
    Scenario,
    SEDDefinition,
    StreamKind,
    StreamSpec,
)
from dtb.domain.calendars import Calendar, CalendarException, Shift
from dtb.domain.economics import (
    EconomicBasis,
    EconomicDirection,
    EconomicItem,
    EconomicKind,
    EconomicsSettings,
)
from dtb.domain.model import SCHEMA_VERSION
from dtb.domain.monitoring import MonitorRule
from dtb.domain.projects import Project, WorkPackage
from dtb.integration.estimate_import import parse_estimate
from dtb.simulation import simulate
from dtb.simulation.engine import SimulationEngine
from dtb.simulation.monitoring import MonitorScheduler
from dtb.simulation.projects import PiecewiseLinear, earned_schedule, evm_indicators
from dtb.simulation.streams import ScheduledSampler
from dtb.validation import validate_model


def project_model(horizon: float = 100.0, **project_kw) -> EnterpriseModel:
    """Два пакета подряд на одной бригаде: a — 10 ч, BAC 150; b — 20 ч, BAC 240."""
    m = EnterpriseModel(
        enterprise=Enterprise(id="p", name="Проект", planning_horizon_hours=horizon)
    )
    m.resources["crew"] = Resource(id="crew", name="Бригада", capacity=1)
    m.seds["work"] = SEDDefinition(
        id="work", name="Работы",
        resources=[ResourceRequirement(resource_id="crew", quantity=1)],
        duration=DurationModel(kind=DurationKind.FIXED, value=1.0),
    )
    m.projects["p1"] = Project(id="p1", name="Проект", **project_kw)
    m.work_packages["a"] = WorkPackage(
        id="a", name="Фундамент", project_ref="p1", object_name="Склад",
        planned_quantity=10, material_unit_cost=5, labor_unit_cost=10,
        sed_ref="work", work_hours=10,
    )
    m.work_packages["b"] = WorkPackage(
        id="b", name="Каркас", project_ref="p1", object_name="Склад",
        planned_quantity=20, material_unit_cost=2, labor_unit_cost=10,
        sed_ref="work", work_hours=20, predecessors=["a"],
    )
    return m


def scenario(mode: str = "plan", horizon: float = 100.0, **kw) -> Scenario:
    return Scenario(id=f"s_{mode}", name=mode, enterprise_id="p", horizon_hours=horizon,
                    project_mode=mode, **kw)


# ------------------------------------------------------------ формулы EVM --


def test_evm_textbook_example():
    """BAC 100, PV 50, EV 40, AC 60 — классический пример PMBOK."""
    project = Project(id="x", planned_finish=100.0)
    ind = evm_indicators(100, 50, 40, 60, 50.0, project)
    assert ind["sv"] == -10 and ind["cv"] == -20
    assert ind["spi"] == pytest.approx(0.8)
    assert ind["cpi"] == pytest.approx(0.6667, abs=1e-4)
    assert ind["eac"] == pytest.approx(150.0, abs=0.01)
    assert ind["eac_atypical"] == pytest.approx(120.0)
    assert ind["eac_cpi_spi"] == pytest.approx(172.5, abs=0.01)
    assert ind["etc"] == pytest.approx(90.0, abs=0.01)
    assert ind["vac"] == pytest.approx(-50.0, abs=0.01)
    assert ind["tcpi"] == pytest.approx(1.5)


def test_earned_schedule_and_completion_freezes_actual_time():
    curve = [(0.0, 0.0), (10.0, 50.0), (20.0, 100.0)]
    assert earned_schedule(curve, 25.0, 0.0) == pytest.approx(5.0)
    assert earned_schedule(curve, 100.0, 0.0) == pytest.approx(20.0)
    project = Project(id="x", planned_finish=20.0)
    running = evm_indicators(100, 50, 25, 25, 10.0, project, curve)
    assert running["spi_t"] == pytest.approx(0.5) and running["sv_t"] == pytest.approx(-5.0)
    assert running["forecast_duration"] == pytest.approx(40.0)
    # проект завершён в 30 ч: через 100 ч отставание то же, а не растёт
    done = evm_indicators(100, 100, 100, 100, 130.0, project, curve, finished_t=30.0)
    assert done["at"] == pytest.approx(30.0) and done["sv_t"] == pytest.approx(-10.0)


def test_piecewise_linear():
    f = PiecewiseLinear()
    f.set(2.0, slope=1.0)
    assert f.at(1.0) == 0.0 and f.at(4.0) == pytest.approx(2.0)
    f.set(4.0, slope=0.0)
    f.add(6.0, 3.0)
    assert f.at(10.0) == pytest.approx(5.0)


# ---------------------------------------------------------- исполнение проекта --


def test_plan_run_network_and_costs():
    m = project_model()
    engine = SimulationEngine(m, scenario())
    result = engine.run()
    data = result.projects["p1"]
    assert data["work_packages"]["a"]["finish"] == pytest.approx(10.0)
    assert data["work_packages"]["b"]["start"] == pytest.approx(10.0)
    assert data["finished_at"] == pytest.approx(30.0)
    final = data["final"]
    assert final["bac"] == 390 and final["ev"] == 390 and final["ac"] == pytest.approx(390)
    mid = engine.projects.indicators("p1", 5.0)
    assert mid["ev"] == pytest.approx(75.0) and mid["ac"] == pytest.approx(75.0)


def test_baseline_sets_planned_dates_and_pv():
    m = project_model()
    report = build_baseline(m, "p1")
    assert report["unscheduled"] == []
    assert m.work_packages["a"].planned_start == 0 and m.work_packages["a"].planned_finish == 10
    assert m.work_packages["b"].planned_finish == pytest.approx(30.0)
    assert m.projects["p1"].planned_finish == pytest.approx(30.0)
    engine = SimulationEngine(m, scenario())
    engine.run()
    ind = engine.projects.indicators("p1", 20.0)
    assert ind["pv"] == pytest.approx(150 + 120) and ind["spi"] == pytest.approx(1.0)


def test_status_mode_continues_from_facts_without_rebuying_materials():
    m = project_model(status_date=5.0)
    build_baseline(m, "p1")
    a = m.work_packages["a"]
    a.actual_quantity = 5  # половина объёма
    a.actual_material_cost = 50  # материалы закуплены целиком
    a.actual_labor_cost = 50
    status = evm_status(m, "p1")
    assert status["indicators"]["ev"] == pytest.approx(75.0)
    assert status["materials_ahead"] == pytest.approx(25.0)
    result = simulate(m, scenario("status"), seed=1)
    data = result.projects["p1"]
    assert data["time_offset"] == 5.0
    # остаток a — 5 ч, затем b — 20 ч: конец в 25 ч прогона = 30 ч проекта
    assert data["finished_at"] == pytest.approx(30.0)
    assert data["work_packages"]["a"]["ac"] == pytest.approx(150.0)  # 100 факт + 50 работ
    assert data["final"]["ac"] == pytest.approx(390.0)


def test_level_of_effort_accrues_until_project_finishes():
    m = project_model()
    m.work_packages["site"] = WorkPackage(
        id="site", name="Бытовки", project_ref="p1", unit="мес",
        planned_quantity=1, labor_total=730, level_of_effort=True,
    )
    result = simulate(m, scenario(), seed=1)
    site = result.projects["p1"]["work_packages"]["site"]
    assert site["ac"] == pytest.approx(30.0)  # 1 ₽/ч × 30 ч до завершения проекта
    assert site["ev"] == pytest.approx(30.0)


def test_advance_and_acts_pay_exactly_the_cost():
    m = project_model(advance_payment=100.0, advance_offset_share=0.5, billing_period_hours=10.0)
    result = simulate(m, scenario(), seed=1)
    finance = result.finance
    flow = cash_flow_statement(finance, "total")
    investing = flow["rows"][0]["sections"]["investing"]
    assert investing["outflow_by_article"]["аванс подрядчику"] == pytest.approx(100.0)
    assert investing["outflow"] == pytest.approx(390.0)
    assert finance["closing_cash"] == pytest.approx(-390.0)
    income = income_statement(finance, "total")
    assert income["totals"]["capital_expenditure"] == pytest.approx(390.0)
    assert income["totals"]["expenses"] == 0


def test_pace_factor_speeds_work_without_cutting_labor_cost():
    m = project_model()
    m.seds["work"].duration_factor = 0.5
    result = simulate(m, scenario(), seed=1)
    data = result.projects["p1"]
    assert data["finished_at"] == pytest.approx(15.0)
    assert data["final"]["ac"] == pytest.approx(390.0)


# ----------------------------------------------------------------- деньги --


def finance_model() -> EnterpriseModel:
    m = project_model()
    m.economics = EconomicsSettings(opening_cash=1000.0, report_bucket_hours=24.0)
    m.economic_items["sales"] = EconomicItem(
        id="sales", name="Выручка", article="выручка", direction=EconomicDirection.INCOME,
        kind=EconomicKind.RATE, basis=EconomicBasis.CONSTANT, rate=10.0,
        payment_delay_hours=24.0,
    )
    m.economic_items["loan"] = EconomicItem(
        id="loan", name="Кредит", article="кредит", direction=EconomicDirection.INCOME,
        kind=EconomicKind.RATE, basis=EconomicBasis.CONSTANT, rate=1.0,
        cash_category="financing",
    )
    return m


def test_cash_flow_statement_reconciles_with_exact_balance():
    result = simulate(finance_model(), scenario(), seed=1).to_dict()
    finance = result["finance"]
    flow = cash_flow_statement(finance, "day")
    assert flow["closing_balance"] == pytest.approx(finance["closing_cash"], abs=0.01)
    # отсрочка: выручка последних суток придёт после конца прогона
    assert flow["after_horizon"]["inflow"] == pytest.approx(240.0, abs=0.01)
    series = cash_balance_series(finance)
    assert series[-1]["balance"] == pytest.approx(finance["closing_cash"], abs=0.01)
    sliced = cash_flow_statement(finance, "day", until=48.0)
    assert len(sliced["rows"]) == 2
    assert sliced["closing_balance"] == pytest.approx(series[1]["balance"], abs=0.01)


def test_income_statement_excludes_financing_and_capex():
    result = simulate(finance_model(), scenario(), seed=1).to_dict()
    income = income_statement(result["finance"], "total")
    assert income["totals"]["revenue"] == pytest.approx(1000.0)  # только выручка
    assert income["totals"]["capital_expenditure"] == pytest.approx(390.0)


def test_cash_gap_and_dashboard():
    m = finance_model()
    m.economics.opening_cash = 0.0
    m.projects["p1"].advance_payment = 300.0
    result = simulate(m, scenario(), seed=1).to_dict()
    gap = cash_gap_report(result["finance"])
    assert gap["has_gap"] and gap["required_reserve"] > 0
    assert gap["first_gap_at"] == 0.0
    dash = dashboard(result, at=48.0)
    assert dash["at_hours"] == 48.0 and "p1" in dash["projects"]


def test_reporting_lag_counts_days_until_monthly_report():
    result = {
        "finance": {"start_date": "2026-07-01", "end_time": 2000.0},
        "monitoring": {"alerts": [
            {"t": 96.0, "rule": "r", "name": "r", "severity": "warning", "state": "raised"},
        ]},
    }
    lag = reporting_lag(result, "month", delay_days=10)
    row = lag["alerts"][0]
    assert row["report_period"] == "07.2026"
    assert row["report_available_date"] == "2026-08-11"
    assert row["lag_days"] == pytest.approx(37.0)


# ------------------------------------------------------------- мониторинг --


def test_monitor_scheduler_rising_edge_and_clearing():
    rules = {"r": MonitorRule(id="r", condition="x", check_period_hours=1.0)}
    scheduler = MonitorScheduler(rules, horizon=10.0)
    values = {0.0: False, 1.0: True, 2.0: True, 3.0: False}
    for t, value in values.items():
        scheduler.check(t, lambda _c, v=value: v)
    report = scheduler.report(10.0)
    states = [a["state"] for a in report["alerts"]]
    assert states == ["raised", "cleared"]
    assert report["alerts"][1]["duration_hours"] == pytest.approx(2.0)
    assert report["rules"]["r"]["fired"] == 1 and report["rules"]["r"]["checks"] == 4

    def broken(_c):
        raise ValueError("ошибка выражения")

    scheduler.check(4.0, broken)
    assert scheduler.report(10.0)["rules"]["r"]["errors"] == 1


def test_monitor_rules_run_inside_simulation():
    m = finance_model()
    m.monitor_rules["cash_up"] = MonitorRule(
        id="cash_up", condition="cash() > 1100", check_period_hours=1.0,
    )
    m.monitor_rules["late"] = MonitorRule(
        id="late", condition="progress('p1') < 0.5 and ac('p1') > 0", check_period_hours=1.0,
    )
    result = simulate(m, scenario(), seed=1)
    raised = {a["rule"] for a in result.monitoring["alerts"] if a["state"] == "raised"}
    assert raised == {"cash_up", "late"}


# ---------------------------------------------------- календари и расписания --


def test_calendar_finds_shift_after_holiday_longer_than_a_week():
    cal = Calendar(
        id="c", name="c",
        shifts=[Shift(day_of_week=d, start_hour=8, end_hour=18) for d in range(6)],
        exceptions=[CalendarException(from_hour=0, to_hour=240, working=False)],
    )
    # 10 суток нерабочие; день 10 при старте в понедельник — четверг, 08:00
    assert cal.next_change(0.0, 1000.0, 0) == pytest.approx(248.0)
    assert cal.next_change(100.0, 1000.0, 0) == pytest.approx(248.0)
    assert cal.next_change(248.0, 1000.0, 0) == pytest.approx(258.0)


def test_scheduled_events_keep_their_dates_in_status_mode():
    spec = StreamSpec(kind=StreamKind.SCHEDULED, times=[0.0, 100.0, 300.0])
    sampler = ScheduledSampler("k", spec, horizon=250.0, origin=100.0)
    assert sampler.times == [0.0, 200.0]


# ------------------------------------------------------- данные и модель --


def test_v5_snapshot_migrates_to_projects():
    m = project_model()
    snapshot = m.to_snapshot()
    snapshot["schema_version"] = 5
    for key in ("projects", "work_packages", "monitor_rules"):
        snapshot.pop(key, None)
    restored = EnterpriseModel.from_snapshot(snapshot)
    assert restored.schema_version == SCHEMA_VERSION
    assert restored.projects == {} and restored.work_packages == {}


def test_precedence_cycle_is_critical():
    m = project_model()
    m.work_packages["a"].predecessors = ["b"]
    report = validate_model(m)
    assert not report.ok_to_simulate
    assert any(i.code == "precedence_cycle" for i in report.issues)


def test_monte_carlo_project_and_finance_statistics():
    m = finance_model()
    m.seds["work"].duration = DurationModel(kind=DurationKind.TRIANGULAR, low=0.8, mode=1.0, high=1.6)
    build_baseline(m, "p1")
    mc = run_monte_carlo(m, scenario(), runs=6)
    stats = mc.project_stats["p1"]
    assert stats["runs"] == 6 and stats["finish"]["p10"] <= stats["finish"]["p90"]
    assert 0.0 <= stats["probability_on_time"] <= 1.0
    assert len(stats["run_cost"]) == 6
    assert mc.finance_stats["runs"] == 6
    restored = type(mc).from_dict(mc.to_dict())
    assert restored.project_stats == mc.project_stats


def _estimate_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Наименование работ", "Обозн.", "Объем", "Материал ед.", "Материал всего",
               "Работа ед.", "Работа всего", "Доставка", "Аренда техники", "Примечание",
               "Объем", "Материал всего", "Работа всего"])
    ws.append(["Геодезия", "мес", 4, 0, 0, 1000, 4000, 0, 0, None, 1, 0, 1000])
    ws.append(["Склад"])
    ws.append(["Фундамент"])
    ws.append(["Бетон", "м3", 10, 100, 900, 50, 500, 0, 0, None, 12, 1200, 600])
    ws.append(["Кровля"])
    ws.append(["Профлист", "м2", 20, 10, 200, 5, 100, 0, 0, None, 0, 0, 0])
    ws.append([None, "аванс", 5000])
    ws.append([None, "зачет аванса 40%"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_estimate_parser_objects_sections_status_and_warnings():
    result = parse_estimate(_estimate_bytes(), "w", "Склад")
    packages = {wp.name: wp for wp in result.work_packages}
    assert set(packages) == {"Геодезия", "Бетон", "Профлист"}
    assert packages["Геодезия"].level_of_effort
    assert packages["Бетон"].object_name == "Склад" and packages["Бетон"].section == "Фундамент"
    assert packages["Профлист"].section == "Кровля"
    assert packages["Бетон"].material_budget() == 900  # итог сметы, а не 10 × 100
    messages = " ".join(w["message"] for w in result.report.warnings)
    assert "объём × цена" in messages and "перерасход объёма" in messages
    assert result.project.advance_payment == 5000
    assert result.project.advance_offset_share == pytest.approx(0.4)


def test_farm_demo_is_deterministic_and_valid():
    from dtb.demo import create_farm_demo

    model, scenarios = create_farm_demo()
    assert validate_model(model).ok_to_simulate
    status = next(s for s in scenarios if s.id == "farm_status")
    first = simulate(model, status, seed=3)
    second = simulate(model, status, seed=3)
    assert first.content_digest() == second.content_digest()
    assert first.projects["farm"]["finished_at"] > model.projects["farm"].planned_finish
