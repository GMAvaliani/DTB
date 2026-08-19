"""DTB domain model — pure, validated entity definitions (no engine, no I/O).

The ubiquitous language follows the source specification:
utility (полезность), SED (структурный элемент деятельности), NP-event
(НП-событие), switching moment (момент переключения, МП).
"""

from dtb.domain.base import EntityBase
from dtb.domain.enterprise import Enterprise, EnterpriseStatus
from dtb.domain.events import (
    EffectKind,
    EventCategory,
    EventEffect,
    EventKind,
    StreamKind,
    StreamSpec,
)
from dtb.domain.goals import Goal, TargetMetric
from dtb.domain.kpi import KPIDefinition
from dtb.domain.model import EnterpriseModel, InitialState, SeriesPoint
from dtb.domain.resources import (
    Employee,
    Equipment,
    ERPMapping,
    ERPObject,
    OrgUnit,
    OrgUnitKind,
    Resource,
    ResourceKind,
)
from dtb.domain.scenario import (
    ControlAction,
    InjectedEvent,
    ObjectiveSpec,
    Scenario,
    SEDOverride,
)
from dtb.domain.seds import (
    DurationKind,
    DurationModel,
    ExternalModelRef,
    OutcomeBranch,
    ResourceRequirement,
    SEDDefinition,
    SEDStatus,
    TriggerKind,
    TriggerSpec,
)
from dtb.domain.utilities import Utility, UtilityFlow, UtilityKind, UtilityType

__all__ = [
    "EntityBase",
    "Enterprise",
    "EnterpriseStatus",
    "EnterpriseModel",
    "InitialState",
    "SeriesPoint",
    "Goal",
    "TargetMetric",
    "KPIDefinition",
    "UtilityType",
    "UtilityKind",
    "UtilityFlow",
    "Utility",
    "SEDDefinition",
    "SEDStatus",
    "DurationModel",
    "DurationKind",
    "TriggerSpec",
    "TriggerKind",
    "ResourceRequirement",
    "OutcomeBranch",
    "ExternalModelRef",
    "Resource",
    "ResourceKind",
    "OrgUnit",
    "OrgUnitKind",
    "Employee",
    "Equipment",
    "ERPObject",
    "ERPMapping",
    "EventKind",
    "EventCategory",
    "EventEffect",
    "EffectKind",
    "StreamSpec",
    "StreamKind",
    "Scenario",
    "SEDOverride",
    "InjectedEvent",
    "ControlAction",
    "ObjectiveSpec",
]
