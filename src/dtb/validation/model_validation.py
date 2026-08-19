"""Pre-simulation model validation.

Simulation must not start while the model has CRITICAL issues (Section 36 of
the platform specification): broken references, invalid expressions,
zero-duration trigger cycles without exit conditions, unhandled events, etc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from dtb.common.expr import ExpressionError, SafeExpression
from dtb.domain.events import EffectKind, StreamKind
from dtb.domain.model import EnterpriseModel
from dtb.domain.scenario import Scenario
from dtb.domain.seds import DurationKind, TriggerKind


class Severity(str, Enum):
    CRITICAL = "critical"  # blocks simulation
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationIssue:
    severity: Severity
    code: str
    entity_type: str
    entity_id: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "message": self.message,
        }


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def critical(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.CRITICAL]

    @property
    def ok_to_simulate(self) -> bool:
        return not self.critical

    def add(self, severity: Severity, code: str, etype: str, eid: str, message: str) -> None:
        self.issues.append(ValidationIssue(severity, code, etype, eid, message))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok_to_simulate": self.ok_to_simulate,
            "critical": len(self.critical),
            "warnings": sum(1 for i in self.issues if i.severity == Severity.WARNING),
            "info": sum(1 for i in self.issues if i.severity == Severity.INFO),
            "issues": [i.to_dict() for i in self.issues],
        }


def _check_expr(
    report: ValidationReport, source: str | None, etype: str, eid: str, code: str
) -> None:
    if not source:
        return
    try:
        SafeExpression(source)
    except ExpressionError as exc:
        report.add(Severity.CRITICAL, code, etype, eid, str(exc))


def validate_model(model: EnterpriseModel) -> ValidationReport:
    r = ValidationReport()
    utypes = model.utility_types
    seds = model.seds
    resources = model.resources
    goals = model.goals
    events = model.event_kinds
    kpis = model.kpis
    org_units = model.org_units

    # --- goals ------------------------------------------------------------------
    for goal in goals.values():
        if goal.parent_id and goal.parent_id not in goals:
            r.add(Severity.CRITICAL, "broken_ref", "goal", goal.id,
                  f"родительская цель {goal.parent_id!r} не существует")
        if goal.responsible_org_unit_id and goal.responsible_org_unit_id not in org_units:
            r.add(Severity.WARNING, "broken_ref", "goal", goal.id,
                  f"центр ответственности {goal.responsible_org_unit_id!r} не существует")
        for tm in goal.target_metrics:
            if tm.kpi_id not in kpis:
                r.add(Severity.CRITICAL, "broken_ref", "goal", goal.id,
                      f"целевой показатель ссылается на несуществующий KPI {tm.kpi_id!r}")
        for sed_id in goal.sed_ids:
            if sed_id not in seds:
                r.add(Severity.WARNING, "broken_ref", "goal", goal.id,
                      f"связанный СЭД {sed_id!r} не существует")
        for expr in goal.constraints:
            _check_expr(r, expr, "goal", goal.id, "invalid_expression")
    _check_hierarchy_cycles(r, {g.id: g.parent_id for g in goals.values()}, "goal")

    # --- SEDs -------------------------------------------------------------------
    reachable = _reachable_seds(model)
    for sed in seds.values():
        if sed.parent_id and sed.parent_id not in seds:
            r.add(Severity.CRITICAL, "broken_ref", "sed", sed.id,
                  f"родительский СЭД {sed.parent_id!r} не существует")
        if sed.goal_id and sed.goal_id not in goals:
            r.add(Severity.WARNING, "broken_ref", "sed", sed.id,
                  f"цель {sed.goal_id!r} не существует")
        if sed.org_unit_id and sed.org_unit_id not in org_units:
            r.add(Severity.WARNING, "broken_ref", "sed", sed.id,
                  f"орг-единица {sed.org_unit_id!r} не существует")
        for flow in sed.inputs + sed.outputs:
            if flow.utility_type_id not in utypes:
                r.add(Severity.CRITICAL, "broken_ref", "sed", sed.id,
                      f"поток ссылается на несуществующую полезность {flow.utility_type_id!r}")
        for branch in sed.outcome_branches:
            for flow in branch.outputs:
                if flow.utility_type_id not in utypes:
                    r.add(Severity.CRITICAL, "broken_ref", "sed", sed.id,
                          f"ветвь {branch.label!r}: несуществующая полезность {flow.utility_type_id!r}")
            for spawn_id in branch.spawn_sed_ids:
                if spawn_id not in seds:
                    r.add(Severity.CRITICAL, "broken_ref", "sed", sed.id,
                          f"ветвь {branch.label!r}: несуществующий СЭД {spawn_id!r}")
        for req in sed.resources:
            if req.resource_id not in resources:
                r.add(Severity.CRITICAL, "missing_resource", "sed", sed.id,
                      f"требуемый ресурс {req.resource_id!r} не существует")
        for trig in sed.triggers:
            if trig.kind == TriggerKind.ON_EVENT and trig.event_kind_id not in events:
                r.add(Severity.CRITICAL, "broken_ref", "sed", sed.id,
                      f"триггер ссылается на несуществующее событие {trig.event_kind_id!r}")
            if trig.kind == TriggerKind.ON_SED_COMPLETED and trig.sed_id not in seds:
                r.add(Severity.CRITICAL, "broken_ref", "sed", sed.id,
                      f"триггер ссылается на несуществующий СЭД {trig.sed_id!r}")
            _check_expr(r, trig.condition, "sed", sed.id, "invalid_expression")
            _check_expr(r, trig.count_expression, "sed", sed.id, "invalid_expression")
        _check_expr(r, sed.start_condition, "sed", sed.id, "invalid_expression")
        _check_expr(r, sed.continuation_condition, "sed", sed.id, "invalid_expression")
        has_outputs = bool(sed.outputs) or any(
            b.outputs for b in sed.outcome_branches
        )
        if not has_outputs:
            r.add(Severity.WARNING, "sed_no_outputs", "sed", sed.id,
                  "СЭД не формирует выходных полезностей")
        if not sed.inputs and not sed.resources:
            r.add(Severity.INFO, "sed_no_inputs", "sed", sed.id,
                  "СЭД не потребляет входов и не использует ресурсов")
        if not sed.triggers and sed.id in reachable["never_triggered"]:
            r.add(Severity.WARNING, "sed_unreachable", "sed", sed.id,
                  "СЭД не имеет триггеров и не создаётся событиями/ветвями — не будет выполняться")
        if (
            sed.duration.kind == DurationKind.FIXED
            and (sed.duration.value or 0) == 0
            and sed.id in reachable["zero_cycle"]
        ):
            r.add(Severity.CRITICAL, "zero_duration_cycle", "sed", sed.id,
                  "цикл СЭД с нулевой длительностью без условий выхода — бесконечный цикл")
    _check_hierarchy_cycles(r, {s.id: s.parent_id for s in seds.values()}, "sed")

    # --- events -----------------------------------------------------------------
    consumed_kinds = _handled_event_kinds(model)
    for kind in events.values():
        for effect in kind.effects:
            if effect.sed_id and effect.sed_id not in seds:
                r.add(Severity.CRITICAL, "broken_ref", "event_kind", kind.id,
                      f"эффект ссылается на несуществующий СЭД {effect.sed_id!r}")
            if effect.utility_type_id and effect.utility_type_id not in utypes:
                r.add(Severity.CRITICAL, "broken_ref", "event_kind", kind.id,
                      f"эффект ссылается на несуществующую полезность {effect.utility_type_id!r}")
            if effect.resource_id and effect.resource_id not in resources:
                r.add(Severity.CRITICAL, "broken_ref", "event_kind", kind.id,
                      f"эффект ссылается на несуществующий ресурс {effect.resource_id!r}")
            _check_expr(r, effect.quantity_expression, "event_kind", kind.id, "invalid_expression")
            _check_expr(r, effect.condition, "event_kind", kind.id, "invalid_expression")
        if (
            kind.stream.kind != StreamKind.NONE
            and not kind.effects
            and kind.id not in consumed_kinds
        ):
            r.add(Severity.WARNING, "event_unhandled", "event_kind", kind.id,
                  "событие имеет поток, но ни эффектов, ни триггеров — оно ни на что не влияет")
        if kind.stream.kind == StreamKind.POISSON and (kind.stream.rate_per_hour or 0) <= 0:
            r.add(Severity.CRITICAL, "invalid_stream", "event_kind", kind.id,
                  "пуассоновский поток с неположительной интенсивностью")

    # --- KPIs / initial state ---------------------------------------------------
    for kpi in kpis.values():
        _check_expr(r, kpi.expression, "kpi", kpi.id, "invalid_expression")
    for utype_id, qty in model.initial_state.utility_stocks.items():
        if utype_id not in utypes:
            r.add(Severity.WARNING, "broken_ref", "initial_state", utype_id,
                  f"начальный запас несуществующей полезности {utype_id!r}")
        if qty < 0:
            r.add(Severity.CRITICAL, "negative_quantity", "initial_state", utype_id,
                  f"отрицательный начальный запас {qty}")
    for org in org_units.values():
        if org.parent_id and org.parent_id not in org_units:
            r.add(Severity.WARNING, "broken_ref", "org_unit", org.id,
                  f"родительская орг-единица {org.parent_id!r} не существует")
    _check_hierarchy_cycles(r, {o.id: o.parent_id for o in org_units.values()}, "org_unit")

    # goals without any implementing SED → недостижимая цель
    implemented = {s.goal_id for s in seds.values() if s.goal_id}
    for goal in goals.values():
        for sed_id in goal.sed_ids:
            implemented.add(None if sed_id not in seds else goal.id)
        has_children = any(g.parent_id == goal.id for g in goals.values())
        if goal.target_metrics and goal.id not in implemented and not has_children:
            r.add(Severity.WARNING, "goal_unreachable", "goal", goal.id,
                  "цель имеет целевые показатели, но ни один СЭД её не реализует")
    return r


def validate_scenario(model: EnterpriseModel, scenario: Scenario) -> ValidationReport:
    r = ValidationReport()
    for utype_id in scenario.initial_stock_overrides:
        if utype_id not in model.utility_types:
            r.add(Severity.CRITICAL, "broken_ref", "scenario", scenario.id,
                  f"переопределение запаса несуществующей полезности {utype_id!r}")
    for kind_id in list(scenario.event_rate_multipliers) + list(scenario.event_stream_overrides):
        if kind_id not in model.event_kinds:
            r.add(Severity.CRITICAL, "broken_ref", "scenario", scenario.id,
                  f"переопределение несуществующего события {kind_id!r}")
    for sed_id in scenario.sed_overrides:
        if sed_id not in model.seds:
            r.add(Severity.CRITICAL, "broken_ref", "scenario", scenario.id,
                  f"переопределение несуществующего СЭД {sed_id!r}")
    for res_id in scenario.resource_capacity_overrides:
        if res_id not in model.resources:
            r.add(Severity.CRITICAL, "broken_ref", "scenario", scenario.id,
                  f"переопределение несуществующего ресурса {res_id!r}")
    for ev in scenario.injected_events:
        if ev.event_kind_id not in model.event_kinds:
            r.add(Severity.CRITICAL, "broken_ref", "scenario", scenario.id,
                  f"вброшенное событие несуществующего вида {ev.event_kind_id!r}")
    if scenario.objective and scenario.objective.kpi_id not in model.kpis:
        r.add(Severity.CRITICAL, "broken_ref", "scenario", scenario.id,
              f"целевая функция ссылается на несуществующий KPI {scenario.objective.kpi_id!r}")
    return r


def _check_hierarchy_cycles(
    report: ValidationReport, parents: dict[str, str | None], etype: str
) -> None:
    for start in parents:
        seen = {start}
        cursor = parents.get(start)
        while cursor is not None:
            if cursor in seen:
                report.add(Severity.CRITICAL, "hierarchy_cycle", etype, start,
                           f"цикл в иерархии через {cursor!r}")
                break
            seen.add(cursor)
            cursor = parents.get(cursor)


def _handled_event_kinds(model: EnterpriseModel) -> set[str]:
    handled = set()
    for sed in model.seds.values():
        for trig in sed.triggers:
            if trig.kind == TriggerKind.ON_EVENT and trig.event_kind_id:
                handled.add(trig.event_kind_id)
    return handled


def _reachable_seds(model: EnterpriseModel) -> dict[str, set[str]]:
    """SEDs spawned by events/effects/branches, and zero-duration trigger cycles."""
    spawned = set()
    for kind in model.event_kinds.values():
        for effect in kind.effects:
            if effect.kind == EffectKind.SPAWN_SED and effect.sed_id:
                spawned.add(effect.sed_id)
    for sed in model.seds.values():
        for branch in sed.outcome_branches:
            spawned.update(branch.spawn_sed_ids)
    never_triggered = {
        s.id for s in model.seds.values() if not s.triggers and s.id not in spawned
    }
    # zero-duration cycles: on_sed_completed edges among fixed-zero-duration SEDs
    zero = {
        s.id
        for s in model.seds.values()
        if s.duration.kind == DurationKind.FIXED and (s.duration.value or 0) == 0
    }
    edges: dict[str, list[str]] = {}
    for sed in model.seds.values():
        if sed.id not in zero:
            continue
        for trig in sed.triggers:
            if (
                trig.kind == TriggerKind.ON_SED_COMPLETED
                and trig.sed_id in zero
                and not trig.condition
            ):
                edges.setdefault(trig.sed_id, []).append(sed.id)
    in_cycle: set[str] = set()
    for start in zero:
        stack, seen = [start], set()
        while stack:
            node = stack.pop()
            for nxt in edges.get(node, []):
                if nxt == start:
                    in_cycle.add(start)
                elif nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
    return {"never_triggered": never_triggered, "zero_cycle": in_cycle}
