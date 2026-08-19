"""Shared base for all domain entities."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EntityBase(BaseModel):
    """Every catalogued domain entity: stable id + name + free metadata."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(min_length=1, max_length=200)
    name: str = ""
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
