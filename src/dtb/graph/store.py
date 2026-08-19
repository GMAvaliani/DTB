"""In-memory knowledge graph with typed queries.

Backed by plain dicts + adjacency indexes; the API surface (`KnowledgeGraph`)
is the storage interface — a graph-DB implementation can replace it (ADR-0002).
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

from dtb.graph.model import (
    CAUSAL_FORWARD,
    CAUSAL_REVERSE,
    GraphNode,
    GraphRelation,
    RelationType,
)


class KnowledgeGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.relations: dict[str, GraphRelation] = {}
        self._out: dict[str, list[str]] = {}  # node id -> relation ids
        self._in: dict[str, list[str]] = {}

    # ------------------------------------------------------------------- CRUD --

    def add_node(self, node: GraphNode) -> GraphNode:
        self.nodes[node.id] = node
        self._out.setdefault(node.id, [])
        self._in.setdefault(node.id, [])
        return node

    def update_node(self, node_id: str, **changes: Any) -> GraphNode:
        node = self.nodes[node_id]
        updated = node.model_copy(update=changes)
        self.nodes[node_id] = updated
        return updated

    def archive_node(self, node_id: str) -> None:
        if node_id in self.nodes:
            self.nodes[node_id].archived = True
            for rel_id in self._out.get(node_id, []) + self._in.get(node_id, []):
                self.relations[rel_id].archived = True

    def add_relation(self, relation: GraphRelation) -> GraphRelation:
        if relation.source not in self.nodes or relation.target not in self.nodes:
            raise KeyError(
                f"relation {relation.id}: unknown node "
                f"{relation.source if relation.source not in self.nodes else relation.target!r}"
            )
        self.relations[relation.id] = relation
        self._out.setdefault(relation.source, []).append(relation.id)
        self._in.setdefault(relation.target, []).append(relation.id)
        return relation

    def archive_relation(self, relation_id: str) -> None:
        if relation_id in self.relations:
            self.relations[relation_id].archived = True

    # ---------------------------------------------------------------- queries --

    def active_nodes(self) -> Iterable[GraphNode]:
        return (n for n in self.nodes.values() if not n.archived)

    def active_relations(self) -> Iterable[GraphRelation]:
        return (r for r in self.relations.values() if not r.archived)

    def relations_of(
        self,
        node_id: str,
        direction: str = "both",
        types: set[RelationType] | None = None,
    ) -> list[GraphRelation]:
        rel_ids: list[str] = []
        if direction in ("out", "both"):
            rel_ids += self._out.get(node_id, [])
        if direction in ("in", "both"):
            rel_ids += self._in.get(node_id, [])
        out = []
        for rid in rel_ids:
            rel = self.relations[rid]
            if rel.archived:
                continue
            if types and rel.type not in types:
                continue
            out.append(rel)
        return out

    def neighborhood(
        self,
        node_id: str,
        depth: int = 1,
        types: set[RelationType] | None = None,
    ) -> dict[str, Any]:
        """BFS subgraph around a node up to `depth` hops (both directions)."""
        if node_id not in self.nodes:
            raise KeyError(f"unknown node {node_id!r}")
        seen = {node_id: 0}
        rels: dict[str, GraphRelation] = {}
        queue = deque([node_id])
        while queue:
            current = queue.popleft()
            if seen[current] >= depth:
                continue
            for rel in self.relations_of(current, "both", types):
                rels[rel.id] = rel
                other = rel.target if rel.source == current else rel.source
                if other not in seen:
                    seen[other] = seen[current] + 1
                    queue.append(other)
        return self._subgraph(seen.keys(), rels.values(), {"center": node_id, "depth": depth})

    def paths(
        self, source: str, target: str, max_length: int = 6, limit: int = 20
    ) -> list[list[str]]:
        """Up to `limit` node-id paths (directed, any relation type)."""
        if source not in self.nodes or target not in self.nodes:
            raise KeyError("unknown source or target node")
        results: list[list[str]] = []
        queue: deque[list[str]] = deque([[source]])
        while queue and len(results) < limit:
            path = queue.popleft()
            if len(path) > max_length:
                continue
            current = path[-1]
            if current == target and len(path) > 1:
                results.append(path)
                continue
            for rel in self.relations_of(current, "out"):
                if rel.target not in path:  # no cycles inside one path
                    queue.append(path + [rel.target])
            if current == target:
                results.append(path)
        return results

    def _causal_step(self, node_id: str, downstream: bool) -> list[tuple[GraphRelation, str]]:
        steps: list[tuple[GraphRelation, str]] = []
        fwd, rev = (CAUSAL_FORWARD, CAUSAL_REVERSE) if downstream else (CAUSAL_REVERSE, CAUSAL_FORWARD)
        for rel in self.relations_of(node_id, "out", CAUSAL_FORWARD | CAUSAL_REVERSE):
            if rel.type in fwd:
                steps.append((rel, rel.target))
        for rel in self.relations_of(node_id, "in", CAUSAL_FORWARD | CAUSAL_REVERSE):
            if rel.type in rev:
                steps.append((rel, rel.source))
        return steps

    def impact(self, node_id: str, max_depth: int = 8) -> dict[str, Any]:
        """Downstream causal closure: what changing `node_id` can affect.

        Follows causal relations forward (TRIGGERS, PRODUCES, CAUSES, AFFECTS,
        IMPLEMENTS…) and consumption/usage relations backwards (a utility's
        consumers, a resource's users), collecting influence chains
        ресурс → СЭД → полезность → цель → KPI.
        """
        return self._causal_closure(node_id, downstream=True, max_depth=max_depth)

    def dependencies(self, node_id: str, max_depth: int = 8) -> dict[str, Any]:
        """Upstream closure: what `node_id` depends on."""
        return self._causal_closure(node_id, downstream=False, max_depth=max_depth)

    def _causal_closure(self, node_id: str, downstream: bool, max_depth: int) -> dict[str, Any]:
        if node_id not in self.nodes:
            raise KeyError(f"unknown node {node_id!r}")
        level = {node_id: 0}
        parent: dict[str, tuple[str, GraphRelation]] = {}
        rels: dict[str, GraphRelation] = {}
        queue = deque([node_id])
        while queue:
            current = queue.popleft()
            if level[current] >= max_depth:
                continue
            for rel, other in self._causal_step(current, downstream):
                rels.setdefault(rel.id, rel)
                if other not in level:
                    level[other] = level[current] + 1
                    parent[other] = (current, rel)
                    queue.append(other)
        chains = []
        for nid in level:
            if nid == node_id:
                continue
            chain = [nid]
            cursor = nid
            while cursor in parent:
                cursor = parent[cursor][0]
                chain.append(cursor)
            chains.append(list(reversed(chain)))
        chains.sort(key=lambda c: (len(c), c[-1]))
        sub = self._subgraph(
            level.keys(),
            rels.values(),
            {
                "center": node_id,
                "direction": "downstream" if downstream else "upstream",
                "levels": level,
            },
        )
        sub["chains"] = chains
        return sub

    def _subgraph(
        self,
        node_ids: Iterable[str],
        relations: Iterable[GraphRelation],
        meta: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "meta": meta,
            "nodes": [self.nodes[n].model_dump() for n in node_ids if n in self.nodes],
            "relations": [r.model_dump() for r in relations],
        }

    def to_dict(self, types: set[str] | None = None) -> dict[str, Any]:
        nodes = [
            n.model_dump()
            for n in self.active_nodes()
            if types is None or n.type in types
        ]
        keep = {n["id"] for n in nodes}
        relations = [
            r.model_dump()
            for r in self.active_relations()
            if r.source in keep and r.target in keep
        ]
        return {"nodes": nodes, "relations": relations}

    def stats(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        for n in self.active_nodes():
            by_type[n.type] = by_type.get(n.type, 0) + 1
        by_rel: dict[str, int] = {}
        for r in self.active_relations():
            by_rel[r.type.value] = by_rel.get(r.type.value, 0) + 1
        return {"nodes": by_type, "relations": by_rel}
