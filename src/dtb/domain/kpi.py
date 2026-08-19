"""KPI definitions — safe expressions over simulation state.

Available accessors inside expressions:
``stock(u)``, ``produced(u)``, ``consumed(u)``, ``completed(sed)``,
``started(sed)``, ``events(kind)``, ``utilization(resource)``, ``t()``.
"""

from __future__ import annotations

from enum import Enum

from dtb.domain.base import EntityBase


class KPIDirection(str, Enum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


class KPIDefinition(EntityBase):
    expression: str = "0"
    unit: str = ""
    direction: KPIDirection = KPIDirection.MAXIMIZE
