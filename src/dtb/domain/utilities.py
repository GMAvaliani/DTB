"""Utilities (полезности) — the fundamental state-forming entity.

A utility is *any* final or intermediate result of activity: products, services,
components, materials, moved stock, trained employees, money, information,
performed work, energy, or anything else — never only physical goods.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field

from dtb.domain.base import EntityBase


class UtilityKind(str, Enum):
    PRODUCT = "product"
    SERVICE = "service"
    COMPONENT = "component"
    MATERIAL = "material"
    RAW_MATERIAL = "raw_material"
    ENERGY = "energy"
    MONEY = "money"
    INFORMATION = "information"
    WORK = "work"
    TRAINED_EMPLOYEE = "trained_employee"
    MOVED_STOCK = "moved_stock"
    OTHER = "other"


class UtilityType(EntityBase):
    """Catalogue entry describing one kind of utility."""

    kind: UtilityKind = UtilityKind.OTHER
    unit: str = "unit"
    quality_attributes: dict[str, Any] = Field(default_factory=dict)


class UtilityFlow(EntityBase):
    """A quantity of one utility type flowing into or out of a SED."""

    id: str = "-"  # flows are positional, not catalogued
    utility_type_id: str
    quantity: float = Field(gt=0)


class Utility(EntityBase):
    """A concrete produced/consumed batch (simulation ledger record)."""

    utility_type_id: str
    quantity: float
    owner: str | None = None
    location: str | None = None
    timestamp: float = 0.0  # model time, hours
    source: str | None = None  # producing SED / event / import
    target: str | None = None  # consuming SED
    quality: dict[str, Any] = Field(default_factory=dict)
