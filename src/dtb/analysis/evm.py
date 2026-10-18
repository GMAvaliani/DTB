"""Метод освоенного объёма (Earned Value Management) для проектов модели.

Три режима работы с проектом:

1. **Статус по фактам** (:func:`evm_status`) — без прогона: на дату статуса
   по фактическим объёмам и затратам считаются PV, EV, AC, индексы и
   прогнозы, с раскладкой по объектам и разделам, перерасходами объёма и
   материалами, закупленными впрок.
2. **Базовый план** (:func:`build_baseline`) — плановые сроки пакетов
   получаются детерминированным прогоном модели: ожидаемые длительности,
   реальные бригады и графики работы, без рисковых событий. Это план с учётом
   ресурсов, а не сетевой график на бесконечных мощностях.
3. **Прогноз** — обычный прогон (или Монте-Карло) со сценарием
   ``project_mode = status``: моделируется остаток работ от даты статуса, и
   EAC, срок завершения и вероятность уложиться получаются как
   распределения, а не одной формулой.
"""

from __future__ import annotations

from typing import Any

from dtb.domain.model import EnterpriseModel
from dtb.domain.scenario import Scenario
from dtb.domain.seds import DurationKind, DurationModel
from dtb.simulation.projects import ProjectTracker, evm_indicators

_EPS = 1e-9


def _tracker(model: EnterpriseModel, mode: str) -> ProjectTracker:
    tracker = ProjectTracker(model.projects, model.work_packages, {}, mode=mode)
    tracker.initialize(0.0)
    return tracker


def evm_status(model: EnterpriseModel, project_id: str) -> dict[str, Any]:
    """Отчёт EVM на дату статуса проекта по фактическим данным пакетов."""
    project = model.projects.get(project_id)
    if project is None:
        raise KeyError(f"проект {project_id!r} не найден")
    tracker = _tracker(model, "status")
    if project_id not in tracker.projects:
        raise KeyError(f"проект {project_id!r} выключен")
    status_t = float(project.status_date or 0.0)
    totals = tracker.indicators(project_id, 0.0)

    groups: dict[tuple[str, str], dict[str, float]] = {}
    objects: dict[str, dict[str, float]] = {}
    overruns: list[dict[str, Any]] = []
    materials_ahead = 0.0
    materials_ahead_items: list[dict[str, Any]] = []
    no_baseline = 0
    for state in tracker.wps.values():
        wp = state.wp
        if wp.project_ref != project_id:
            continue
        bac = wp.bac()
        fraction = wp.actual_fraction()
        ev = bac * fraction
        ac = wp.actual_cost()
        pv = bac * tracker._pv_fraction(wp, status_t)
        if wp.planned_start is None or wp.planned_finish is None:
            no_baseline += 1
        for bucket in (
            groups.setdefault((wp.object_name, wp.section), _zero()),
            objects.setdefault(wp.object_name, _zero()),
        ):
            bucket["bac"] += bac
            bucket["pv"] += pv
            bucket["ev"] += ev
            bucket["ac"] += ac
            bucket["positions"] += 1
        installed = wp.material_budget() * fraction
        ahead = wp.materials_on_site()
        if ahead > 1.0 and wp.actual_quantity < wp.planned_quantity:
            materials_ahead += ahead
            materials_ahead_items.append(
                {
                    "id": wp.id, "name": wp.name, "object": wp.object_name,
                    "purchased": round(wp.actual_material_cost, 2),
                    "installed": round(installed, 2),
                    "ahead": round(ahead, 2),
                    "progress": round(fraction, 4),
                }
            )
        if wp.planned_quantity > 0 and wp.actual_quantity > wp.planned_quantity * 1.01:
            overruns.append(
                {
                    "id": wp.id, "name": wp.name, "object": wp.object_name,
                    "kind": "volume",
                    "planned_quantity": wp.planned_quantity,
                    "actual_quantity": wp.actual_quantity,
                    "unit": wp.unit,
                    "excess_cost": round(max(0.0, ac - bac), 2),
                }
            )
        elif ac > bac * 1.05 and bac > 0:
            overruns.append(
                {
                    "id": wp.id, "name": wp.name, "object": wp.object_name,
                    "kind": "cost", "bac": round(bac, 2), "ac": round(ac, 2),
                    "excess_cost": round(ac - bac, 2),
                }
            )

    def finish(bucket: dict[str, float]) -> dict[str, float]:
        out = {k: round(v, 2) for k, v in bucket.items()}
        out["positions"] = int(bucket["positions"])
        out["cpi"] = round(bucket["ev"] / bucket["ac"], 4) if bucket["ac"] > _EPS else None
        out["spi"] = round(bucket["ev"] / bucket["pv"], 4) if bucket["pv"] > _EPS else None
        out["percent_complete"] = round(bucket["ev"] / bucket["bac"], 4) if bucket["bac"] > _EPS else 0.0
        return out

    # AC без материалов впрок: насколько дорого обходится то, что реально сделано.
    # Прогноз с поправкой: материалы на объекте уже оплачены и второй раз не
    # покупаются, остаток работ идёт с эффективностью по фактически сделанному
    adjusted_ac = totals["ac"] - materials_ahead
    cpi_adjusted = totals["ev"] / adjusted_ac if adjusted_ac > _EPS else None
    eac_adjusted = None
    if cpi_adjusted and cpi_adjusted > _EPS:
        remaining = max(0.0, totals["bac"] - totals["ev"] - materials_ahead)
        eac_adjusted = round(totals["ac"] + remaining / cpi_adjusted, 2)
    return {
        "project": project_id,
        "name": project.name,
        "status_label": project.status_label,
        "status_date": status_t,
        "indicators": totals,
        "materials_ahead": round(materials_ahead, 2),
        "materials_ahead_items": sorted(
            materials_ahead_items, key=lambda x: -x["ahead"]
        ),
        "cpi_excluding_materials_ahead": (
            round(cpi_adjusted, 4) if cpi_adjusted is not None else None
        ),
        "eac_adjusted": eac_adjusted,
        "advance": {
            "paid": project.advance_payment,
            "offset_share": project.advance_offset_share,
            "offset_to_date": round(totals["ac"] * project.advance_offset_share, 2),
            "remaining": round(
                max(0.0, project.advance_payment - totals["ac"] * project.advance_offset_share), 2
            ),
        },
        "by_object": {k: finish(v) for k, v in objects.items()},
        "by_section": [
            {"object": obj, "section": sec, **finish(v)}
            for (obj, sec), v in groups.items()
        ],
        "overruns": overruns,
        "without_baseline": no_baseline,
        "interpretation": _interpret(totals, materials_ahead, eac_adjusted),
    }


def _zero() -> dict[str, float]:
    return {"bac": 0.0, "pv": 0.0, "ev": 0.0, "ac": 0.0, "positions": 0.0}


def _rub(value: float) -> str:
    """Сумма с пробелами между разрядами: 12 852 562."""
    return f"{value:,.0f}".replace(",", " ")


def _interpret(
    ind: dict[str, Any], materials_ahead: float, eac_adjusted: float | None = None
) -> list[str]:
    """Краткие выводы простым языком — для руководителя, а не для аналитика."""
    notes: list[str] = []
    spi, cpi = ind.get("spi"), ind.get("cpi")
    if spi is not None and ind.get("pv"):
        if spi < 0.9:
            notes.append(
                f"Отставание от графика: освоено {spi:.0%} от запланированного к дате статуса."
            )
        elif spi > 1.05:
            notes.append(f"Опережение графика: SPI = {spi:.2f}.")
        else:
            notes.append("Сроки в пределах плана.")
    if cpi is not None and ind.get("ac"):
        if cpi < 0.95:
            text = (
                f"Затраты выше освоенного: на каждый рубль работ потрачено "
                f"{1 / cpi:.2f} руб."
            )
            if materials_ahead > 0:
                text += (
                    f" Часть разницы — материалы, закупленные впрок "
                    f"({_rub(materials_ahead)} руб.): это не перерасход, а "
                    "опережающая закупка; её нужно контролировать на объекте."
                )
            notes.append(text)
        elif cpi > 1.05:
            notes.append(f"Затраты ниже освоенного: CPI = {cpi:.2f}.")
        else:
            notes.append("Затраты соответствуют освоенному объёму.")
    if ind.get("eac") and ind.get("bac"):
        vac = ind["bac"] - ind["eac"]
        if vac < 0:
            notes.append(
                f"При сохранении текущей эффективности проект обойдётся на "
                f"{_rub(-vac)} руб. дороже бюджета (EAC = {_rub(ind['eac'])})."
            )
    if eac_adjusted is not None and materials_ahead > 0 and ind.get("bac"):
        notes.append(
            f"С поправкой на оплаченные материалы на объекте прогноз — "
            f"{_rub(eac_adjusted)} руб. ({'+' if eac_adjusted >= ind['bac'] else '−'}"
                f"{_rub(abs(eac_adjusted - ind['bac']))} к бюджету): "
            "формула BAC / CPI здесь завышает прогноз."
        )
    if ind.get("tcpi") and ind["tcpi"] > 1.1:
        notes.append(
            f"Чтобы уложиться в бюджет, на остатке работ нужна эффективность "
            f"{ind['tcpi']:.2f} — заметно выше достигнутой."
        )
    return notes


def build_baseline(
    model: EnterpriseModel,
    project_id: str | None = None,
    horizon_hours: float | None = None,
    risk_categories: tuple[str, ...] = ("uncertain",),
) -> dict[str, Any]:
    """Базовый план: сроки пакетов по детерминированному прогону модели.

    В копии модели длительности СЭД пакетов заменяются ожидаемыми, события
    рисковых категорий отключаются, управляющие контуры, мониторинг и
    экономика снимаются (базовый план — это график без вмешательств), после
    чего модель прогоняется с начала до завершения всех проектов. Фактические
    старт и финиш каждого пакета в этом прогоне и становятся его плановыми
    сроками. Работы «по времени» получают окно от старта проекта до его
    планового завершения. Модель-оригинал меняется только в полях сроков.

    Все проекты модели делят одни бригады, поэтому прогон всегда идёт по всем
    проектам сразу; ``project_id`` ограничивает, чьи сроки записываются
    (пусто — всех включённых проектов).
    """
    from dtb.domain.events import StreamKind, StreamSpec
    from dtb.simulation.engine import SimulationConfig, expected_duration, simulate

    if project_id is not None and project_id not in model.projects:
        raise KeyError(f"проект {project_id!r} не найден")
    targets = (
        [project_id]
        if project_id is not None
        else sorted(pid for pid, p in model.projects.items() if p.enabled)
    )
    work = model.clone()
    used_seds = {wp.sed_ref for wp in work.work_packages.values() if wp.sed_ref}
    for sed_id in used_seds:
        sed = work.seds.get(sed_id)
        if sed is None:
            continue
        expected = expected_duration(sed)
        sed.duration = DurationModel(kind=DurationKind.FIXED, value=expected)
    for kind in work.event_kinds.values():
        if kind.category.value in risk_categories:
            kind.stream = StreamSpec(kind=StreamKind.NONE)
    # график без вмешательств: ни политик, ни сигналов, ни денег
    work.decision_points.clear()
    work.observations.clear()
    work.monitor_rules.clear()
    work.economic_items.clear()
    # базовый план строится по проекту целиком, без фактов статуса
    for wp in work.work_packages.values():
        wp.planned_start = None
        wp.planned_finish = None
    horizon = horizon_hours or max(work.enterprise.planning_horizon_hours * 5, 24 * 365 * 2)
    scenario = Scenario(
        id="baseline", name="Базовый план", enterprise_id=work.enterprise.id,
        horizon_hours=horizon, project_mode="plan",
    )
    result = simulate(
        work, scenario, seed=0,
        config=SimulationConfig(stop_when_projects_done=True, max_log_entries=0),
    )
    reports = {
        pid: _apply_baseline(model, pid, result.projects.get(pid, {}), result.status)
        for pid in targets
    }
    return reports[project_id] if project_id is not None else reports


def _apply_baseline(
    model: EnterpriseModel, project_id: str, data: dict[str, Any], status: str
) -> dict[str, Any]:
    """Записать сроки пакетов проекта из прогона базового плана."""
    project = model.projects[project_id]
    packages = data.get("work_packages", {})
    finish_time = 0.0
    for item in packages.values():
        if item.get("start") is not None and item.get("finish") is not None:
            finish_time = max(finish_time, float(item["finish"]))
    scheduled = 0
    unscheduled: list[str] = []
    for wid, wp in model.work_packages.items():
        if wp.project_ref != project_id or wp.level_of_effort:
            continue
        item = packages.get(wid, {})
        if item.get("start") is not None and item.get("finish") is not None:
            wp.planned_start = float(item["start"]) + project.planned_start
            wp.planned_finish = float(item["finish"]) + project.planned_start
            scheduled += 1
        else:
            unscheduled.append(wid)
    planned_finish = project.planned_start + finish_time if finish_time else None
    for wp in model.work_packages.values():
        if wp.project_ref == project_id and wp.level_of_effort:
            wp.planned_start = project.planned_start
            wp.planned_finish = planned_finish or wp.planned_finish
    if planned_finish:
        project.planned_finish = planned_finish
    return {
        "project": project_id,
        "planned_finish": planned_finish,
        "planned_duration_days": round(finish_time / 24.0, 2) if finish_time else None,
        "scheduled": scheduled,
        "unscheduled": unscheduled,
        "status": status,
    }


def project_summary(result_projects: dict[str, Any]) -> dict[str, Any]:
    """Краткий итог прогона по проектам: срок, EAC, индексы на конце."""
    out = {}
    for pid, data in result_projects.items():
        final = data.get("final", {})
        out[pid] = {
            "finished_at": data.get("finished_at"),
            "planned_finish": data.get("planned_finish"),
            "late_hours": (
                None
                if data.get("finished_at") is None or data.get("planned_finish") is None
                else round(data["finished_at"] - data["planned_finish"], 4)
            ),
            "ac": final.get("ac"),
            "ev": final.get("ev"),
            "bac": final.get("bac"),
            "cpi": final.get("cpi"),
            "spi": final.get("spi"),
            "eac": final.get("eac"),
        }
    return out


__all__ = ["build_baseline", "evm_indicators", "evm_status", "project_summary"]
