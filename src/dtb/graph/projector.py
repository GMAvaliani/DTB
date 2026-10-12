"""Projection of the domain model into the knowledge graph.

Deterministic: same model ⇒ same graph (stable ids), so projections can be
recomputed at any time; custom nodes/relations added through the graph API are
kept separately by the service layer and merged on top.
"""

from __future__ import annotations

from dtb.domain.events import EffectKind
from dtb.domain.model import EnterpriseModel
from dtb.domain.seds import TriggerKind
from dtb.graph.model import GraphNode, GraphRelation, RelationType
from dtb.graph.store import KnowledgeGraph


def node_id(entity_type: str, ref: str) -> str:
    return f"{entity_type}:{ref}"


def project_model(model: EnterpriseModel) -> KnowledgeGraph:
    g = KnowledgeGraph()
    ent_node = GraphNode(
        id=node_id("enterprise", model.enterprise.id),
        type="enterprise",
        label=model.enterprise.name or model.enterprise.id,
        ref_id=model.enterprise.id,
    )
    g.add_node(ent_node)

    label_props = {
        "goal": lambda e: {"priority": e.priority},
        "sed": lambda e: {"priority": e.priority, "external": e.external_model is not None},
        "utility_type": lambda e: {"kind": e.kind.value, "unit": e.unit},
        "resource": lambda e: {"kind": e.kind.value, "capacity": e.capacity},
        "org_unit": lambda e: {"kind": e.kind.value},
        "employee": lambda e: {"position": e.position},
        "equipment": lambda e: {"tech_place": e.tech_place, "status": e.status},
        "erp_object": lambda e: {"system": e.system, "object_type": e.object_type},
        "event_kind": lambda e: {"category": e.category.value, "stream": e.stream.kind.value},
        "kpi": lambda e: {"unit": e.unit},
        "subject": lambda e: {"authority_level": e.authority_level},
        "control_action": lambda e: {
            "loop": e.loop.value if e.loop else "",
            "one_time_cost": e.one_time_cost,
            "reversible": e.reversible,
        },
        "policy": lambda e: {"loop": e.loop.value, "enabled": e.enabled},
        "decision_point": lambda e: {
            "kind": e.kind.value,
            "requires_decision": e.requires_decision,
        },
        "observation": lambda e: {
            "target": e.target_kind.value,
            "delay": e.delay,
            "relative_error": e.relative_error,
            "mode": e.mode.value,
        },
    }
    for etype, entity in model.iter_entities():
        props = label_props.get(etype, lambda e: {})(entity)
        g.add_node(
            GraphNode(
                id=node_id(etype, entity.id),
                type=etype,
                label=entity.name or entity.id,
                ref_id=entity.id,
                properties=props,
            )
        )

    counter = 0

    def rel(source: str, target: str, rtype: RelationType, **props) -> None:
        nonlocal counter
        if source not in g.nodes or target not in g.nodes:
            return  # broken references are reported by validation, not here
        counter += 1
        g.add_relation(
            GraphRelation(
                id=f"r{counter:05d}:{rtype.value}",
                source=source,
                target=target,
                type=rtype,
                properties=props,
            )
        )

    for goal in model.goals.values():
        gid = node_id("goal", goal.id)
        if goal.parent_id:
            rel(node_id("goal", goal.parent_id), gid, RelationType.PARENT_OF)
        else:
            rel(ent_node.id, gid, RelationType.HAS_GOAL)
        if goal.responsible_org_unit_id:
            rel(
                node_id("org_unit", goal.responsible_org_unit_id),
                gid,
                RelationType.RESPONSIBLE_FOR,
            )
        for tm in goal.target_metrics:
            rel(gid, node_id("kpi", tm.kpi_id), RelationType.MEASURED_BY, target_value=tm.target_value)
        for sed_id in goal.sed_ids:
            rel(node_id("sed", sed_id), gid, RelationType.IMPLEMENTS)
        for res_id in goal.resource_ids:
            rel(gid, node_id("resource", res_id), RelationType.REQUIRES)

    for sed in model.seds.values():
        sid = node_id("sed", sed.id)
        if sed.parent_id:
            rel(node_id("sed", sed.parent_id), sid, RelationType.PARENT_OF)
        if sed.goal_id:
            rel(sid, node_id("goal", sed.goal_id), RelationType.IMPLEMENTS)
        if sed.org_unit_id:
            rel(sid, node_id("org_unit", sed.org_unit_id), RelationType.EXECUTED_BY)
        for flow in sed.inputs:
            rel(sid, node_id("utility_type", flow.utility_type_id), RelationType.CONSUMES, quantity=flow.quantity)
        for branch in sed.effective_branches():
            for flow in branch.outputs:
                rel(
                    sid,
                    node_id("utility_type", flow.utility_type_id),
                    RelationType.PRODUCES,
                    quantity=flow.quantity,
                    probability=branch.probability,
                    branch=branch.label,
                )
        for req in sed.resources:
            rel(sid, node_id("resource", req.resource_id), RelationType.USES_RESOURCE, quantity=req.quantity)
        for eq_id in sed.equipment_ids:
            rel(sid, node_id("equipment", eq_id), RelationType.USES_EQUIPMENT)
        for trig in sed.triggers:
            if trig.kind == TriggerKind.ON_EVENT and trig.event_kind_id:
                rel(node_id("event_kind", trig.event_kind_id), sid, RelationType.TRIGGERS)
            elif trig.kind == TriggerKind.ON_SED_COMPLETED and trig.sed_id:
                rel(node_id("sed", trig.sed_id), sid, RelationType.TRIGGERS, on="completion")
                rel(sid, node_id("sed", trig.sed_id), RelationType.DEPENDS_ON)

    for kind in model.event_kinds.values():
        kid = node_id("event_kind", kind.id)
        for effect in kind.effects:
            if effect.kind == EffectKind.SPAWN_SED and effect.sed_id:
                rel(kid, node_id("sed", effect.sed_id), RelationType.TRIGGERS)
            elif effect.kind in (EffectKind.ADD_UTILITY, EffectKind.REMOVE_UTILITY):
                rel(kid, node_id("utility_type", effect.utility_type_id or ""), RelationType.AFFECTS)
            elif effect.kind == EffectKind.RESOURCE_CAPACITY_DELTA:
                rel(kid, node_id("resource", effect.resource_id or ""), RelationType.AFFECTS, delta=effect.delta)
        for target_id in kind.affected_entity_ids:
            if ":" in target_id and target_id in g.nodes:
                rel(kid, target_id, RelationType.AFFECTS)

    # -- контур управления: кто решает, что делает, что наблюдает ---------------
    for action in model.control_actions.values():
        aid = node_id("control_action", action.id)
        effect = action.effect
        if effect.resource_id:
            rel(aid, node_id("resource", effect.resource_id), RelationType.AFFECTS,
                delta=effect.delta)
        if effect.utility_type_id:
            rel(aid, node_id("utility_type", effect.utility_type_id), RelationType.AFFECTS)
        if effect.sed_id:
            rel(aid, node_id("sed", effect.sed_id), RelationType.AFFECTS)
        if action.goal_ref:
            rel(aid, node_id("goal", action.goal_ref), RelationType.IMPLEMENTS)
        if action.subject_ref:
            rel(node_id("subject", action.subject_ref), aid, RelationType.APPLIES)

    for policy in model.policies.values():
        pid = node_id("policy", policy.id)
        for prule in policy.effective_rules():
            if prule.action_ref:
                rel(pid, node_id("control_action", prule.action_ref), RelationType.DECIDES,
                    condition=prule.condition)
        if policy.subject_ref:
            rel(node_id("subject", policy.subject_ref), pid, RelationType.APPLIES)

    for dp in model.decision_points.values():
        did = node_id("decision_point", dp.id)
        if dp.event_ref:
            rel(node_id("event_kind", dp.event_ref), did, RelationType.TRIGGERS)
        if dp.policy_ref:
            rel(did, node_id("policy", dp.policy_ref), RelationType.DECIDES)
        else:
            for policy in model.policies.values():
                rel(did, node_id("policy", policy.id), RelationType.DECIDES)

    for obs in model.observations.values():
        rel(
            node_id("observation", obs.id),
            node_id(obs.target_kind.value, obs.target_ref),
            RelationType.OBSERVES,
            metric=obs.effective_metric(),
            delay=obs.delay,
        )

    for subject in model.subjects.values():
        if subject.org_unit_ref:
            rel(node_id("subject", subject.id), node_id("org_unit", subject.org_unit_ref),
                RelationType.MEMBER_OF)

    for org in model.org_units.values():
        oid = node_id("org_unit", org.id)
        if org.parent_id:
            rel(node_id("org_unit", org.parent_id), oid, RelationType.PARENT_OF)

    for emp in model.employees.values():
        eid = node_id("employee", emp.id)
        if emp.org_unit_id:
            rel(eid, node_id("org_unit", emp.org_unit_id), RelationType.MEMBER_OF)
        if emp.resource_id:
            rel(eid, node_id("resource", emp.resource_id), RelationType.CONTRIBUTES_TO)

    for eq in model.equipment.values():
        qid = node_id("equipment", eq.id)
        if eq.org_unit_id:
            rel(qid, node_id("org_unit", eq.org_unit_id), RelationType.MEMBER_OF)
        if eq.resource_id:
            rel(qid, node_id("resource", eq.resource_id), RelationType.CONTRIBUTES_TO)

    for mapping in model.erp_mappings:
        rel(
            node_id(mapping.entity_type, mapping.entity_id),
            node_id("erp_object", mapping.erp_object_id),
            RelationType.MAPPED_TO_ERP,
            kind=mapping.kind,
        )

    # producer → consumer causality through shared utilities (CAUSES): the
    # backbone of the causal model Event → SED → Utility → next SED → Goal.
    producers: dict[str, list[str]] = {}
    for sed in model.seds.values():
        for branch in sed.effective_branches():
            for flow in branch.outputs:
                producers.setdefault(flow.utility_type_id, []).append(sed.id)
    for sed in model.seds.values():
        for flow in sed.inputs:
            for producer_id in producers.get(flow.utility_type_id, []):
                if producer_id != sed.id:
                    rel(
                        node_id("sed", producer_id),
                        node_id("sed", sed.id),
                        RelationType.CAUSES,
                        via=flow.utility_type_id,
                    )
    return g
