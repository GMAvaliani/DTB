"""Shared row-based (CSV/Excel) codecs for model entities.

Compact cell syntax:
- flows / resource requirements: ``"steel:2; energy:0.5"``
- triggers: ``"on_event:customer_order"``, ``"on_sed_completed:production"``,
  ``"at_time:5"``, ``"recurring:24@8"`` (interval@first-occurrence)
- duration: ``"fixed:4"``, ``"uniform:2..6"``, ``"triangular:2..6@4"``,
  ``"normal:5,1"``, ``"lognormal:1,0.5"``, ``"exponential:5"``
- goal targets: ``"revenue:>=:100000"`` or ``"revenue:>=:100000:720"`` (deadline)
- complex structures (outcome branches, event effects) are JSON cells.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from dtb.domain import (
    DurationKind,
    DurationModel,
    Employee,
    Equipment,
    ERPMapping,
    ERPObject,
    EventCategory,
    EventEffect,
    EventKind,
    Goal,
    KPIDefinition,
    OrgUnit,
    OrgUnitKind,
    OutcomeBranch,
    Resource,
    ResourceKind,
    ResourceRequirement,
    SEDDefinition,
    SeriesPoint,
    StreamKind,
    StreamSpec,
    TargetMetric,
    TriggerKind,
    TriggerSpec,
    UtilityFlow,
    UtilityKind,
    UtilityType,
)
from dtb.domain.model import EnterpriseModel
from dtb.integration.base import ImportReport

# Sheet/file names in the workbook layout (8-view structure of the source spec).
SHEETS = [
    "goals",  # view 1
    "seds",  # view 2
    "utility_types",  # view 2/6
    "kpis",  # view 3 definitions
    "plans",  # view 3
    "actuals",  # view 3
    "org_units",  # view 4
    "employees",  # view 4
    "equipment",  # view 5
    "resources",  # view 6
    "event_kinds",  # environment events
    "erp_objects",  # view 7
    "erp_mappings",  # view 8
    "initial_state",  # view 7
]


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _f(value: Any, default: float | None = None) -> float | None:
    text = _s(value)
    if not text:
        return default
    return float(text.replace(",", "."))


def _i(value: Any, default: int | None = None) -> int | None:
    text = _s(value)
    return int(float(text)) if text else default


def parse_flows(cell: Any) -> list[UtilityFlow]:
    out = []
    for part in _s(cell).split(";"):
        part = part.strip()
        if not part:
            continue
        ref, _, qty = part.partition(":")
        out.append(UtilityFlow(utility_type_id=ref.strip(), quantity=float(qty or 1)))
    return out


def encode_flows(flows: list[UtilityFlow]) -> str:
    return "; ".join(f"{f.utility_type_id}:{f.quantity:g}" for f in flows)


def parse_requirements(cell: Any) -> list[ResourceRequirement]:
    out = []
    for part in _s(cell).split(";"):
        part = part.strip()
        if not part:
            continue
        ref, _, qty = part.partition(":")
        out.append(ResourceRequirement(resource_id=ref.strip(), quantity=float(qty or 1)))
    return out


def encode_requirements(reqs: list[ResourceRequirement]) -> str:
    return "; ".join(f"{r.resource_id}:{r.quantity:g}" for r in reqs)


def parse_duration(cell: Any) -> DurationModel:
    text = _s(cell) or "fixed:0"
    kind, _, rest = text.partition(":")
    kind = kind.strip().lower()
    rest = rest.strip()
    if kind == "fixed":
        return DurationModel(kind=DurationKind.FIXED, value=float(rest or 0))
    if kind == "uniform":
        low, _, high = rest.partition("..")
        return DurationModel(kind=DurationKind.UNIFORM, low=float(low), high=float(high))
    if kind == "triangular":
        rng, _, mode = rest.partition("@")
        low, _, high = rng.partition("..")
        return DurationModel(
            kind=DurationKind.TRIANGULAR, low=float(low), high=float(high), mode=float(mode)
        )
    if kind in ("normal", "lognormal"):
        mean, _, std = rest.replace("±", ",").partition(",")
        return DurationModel(
            kind=DurationKind(kind), mean=float(mean), std=float(std or 1)
        )
    if kind == "exponential":
        return DurationModel(kind=DurationKind.EXPONENTIAL, mean=float(rest))
    raise ValueError(f"неизвестный формат длительности: {text!r}")


def encode_duration(d: DurationModel) -> str:
    if d.kind == DurationKind.FIXED:
        return f"fixed:{d.value:g}"
    if d.kind == DurationKind.UNIFORM:
        return f"uniform:{d.low:g}..{d.high:g}"
    if d.kind == DurationKind.TRIANGULAR:
        return f"triangular:{d.low:g}..{d.high:g}@{d.mode:g}"
    if d.kind in (DurationKind.NORMAL, DurationKind.LOGNORMAL):
        return f"{d.kind.value}:{d.mean:g},{d.std:g}"
    return f"exponential:{d.mean:g}"


def parse_triggers(cell: Any) -> list[TriggerSpec]:
    out = []
    for part in _s(cell).split(";"):
        part = part.strip()
        if not part:
            continue
        kind, _, rest = part.partition(":")
        kind = kind.strip().lower()
        rest = rest.strip()
        if kind == "on_event":
            out.append(TriggerSpec(kind=TriggerKind.ON_EVENT, event_kind_id=rest))
        elif kind == "on_sed_completed":
            out.append(TriggerSpec(kind=TriggerKind.ON_SED_COMPLETED, sed_id=rest))
        elif kind == "at_time":
            out.append(TriggerSpec(kind=TriggerKind.AT_TIME, at=float(rest)))
        elif kind == "recurring":
            interval, _, start = rest.partition("@")
            out.append(
                TriggerSpec(
                    kind=TriggerKind.RECURRING,
                    interval=float(interval),
                    start=float(start or 0),
                )
            )
        else:
            raise ValueError(f"неизвестный триггер: {part!r}")
    return out


def encode_triggers(triggers: list[TriggerSpec]) -> str:
    parts = []
    for trig in triggers:
        if trig.kind == TriggerKind.ON_EVENT:
            parts.append(f"on_event:{trig.event_kind_id}")
        elif trig.kind == TriggerKind.ON_SED_COMPLETED:
            parts.append(f"on_sed_completed:{trig.sed_id}")
        elif trig.kind == TriggerKind.AT_TIME:
            parts.append(f"at_time:{trig.at:g}")
        else:
            parts.append(f"recurring:{trig.interval:g}@{trig.start:g}")
    return "; ".join(parts)


def parse_targets(cell: Any) -> list[TargetMetric]:
    out = []
    for part in _s(cell).split(";"):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        if len(bits) < 3:
            raise ValueError(f"неверный формат целевого показателя: {part!r}")
        out.append(
            TargetMetric(
                kpi_id=bits[0].strip(),
                comparison=bits[1].strip(),
                target_value=float(bits[2]),
                deadline_hours=float(bits[3]) if len(bits) > 3 and bits[3] else None,
            )
        )
    return out


def encode_targets(targets: list[TargetMetric]) -> str:
    parts = []
    for tm in targets:
        base = f"{tm.kpi_id}:{tm.comparison}:{tm.target_value:g}"
        if tm.deadline_hours is not None:
            base += f":{tm.deadline_hours:g}"
        parts.append(base)
    return "; ".join(parts)


def _list(cell: Any) -> list[str]:
    return [p.strip() for p in _s(cell).split(";") if p.strip()]


def _json_cell(cell: Any) -> Any:
    text = _s(cell)
    return json.loads(text) if text else None


# ---------------------------------------------------------------- row appliers --


def _apply_goal(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    gid = _s(row.get("id"))
    is_new = gid not in model.goals
    model.goals[gid] = Goal(
        id=gid,
        name=_s(row.get("name")),
        description=_s(row.get("description")),
        parent_id=_s(row.get("parent_id")) or None,
        responsible_org_unit_id=_s(row.get("responsible_org_unit_id")) or None,
        priority=_i(row.get("priority"), 100) or 100,
        target_metrics=parse_targets(row.get("targets")),
        sed_ids=_list(row.get("sed_ids")),
        resource_ids=_list(row.get("resource_ids")),
    )
    return gid, is_new


def _apply_utility_type(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    uid = _s(row.get("id"))
    is_new = uid not in model.utility_types
    kind = _s(row.get("kind")) or "other"
    model.utility_types[uid] = UtilityType(
        id=uid,
        name=_s(row.get("name")),
        description=_s(row.get("description")),
        kind=UtilityKind(kind) if kind in UtilityKind._value2member_map_ else UtilityKind.OTHER,
        unit=_s(row.get("unit")) or "unit",
    )
    return uid, is_new


def _apply_sed(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    sid = _s(row.get("id"))
    is_new = sid not in model.seds
    branches_raw = _json_cell(row.get("branches"))
    branches = (
        [OutcomeBranch.model_validate(b) for b in branches_raw] if branches_raw else []
    )
    model.seds[sid] = SEDDefinition(
        id=sid,
        name=_s(row.get("name")),
        description=_s(row.get("description")),
        goal_id=_s(row.get("goal_id")) or None,
        parent_id=_s(row.get("parent_id")) or None,
        org_unit_id=_s(row.get("org_unit_id")) or None,
        inputs=parse_flows(row.get("inputs")),
        outputs=parse_flows(row.get("outputs")),
        outcome_branches=branches,
        resources=parse_requirements(row.get("resources")),
        equipment_ids=_list(row.get("equipment_ids")),
        duration=parse_duration(row.get("duration")),
        triggers=parse_triggers(row.get("triggers")),
        start_condition=_s(row.get("start_condition")) or None,
        continuation_condition=_s(row.get("continuation_condition")) or None,
        priority=_i(row.get("priority"), 100) or 100,
        max_concurrent=_i(row.get("max_concurrent")),
    )
    return sid, is_new


def _apply_resource(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    rid = _s(row.get("id"))
    is_new = rid not in model.resources
    kind = _s(row.get("kind")) or "other"
    model.resources[rid] = Resource(
        id=rid,
        name=_s(row.get("name")),
        kind=ResourceKind(kind) if kind in ResourceKind._value2member_map_ else ResourceKind.OTHER,
        capacity=_f(row.get("capacity"), 1.0) or 0.0,
        unit=_s(row.get("unit")) or "unit",
        org_unit_id=_s(row.get("org_unit_id")) or None,
        cost_per_hour=_f(row.get("cost_per_hour"), 0.0) or 0.0,
    )
    return rid, is_new


def _apply_org_unit(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    oid = _s(row.get("id"))
    is_new = oid not in model.org_units
    kind = _s(row.get("kind")) or "department"
    model.org_units[oid] = OrgUnit(
        id=oid,
        name=_s(row.get("name")),
        kind=OrgUnitKind(kind) if kind in OrgUnitKind._value2member_map_ else OrgUnitKind.DEPARTMENT,
        parent_id=_s(row.get("parent_id")) or None,
        headcount_plan=_i(row.get("headcount_plan")),
    )
    return oid, is_new


def _apply_employee(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    eid = _s(row.get("id"))
    is_new = eid not in model.employees
    model.employees[eid] = Employee(
        id=eid,
        name=_s(row.get("name")),
        org_unit_id=_s(row.get("org_unit_id")) or None,
        position=_s(row.get("position")),
        skills=_list(row.get("skills")),
        resource_id=_s(row.get("resource_id")) or None,
    )
    return eid, is_new


def _apply_equipment(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    qid = _s(row.get("id"))
    is_new = qid not in model.equipment
    model.equipment[qid] = Equipment(
        id=qid,
        name=_s(row.get("name")),
        tech_place=_s(row.get("tech_place")),
        org_unit_id=_s(row.get("org_unit_id")) or None,
        resource_id=_s(row.get("resource_id")) or None,
        status=_s(row.get("status")) or "operational",
    )
    return qid, is_new


def _apply_event_kind(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    kid = _s(row.get("id"))
    is_new = kid not in model.event_kinds
    category = _s(row.get("category")) or "external"
    stream_kind = _s(row.get("stream_kind")) or "none"
    times = [float(x) for x in _list(row.get("times"))]
    stream = StreamSpec(
        kind=StreamKind(stream_kind)
        if stream_kind in StreamKind._value2member_map_
        else StreamKind.NONE,
        rate_per_hour=_f(row.get("rate_per_hour")),
        times=times,
        interval=_f(row.get("interval")),
        start=_f(row.get("start"), 0.0) or 0.0,
    )
    effects_raw = _json_cell(row.get("effects")) or []
    model.event_kinds[kid] = EventKind(
        id=kid,
        name=_s(row.get("name")),
        description=_s(row.get("description")),
        category=EventCategory(category)
        if category in EventCategory._value2member_map_
        else EventCategory.EXTERNAL,
        stream=stream,
        effects=[EventEffect.model_validate(e) for e in effects_raw],
    )
    return kid, is_new


def _apply_kpi(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    kid = _s(row.get("id"))
    is_new = kid not in model.kpis
    model.kpis[kid] = KPIDefinition(
        id=kid,
        name=_s(row.get("name")),
        expression=_s(row.get("expression")) or "0",
        unit=_s(row.get("unit")),
    )
    return kid, is_new


def _apply_erp_object(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    oid = _s(row.get("id"))
    is_new = oid not in model.erp_objects
    payload = _json_cell(row.get("payload")) or {}
    model.erp_objects[oid] = ERPObject(
        id=oid,
        name=_s(row.get("name")),
        system=_s(row.get("system")) or "import",
        object_type=_s(row.get("object_type")),
        external_id=_s(row.get("external_id")),
        payload=payload,
    )
    return oid, is_new


def _apply_erp_mapping(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    mapping = ERPMapping(
        entity_type=_s(row.get("entity_type")),
        entity_id=_s(row.get("entity_id")),
        erp_object_id=_s(row.get("erp_object_id")),
        kind=_s(row.get("kind")) or "represents",
    )
    for existing in model.erp_mappings:
        if (
            existing.entity_type == mapping.entity_type
            and existing.entity_id == mapping.entity_id
            and existing.erp_object_id == mapping.erp_object_id
        ):
            return mapping.entity_id, False
    model.erp_mappings.append(mapping)
    return mapping.entity_id, True


def _apply_initial_state(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    uid = _s(row.get("utility_type_id") or row.get("id"))
    is_new = uid not in model.initial_state.utility_stocks
    model.initial_state.utility_stocks[uid] = _f(row.get("quantity"), 0.0) or 0.0
    return uid, is_new


def _apply_series(kind: str) -> Callable[[EnterpriseModel, dict[str, Any]], tuple[str, bool]]:
    def apply(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
        kpi_id = _s(row.get("kpi_id") or row.get("id"))
        target = model.plans if kind == "plans" else model.actuals
        series = target.setdefault(kpi_id, [])
        series.append(SeriesPoint(t=_f(row.get("t"), 0.0) or 0.0, value=_f(row.get("value"), 0.0) or 0.0))
        series.sort(key=lambda p: p.t)
        return kpi_id, True

    return apply


_APPLIERS: dict[str, Callable[[EnterpriseModel, dict[str, Any]], tuple[str, bool]]] = {
    "goals": _apply_goal,
    "utility_types": _apply_utility_type,
    "seds": _apply_sed,
    "resources": _apply_resource,
    "org_units": _apply_org_unit,
    "employees": _apply_employee,
    "equipment": _apply_equipment,
    "event_kinds": _apply_event_kind,
    "kpis": _apply_kpi,
    "erp_objects": _apply_erp_object,
    "erp_mappings": _apply_erp_mapping,
    "initial_state": _apply_initial_state,
    "plans": _apply_series("plans"),
    "actuals": _apply_series("actuals"),
}

_TYPE_NAME = {
    "goals": "goal",
    "utility_types": "utility_type",
    "seds": "sed",
    "resources": "resource",
    "org_units": "org_unit",
    "employees": "employee",
    "equipment": "equipment",
    "event_kinds": "event_kind",
    "kpis": "kpi",
    "erp_objects": "erp_object",
    "erp_mappings": "erp_mapping",
    "initial_state": "initial_stock",
    "plans": "plan_point",
    "actuals": "actual_point",
}


def apply_rows(
    model: EnterpriseModel,
    sheet: str,
    rows: list[dict[str, Any]],
    report: ImportReport,
) -> None:
    applier = _APPLIERS.get(sheet)
    if applier is None:
        report.warn(sheet, f"неизвестный тип листа/файла {sheet!r} — пропущен")
        return
    for index, row in enumerate(rows, start=2):  # header is row 1
        if not any(_s(v) for v in row.values()):
            continue
        needs_id = sheet not in ("erp_mappings", "plans", "actuals", "initial_state")
        if needs_id and not _s(row.get("id")):
            report.error(sheet, "отсутствует id", row=index)
            continue
        try:
            _, is_new = applier(model, row)
            report.count(_TYPE_NAME[sheet], is_new)
        except (ValueError, TypeError, KeyError) as exc:
            report.error(sheet, str(exc), row=index)


# ---------------------------------------------------------------- row exporters --


def model_to_rows(model: EnterpriseModel, sheet: str) -> tuple[list[str], list[list[Any]]]:
    if sheet == "goals":
        header = ["id", "name", "description", "parent_id", "responsible_org_unit_id",
                  "priority", "targets", "sed_ids", "resource_ids"]
        rows = [
            [g.id, g.name, g.description, g.parent_id or "", g.responsible_org_unit_id or "",
             g.priority, encode_targets(g.target_metrics), "; ".join(g.sed_ids),
             "; ".join(g.resource_ids)]
            for g in model.goals.values()
        ]
    elif sheet == "utility_types":
        header = ["id", "name", "description", "kind", "unit"]
        rows = [[u.id, u.name, u.description, u.kind.value, u.unit] for u in model.utility_types.values()]
    elif sheet == "seds":
        header = ["id", "name", "description", "goal_id", "parent_id", "org_unit_id",
                  "inputs", "outputs", "resources", "equipment_ids", "duration",
                  "triggers", "start_condition", "continuation_condition", "priority",
                  "max_concurrent", "branches"]
        rows = [
            [s.id, s.name, s.description, s.goal_id or "", s.parent_id or "", s.org_unit_id or "",
             encode_flows(s.inputs), encode_flows(s.outputs), encode_requirements(s.resources),
             "; ".join(s.equipment_ids), encode_duration(s.duration), encode_triggers(s.triggers),
             s.start_condition or "", s.continuation_condition or "", s.priority,
             s.max_concurrent or "",
             json.dumps([b.model_dump(mode="json", exclude_defaults=True) for b in s.outcome_branches], ensure_ascii=False)
             if s.outcome_branches else ""]
            for s in model.seds.values()
        ]
    elif sheet == "resources":
        header = ["id", "name", "kind", "capacity", "unit", "org_unit_id", "cost_per_hour"]
        rows = [[r.id, r.name, r.kind.value, r.capacity, r.unit, r.org_unit_id or "", r.cost_per_hour]
                for r in model.resources.values()]
    elif sheet == "org_units":
        header = ["id", "name", "kind", "parent_id", "headcount_plan"]
        rows = [[o.id, o.name, o.kind.value, o.parent_id or "", o.headcount_plan or ""]
                for o in model.org_units.values()]
    elif sheet == "employees":
        header = ["id", "name", "org_unit_id", "position", "skills", "resource_id"]
        rows = [[e.id, e.name, e.org_unit_id or "", e.position, "; ".join(e.skills), e.resource_id or ""]
                for e in model.employees.values()]
    elif sheet == "equipment":
        header = ["id", "name", "tech_place", "org_unit_id", "resource_id", "status"]
        rows = [[q.id, q.name, q.tech_place, q.org_unit_id or "", q.resource_id or "", q.status]
                for q in model.equipment.values()]
    elif sheet == "event_kinds":
        header = ["id", "name", "description", "category", "stream_kind", "rate_per_hour",
                  "times", "interval", "start", "effects"]
        rows = [
            [k.id, k.name, k.description, k.category.value, k.stream.kind.value,
             k.stream.rate_per_hour or "", "; ".join(f"{t:g}" for t in k.stream.times),
             k.stream.interval or "", k.stream.start,
             json.dumps([e.model_dump(mode="json", exclude_defaults=True) for e in k.effects], ensure_ascii=False)
             if k.effects else ""]
            for k in model.event_kinds.values()
        ]
    elif sheet == "kpis":
        header = ["id", "name", "expression", "unit"]
        rows = [[k.id, k.name, k.expression, k.unit] for k in model.kpis.values()]
    elif sheet == "erp_objects":
        header = ["id", "name", "system", "object_type", "external_id", "payload"]
        rows = [[o.id, o.name, o.system, o.object_type, o.external_id,
                 json.dumps(o.payload, ensure_ascii=False) if o.payload else ""]
                for o in model.erp_objects.values()]
    elif sheet == "erp_mappings":
        header = ["entity_type", "entity_id", "erp_object_id", "kind"]
        rows = [[m.entity_type, m.entity_id, m.erp_object_id, m.kind] for m in model.erp_mappings]
    elif sheet == "initial_state":
        header = ["utility_type_id", "quantity"]
        rows = [[uid, qty] for uid, qty in model.initial_state.utility_stocks.items()]
    elif sheet == "plans":
        header = ["kpi_id", "t", "value"]
        rows = [[kid, p.t, p.value] for kid, pts in model.plans.items() for p in pts]
    elif sheet == "actuals":
        header = ["kpi_id", "t", "value"]
        rows = [[kid, p.t, p.value] for kid, pts in model.actuals.items() for p in pts]
    else:
        raise KeyError(f"unknown sheet {sheet!r}")
    return header, rows
