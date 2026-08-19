"""НП-события — uncertain and planned events of the environment and the model.

An EventKind describes a class of events: how occurrences arise in model time
(its *stream*) and what an occurrence does (its *effects*). Effects are the only
mechanism by which events touch the model — the engine carries no business
semantics ("order", "breakdown" etc. are model content).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field, model_validator

from dtb.domain.base import EntityBase
from dtb.domain.seds import ExternalModelRef


class EventCategory(str, Enum):
    EXTERNAL = "external"  # environment: orders, demand shifts, regulation…
    INTERNAL = "internal"  # inside the enterprise: breakdowns, resignations…
    PLANNED = "planned"  # регламентные/плановые: maintenance, period close…
    REGULATORY = "regulatory"
    UNCERTAIN = "uncertain"
    SED_COMPLETION = "sed_completion"  # engine-internal completion events


class StreamKind(str, Enum):
    POISSON = "poisson"  # простейший поток, sampled in-engine
    SCHEDULED = "scheduled"  # fixed times and/or a regular interval
    EXTERNAL = "external"  # produced by an external model via URI adapter
    NONE = "none"  # only injected by scenarios / internal logic


class StreamSpec(EntityBase):
    id: str = "-"
    kind: StreamKind = StreamKind.NONE
    rate_per_hour: float | None = Field(default=None, gt=0)  # poisson
    times: list[float] = Field(default_factory=list)  # scheduled: explicit
    interval: float | None = Field(default=None, gt=0)  # scheduled: recurring
    start: float = 0.0  # scheduled: first recurring occurrence
    external: ExternalModelRef | None = None

    @model_validator(mode="after")
    def _check(self) -> "StreamSpec":
        if self.kind == StreamKind.POISSON and self.rate_per_hour is None:
            raise ValueError("poisson stream requires rate_per_hour")
        if self.kind == StreamKind.SCHEDULED and not self.times and self.interval is None:
            raise ValueError("scheduled stream requires times or interval")
        if self.kind == StreamKind.EXTERNAL and self.external is None:
            raise ValueError("external stream requires external model ref")
        return self


class EffectKind(str, Enum):
    SPAWN_SED = "spawn_sed"
    ADD_UTILITY = "add_utility"
    REMOVE_UTILITY = "remove_utility"
    RESOURCE_CAPACITY_DELTA = "resource_capacity_delta"
    SET_PARAMETER = "set_parameter"
    CANCEL_SED_INSTANCES = "cancel_sed_instances"


class EventEffect(EntityBase):
    id: str = "-"
    kind: EffectKind
    sed_id: str | None = None  # spawn_sed / cancel_sed_instances
    utility_type_id: str | None = None  # add/remove_utility
    resource_id: str | None = None  # resource_capacity_delta
    quantity_expression: str = "1"  # amount / instance count; payload vars usable
    delta: float | None = None  # capacity delta (negative = failure)
    duration: float | None = Field(default=None, gt=0)  # temp effect length, hours
    parameter_path: str | None = None  # set_parameter target
    value: Any | None = None
    condition: str | None = None  # guard over payload/state

    @model_validator(mode="after")
    def _check(self) -> "EventEffect":
        need = {
            EffectKind.SPAWN_SED: self.sed_id,
            EffectKind.CANCEL_SED_INSTANCES: self.sed_id,
            EffectKind.ADD_UTILITY: self.utility_type_id,
            EffectKind.REMOVE_UTILITY: self.utility_type_id,
            EffectKind.RESOURCE_CAPACITY_DELTA: self.resource_id,
            EffectKind.SET_PARAMETER: self.parameter_path,
        }
        if need[self.kind] is None:
            raise ValueError(f"effect {self.kind.value}: required reference missing")
        if self.kind == EffectKind.RESOURCE_CAPACITY_DELTA and self.delta is None:
            raise ValueError("resource_capacity_delta effect requires delta")
        return self


class EventKind(EntityBase):
    category: EventCategory = EventCategory.EXTERNAL
    stream: StreamSpec = Field(default_factory=StreamSpec)
    payload_defaults: dict[str, Any] = Field(default_factory=dict)
    effects: list[EventEffect] = Field(default_factory=list)
    affected_entity_ids: list[str] = Field(default_factory=list)  # documentation links
