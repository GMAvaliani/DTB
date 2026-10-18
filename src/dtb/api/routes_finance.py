"""Проекты (EVM), финансовая отчётность и мониторинг — версия 0.2.

Отчёты строятся из сохранённого результата прогона: для Монте-Карло — по
образцовому прогону, а распределения сроков, стоимости и денег — по всем
прогонам (``project_stats``, ``finance_stats``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile

from dtb.analysis import evm as evm_analysis
from dtb.analysis import finance as fin
from dtb.api.deps import actor_of, ctx, get_model_or_404
from dtb.api.routes_sim import _as_mc, _record_or_404, _sample_result
from dtb.api.security import import_limiter, require_role
from dtb.integration.estimate_import import import_estimate

router = APIRouter()

VIEW = Depends(require_role("viewer"))
EDIT = Depends(require_role("editor"))

_PERIODS = ("hour", "day", "week", "month", "quarter", "year", "total")


def _period(value: str) -> str | float:
    if value in _PERIODS:
        return value
    try:
        hours = float(value)
    except ValueError as exc:
        raise HTTPException(400, f"период: {', '.join(_PERIODS)} или число часов") from exc
    if hours <= 0:
        raise HTTPException(400, "период в часах должен быть больше нуля")
    return hours


def _result_dict(request: Request, sim_id: str) -> dict[str, Any]:
    record = _record_or_404(ctx(request), sim_id)
    return _sample_result(record).to_dict()


def _finance(request: Request, sim_id: str) -> dict[str, Any]:
    finance = _result_dict(request, sim_id).get("finance") or {}
    if not finance:
        raise HTTPException(409, "в прогоне нет финансовых данных")
    return finance


# ------------------------------------------------------------------- отчёты --


@router.get("/reports/recommended", dependencies=[VIEW])
def recommended_reports() -> list[dict[str, str]]:
    """Какие отчёты выводить и как часто их обновлять."""
    return fin.RECOMMENDED_REPORTS


@router.get("/simulations/{sim_id}/reports/cash-flow", dependencies=[VIEW])
def report_cash_flow(
    request: Request, sim_id: str, period: str = "month", until: float | None = None
) -> dict[str, Any]:
    """ОДДС по периодам; ``until`` — срез на момент (часы прогона)."""
    return fin.cash_flow_statement(_finance(request, sim_id), _period(period), until)


@router.get("/simulations/{sim_id}/reports/income", dependencies=[VIEW])
def report_income(
    request: Request, sim_id: str, period: str = "month", until: float | None = None
) -> dict[str, Any]:
    """ОПиУ по начислениям; капвложения — отдельной строкой."""
    return fin.income_statement(_finance(request, sim_id), _period(period), until)


@router.get("/simulations/{sim_id}/reports/cash-balance", dependencies=[VIEW])
def report_cash_balance(request: Request, sim_id: str) -> list[dict[str, Any]]:
    """Остаток денег на конец каждого шага хранения."""
    return fin.cash_balance_series(_finance(request, sim_id))


@router.get("/simulations/{sim_id}/reports/cash-gap", dependencies=[VIEW])
def report_cash_gap(request: Request, sim_id: str, threshold: float = 0.0) -> dict[str, Any]:
    """Кассовые разрывы образцового прогона и их вероятность по всем прогонам."""
    record = _record_or_404(ctx(request), sim_id)
    finance = _sample_result(record).finance or {}
    if not finance:
        raise HTTPException(409, "в прогоне нет финансовых данных")
    out = fin.cash_gap_report(finance, threshold)
    out["monte_carlo"] = _as_mc(record).finance_stats
    return out


@router.get("/simulations/{sim_id}/reports/payment-calendar", dependencies=[VIEW])
def report_payment_calendar(
    request: Request, sim_id: str, from_hours: float = 0.0, days: int = 30
) -> dict[str, Any]:
    if days <= 0 or days > 3660:
        raise HTTPException(400, "days: от 1 до 3660")
    return fin.payment_calendar(_finance(request, sim_id), from_hours, days)


@router.get("/simulations/{sim_id}/reports/variance", dependencies=[VIEW])
def report_variance(
    request: Request, sim_id: str, plan: str, period: str = "month"
) -> dict[str, Any]:
    """План-факт: ``plan`` — прогон-план, ``sim_id`` — прогноз или факт."""
    return fin.variance_report(_finance(request, plan), _finance(request, sim_id), _period(period))


@router.get("/simulations/{sim_id}/reports/dashboard", dependencies=[VIEW])
def report_dashboard(
    request: Request, sim_id: str, at: float | None = None, window_days: float = 30.0
) -> dict[str, Any]:
    """Онлайн-панель на момент ``at`` (часы прогона; пусто — конец)."""
    return fin.dashboard(_result_dict(request, sim_id), at, window_days)


@router.get("/simulations/{sim_id}/reports/evm", dependencies=[VIEW])
def report_evm(request: Request, sim_id: str) -> dict[str, Any]:
    """Освоенный объём: кривые PV/EV/AC, итоговые индексы, распределения."""
    record = _record_or_404(ctx(request), sim_id)
    result = _sample_result(record).to_dict()
    projects = result.get("projects") or {}
    return {
        "projects": {
            pid: {k: v for k, v in data.items() if k != "work_packages"}
            for pid, data in projects.items()
        },
        "summary": evm_analysis.project_summary(projects),
        "monte_carlo": _as_mc(record).project_stats,
        "start_date": (result.get("finance") or {}).get("start_date"),
        "time_origin": (result.get("finance") or {}).get("time_origin", 0.0),
    }


@router.get("/simulations/{sim_id}/reports/evm/{project_id}/packages", dependencies=[VIEW])
def report_evm_packages(request: Request, sim_id: str, project_id: str) -> list[dict[str, Any]]:
    """Пакеты работ проекта на конец прогона."""
    projects = _result_dict(request, sim_id).get("projects") or {}
    if project_id not in projects:
        raise HTTPException(404, f"проект {project_id!r} в прогоне не найден")
    return [
        {"id": wid, **data}
        for wid, data in projects[project_id].get("work_packages", {}).items()
    ]


@router.get("/simulations/{sim_id}/alerts", dependencies=[VIEW])
def simulation_alerts(
    request: Request, sim_id: str, period: str = "month", delay_days: float = 10.0
) -> dict[str, Any]:
    """Сигналы мониторинга и на сколько позже их показал бы периодический отчёт."""
    result = _result_dict(request, sim_id)
    return {
        "monitoring": result.get("monitoring") or {},
        "reporting_lag": fin.reporting_lag(result, period, delay_days),
    }


# -------------------------------------------------------------- проекты модели --


@router.get("/enterprises/{eid}/projects", dependencies=[VIEW])
def list_projects(request: Request, eid: str) -> list[dict[str, Any]]:
    model = get_model_or_404(ctx(request), eid)
    out = []
    for pid, project in sorted(model.projects.items()):
        packages = [wp for wp in model.work_packages.values() if wp.project_ref == pid]
        out.append(
            {
                **project.model_dump(mode="json"),
                "work_packages": len(packages),
                "bac": round(sum(wp.bac() for wp in packages), 2),
                "with_baseline": sum(
                    1 for wp in packages
                    if wp.planned_start is not None and wp.planned_finish is not None
                ),
            }
        )
    return out


@router.get("/enterprises/{eid}/projects/{pid}/evm-status", dependencies=[VIEW])
def project_evm_status(request: Request, eid: str, pid: str) -> dict[str, Any]:
    """EVM на дату статуса по фактическим данным пакетов, без прогона."""
    model = get_model_or_404(ctx(request), eid)
    try:
        return evm_analysis.evm_status(model, pid)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/enterprises/{eid}/projects/baseline", dependencies=[EDIT])
def project_baseline(request: Request, eid: str, project_id: str | None = None) -> dict[str, Any]:
    """Построить базовый план детерминированным прогоном и сохранить сроки."""
    context = ctx(request)
    model = get_model_or_404(context, eid)
    try:
        report = evm_analysis.build_baseline(model, project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    context.db.save_model(model)
    context.db.save_version(model, author=actor_of(request), comment="базовый план проектов")
    context.db.audit(actor_of(request), "baseline_built", eid, "project", project_id or "*")
    return report if project_id is not None else {"projects": report}


@router.post("/enterprises/{eid}/import/estimate", dependencies=[EDIT])
async def import_estimate_endpoint(
    request: Request,
    eid: str,
    file: UploadFile,
    project_id: str = "project",
    project_name: str = "",
    sheet: str | None = None,
) -> dict[str, Any]:
    """Импорт сметы со статусом выполнения как проекта с пакетами работ."""
    context = ctx(request)
    import_limiter.check(actor_of(request))
    model = get_model_or_404(context, eid)
    content = await file.read()
    try:
        result = import_estimate(model, content, project_id, project_name, sheet)
    except Exception as exc:  # noqa: BLE001 — повреждённый файл не роняет сервис
        raise HTTPException(400, f"не удалось прочитать смету: {exc}") from exc
    if not result.report.errors:
        context.db.save_model(model)
        context.db.save_version(model, author=actor_of(request), comment=f"смета {project_id}")
    context.db.audit(actor_of(request), "import_estimate", eid, "project", project_id,
                     details=result.report.to_dict())
    return result.to_dict()
