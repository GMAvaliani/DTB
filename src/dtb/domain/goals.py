"""Goals — hierarchy of enterprise objectives (View 1).

The goal tree simultaneously represents the goal structure, links to the SED
hierarchy (IMPLEMENTS) and the responsibility-centre structure (responsible org
units).
"""

from __future__ import annotations

from pydantic import Field, model_validator

from dtb.domain.base import EntityBase


class TargetMetric(EntityBase):
    id: str = "-"
    kpi_id: str
    target_value: float
    comparison: str = ">="  # ">=" | "<="
    deadline_hours: float | None = Field(default=None, gt=0)  # None = horizon end
    weight: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def _check(self) -> "TargetMetric":
        if self.comparison not in (">=", "<="):
            raise ValueError("comparison must be '>=' or '<='")
        return self


class Goal(EntityBase):
    parent_id: str | None = None
    responsible_org_unit_id: str | None = None
    priority: int = 100
    target_metrics: list[TargetMetric] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)  # safe expressions
    sed_ids: list[str] = Field(default_factory=list)  # SEDs implementing the goal
    resource_ids: list[str] = Field(default_factory=list)
    valid_from: str | None = None  # ISO date
    valid_to: str | None = None
