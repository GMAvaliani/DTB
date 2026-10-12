"""Resources, organization, equipment and the ERP micro-level.

Resources are reusable capacities used by SEDs (personnel pools, equipment,
facilities, vehicles, financial limits…). Consumables are modelled as input
*utilities*, not resources. Employees / Equipment / OrgUnits are the micro-level
(views 4–5) and may reference the resource pools they contribute to and the ERP
objects they were imported from.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field

from dtb.domain.base import EntityBase


class ResourceKind(str, Enum):
    PERSONNEL = "personnel"
    EQUIPMENT = "equipment"
    FACILITY = "facility"
    VEHICLE = "vehicle"
    FINANCIAL = "financial"
    ENERGY = "energy"
    OTHER = "other"


class QueueDiscipline(str, Enum):
    """Порядок выбора из очереди при конкуренции за ресурс.

    * ``fifo`` — первым пришёл, первым обслужен (по порядку создания);
    * ``lifo`` — последним пришёл, первым обслужен (стек);
    * ``spt`` — сначала короткие работы (shortest processing time): ожидаемая
      длительность СЭД берётся из модели длительности;
    * ``priority`` — по приоритету СЭД, при равенстве — по порядку создания
      (поведение по умолчанию, совпадает с прежним).
    """

    PRIORITY = "priority"
    FIFO = "fifo"
    LIFO = "lifo"
    SPT = "spt"


class Resource(EntityBase):
    kind: ResourceKind = ResourceKind.OTHER
    capacity: float = Field(default=1.0, ge=0)
    unit: str = "unit"
    org_unit_id: str | None = None
    cost_per_hour: float = Field(default=0.0, ge=0)
    #: дисциплина очереди к этому ресурсу; пусто — дисциплина модели
    queue_discipline: QueueDiscipline | None = None
    #: график работы ресурса; вне смены доступная ёмкость равна нулю
    calendar_ref: str | None = None


class OrgUnitKind(str, Enum):
    ENTERPRISE = "enterprise"
    DIVISION = "division"
    DEPARTMENT = "department"
    TEAM = "team"
    RESPONSIBILITY_CENTER = "responsibility_center"
    POSITION = "position"


class OrgUnit(EntityBase):
    kind: OrgUnitKind = OrgUnitKind.DEPARTMENT
    parent_id: str | None = None
    headcount_plan: int | None = Field(default=None, ge=0)  # штатное расписание


class Employee(EntityBase):
    org_unit_id: str | None = None
    position: str = ""
    skills: list[str] = Field(default_factory=list)
    resource_id: str | None = None  # capacity pool this person contributes to


class Equipment(EntityBase):
    tech_place: str = ""  # технологическое место
    org_unit_id: str | None = None
    resource_id: str | None = None  # capacity pool this unit contributes to
    status: str = "operational"


class ERPObject(EntityBase):
    """An atomic object imported from an external system (1С, Галактика, …)."""

    system: str = "mock_erp"
    object_type: str = ""
    external_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class ERPMapping(EntityBase):
    """Cross-link (View 8) between a domain entity and an ERP object."""

    id: str = "-"
    entity_type: str  # goal|sed|utility_type|resource|org_unit|employee|equipment|kpi
    entity_id: str
    erp_object_id: str
    kind: str = "represents"  # represents | mapped_to_erp | derived_from
