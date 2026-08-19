"""Data quality layer (Section 35): report on the model's data health.

Unlike model validation (a hard gate before simulation), data quality issues
describe completeness/consistency of the twin's data and produce a scored
report for the user.
"""

from __future__ import annotations

from typing import Any

from dtb.domain.model import EnterpriseModel


def data_quality_report(model: EnterpriseModel) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []

    def add(check: str, etype: str, eid: str, message: str, severity: str = "warning") -> None:
        issues.append(
            {
                "check": check,
                "entity_type": etype,
                "entity_id": eid,
                "message": message,
                "severity": severity,
            }
        )

    # missing values -------------------------------------------------------------
    for etype, entity in model.iter_entities():
        if not entity.name:
            add("missing_values", etype, entity.id, "отсутствует название")

    # duplicates -----------------------------------------------------------------
    for etype in ("goal", "sed", "utility_type", "resource", "org_unit"):
        seen: dict[str, str] = {}
        for entity in model.collection(etype).values():
            key = entity.name.strip().lower()
            if not key:
                continue
            if key in seen:
                add("duplicate_entities", etype, entity.id,
                    f"дубликат названия с {seen[key]!r}: {entity.name!r}")
            else:
                seen[key] = entity.id

    # orphans / broken links -----------------------------------------------------
    produced, consumed = set(), set()
    for sed in model.seds.values():
        consumed.update(f.utility_type_id for f in sed.inputs)
        for branch in sed.effective_branches():
            produced.update(f.utility_type_id for f in branch.outputs)
    for kind in model.event_kinds.values():
        for effect in kind.effects:
            if effect.utility_type_id:
                produced.add(effect.utility_type_id)
    stocked = set(model.initial_state.utility_stocks)
    for utype in model.utility_types.values():
        if utype.id not in produced | consumed | stocked:
            add("orphan_entities", "utility_type", utype.id,
                "полезность нигде не производится, не потребляется и не имеет запаса")
        if utype.id in consumed and utype.id not in produced | stocked:
            add("broken_references", "utility_type", utype.id,
                "полезность потребляется, но никогда не производится и без запаса",
                "error")

    used_resources = {
        req.resource_id for sed in model.seds.values() for req in sed.resources
    }
    for res in model.resources.values():
        if res.id not in used_resources:
            add("orphan_entities", "resource", res.id, "ресурс не используется ни одним СЭД")

    for emp in model.employees.values():
        if not emp.org_unit_id:
            add("missing_values", "employee", emp.id, "сотрудник без орг-единицы")
        elif emp.org_unit_id not in model.org_units:
            add("broken_references", "employee", emp.id,
                f"орг-единица {emp.org_unit_id!r} не существует", "error")
        if emp.resource_id and emp.resource_id not in model.resources:
            add("broken_references", "employee", emp.id,
                f"ресурс {emp.resource_id!r} не существует", "error")
    for eq in model.equipment.values():
        if eq.resource_id and eq.resource_id not in model.resources:
            add("broken_references", "equipment", eq.id,
                f"ресурс {eq.resource_id!r} не существует", "error")

    # ERP mappings ---------------------------------------------------------------
    mapping_keys: dict[tuple[str, str], str] = {}
    for mapping in model.erp_mappings:
        if mapping.erp_object_id not in model.erp_objects:
            add("broken_references", "erp_mapping", mapping.entity_id,
                f"ERP-объект {mapping.erp_object_id!r} не существует", "error")
        entity = None
        try:
            entity = model.get_entity(mapping.entity_type, mapping.entity_id)
        except KeyError:
            pass
        if entity is None:
            add("broken_references", "erp_mapping", mapping.entity_id,
                f"сущность {mapping.entity_type}:{mapping.entity_id} не существует", "error")
        key = (mapping.entity_type, mapping.entity_id)
        if mapping.kind == "represents":
            if key in mapping_keys and mapping_keys[key] != mapping.erp_object_id:
                add("conflicting_erp_mappings", mapping.entity_type, mapping.entity_id,
                    f"противоречивые ERP-связи: {mapping_keys[key]!r} и {mapping.erp_object_id!r}",
                    "error")
            mapping_keys[key] = mapping.erp_object_id

    # units ----------------------------------------------------------------------
    unit_by_name: dict[str, dict[str, str]] = {}
    for utype in model.utility_types.values():
        bucket = unit_by_name.setdefault(utype.name.strip().lower(), {})
        bucket[utype.id] = utype.unit
    for name, variants in unit_by_name.items():
        if len(set(variants.values())) > 1:
            add("inconsistent_units", "utility_type", ",".join(variants),
                f"одноимённые полезности {name!r} имеют разные единицы: {sorted(set(variants.values()))}")

    # impossible states ----------------------------------------------------------
    for utype_id, qty in model.initial_state.utility_stocks.items():
        if qty < 0:
            add("impossible_states", "initial_state", utype_id,
                f"отрицательный запас {qty}", "error")
    for res in model.resources.values():
        if res.capacity == 0:
            add("impossible_states", "resource", res.id, "нулевая ёмкость ресурса")

    # timestamps -----------------------------------------------------------------
    for kpi_id, series in list(model.plans.items()) + list(model.actuals.items()):
        times = [p.t for p in series]
        if times != sorted(times):
            add("inconsistent_timestamps", "kpi", kpi_id,
                "точки временного ряда не упорядочены по времени", "error")

    errors = sum(1 for i in issues if i["severity"] == "error")
    warnings = len(issues) - errors
    total_entities = sum(1 for _ in model.iter_entities()) or 1
    score = max(0.0, round(100.0 * (1 - (errors * 3 + warnings) / (total_entities * 3)), 1))
    return {
        "score": score,
        "errors": errors,
        "warnings": warnings,
        "checks": sorted({i["check"] for i in issues}),
        "issues": issues,
    }
