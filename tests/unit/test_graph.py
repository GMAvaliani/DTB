from dtb.graph import GraphNode, GraphRelation, KnowledgeGraph, RelationType, project_model
from tests.conftest import build_small_model


def test_projection_contains_expected_relations(small_model):
    g = project_model(small_model)
    stats = g.stats()
    assert stats["nodes"]["sed"] == 2
    assert stats["relations"]["CONSUMES"] == 3
    assert stats["relations"]["PRODUCES"] == 2
    assert stats["relations"]["TRIGGERS"] == 2  # event→production, production→sale
    assert stats["relations"]["CAUSES"] == 1  # production →(product)→ sale


def test_projection_is_deterministic(small_model):
    a = project_model(small_model).to_dict()
    b = project_model(small_model).to_dict()
    assert a == b


def test_impact_chain_resource_to_goal(small_model):
    g = project_model(small_model)
    impact = g.impact("resource:line")
    node_ids = {n["id"] for n in impact["nodes"]}
    assert {"sed:production", "utility_type:product", "sed:sale",
            "utility_type:money", "goal:g1"} <= node_ids
    chains = [" → ".join(c) for c in impact["chains"]]
    assert any(c.endswith("goal:g1") for c in chains)


def test_dependencies_upstream(small_model):
    g = project_model(small_model)
    deps = g.dependencies("goal:g1")
    node_ids = {n["id"] for n in deps["nodes"]}
    assert "event_kind:customer_order" in node_ids
    assert "resource:line" in node_ids


def test_neighborhood_and_paths(small_model):
    g = project_model(small_model)
    nb = g.neighborhood("sed:production", depth=1)
    assert len(nb["nodes"]) >= 5
    paths = g.paths("event_kind:customer_order", "utility_type:money")
    assert paths and all(p[0] == "event_kind:customer_order" for p in paths)


def test_custom_nodes_and_archive():
    g = KnowledgeGraph()
    g.add_node(GraphNode(id="custom:x", type="custom", label="X"))
    g.add_node(GraphNode(id="custom:y", type="custom", label="Y"))
    g.add_relation(GraphRelation(id="r1", source="custom:x", target="custom:y",
                                 type=RelationType.AFFECTS))
    assert len(list(g.active_relations())) == 1
    g.archive_node("custom:x")
    assert len(list(g.active_nodes())) == 1
    assert len(list(g.active_relations())) == 0  # relations archived with the node


def test_unknown_relation_endpoints_rejected():
    g = KnowledgeGraph()
    g.add_node(GraphNode(id="a", type="custom", label="A"))
    try:
        g.add_relation(GraphRelation(id="r", source="a", target="ghost",
                                     type=RelationType.AFFECTS))
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
