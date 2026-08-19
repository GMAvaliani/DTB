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
    planning_horizon_hours: float = Field(default=720.0, gt=0)
    status: EnterpriseStatus = EnterpriseStatus.DRAFT
    created_at: str = Field(default_factory=utcnow_iso)
