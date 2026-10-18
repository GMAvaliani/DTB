"""Model routes: enterprises, entities, versions, validation, graph, AI, import."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field, ValidationError

from dtb.ai import AIAssistant
from dtb.api.deps import actor_of, build_graph, ctx, get_model_or_404
from dtb.api.security import import_limiter, require_role
from dtb.domain import Enterprise, EnterpriseModel
from dtb.domain.model import ENTITY_COLLECTIONS, InitialState, SeriesPoint
from dtb.graph import GraphNode, GraphRelation
from dtb.integration import (
    MockERPConnector,
    export_graph_json,
    export_model_json,
    export_workbook,
    import_csv,
    import_model_json,
    import_workbook,
    sync_connector,
)
from dtb.persistence.versioning import diff_snapshots
from dtb.validation import data_quality_report, validate_model

router = APIRouter()

VIEW = Depends(require_role("viewer"))
EDIT = Depends(require_role("editor"))
ADMIN = Depends(require_role("admin"))


class EnterpriseCreate(BaseModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_\-]+$")
    name: str = ""
    description: str = ""
    planning_horizon_hours: float = Field(default=720.0, gt=0)
    time_zone: str = "UTC"
    simulation_calendar: str = "24x7"


# ------------------------------------------------------------------ enterprises --


@router.get("/enterprises", dependencies=[VIEW])
def list_enterprises(request: Request) -> list[dict[str, Any]]:
    return ctx(request).db.list_enterprises()


@router.post("/enterprises", dependencies=[EDIT], status_code=201)
def create_enterprise(request: Request, body: EnterpriseCreate) -> dict[str, Any]:
    context = ctx(request)
    if context.db.load_model(body.id) is not None:
        raise HTTPException(status_code=409, detail=f"enterprise {body.id!r} already exists")
    model = EnterpriseModel(enterprise=Enterprise(**body.model_dump()))
    context.db.save_model(model)
    context.db.audit(actor_of(request), "enterprise_created", body.id, "enterprise", body.id)
    return {"id": body.id, "model_version": model.enterprise.model_version}


@router.get("/enterprises/{eid}", dependencies=[VIEW])
def get_enterprise(request: Request, eid: str) -> dict[str, Any]:
    model = get_model_or_404(ctx(request), eid)
    counts = {}
    for etype in ENTITY_COLLECTIONS:
        counts[etype] = len(model.collection(etype))
    return {
        "enterprise": model.enterprise.model_dump(mode="json"),
        "counts": counts,
        "initial_state": model.initial_state.model_dump(mode="json"),
        "content_hash": model.content_hash(),
    }


@router.delete("/enterprises/{eid}", dependencies=[ADMIN])
def delete_enterprise(request: Request, eid: str) -> dict[str, Any]:
    context = ctx(request)
    get_model_or_404(context, eid)
    context.db.delete_enterprise(eid)
    context.db.audit(actor_of(request), "enterprise_deleted", eid, "enterprise", eid)
    return {"deleted": eid}


@router.get("/enterprises/{eid}/model", dependencies=[VIEW])
def get_full_model(request: Request, eid: str) -> dict[str, Any]:
    return get_model_or_404(ctx(request), eid).to_snapshot()


@router.put("/enterprises/{eid}/model", dependencies=[EDIT])
def replace_model(request: Request, eid: str, body: dict[str, Any]) -> dict[str, Any]:
    context = ctx(request)
    get_model_or_404(context, eid)
    model, report = import_model_json(body)
    if model is None:
        raise HTTPException(status_code=422, detail=report.to_dict())
    if model.enterprise.id != eid:
        raise HTTPException(status_code=422, detail="enterprise id mismatch")
    context.db.save_model(model)
    context.db.audit(actor_of(request), "model_replaced", eid, "enterprise", eid)
    return {"ok": True, "content_hash": model.content_hash()}


# --------------------------------------------------------------------- entities --


@router.get("/enterprises/{eid}/entities/{etype}", dependencies=[VIEW])
def list_entities(request: Request, eid: str, etype: str) -> list[dict[str, Any]]:
    model = get_model_or_404(ctx(request), eid)
    try:
        collection = model.collection(etype)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown entity type {etype!r}")
    return [e.model_dump(mode="json") for e in collection.values()]


@router.post("/enterprises/{eid}/entities/{etype}", dependencies=[EDIT])
def put_entity(request: Request, eid: str, etype: str, body: dict[str, Any]) -> dict[str, Any]:
    context = ctx(request)
    model = get_model_or_404(context, eid)
    cls = ENTITY_COLLECTIONS.get(etype)
    if cls is None:
        raise HTTPException(status_code=404, detail=f"unknown entity type {etype!r}")
    try:
        entity = cls.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json()))
    is_new = entity.id not in model.collection(etype)
    model.put_entity(etype, entity)
    context.db.save_model(model)
    context.db.audit(
        actor_of(request),
        "entity_created" if is_new else "entity_updated",
        eid, etype, entity.id,
    )
    return {"ok": True, "id": entity.id, "created": is_new}


@router.get("/enterprises/{eid}/entities/{etype}/{entity_id}", dependencies=[VIEW])
def get_entity(request: Request, eid: str, etype: str, entity_id: str) -> dict[str, Any]:
    model = get_model_or_404(ctx(request), eid)
    try:
        entity = model.get_entity(etype, entity_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown entity type {etype!r}")
    if entity is None:
        raise HTTPException(status_code=404, detail=f"{etype} {entity_id!r} not found")
    return entity.model_dump(mode="json")


@router.delete("/enterprises/{eid}/entities/{etype}/{entity_id}", dependencies=[EDIT])
def delete_entity(request: Request, eid: str, etype: str, entity_id: str) -> dict[str, Any]:
    context = ctx(request)
    model = get_model_or_404(context, eid)
    try:
        deleted = model.delete_entity(etype, entity_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown entity type {etype!r}")
    if not deleted:
        raise HTTPException(status_code=404, detail=f"{etype} {entity_id!r} not found")
    context.db.save_model(model)
    context.db.audit(actor_of(request), "entity_deleted", eid, etype, entity_id)
    return {"deleted": entity_id}


@router.put("/enterprises/{eid}/initial-state", dependencies=[EDIT])
def put_initial_state(request: Request, eid: str, body: dict[str, Any]) -> dict[str, Any]:
    context = ctx(request)
    model = get_model_or_404(context, eid)
    try:
        model.initial_state = InitialState.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json()))
    context.db.save_model(model)
    context.db.audit(actor_of(request), "initial_state_updated", eid, "initial_state", "")
    return {"ok": True}


# ----------------------------------------------------------------- plans/actuals --


@router.get("/enterprises/{eid}/series/{kind}", dependencies=[VIEW])
def get_series(request: Request, eid: str, kind: str) -> dict[str, Any]:
    model = get_model_or_404(ctx(request), eid)
    if kind not in ("plans", "actuals"):
        raise HTTPException(status_code=404, detail="kind must be plans|actuals")
    source = model.plans if kind == "plans" else model.actuals
    return {k: [p.model_dump() for p in pts] for k, pts in source.items()}


@router.put("/enterprises/{eid}/series/{kind}/{kpi_id}", dependencies=[EDIT])
def put_series(
    request: Request, eid: str, kind: str, kpi_id: str, body: list[dict[str, Any]]
) -> dict[str, Any]:
    context = ctx(request)
    model = get_model_or_404(context, eid)
    if kind not in ("plans", "actuals"):
        raise HTTPException(status_code=404, detail="kind must be plans|actuals")
    try:
        points = sorted(
            (SeriesPoint.model_validate(p) for p in body), key=lambda p: p.t
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json()))
    target = model.plans if kind == "plans" else model.actuals
    target[kpi_id] = points
    context.db.save_model(model)
    context.db.append_series(
        eid, "plan" if kind == "plans" else "actual", kpi_id,
        [(p.t, p.value) for p in points], source=actor_of(request),
    )
    context.db.audit(actor_of(request), f"{kind}_updated", eid, "kpi", kpi_id)
    return {"ok": True, "points": len(points)}


# ---------------------------------------------------------------------- versions --


@router.post("/enterprises/{eid}/versions", dependencies=[EDIT])
def save_version(request: Request, eid: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    context = ctx(request)
    model = get_model_or_404(context, eid)
    comment = (body or {}).get("comment", "")
    version = context.db.save_version(model, author=actor_of(request), comment=comment)
    model.enterprise.model_version = version + 1
    context.db.save_model(model)
    context.db.audit(actor_of(request), "version_saved", eid, "model_version", str(version))
    return {"version": version, "next_version": version + 1}


@router.get("/enterprises/{eid}/versions", dependencies=[VIEW])
def list_versions(request: Request, eid: str) -> list[dict[str, Any]]:
    get_model_or_404(ctx(request), eid)
    return ctx(request).db.list_versions(eid)


@router.get("/enterprises/{eid}/versions/{version}", dependencies=[VIEW])
def get_version(request: Request, eid: str, version: int) -> dict[str, Any]:
    snapshot = ctx(request).db.get_version_snapshot(eid, version)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"version {version} not found")
    return snapshot


@router.get("/enterprises/{eid}/versions-diff", dependencies=[VIEW])
def diff_versions(
    request: Request, eid: str, from_version: int = Query(alias="from"), to_version: int = Query(alias="to")
) -> dict[str, Any]:
    context = ctx(request)
    a = context.db.get_version_snapshot(eid, from_version)
    b = (
        get_model_or_404(context, eid).to_snapshot()
        if to_version == 0
        else context.db.get_version_snapshot(eid, to_version)
    )
    if a is None or b is None:
        raise HTTPException(status_code=404, detail="version not found")
    return diff_snapshots(a, b)


# -------------------------------------------------------- validation / quality --


@router.get("/enterprises/{eid}/validation", dependencies=[VIEW])
def validation(request: Request, eid: str) -> dict[str, Any]:
    model = get_model_or_404(ctx(request), eid)
    return validate_model(model).to_dict()


@router.get("/enterprises/{eid}/data-quality", dependencies=[VIEW])
def data_quality(request: Request, eid: str) -> dict[str, Any]:
    model = get_model_or_404(ctx(request), eid)
    return data_quality_report(model)


# ------------------------------------------------------------------------ graph --


@router.get("/enterprises/{eid}/graph", dependencies=[VIEW])
def get_graph(request: Request, eid: str, types: str | None = None) -> dict[str, Any]:
    model = get_model_or_404(ctx(request), eid)
    graph = build_graph(model)
    type_set = set(types.split(",")) if types else None
    data = graph.to_dict(type_set)
    data["stats"] = graph.stats()
    return data


@router.get("/enterprises/{eid}/graph/neighborhood/{node_id:path}", dependencies=[VIEW])
def graph_neighborhood(
    request: Request, eid: str, node_id: str, depth: int = 1
) -> dict[str, Any]:
    graph = build_graph(get_model_or_404(ctx(request), eid))
    try:
        return graph.neighborhood(node_id, depth=min(depth, 5))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/enterprises/{eid}/graph/impact/{node_id:path}", dependencies=[VIEW])
def graph_impact(request: Request, eid: str, node_id: str) -> dict[str, Any]:
    graph = build_graph(get_model_or_404(ctx(request), eid))
    try:
        return graph.impact(node_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/enterprises/{eid}/graph/dependencies/{node_id:path}", dependencies=[VIEW])
def graph_dependencies(request: Request, eid: str, node_id: str) -> dict[str, Any]:
    graph = build_graph(get_model_or_404(ctx(request), eid))
    try:
        return graph.dependencies(node_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/enterprises/{eid}/graph/paths", dependencies=[VIEW])
def graph_paths(
    request: Request, eid: str, source: str, target: str, max_length: int = 6
) -> dict[str, Any]:
    graph = build_graph(get_model_or_404(ctx(request), eid))
    try:
        return {"paths": graph.paths(source, target, max_length=min(max_length, 10))}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/enterprises/{eid}/graph/entities", dependencies=[EDIT])
def add_graph_node(request: Request, eid: str, body: dict[str, Any]) -> dict[str, Any]:
    context = ctx(request)
    model = get_model_or_404(context, eid)
    try:
        node = GraphNode.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json()))
    custom = model.enterprise.metadata.get("custom_graph") or {"nodes": [], "relations": []}
    custom["nodes"] = [n for n in custom["nodes"] if n.get("id") != node.id]
    custom["nodes"].append(node.model_dump(mode="json"))
    model.enterprise.metadata["custom_graph"] = custom
    context.db.save_model(model)
    context.db.audit(actor_of(request), "graph_node_added", eid, "graph_node", node.id)
    return {"ok": True, "id": node.id}


@router.post("/enterprises/{eid}/graph/relations", dependencies=[EDIT])
def add_graph_relation(request: Request, eid: str, body: dict[str, Any]) -> dict[str, Any]:
    context = ctx(request)
    model = get_model_or_404(context, eid)
    try:
        relation = GraphRelation.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json()))
    graph = build_graph(model)
    if relation.source not in graph.nodes or relation.target not in graph.nodes:
        raise HTTPException(status_code=422, detail="unknown source or target node")
    custom = model.enterprise.metadata.get("custom_graph") or {"nodes": [], "relations": []}
    custom["relations"] = [r for r in custom["relations"] if r.get("id") != relation.id]
    custom["relations"].append(relation.model_dump(mode="json"))
    model.enterprise.metadata["custom_graph"] = custom
    context.db.save_model(model)
    context.db.audit(actor_of(request), "graph_relation_added", eid, "graph_relation", relation.id)
    return {"ok": True, "id": relation.id}


# --------------------------------------------------------------------------- AI --


@router.get("/enterprises/{eid}/ai/explain/{node_id:path}", dependencies=[VIEW])
def ai_explain(request: Request, eid: str, node_id: str) -> dict[str, Any]:
    model = get_model_or_404(ctx(request), eid)
    assistant = AIAssistant(model, build_graph(model))
    try:
        return assistant.explain_entity(node_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/enterprises/{eid}/ai/search", dependencies=[VIEW])
def ai_search(request: Request, eid: str, q: str) -> list[dict[str, Any]]:
    model = get_model_or_404(ctx(request), eid)
    return AIAssistant(model, build_graph(model)).search(q)


@router.get("/enterprises/{eid}/ai/suggestions", dependencies=[VIEW])
def ai_suggestions(request: Request, eid: str) -> list[dict[str, Any]]:
    model = get_model_or_404(ctx(request), eid)
    return AIAssistant(model, build_graph(model)).suggest_missing_links()


@router.get("/enterprises/{eid}/ai/questions", dependencies=[VIEW])
def ai_questions(request: Request, eid: str) -> list[str]:
    model = get_model_or_404(ctx(request), eid)
    return AIAssistant(model, build_graph(model)).generate_questions()


# ---------------------------------------------------------------- import/export --


@router.post("/enterprises/{eid}/import/csv", dependencies=[EDIT])
async def import_csv_endpoint(
    request: Request, eid: str, sheet: str, file: UploadFile
) -> dict[str, Any]:
    context = ctx(request)
    import_limiter.check(actor_of(request))
    model = get_model_or_404(context, eid)
    content = await file.read()
    report = import_csv(model, sheet, content)
    if report.ok:
        context.db.save_model(model)
    context.db.audit(actor_of(request), "import_csv", eid, "import", sheet,
                     details=report.to_dict())
    return report.to_dict()


@router.post("/enterprises/{eid}/import/excel", dependencies=[EDIT])
async def import_excel_endpoint(request: Request, eid: str, file: UploadFile) -> dict[str, Any]:
    context = ctx(request)
    import_limiter.check(actor_of(request))
    model = get_model_or_404(context, eid)
    content = await file.read()
    report = import_workbook(model, content)
    if report.ok:
        context.db.save_model(model)
    context.db.audit(actor_of(request), "import_excel", eid, "import", file.filename or "",
                     details=report.to_dict())
    return report.to_dict()


@router.post("/enterprises/{eid}/import/json", dependencies=[EDIT])
async def import_json_endpoint(request: Request, eid: str, file: UploadFile) -> dict[str, Any]:
    context = ctx(request)
    import_limiter.check(actor_of(request))
    existing = get_model_or_404(context, eid)
    content = await file.read()
    model, report = import_model_json(content, existing)
    if model is not None and report.ok:
        context.db.save_model(model)
    context.db.audit(actor_of(request), "import_json", eid, "import", file.filename or "",
                     details={"ok": report.ok, "errors": len(report.errors)})
    return report.to_dict()


@router.post("/enterprises/{eid}/erp/sync-mock", dependencies=[EDIT])
def sync_mock_erp(request: Request, eid: str) -> dict[str, Any]:
    context = ctx(request)
    model = get_model_or_404(context, eid)
    report = sync_connector(model, MockERPConnector())
    context.db.save_model(model)
    context.db.audit(actor_of(request), "erp_sync", eid, "erp", "mock_1c",
                     details=report.to_dict())
    return report.to_dict()


@router.get("/enterprises/{eid}/export/excel", dependencies=[VIEW])
def export_excel(request: Request, eid: str) -> Response:
    model = get_model_or_404(ctx(request), eid)
    data = export_workbook(model)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{eid}_model.xlsx"'},
    )


@router.get("/enterprises/{eid}/export/json", dependencies=[VIEW])
def export_json(request: Request, eid: str) -> Response:
    model = get_model_or_404(ctx(request), eid)
    return Response(content=export_model_json(model), media_type="application/json")


@router.get("/enterprises/{eid}/export/graph-json", dependencies=[VIEW])
def export_graph(request: Request, eid: str) -> Response:
    model = get_model_or_404(ctx(request), eid)
    return Response(content=export_graph_json(model), media_type="application/json")


@router.get("/import/template", dependencies=[VIEW])
def import_template(request: Request) -> Response:
    empty = EnterpriseModel(enterprise=Enterprise(id="template", name="Шаблон модели"))
    return Response(
        content=export_workbook(empty),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="dtb_template.xlsx"'},
    )


# ------------------------------------------------------------------------- demo --


@router.post("/demo", dependencies=[EDIT], status_code=201)
def create_demo_enterprise(request: Request, which: str = "plant") -> dict[str, Any]:
    """Создать демо: ``plant`` (по умолчанию), ``farm`` (стройка) или ``all``."""
    from dtb.demo import create_demos

    context = ctx(request)
    try:
        demos = create_demos(which)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc
    created = []
    for model, scenarios in demos:
        context.db.save_model(model)
        context.db.save_version(model, author=actor_of(request), comment="demo model v1")
        for scenario in scenarios:
            context.db.save_scenario(scenario)
        context.db.audit(actor_of(request), "demo_created", model.enterprise.id,
                         "enterprise", model.enterprise.id)
        created.append(
            {"enterprise_id": model.enterprise.id, "scenarios": [s.id for s in scenarios]}
        )
    return {**created[0], "enterprises": created}


# ------------------------------------------------------------------------ audit --


@router.get("/audit", dependencies=[VIEW])
def audit_log(request: Request, enterprise_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    return ctx(request).db.audit_entries(enterprise_id, min(limit, 1000))
