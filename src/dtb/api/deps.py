"""Shared API context and helpers."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from dtb.domain.model import EnterpriseModel
from dtb.graph import GraphNode, GraphRelation, KnowledgeGraph, project_model
from dtb.persistence import Database
from dtb.services.jobs import JobManager


class AppContext:
    def __init__(self, db: Database):
        self.db = db
        self.jobs = JobManager()


def ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_model_or_404(context: AppContext, eid: str) -> EnterpriseModel:
    model = context.db.load_model(eid)
    if model is None:
        raise HTTPException(status_code=404, detail=f"enterprise {eid!r} not found")
    return model


def actor_of(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    return principal.actor if principal else "dev"


def build_graph(model: EnterpriseModel) -> KnowledgeGraph:
    """Projected graph + user-added custom nodes/relations (stored in model metadata)."""
    graph = project_model(model)
    custom: dict[str, Any] = model.enterprise.metadata.get("custom_graph") or {}
    for node_data in custom.get("nodes", []):
        try:
            graph.add_node(GraphNode.model_validate(node_data))
        except Exception:  # noqa: BLE001 — a broken custom node must not kill the graph
            continue
    for rel_data in custom.get("relations", []):
        try:
            graph.add_relation(GraphRelation.model_validate(rel_data))
        except Exception:  # noqa: BLE001
            continue
    return graph


def save_custom_graph(
    context: AppContext, model: EnterpriseModel, custom: dict[str, Any]
) -> None:
    model.enterprise.metadata["custom_graph"] = custom
    context.db.save_model(model)
