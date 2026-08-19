"""Model version comparison: structural diff of two snapshots."""

from __future__ import annotations

from typing import Any

_COLLECTIONS = (
    "goals",
    "utility_types",
    "seds",
    "resources",
    "org_units",
    "employees",
    "equipment",
    "erp_objects",
    "event_kinds",
    "kpis",
)


def _changed_fields(a: dict[str, Any], b: dict[str, Any], prefix: str = "") -> list[dict[str, Any]]:
    changes = []
    for key in sorted(set(a) | set(b)):
        va, vb = a.get(key), b.get(key)
        if va == vb:
            continue
        path = f"{prefix}{key}"
        if isinstance(va, dict) and isinstance(vb, dict):
            changes.extend(_changed_fields(va, vb, f"{path}."))
        else:
            changes.append({"field": path, "from": va, "to": vb})
    return changes


def diff_snapshots(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Diff two EnterpriseModel snapshots: added/removed/changed per collection."""
    out: dict[str, Any] = {"collections": {}, "enterprise": [], "initial_state": []}
    out["enterprise"] = _changed_fields(a.get("enterprise", {}), b.get("enterprise", {}))
    out["initial_state"] = _changed_fields(a.get("initial_state", {}), b.get("initial_state", {}))
    for coll in _COLLECTIONS:
        ca: dict[str, Any] = a.get(coll, {}) or {}
        cb: dict[str, Any] = b.get(coll, {}) or {}
        added = sorted(set(cb) - set(ca))
        removed = sorted(set(ca) - set(cb))
        changed = []
        for eid in sorted(set(ca) & set(cb)):
            fields = _changed_fields(ca[eid], cb[eid])
            if fields:
                changed.append({"id": eid, "changes": fields})
        if added or removed or changed:
            out["collections"][coll] = {
                "added": added,
                "removed": removed,
                "changed": changed,
            }
    # mappings & plans compared coarsely
    if a.get("erp_mappings") != b.get("erp_mappings"):
        out["erp_mappings_changed"] = True
    for key in ("plans", "actuals"):
        if a.get(key) != b.get(key):
            out[f"{key}_changed"] = True
    return out
