"""SED — структурный элемент деятельности (structural activity element).

The central entity: at runtime each SED instance is an agent that consumes input
utilities, uses resources, executes for a (possibly stochastic) duration and
produces output utilities. Hierarchy depth is arbitrary (parent_id), so
Enterprise → Goal → Capability/СЭД → Sub-СЭД → Activity → Action is model
content, never a fixed schema.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field, model_validator

from dtb.domain.base import EntityBase
from dtb.domain.utilities import UtilityFlow


class SEDStatus(str, Enum):
    """Lifecycle of a SED *instance* (agent) inside the simulation."""

    CREATED = "created"
    WAITING = "waiting"
    EXECUTING = "executing"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class DurationKind(str, Enum):
    FIXED = "fixed"
    UNIFORM = "uniform"
    TRIANGULAR = "triangular"
    NORMAL = "normal"
    LOGNORMAL = "lognormal"
    EXPONENTIAL = "exponential"


class DurationModel(EntityBase):
    """Execution-time model, hours of model time."""

    id: str = "-"
    kind: DurationKind = DurationKind.FIXED
    value: float | None = None  # fixed
    low: float | None = None  # uniform / triangular
    high: float | None = None
    mode: float | None = None  # triangular
    mean: float | None = None  # normal / lognormal / exponential
    std: float | None = None  # normal / lognormal

    @model_validator(mode="after")
    def _check_params(self) -> "DurationModel":
        need: dict[DurationKind, list[str]] = {
            DurationKind.FIXED: ["value"],
            DurationKind.UNIFORM: ["low", "high"],
            DurationKind.TRIANGULAR: ["low", "high", "mode"],
            DurationKind.NORMAL: ["mean", "std"],
            DurationKind.LOGNORMAL: ["mean", "std"],
            DurationKind.EXPONENTIAL: ["mean"],
        }
        missing = [f for f in need[self.kind] if getattr(self, f) is None]
        if missing:
            raise ValueError(f"duration kind {self.kind.value} requires {missing}")
        return self


class TriggerKind(str, Enum):
    ON_EVENT = "on_event"
    ON_SED_COMPLETED = "on_sed_completed"
    AT_TIME = "at_time"
    RECURRING = "recurring"


class TriggerSpec(EntityBase):
    """When new instances of a SED are created (условия формирования заданий)."""

    id: str = "-"
    kind: TriggerKind
    event_kind_id: str | None = None  # on_event
    sed_id: str | None = None  # on_sed_completed
    at: float | None = None  # at_time, hours
    interval: float | None = None  # recurring, hours
    start: float = 0.0  # recurring: first occurrence
    count_expression: str = "1"  # instances per firing; may use payload vars
    condition: str | None = None  # extra guard over payload/state

    @model_validator(mode="after")
    def _check(self) -> "TriggerSpec":
        if self.kind == TriggerKind.ON_EVENT and not self.event_kind_id:
            raise ValueError("on_event trigger requires event_kind_id")
        if self.kind == TriggerKind.ON_SED_COMPLETED and not self.sed_id:
            raise ValueError("on_sed_completed trigger requires sed_id")
        if self.kind == TriggerKind.AT_TIME and self.at is None:
            raise ValueError("at_time trigger requires at")
        if self.kind == TriggerKind.RECURRING and not self.interval:
            raise ValueError("recurring trigger requires interval")
        return self


class ResourceRequirement(EntityBase):
    """Reusable capacity held while the SED executes (released on completion)."""

    id: str = "-"
    resource_id: str
    quantity: float = Field(default=1.0, gt=0)


class OutcomeBranch(EntityBase):
    """Probabilistic outcome: which outputs completion produces (e.g. QC pass/fail)."""

    id: str = "-"
    label: str = "success"
    probability: float = Field(default=1.0, gt=0, le=1)
    outputs: list[UtilityFlow] = Field(default_factory=list)
    # optional chain: extra SEDs spawned when this branch fires (e.g. rework)
    spawn_sed_ids: list[str] = Field(default_factory=list)


class ExternalModelRef(EntityBase):
    """Mode B: SED execution (or event stream) computed by an external model."""

    id: str = "-"
    uri: str
    adapter: str = "http"  # adapter name in the AdapterRegistry
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = 30.0


class SEDDefinition(EntityBase):
    goal_id: str | None = None
    parent_id: str | None = None  # SED hierarchy, arbitrary depth
    org_unit_id: str | None = None  # responsible centre / subject
    inputs: list[UtilityFlow] = Field(default_factory=list)
    outputs: list[UtilityFlow] = Field(default_factory=list)
    outcome_branches: list[OutcomeBranch] = Field(default_factory=list)
    resources: list[ResourceRequirement] = Field(default_factory=list)
    equipment_ids: list[str] = Field(default_factory=list)
    duration: DurationModel = Field(default_factory=lambda: DurationModel(value=0.0))
    triggers: list[TriggerSpec] = Field(default_factory=list)
    start_condition: str | None = None  # extra guard beyond inputs/resources
    continuation_condition: str | None = None  # checked at every МП while executing
    max_concurrent: int | None = Field(default=None, ge=1)
    priority: int = 100  # lower number starts first when competing for inputs
    external_model: ExternalModelRef | None = None
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_branches(self) -> "SEDDefinition":
        if self.outcome_branches:
            total = sum(b.probability for b in self.outcome_branches)
            if total > 1.0 + 1e-9:
                raise ValueError(
                    f"SED {self.id}: outcome branch probabilities sum to {total:.4f} > 1"
                )
        return self

    def effective_branches(self) -> list[OutcomeBranch]:
        """Branches actually used at completion; plain outputs = one certain branch."""
        if self.outcome_branches:
            branches = list(self.outcome_branches)
            total = sum(b.probability for b in branches)
            if total < 1.0 - 1e-9:
                branches.append(
                    OutcomeBranch(label="no_output", probability=1.0 - total, outputs=[])
                )
            return branches
        return [OutcomeBranch(label="success", probability=1.0, outputs=list(self.outputs))]
