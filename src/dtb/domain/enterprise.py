"""Enterprise — the modelled business / firm / division / system of systems."""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from dtb.domain.base import EntityBase, utcnow_iso


class EnterpriseStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class Enterprise(EntityBase):
    version: str = "0.1.0"
    model_version: int = Field(default=1, ge=1)
    time_zone: str = "UTC"
    simulation_calendar: str = "24x7"
    #: день недели, которому соответствует модельное время t = 0 (0 — понедельник)
    calendar_start_weekday: int = Field(default=0, ge=0, le=6)
    #: календарь по умолчанию для ресурсов без собственного графика
    default_calendar_ref: str | None = None
    #: дисциплина очереди по умолчанию (ресурс может задать свою)
    default_queue_discipline: str = "priority"
    planning_horizon_hours: float = Field(default=720.0, gt=0)
    status: EnterpriseStatus = EnterpriseStatus.DRAFT
    created_at: str = Field(default_factory=utcnow_iso)
