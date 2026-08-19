"""Scenario — one fully parameterised simulated future.

A scenario overrides the base model (what-if), injects/reshapes NP-event
streams, applies management control actions u(t) at chosen moments, and defines
the objective for optimisation and forecasting. Scenarios derived from a base
scenario keep ``base_scenario_id`` for comparison lineage.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from dtb.domain.base import EntityBase, utcnow_iso
from dtb.domain.events import EventEffect, StreamSpec
from dtb.domain.kpi import KPIDirection


class InjectedEvent(EntityBase):
    id: str = "-"
    time: float = Field(ge=0)  # model hours from scenario start
    event_kind_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ControlAction(EntityBase):
    """Management control u(t): a timed effect applied to the running model."""

    id: str = "-"
    time: float = Field(ge=0)
    effect: EventEffect
    label: str = ""


class SEDOverride(EntityBase):
    id: str = "-"
    duration_scale: float = Field(default=1.0, gt=0)
    priority: int | None = None
    max_concurrent: int | None = Field(default=None, ge=1)


class ObjectiveSpec(EntityBase):
    id: str = "-"
    kpi_id: str
    direction: KPIDirection = KPIDirection.MAXIMIZE
    target: float | None = None  # for probability-of-achievement reporting


class Scenario(EntityBase):
    enterprise_id: str
    base_scenario_id: str | None = None
    model_version: int | None = None  # None = latest at run time (pinned in provenance)
    horizon_hours: float | None = Field(default=None, gt=0)  # None = enterprise default
    start_time: str | None = None  # ISO datetime anchoring model t=0
    calendar: str | None = None
    seed: int | None = None  # default master seed for runs
    monte_carlo_runs: int = Field(default=1, ge=1)

    initial_stock_overrides: dict[str, float] = Field(default_factory=dict)
    event_rate_multipliers: dict[str, float] = Field(default_factory=dict)
    event_stream_overrides: dict[str, StreamSpec] = Field(default_factory=dict)
    injected_events: list[InjectedEvent] = Field(default_factory=list)
    sed_overrides: dict[str, SEDOverride] = Field(default_factory=dict)
    resource_capacity_overrides: dict[str, float] = Field(default_factory=dict)
    parameter_overrides: dict[str, Any] = Field(default_factory=dict)
    control_actions: list[ControlAction] = Field(default_factory=list)

    objective: ObjectiveSpec | None = None
    constraints: list[str] = Field(default_factory=list)
    version: int = Field(default=1, ge=1)
    created_at: str = Field(default_factory=utcnow_iso)

    @model_validator(mode="after")
    def _check(self) -> "Scenario":
        for kind_id, mult in self.event_rate_multipliers.items():
            if mult < 0:
                raise ValueError(f"event rate multiplier for {kind_id} must be >= 0")
        for res_id, cap in self.resource_capacity_overrides.items():
            if cap < 0:
                raise ValueError(f"resource capacity override for {res_id} must be >= 0")
        return self
