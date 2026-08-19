"""Connector interface + normalization pipeline + import reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from dtb.domain.base import utcnow_iso
from dtb.domain.model import EnterpriseModel
from dtb.domain.resources import ERPMapping, ERPObject


@dataclass
class ImportReport:
    source: str = ""
    created: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def count(self, entity_type: str, is_new: bool) -> None:
        bucket = self.created if is_new else self.updated
        bucket[entity_type] = bucket.get(entity_type, 0) + 1

    def error(self, where: str, message: str, row: int | None = None) -> None:
        self.errors.append({"where": where, "row": row, "message": message})

    def warn(self, where: str, message: str, row: int | None = None) -> None:
        self.warnings.append({"where": where, "row": row, "message": message})

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ok": self.ok,
            "created": self.created,
            "updated": self.updated,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class ERPConnector(Protocol):
    """A source system adapter.

    ``fetch`` returns raw objects grouped by object type; ``system`` names the
    source (goes into ERPObject.system and provenance).
    """

    system: str

    def fetch(self) -> dict[str, list[dict[str, Any]]]: ...


# Normalization: how raw ERP object types map into the twin.
# Each rule: erp object type -> handler(model, raw, report) that may create/update
# domain entities and returns the entity (type, id) to map, or None.
_STANDARD_TYPES = {
    "material_stock",  # -> initial_state.utility_stocks
    "employee",  # -> employees
    "equipment",  # -> equipment
    "org_unit",  # -> org_units
    "kpi_actual",  # -> actuals series
}


def sync_connector(model: EnterpriseModel, connector: ERPConnector) -> ImportReport:
    """Pull atomic data from a connector and normalise it into the model.

    Every raw object becomes an ERPObject; recognised types additionally update
    the corresponding domain collections / initial state (the Actualizer-agent
    behaviour from the source spec).
    """
    report = ImportReport(source=f"erp:{connector.system}")
    data = connector.fetch()
    for object_type, rows in sorted(data.items()):
        for index, raw in enumerate(rows):
            external_id = str(raw.get("id") or raw.get("external_id") or f"{object_type}_{index}")
            erp_id = f"{connector.system}:{object_type}:{external_id}"
            is_new = erp_id not in model.erp_objects
            model.erp_objects[erp_id] = ERPObject(
                id=erp_id,
                name=str(raw.get("name", external_id)),
                system=connector.system,
                object_type=object_type,
                external_id=external_id,
                payload={k: v for k, v in raw.items()},
            )
            report.count("erp_object", is_new)
            try:
                _normalize(model, connector.system, object_type, erp_id, raw, report)
            except (KeyError, ValueError, TypeError) as exc:
                report.error(f"{object_type}[{index}]", str(exc), row=index)
    model.initial_state.as_of = utcnow_iso()
    model.initial_state.source = f"erp:{connector.system}"
    return report


def _map(model: EnterpriseModel, entity_type: str, entity_id: str, erp_id: str) -> None:
    for mapping in model.erp_mappings:
        if (
            mapping.entity_type == entity_type
            and mapping.entity_id == entity_id
            and mapping.erp_object_id == erp_id
        ):
            return
    model.erp_mappings.append(
        ERPMapping(
            entity_type=entity_type,
            entity_id=entity_id,
            erp_object_id=erp_id,
            kind="represents",
        )
    )


def _normalize(
    model: EnterpriseModel,
    system: str,
    object_type: str,
    erp_id: str,
    raw: dict[str, Any],
    report: ImportReport,
) -> None:
    if object_type == "material_stock":
        utype_id = str(raw.get("utility_type_id") or raw.get("id") or "")
        if utype_id not in model.utility_types:
            report.warn(object_type, f"полезность {utype_id!r} не описана в модели — запас пропущен")
            return
        model.initial_state.utility_stocks[utype_id] = float(raw.get("quantity", 0))
        _map(model, "utility_type", utype_id, erp_id)
    elif object_type == "employee":
        from dtb.domain.resources import Employee

        emp_id = str(raw.get("id") or "")
        is_new = emp_id not in model.employees
        model.employees[emp_id] = Employee(
            id=emp_id,
            name=str(raw.get("name", emp_id)),
            org_unit_id=raw.get("org_unit_id"),
            position=str(raw.get("position", "")),
            skills=list(raw.get("skills", [])),
            resource_id=raw.get("resource_id"),
        )
        report.count("employee", is_new)
        _map(model, "employee", emp_id, erp_id)
    elif object_type == "equipment":
        from dtb.domain.resources import Equipment

        eq_id = str(raw.get("id") or "")
        is_new = eq_id not in model.equipment
        model.equipment[eq_id] = Equipment(
            id=eq_id,
            name=str(raw.get("name", eq_id)),
            tech_place=str(raw.get("tech_place", "")),
            org_unit_id=raw.get("org_unit_id"),
            resource_id=raw.get("resource_id"),
            status=str(raw.get("status", "operational")),
        )
        report.count("equipment", is_new)
        _map(model, "equipment", eq_id, erp_id)
    elif object_type == "org_unit":
        from dtb.domain.resources import OrgUnit, OrgUnitKind

        org_id = str(raw.get("id") or "")
        is_new = org_id not in model.org_units
        kind_raw = str(raw.get("kind", "department"))
        model.org_units[org_id] = OrgUnit(
            id=org_id,
            name=str(raw.get("name", org_id)),
            kind=OrgUnitKind(kind_raw) if kind_raw in OrgUnitKind._value2member_map_ else OrgUnitKind.DEPARTMENT,
            parent_id=raw.get("parent_id"),
        )
        report.count("org_unit", is_new)
        _map(model, "org_unit", org_id, erp_id)
    elif object_type == "kpi_actual":
        from dtb.domain.model import SeriesPoint

        kpi_id = str(raw.get("kpi_id") or "")
        if kpi_id not in model.kpis:
            report.warn(object_type, f"KPI {kpi_id!r} не описан в модели — факт пропущен")
            return
        series = model.actuals.setdefault(kpi_id, [])
        series.append(SeriesPoint(t=float(raw.get("t", 0)), value=float(raw.get("value", 0))))
        series.sort(key=lambda p: p.t)
    # unrecognised object types stay as raw ERPObjects (view 7 atomic data)
