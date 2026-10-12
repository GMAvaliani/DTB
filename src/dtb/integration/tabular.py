"""Shared row-based (CSV/Excel) codecs for model entities.

Compact cell syntax:
- flows / resource requirements: ``"steel:2; energy:0.5"``
- triggers: ``"on_event:customer_order"``, ``"on_sed_completed:production"``,
  ``"at_time:5"``, ``"recurring:24@8"`` (interval@first-occurrence)
- duration: ``"fixed:4"``, ``"uniform:2..6"``, ``"triangular:2..6@4"``,
  ``"normal:5,1"``, ``"lognormal:1,0.5"``, ``"exponential:5"``
- goal targets: ``"revenue:>=:100000"`` or ``"revenue:>=:100000:720"`` (deadline)
- policy rules: ``"stock('steel') < 50 -> buy_steel @ 2; true -> hold"``
  (condition ``->`` action ``@`` magnitude expression)
- policy thresholds: ``"queue:3; load:0.8"`` (referenced in conditions as
  ``threshold('queue')``, tuned by the optimizer via ``policy:<id>.<name>``)
- calendar shifts: ``"пн-пт:8-20; сб:9-14@0.5"`` (days ``:`` hours ``@`` capacity
  factor; days may be omitted for «every day», named ``пн…вс`` or numbered 0–6)
- calendar exceptions: ``"600-648; 1000-1008:работа"`` (model-time interval;
  the ``работа``/``work`` marker turns it into an extra shift)
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
from dtb.domain.calendars import WEEKDAY_NAMES, Calendar, CalendarException, Shift
from dtb.domain.links import GapType, Link
from dtb.domain.resources import QueueDiscipline
from dtb.domain.seds import InterruptionMode
from dtb.domain.economics import (
    EconomicBasis,
    EconomicDirection,
    EconomicItem,
    EconomicKind,
    EconomicsSettings,
)
from dtb.domain.control import (
    Lookahead,
    ControlActionDef,
    ControlLoop,
    DecisionPoint,
    DecisionPointKind,
    Estimator,
    Observation,
    ObservationMode,
    ObservationTarget,
    Policy,
    PolicyRule,
    Subject,
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
    "subjects",  # контур управления: кто действует
    "control_actions",  # контур управления: каталог действий
    "policies",  # контур управления: правила выбора действия
    "decision_points",  # контур управления: когда вызывается политика
    "observations",  # контур управления: что и как наблюдается
    "calendars",  # режимы работы: смены, выходные, остановки
    "economic_items",  # экономика: статьи начисления
    "economics",  # экономика: настройки прогона (валюта, дисконт, разогрев)
    "links",  # ЗАГОТОВКА: связь как объект (движком не используется)
    "gap_types",  # ЗАГОТОВКА: типология разрывов (движком не используется)
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
        interruption_mode=_enum(
            row.get("interruption_mode"), InterruptionMode, InterruptionMode.RESUME
        ),
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
        queue_discipline=_enum(row.get("queue_discipline"), QueueDiscipline, None),
        calendar_ref=_s(row.get("calendar_ref")) or None,
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


def _enum(value: Any, enum_cls, default):
    """Безопасный разбор перечисления: неизвестное значение заменяется на default."""
    text = _s(value)
    return enum_cls(text) if text in enum_cls._value2member_map_ else default


def _b(value: Any, default: bool = True) -> bool:
    text = _s(value).lower()
    if not text:
        return default
    return text not in ("0", "false", "нет", "no", "off", "ложь")


def _apply_subject(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    sid = _s(row.get("id"))
    is_new = sid not in model.subjects
    model.subjects[sid] = Subject(
        id=sid,
        name=_s(row.get("name")),
        description=_s(row.get("description")),
        authority_level=_i(row.get("authority_level"), 1) or 0,
        org_unit_ref=_s(row.get("org_unit_ref")) or None,
        role_ref=_s(row.get("role_ref")) or None,
        external=_b(row.get("external"), False),
    )
    return sid, is_new


def _apply_control_action(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    aid = _s(row.get("id"))
    is_new = aid not in model.control_actions
    effect_raw = _json_cell(row.get("effect"))
    if effect_raw is None:
        raise ValueError(f"действие {aid}: требуется JSON-ячейка effect")
    model.control_actions[aid] = ControlActionDef(
        id=aid,
        name=_s(row.get("name")),
        description=_s(row.get("description")),
        loop=_enum(row.get("loop"), ControlLoop, None),
        effect=EventEffect.model_validate(effect_raw),
        goal_ref=_s(row.get("goal_ref")) or None,
        magnitude=_f(row.get("magnitude")),
        activation_delay=_f(row.get("activation_delay"), 0.0) or 0.0,
        one_time_cost=_f(row.get("one_time_cost"), 0.0) or 0.0,
        recurring_cost=_f(row.get("recurring_cost"), 0.0) or 0.0,
        term=_f(row.get("term")),
        reversible=_b(row.get("reversible"), True),
        success_probability=_f(row.get("success_probability"), 1.0) or 0.0,
        min_authority_level=_i(row.get("min_authority_level"), 0) or 0,
        subject_ref=_s(row.get("subject_ref")) or None,
    )
    return aid, is_new


def parse_policy_rules(cell: Any) -> list[PolicyRule]:
    """``"stock('x') < 5 -> add_line @ 2; true -> hold"`` либо JSON-массив."""
    text = _s(cell)
    if not text:
        return []
    if text.startswith("["):
        return [PolicyRule.model_validate(r) for r in json.loads(text)]
    out: list[PolicyRule] = []
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        condition, sep, tail = part.partition("->")
        if not sep:
            raise ValueError(f"правило политики без '->': {part!r}")
        action, _, magnitude = tail.partition("@")
        out.append(
            PolicyRule(
                condition=condition.strip() or "true",
                action_ref=action.strip(),
                magnitude_expression=magnitude.strip() or None,
            )
        )
    return out


def encode_policy_rules(rules: list[PolicyRule]) -> str:
    return "; ".join(
        f"{r.condition} -> {r.action_ref}"
        + (f" @ {r.magnitude_expression}" if r.magnitude_expression else "")
        for r in rules
    )


def parse_thresholds(cell: Any) -> dict[str, float]:
    """``"queue:3; load:0.8"`` либо JSON-объект порогов политики."""
    text = _s(cell)
    if not text:
        return {}
    if text.startswith("{"):
        return {str(k): float(v) for k, v in json.loads(text).items()}
    out: dict[str, float] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition(":")
        out[name.strip()] = float(value.strip().replace(",", "."))
    return out


def encode_thresholds(thresholds: dict[str, float]) -> str:
    return "; ".join(f"{k}:{v:g}" for k, v in sorted(thresholds.items()))


def _apply_policy(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    pid = _s(row.get("id"))
    is_new = pid not in model.policies
    model.policies[pid] = Policy(
        id=pid,
        name=_s(row.get("name")),
        description=_s(row.get("description")),
        loop=_enum(row.get("loop"), ControlLoop, ControlLoop.REGULATION),
        state_selector=_list(row.get("state_selector")),
        rule=_s(row.get("rule")) or None,
        action_ref=_s(row.get("action_ref")) or None,
        rules=parse_policy_rules(row.get("rules")),
        subject_ref=_s(row.get("subject_ref")) or None,
        priority=_i(row.get("priority"), 100) or 100,
        active_from=_f(row.get("active_from"), 0.0) or 0.0,
        active_to=_f(row.get("active_to")),
        cooldown_hours=_f(row.get("cooldown_hours")),
        thresholds=parse_thresholds(row.get("thresholds")),
        lookahead=(
            Lookahead.model_validate(_json_cell(row.get("lookahead")))
            if _s(row.get("lookahead"))
            else None
        ),
        enabled=_b(row.get("enabled")),
    )
    return pid, is_new


def _apply_decision_point(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    did = _s(row.get("id"))
    is_new = did not in model.decision_points
    scope_raw = _json_cell(row.get("scope"))
    model.decision_points[did] = DecisionPoint(
        id=did,
        name=_s(row.get("name")),
        description=_s(row.get("description")),
        kind=_enum(row.get("kind"), DecisionPointKind, DecisionPointKind.TIMER),
        event_ref=_s(row.get("event_ref")) or None,
        timer_period=_f(row.get("timer_period")),
        timer_start=_f(row.get("timer_start"), 0.0) or 0.0,
        watchdog_condition=_s(row.get("watchdog_condition")) or None,
        requires_decision=_b(row.get("requires_decision")),
        policy_ref=_s(row.get("policy_ref")) or None,
        **({"scope": scope_raw} if scope_raw else {}),
        enabled=_b(row.get("enabled")),
    )
    return did, is_new


def _apply_observation(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    oid = _s(row.get("id"))
    is_new = oid not in model.observations
    model.observations[oid] = Observation(
        id=oid,
        name=_s(row.get("name")),
        description=_s(row.get("description")),
        target_kind=_enum(row.get("target_kind"), ObservationTarget, ObservationTarget.UTILITY_TYPE),
        target_ref=_s(row.get("target_ref")),
        metric=_s(row.get("metric")) or None,
        period=_f(row.get("period"), 1.0) or 1.0,
        delay=_f(row.get("delay"), 0.0) or 0.0,
        relative_error=_f(row.get("relative_error"), 0.0) or 0.0,
        estimator=_enum(row.get("estimator"), Estimator, Estimator.LAST),
        mode=_enum(row.get("mode"), ObservationMode, ObservationMode.TWIN),
        alpha=_f(row.get("alpha"), 0.5) or 0.5,
        enabled=_b(row.get("enabled")),
    )
    return oid, is_new


def parse_shifts(cell: Any) -> list[Shift]:
    """``"пн-пт:8-20; сб:9-14@0.5"`` либо JSON-массив смен."""
    text = _s(cell)
    if not text:
        return []
    if text.startswith("["):
        return [Shift.model_validate(x) for x in json.loads(text)]
    out: list[Shift] = []
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        head, sep, tail = part.rpartition(":")
        hours, days = (tail, head) if sep else (part, "")
        hours, _, factor = hours.partition("@")
        start, _, end = hours.partition("-")
        try:
            start_hour, end_hour = float(start), float(end)
        except ValueError as exc:
            raise ValueError(f"смена {part!r}: ожидается запись «часы 8-20»") from exc
        capacity_factor = float(factor) if factor.strip() else 1.0
        for day in _parse_weekdays(days):
            out.append(
                Shift(
                    day_of_week=day, start_hour=start_hour, end_hour=end_hour,
                    capacity_factor=capacity_factor, label=part,
                )
            )
    return out


def _parse_weekdays(text: str) -> list[int | None]:
    """Дни недели: пусто или ``*`` — все дни; ``пн-пт``; ``сб``; ``0-4``; ``1,3``."""
    text = text.strip().lower()
    if not text or text in ("*", "все", "ежедневно"):
        return [None]
    days: list[int | None] = []
    for chunk in text.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            first, _, last = chunk.partition("-")
            a, b = _weekday_index(first), _weekday_index(last)
            days.extend(range(a, b + 1) if a <= b else list(range(a, 7)) + list(range(0, b + 1)))
        else:
            days.append(_weekday_index(chunk))
    return days or [None]


def _weekday_index(token: str) -> int:
    token = token.strip().lower()
    if token in WEEKDAY_NAMES:
        return WEEKDAY_NAMES.index(token)
    try:
        value = int(token)
    except ValueError as exc:
        raise ValueError(
            f"день недели {token!r} не распознан: допустимы "
            f"{', '.join(WEEKDAY_NAMES)} либо числа 0–6"
        ) from exc
    if not 0 <= value <= 6:
        raise ValueError(f"день недели {value} вне диапазона 0–6")
    return value


def encode_shifts(shifts: list[Shift]) -> str:
    parts = []
    for shift in shifts:
        day = WEEKDAY_NAMES[shift.day_of_week] + ":" if shift.day_of_week is not None else ""
        factor = f"@{shift.capacity_factor:g}" if shift.capacity_factor != 1.0 else ""
        parts.append(f"{day}{shift.start_hour:g}-{shift.end_hour:g}{factor}")
    return "; ".join(parts)


def parse_calendar_exceptions(cell: Any) -> list[CalendarException]:
    """``"600-648; 1000-1008:работа"`` либо JSON-массив исключений."""
    text = _s(cell)
    if not text:
        return []
    if text.startswith("["):
        return [CalendarException.model_validate(x) for x in json.loads(text)]
    out: list[CalendarException] = []
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        interval, _, marker = part.partition(":")
        interval, _, factor = interval.partition("@")
        start, _, end = interval.partition("-")
        working = marker.strip().lower() in ("работа", "work", "смена", "да", "true")
        out.append(
            CalendarException(
                from_hour=float(start), to_hour=float(end), working=working,
                capacity_factor=float(factor) if factor.strip() else 1.0,
                label=part,
            )
        )
    return out


def encode_calendar_exceptions(exceptions: list[CalendarException]) -> str:
    parts = []
    for exc in exceptions:
        marker = ":работа" if exc.working else ""
        factor = f"@{exc.capacity_factor:g}" if exc.working and exc.capacity_factor != 1.0 else ""
        parts.append(f"{exc.from_hour:g}-{exc.to_hour:g}{factor}{marker}")
    return "; ".join(parts)


def _apply_calendar(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    cid = _s(row.get("id"))
    is_new = cid not in model.calendars
    model.calendars[cid] = Calendar(
        id=cid,
        name=_s(row.get("name")),
        description=_s(row.get("description")),
        shifts=parse_shifts(row.get("shifts")),
        exceptions=parse_calendar_exceptions(row.get("exceptions")),
        off_shift_factor=_f(row.get("off_shift_factor"), 0.0) or 0.0,
        enabled=_b(row.get("enabled")),
    )
    return cid, is_new


def _apply_economic_item(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    iid = _s(row.get("id"))
    is_new = iid not in model.economic_items
    model.economic_items[iid] = EconomicItem(
        id=iid,
        name=_s(row.get("name")),
        description=_s(row.get("description")),
        article=_s(row.get("article")) or "прочее",
        kind=_enum(row.get("kind"), EconomicKind, EconomicKind.RATE),
        basis=_enum(row.get("basis"), EconomicBasis, EconomicBasis.CONSTANT),
        direction=_enum(row.get("direction"), EconomicDirection, EconomicDirection.EXPENSE),
        ref=_s(row.get("ref")) or None,
        rate=_f(row.get("rate"), 0.0) or 0.0,
        expression=_s(row.get("expression")) or None,
        enabled=_b(row.get("enabled")),
    )
    return iid, is_new


def _apply_link(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    """ЗАГОТОВКА: связь сохраняется и версионируется, расчётом не используется."""
    lid = _s(row.get("id"))
    is_new = lid not in model.links
    model.links[lid] = Link(
        id=lid,
        name=_s(row.get("name")),
        description=_s(row.get("description")),
        source_sed_id=_s(row.get("source_sed_id")),
        target_sed_id=_s(row.get("target_sed_id")),
        utility_type_id=_s(row.get("utility_type_id")) or None,
        health_criterion=_s(row.get("health_criterion")) or None,
        delay=_f(row.get("delay")),
        reaction_horizon=_f(row.get("reaction_horizon")),
        critical_moment=_f(row.get("critical_moment")),
        responsible_subject_ref=_s(row.get("responsible_subject_ref")) or None,
    )
    return lid, is_new


def _apply_gap_type(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    """ЗАГОТОВКА: справочник типов разрывов без логики локализации."""
    gid = _s(row.get("id"))
    is_new = gid not in model.gap_types
    model.gap_types[gid] = GapType(
        id=gid,
        name=_s(row.get("name")),
        description=_s(row.get("description")),
        broken_object=_s(row.get("broken_object")),
        localization_check=_s(row.get("localization_check")) or None,
        decision_class=_s(row.get("decision_class")),
        closure_mode=_s(row.get("closure_mode")),
    )
    return gid, is_new


def _apply_economics(model: EnterpriseModel, row: dict[str, Any]) -> tuple[str, bool]:
    """Лист настроек экономики: одна строка на модель."""
    model.economics = EconomicsSettings(
        currency=_s(row.get("currency")) or "руб",
        discount_rate_annual=_f(row.get("discount_rate_annual"), 0.0) or 0.0,
        hours_per_year=_f(row.get("hours_per_year"), 8760.0) or 8760.0,
        warmup_hours=_f(row.get("warmup_hours"), 0.0) or 0.0,
        include_control_costs=_b(row.get("include_control_costs")),
        control_article=_s(row.get("control_article")) or "управление",
    )
    return "economics", True


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
    "subjects": _apply_subject,
    "control_actions": _apply_control_action,
    "policies": _apply_policy,
    "decision_points": _apply_decision_point,
    "observations": _apply_observation,
    "calendars": _apply_calendar,
    "economic_items": _apply_economic_item,
    "economics": _apply_economics,
    "links": _apply_link,
    "gap_types": _apply_gap_type,
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
    "subjects": "subject",
    "control_actions": "control_action",
    "policies": "policy",
    "decision_points": "decision_point",
    "observations": "observation",
    "calendars": "calendar",
    "economic_items": "economic_item",
    "economics": "economics_settings",
    "links": "link",
    "gap_types": "gap_type",
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
        needs_id = sheet not in (
            "erp_mappings", "plans", "actuals", "initial_state", "economics",
        )
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
                  "max_concurrent", "interruption_mode", "branches"]
        rows = [
            [s.id, s.name, s.description, s.goal_id or "", s.parent_id or "", s.org_unit_id or "",
             encode_flows(s.inputs), encode_flows(s.outputs), encode_requirements(s.resources),
             "; ".join(s.equipment_ids), encode_duration(s.duration), encode_triggers(s.triggers),
             s.start_condition or "", s.continuation_condition or "", s.priority,
             s.max_concurrent or "", s.interruption_mode.value,
             json.dumps([b.model_dump(mode="json", exclude_defaults=True) for b in s.outcome_branches], ensure_ascii=False)
             if s.outcome_branches else ""]
            for s in model.seds.values()
        ]
    elif sheet == "resources":
        header = ["id", "name", "kind", "capacity", "unit", "org_unit_id", "cost_per_hour",
                  "queue_discipline", "calendar_ref"]
        rows = [[r.id, r.name, r.kind.value, r.capacity, r.unit, r.org_unit_id or "",
                 r.cost_per_hour,
                 r.queue_discipline.value if r.queue_discipline else "", r.calendar_ref or ""]
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
    elif sheet == "subjects":
        header = ["id", "name", "description", "authority_level", "org_unit_ref",
                  "role_ref", "external"]
        rows = [[s.id, s.name, s.description, s.authority_level, s.org_unit_ref or "",
                 s.role_ref or "", int(s.external)]
                for s in model.subjects.values()]
    elif sheet == "control_actions":
        header = ["id", "name", "description", "loop", "effect", "goal_ref", "magnitude",
                  "activation_delay", "one_time_cost", "recurring_cost", "term",
                  "reversible", "success_probability", "min_authority_level", "subject_ref"]
        rows = [
            [a.id, a.name, a.description, a.loop.value if a.loop else "",
             json.dumps(a.effect.model_dump(mode="json", exclude_defaults=True), ensure_ascii=False),
             a.goal_ref or "", a.magnitude if a.magnitude is not None else "",
             a.activation_delay, a.one_time_cost, a.recurring_cost,
             a.term if a.term is not None else "", int(a.reversible),
             a.success_probability, a.min_authority_level, a.subject_ref or ""]
            for a in model.control_actions.values()
        ]
    elif sheet == "policies":
        header = ["id", "name", "description", "loop", "rule", "action_ref", "rules",
                  "thresholds", "lookahead", "state_selector", "subject_ref", "priority",
                  "active_from", "active_to", "cooldown_hours", "enabled"]
        rows = [
            [p.id, p.name, p.description, p.loop.value, p.rule or "", p.action_ref or "",
             encode_policy_rules(p.rules), encode_thresholds(p.thresholds),
             json.dumps(
                 p.lookahead.model_dump(mode="json", exclude_defaults=True),
                 ensure_ascii=False,
             ) if p.lookahead else "",
             "; ".join(p.state_selector), p.subject_ref or "",
             p.priority, p.active_from, p.active_to if p.active_to is not None else "",
             p.cooldown_hours if p.cooldown_hours is not None else "", int(p.enabled)]
            for p in model.policies.values()
        ]
    elif sheet == "decision_points":
        header = ["id", "name", "description", "kind", "event_ref", "timer_period",
                  "timer_start", "watchdog_condition", "requires_decision", "policy_ref",
                  "scope", "enabled"]
        rows = [
            [d.id, d.name, d.description, d.kind.value, d.event_ref or "",
             d.timer_period if d.timer_period is not None else "", d.timer_start,
             d.watchdog_condition or "", int(d.requires_decision), d.policy_ref or "",
             "" if d.scope.all else json.dumps(
                 d.scope.model_dump(mode="json", exclude_defaults=True), ensure_ascii=False
             ),
             int(d.enabled)]
            for d in model.decision_points.values()
        ]
    elif sheet == "observations":
        header = ["id", "name", "description", "target_kind", "target_ref", "metric",
                  "period", "delay", "relative_error", "estimator", "mode", "alpha", "enabled"]
        rows = [
            [o.id, o.name, o.description, o.target_kind.value, o.target_ref, o.metric or "",
             o.period, o.delay, o.relative_error, o.estimator.value, o.mode.value,
             o.alpha, int(o.enabled)]
            for o in model.observations.values()
        ]
    elif sheet == "calendars":
        header = ["id", "name", "description", "shifts", "exceptions",
                  "off_shift_factor", "enabled"]
        rows = [
            [c.id, c.name, c.description, encode_shifts(c.shifts),
             encode_calendar_exceptions(c.exceptions), c.off_shift_factor, int(c.enabled)]
            for c in model.calendars.values()
        ]
    elif sheet == "economic_items":
        header = ["id", "name", "description", "article", "kind", "basis", "direction",
                  "ref", "rate", "expression", "enabled"]
        rows = [
            [i.id, i.name, i.description, i.article, i.kind.value, i.basis.value,
             i.direction.value, i.ref or "", i.rate, i.expression or "", int(i.enabled)]
            for i in model.economic_items.values()
        ]
    elif sheet == "economics":
        e = model.economics
        header = ["currency", "discount_rate_annual", "hours_per_year", "warmup_hours",
                  "include_control_costs", "control_article"]
        rows = [[e.currency, e.discount_rate_annual, e.hours_per_year, e.warmup_hours,
                 int(e.include_control_costs), e.control_article]]
    elif sheet == "links":
        header = ["id", "name", "description", "source_sed_id", "target_sed_id",
                  "utility_type_id", "health_criterion", "delay", "reaction_horizon",
                  "critical_moment", "responsible_subject_ref", "implemented"]
        rows = [
            [x.id, x.name, x.description, x.source_sed_id, x.target_sed_id,
             x.utility_type_id or "", x.health_criterion or "",
             x.delay if x.delay is not None else "",
             x.reaction_horizon if x.reaction_horizon is not None else "",
             x.critical_moment if x.critical_moment is not None else "",
             x.responsible_subject_ref or "", int(x.implemented)]
            for x in model.links.values()
        ]
    elif sheet == "gap_types":
        header = ["id", "name", "description", "broken_object", "localization_check",
                  "decision_class", "closure_mode"]
        rows = [
            [x.id, x.name, x.description, x.broken_object, x.localization_check or "",
             x.decision_class, x.closure_mode]
            for x in model.gap_types.values()
        ]
    else:
        raise KeyError(f"unknown sheet {sheet!r}")
    return header, rows
