"""DTB Simulation Engine — event-driven continuous-time simulation.

Independent of API/persistence/UI: depends only on ``dtb.domain`` and
``dtb.common``. Model time advances between switching moments (МП): the earliest
of the next NP-event across all streams, the earliest completion of an executing
SED, scheduled SED triggers and timed control actions.
"""

from dtb.simulation.adapters import (
    AdapterRegistry,
    CallableAdapter,
    ExternalModelAdapter,
    ExternalModelError,
    HTTPModelAdapter,
    default_registry,
)
from dtb.simulation.engine import SimulationConfig, SimulationEngine, simulate
from dtb.simulation.result import Provenance, SimulationResult
from dtb.simulation.state import SEDInstance, SimulationState

__all__ = [
    "SimulationEngine",
    "SimulationConfig",
    "simulate",
    "SimulationResult",
    "Provenance",
    "SimulationState",
    "SEDInstance",
    "ExternalModelAdapter",
    "ExternalModelError",
    "HTTPModelAdapter",
    "CallableAdapter",
    "AdapterRegistry",
    "default_registry",
]
