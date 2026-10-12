"""EnterpriseModel — the versioned container of one digital twin's description.

Serialises to/from JSON via pydantic; that JSON document *is* the model-version
snapshot format used by persistence, import/export and diffing.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from dtb.domain.base import EntityBase
from dtb.domain.calendars import Calendar
from dtb.domain.economics import EconomicItem, EconomicsSettings
from dtb.domain.control import (
    ControlActionDef,
    DecisionPoint,
    Observation,
    Policy,
    Subject,
)
from dtb.domain.enterprise import Enterprise
from dtb.domain.events import EventKind
from dtb.domain.goals import Goal
from dtb.domain.kpi import KPIDefinition
from dtb.domain.links import Gap, GapType, Link
from dtb.domain.resources import (
    Employee,
    Equipment,
    ERPMapping,
    ERPObject,
    OrgUnit,
    Resource,
)
from dtb.domain.scenario import Scenario
from dtb.domain.seds import SEDDefinition
from dtb.domain.utilities import UtilityType


class InitialState(BaseModel):
    """View 7 seed: the current (imported or hand-filled) operational state."""

    model_config = ConfigDict(extra="forbid")

    utility_stocks: dict[str, float] = Field(default_factory=dict)
    as_of: str | None = None  # ISO timestamp of the actualisation
    source: str = "manual"  # manual | erp:<system> | import:<file>
    metadata: dict[str, Any] = Field(default_factory=dict)


class SeriesPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t: float  # model hours relative to model t=0 (negative = history)
    value: float


# Collections addressable by entity_type name (used by API, import, graph, diff).
ENTITY_COLLECTIONS: dict[str, type[EntityBase]] = {
    "goal": Goal,
    "utility_type": UtilityType,
    "sed": SEDDefinition,
    "resource": Resource,
    "org_unit": OrgUnit,
    "employee": Employee,
    "equipment": Equipment,
    "erp_object": ERPObject,
    "event_kind": EventKind,
    "kpi": KPIDefinition,
    # контур управления
    "subject": Subject,
    "control_action": ControlActionDef,
    "policy": Policy,
    "decision_point": DecisionPoint,
    "observation": Observation,
    # режимы работы и экономика деятельности
    "calendar": Calendar,
    "economic_item": EconomicItem,
    # заготовки ниши (NOT IMPLEMENTED, см. dtb.domain.links)
    "link": Link,
    "gap_type": GapType,
}

_COLLECTION_FIELD = {
    "goal": "goals",
    "utility_type": "utility_types",
    "sed": "seds",
    "resource": "resources",
    "org_unit": "org_units",
    "employee": "employees",
    "equipment": "equipment",
    "erp_object": "erp_objects",
    "event_kind": "event_kinds",
    "kpi": "kpis",
    "subject": "subjects",
    "control_action": "control_actions",
    "policy": "policies",
    "decision_point": "decision_points",
    "observation": "observations",
    "calendar": "calendars",
    "economic_item": "economic_items",
    "link": "links",
    "gap_type": "gap_types",
}

#: Версия схемы модели. Увеличивается при добавлении коллекций и полей,
#: требующих миграции сохранённых снимков (см. migrate_snapshot).
SCHEMA_VERSION = 4


class EnterpriseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION  # старые снимки поднимает migrate_snapshot
    enterprise: Enterprise
    goals: dict[str, Goal] = Field(default_factory=dict)
    utility_types: dict[str, UtilityType] = Field(default_factory=dict)
    seds: dict[str, SEDDefinition] = Field(default_factory=dict)
    resources: dict[str, Resource] = Field(default_factory=dict)
    org_units: dict[str, OrgUnit] = Field(default_factory=dict)
    employees: dict[str, Employee] = Field(default_factory=dict)
    equipment: dict[str, Equipment] = Field(default_factory=dict)
    erp_objects: dict[str, ERPObject] = Field(default_factory=dict)
    erp_mappings: list[ERPMapping] = Field(default_factory=list)
    event_kinds: dict[str, EventKind] = Field(default_factory=dict)
    kpis: dict[str, KPIDefinition] = Field(default_factory=dict)
    # контур управления (МР, политики, наблюдения, действия, субъекты)
    subjects: dict[str, Subject] = Field(default_factory=dict)
    control_actions: dict[str, ControlActionDef] = Field(default_factory=dict)
    policies: dict[str, Policy] = Field(default_factory=dict)
    decision_points: dict[str, DecisionPoint] = Field(default_factory=dict)
    observations: dict[str, Observation] = Field(default_factory=dict)
    # календари работы: смены, выходные, плановые остановки
    calendars: dict[str, Calendar] = Field(default_factory=dict)
    # экономика деятельности (ставки, разовые начисления, остаточная стоимость)
    economic_items: dict[str, EconomicItem] = Field(default_factory=dict)
    economics: EconomicsSettings = Field(default_factory=EconomicsSettings)
    # заготовки ниши: хранятся и версионируются, на расчёт не влияют
    links: dict[str, Link] = Field(default_factory=dict)
    gap_types: dict[str, GapType] = Field(default_factory=dict)
    gaps: list[Gap] = Field(default_factory=list)
    initial_state: InitialState = Field(default_factory=InitialState)
    # View 3/7 series per KPI id: план и факт; forecast comes from simulation.
    plans: dict[str, list[SeriesPoint]] = Field(default_factory=dict)
    actuals: dict[str, list[SeriesPoint]] = Field(default_factory=dict)

    # -- generic collection access ------------------------------------------------

    def collection(self, entity_type: str) -> dict[str, Any]:
        try:
            return getattr(self, _COLLECTION_FIELD[entity_type])
        except KeyError as exc:
            raise KeyError(f"unknown entity type {entity_type!r}") from exc

    def get_entity(self, entity_type: str, entity_id: str) -> EntityBase | None:
        return self.collection(entity_type).get(entity_id)

    def put_entity(self, entity_type: str, entity: EntityBase) -> None:
        self.collection(entity_type)[entity.id] = entity

    def delete_entity(self, entity_type: str, entity_id: str) -> bool:
        return self.collection(entity_type).pop(entity_id, None) is not None

    def iter_entities(self) -> Iterator[tuple[str, EntityBase]]:
        for etype in _COLLECTION_FIELD:
            for entity in self.collection(etype).values():
                yield etype, entity

    # -- snapshots / hashing ------------------------------------------------------

    def to_snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> "EnterpriseModel":
        return cls.model_validate(migrate_snapshot(data))

    def content_hash(self) -> str:
        """Хеш содержания модели без служебных отметок времени.

        Момент создания записи о предприятии к содержанию деятельности не
        относится: две структурно одинаковые модели обязаны иметь одинаковый
        хеш, иначе provenance прогонов нельзя сравнивать между собой.
        """
        snapshot = self.to_snapshot()
        enterprise = dict(snapshot.get("enterprise", {}))
        enterprise.pop("created_at", None)
        snapshot["enterprise"] = enterprise
        payload = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def clone(self) -> "EnterpriseModel":
        return copy.deepcopy(self)


def migrate_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    """Поднять снимок модели до текущей версии схемы.

    Снимок — это JSON-документ, поэтому «миграция БД» для модели сводится к
    дополнению документа значениями по умолчанию: ранее сохранённые модели
    грузятся без ошибок и получают пустые коллекции контура управления.
    Несовместимых изменений схемы не вносилось, поэтому миграция аддитивна.
    """
    if not isinstance(data, dict):
        return data
    version = int(data.get("schema_version") or 1)
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"снимок модели имеет версию схемы {version}, "
            f"поддерживается до {SCHEMA_VERSION}: обновите платформу"
        )
    if version < 2:
        # v1 → v2: добавлены коллекции контура управления и заготовки ниши
        data = dict(data)
        for field in (
            "subjects",
            "control_actions",
            "policies",
            "decision_points",
            "observations",
            "links",
            "gap_types",
        ):
            data.setdefault(field, {})
        data.setdefault("gaps", [])
        data["schema_version"] = 2
    if version < 3:
        # v2 → v3: добавлены статьи экономики и настройки прогона
        data = dict(data)
        data.setdefault("economic_items", {})
        data.setdefault("economics", {})
        data["schema_version"] = 3
    if version < 4:
        # v3 → v4: добавлены календари работы; дисциплина очереди и режим
        # прерывания получают значения по умолчанию, совпадающие с прежним
        # поведением (priority и resume), поэтому расчёт не меняется
        data = dict(data)
        data.setdefault("calendars", {})
        data["schema_version"] = 4
    return data


def apply_scenario_overrides(model: EnterpriseModel, scenario: Scenario) -> EnterpriseModel:
    """Return a deep copy of the model with the scenario's what-if overrides applied.

    Parameter override paths use the form ``entity_type:id.field`` (e.g.
    ``resource:line.capacity``, ``sed:production.priority``); plain paths address
    the enterprise (``planning_horizon_hours``).
    """
    m = model.clone()
    for utype_id, qty in scenario.initial_stock_overrides.items():
        m.initial_state.utility_stocks[utype_id] = qty
    for res_id, cap in scenario.resource_capacity_overrides.items():
        if res_id in m.resources:
            m.resources[res_id].capacity = cap
    for kind_id, mult in scenario.event_rate_multipliers.items():
        kind = m.event_kinds.get(kind_id)
        if kind and kind.stream.rate_per_hour is not None:
            if mult == 0:
                # a zero multiplier disables the stream entirely
                from dtb.domain.events import StreamKind, StreamSpec

                kind.stream = StreamSpec(kind=StreamKind.NONE)
            else:
                kind.stream.rate_per_hour = kind.stream.rate_per_hour * mult
    for kind_id, stream in scenario.event_stream_overrides.items():
        if kind_id in m.event_kinds:
            m.event_kinds[kind_id].stream = stream.model_copy(deep=True)
    for sed_id, override in scenario.sed_overrides.items():
        sed = m.seds.get(sed_id)
        if not sed:
            continue
        if override.priority is not None:
            sed.priority = override.priority
        if override.max_concurrent is not None:
            sed.max_concurrent = override.max_concurrent
        if override.duration_scale != 1.0:
            d = sed.duration
            for field in ("value", "low", "high", "mode", "mean"):
                v = getattr(d, field)
                if v is not None:
                    setattr(d, field, v * override.duration_scale)
    for path, value in scenario.parameter_overrides.items():
        _apply_parameter(m, path, value)
    return m


def _read_parameter(model: EnterpriseModel, path: str) -> Any:
    """Текущее значение по пути параметра (для отката обратимого действия)."""
    if ":" in path:
        etype, rest = path.split(":", 1)
        entity_id, _, field = rest.partition(".")
        entity = model.get_entity(etype, entity_id)
        if entity is None or not field:
            raise KeyError(f"parameter path {path!r}: entity not found")
        target: Any = entity
        for part in field.split("."):
            target = target[part] if isinstance(target, dict) else getattr(target, part)
        return target
    return getattr(model.enterprise, path)


def _apply_parameter(model: EnterpriseModel, path: str, value: Any) -> None:
    if ":" in path:
        etype, rest = path.split(":", 1)
        entity_id, _, field = rest.partition(".")
        entity = model.get_entity(etype, entity_id)
        if entity is None or not field:
            raise KeyError(f"parameter path {path!r}: entity not found")
        target: Any = entity
        parts = field.split(".")
        for part in parts[:-1]:
            target = target[part] if isinstance(target, dict) else getattr(target, part)
        if isinstance(target, dict):
            # словарные поля (например, пороги политики) адресуются по ключу
            target[parts[-1]] = value
        else:
            setattr(target, parts[-1], value)
    else:
        setattr(model.enterprise, path, value)
