"""Knowledge graph + causal model over the digital twin (View 8 and Views 1–2).

The graph is projected from the domain model (`project_model`) and can be
extended with custom nodes/relations. Queries: neighborhood, paths, downstream
impact ("что произойдёт, если изменить X?") and upstream dependencies.
"""

from dtb.graph.model import GraphNode, GraphRelation, RelationType
from dtb.graph.projector import project_model
from dtb.graph.store import KnowledgeGraph

__all__ = ["KnowledgeGraph", "GraphNode", "GraphRelation", "RelationType", "project_model"]
