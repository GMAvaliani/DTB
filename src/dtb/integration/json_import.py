"""JSON import/export: full model snapshots and Graph JSON."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from dtb.domain.model import EnterpriseModel
from dtb.graph import project_model
from dtb.integration.base import ImportReport


def import_model_json(
    data: str | bytes | dict[str, Any],
    existing: EnterpriseModel | None = None,
) -> tuple[EnterpriseModel | None, ImportReport]:
    """Import a full model snapshot (replaces the model; id must match if
    an existing model is supplied)."""
    report = ImportReport(source="json")
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            report.error("json", f"невалидный JSON: {exc}")
            return None, report
    try:
        model = EnterpriseModel.from_snapshot(data)  # type: ignore[arg-type]
    except ValidationError as exc:
        for err in exc.errors()[:50]:
            loc = ".".join(str(p) for p in err["loc"])
            report.error("json", f"{loc}: {err['msg']}")
        return None, report
    if existing is not None and model.enterprise.id != existing.enterprise.id:
        report.error(
            "json",
            f"id предприятия в файле ({model.enterprise.id!r}) не совпадает "
            f"с целевым ({existing.enterprise.id!r})",
        )
        return None, report
    for etype, _ in model.iter_entities():
        report.count(etype, True)
    return model, report


def export_model_json(model: EnterpriseModel) -> str:
    return json.dumps(model.to_snapshot(), ensure_ascii=False, indent=2)


def export_graph_json(model: EnterpriseModel) -> str:
    """Graph JSON (nodes/relations) — the knowledge-graph exchange format."""
    return json.dumps(project_model(model).to_dict(), ensure_ascii=False, indent=2)
