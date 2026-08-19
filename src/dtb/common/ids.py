"""Identifier helpers."""

from __future__ import annotations

import re
import uuid

_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def new_id(prefix: str = "") -> str:
    suffix = uuid.uuid4().hex[:12]
    return f"{prefix}_{suffix}" if prefix else suffix


def slugify(name: str, prefix: str = "") -> str:
    slug = _SLUG_RE.sub("_", name.strip().lower()).strip("_") or uuid.uuid4().hex[:8]
    return f"{prefix}_{slug}" if prefix else slug
