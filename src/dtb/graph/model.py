"""Typed graph primitives."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RelationType(str, Enum):
    HAS_GOAL = "HAS_GOAL"
    PARENT_OF = "PARENT_OF"
    IMPLEMENTS = "IMPLEMENTS"  # SED → Goal
    REQUIRES = "REQUIRES"
    PRODUCES = "PRODUCES"  # SED → UtilityType
    CONSUMES = "CONSUMES"  # SED → UtilityType
    TRIGGERS = "TRIGGERS"  # EventKind → SED; SED → SED (completion chain)
    EXECUTED_BY = "EXECUTED_BY"  # SED → OrgUnit
    USES_RESOURCE = "USES_RESOURCE"
    USES_EQUIPMENT = "USES_EQUIPMENT"
    PRODUCES_UTILITY = "PRODUCES_UTILITY"
    DEPENDS_ON = "DEPENDS_ON"
    RESPONSIBLE_FOR = "RESPONSIBLE_FOR"  # OrgUnit → Goal
    REPRESENTS = "REPRESENTS"
    MAPPED_TO_ERP = "MAPPED_TO_ERP"
    AFFECTS = "AFFECTS"  # EventKind → Resource/parameter
    CAUSES = "CAUSES"
    MEMBER_OF = "MEMBER_OF"  # Employee → OrgUnit
    CONTRIBUTES_TO = "CONTRIBUTES_TO"  # Employee/Equipment → Resource
    MEASURED_BY = "MEASURED_BY"  # Goal → KPI
    DECIDES = "DECIDES"  # DecisionPoint → Policy; Policy → ControlAction
    OBSERVES = "OBSERVES"  # Observation → наблюдаемая компонента состояния
    APPLIES = "APPLIES"  # Subject → Policy/ControlAction


# Relation types that carry causal influence downstream (impact analysis).
CAUSAL_FORWARD: set[RelationType] = {
    RelationType.TRIGGERS,
    RelationType.PRODUCES,
    RelationType.PRODUCES_UTILITY,
    RelationType.CAUSES,
    RelationType.AFFECTS,
    RelationType.IMPLEMENTS,
    RelationType.MEASURED_BY,
    RelationType.CONTRIBUTES_TO,
    RelationType.DECIDES,  # решение распространяется на действие и дальше по модели
    RelationType.APPLIES,
}
# Relations traversed in reverse for downstream impact (consumer depends on utility;
# SED depends on resource it uses).
CAUSAL_REVERSE: set[RelationType] = {
    RelationType.CONSUMES,
    RelationType.USES_RESOURCE,
    RelationType.USES_EQUIPMENT,
    RelationType.REQUIRES,
    RelationType.OBSERVES,  # наблюдение зависит от наблюдаемой компоненты
}


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str  # namespaced: "sed:production", "goal:g1", custom ids free-form
    type: str  # enterprise|goal|sed|utility_type|resource|org_unit|employee|equipment|erp_object|event_kind|kpi|custom
    label: str = ""
    ref_id: str | None = None  # id inside the domain collection, if projected
    properties: dict[str, Any] = Field(default_factory=dict)
    archived: bool = False


class GraphRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    target: str
    type: RelationType
    properties: dict[str, Any] = Field(default_factory=dict)
    archived: bool = False
