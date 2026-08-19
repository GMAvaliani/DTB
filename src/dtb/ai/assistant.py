"""Rule-based AI assistant over the structured twin + pluggable LLM interface.

Implemented capabilities (all computed from the model/graph/results — the LLM
is never the source of truth about enterprise state):

- explain_entity — объяснение сущности по её графовому окружению;
- search — поиск сущностей;
- find_dependencies — зависимости через causal-запросы графа;
- analyze_deviations — причины отклонений из forecast/bottlenecks;
- suggest_missing_links — эвристики потенциально отсутствующих связей;
- generate_questions — вопросы к модели для её уточнения;
- propose_change — контролируемое изменение модели (validate → diff → confirm).

The LLMProvider protocol is the integration point for natural-language
rendering/report generation; live wiring is a planned iteration (ROADMAP §6) —
no fake calls are made.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from dtb.domain.model import EnterpriseModel
from dtb.graph import KnowledgeGraph, project_model
from dtb.persistence.versioning import diff_snapshots
from dtb.validation import validate_model

_RELATION_PHRASES = {
    "CONSUMES": "потребляет",
    "PRODUCES": "формирует",
    "TRIGGERS": "запускает",
    "IMPLEMENTS": "реализует цель",
    "EXECUTED_BY": "выполняется",
    "USES_RESOURCE": "использует ресурс",
    "USES_EQUIPMENT": "использует оборудование",
    "RESPONSIBLE_FOR": "отвечает за",
    "MEASURED_BY": "измеряется показателем",
    "AFFECTS": "влияет на",
    "CAUSES": "обусловливает",
    "PARENT_OF": "включает",
    "HAS_GOAL": "имеет цель",
    "MAPPED_TO_ERP": "связан с ERP-объектом",
    "MEMBER_OF": "входит в",
    "CONTRIBUTES_TO": "входит в ресурсный пул",
    "DEPENDS_ON": "зависит от",
    "REQUIRES": "требует",
}


class LLMProvider(Protocol):
    """Natural-language rendering layer (optional, pluggable)."""

    def complete(self, system: str, prompt: str) -> str: ...


class NullLLMProvider:
    """No-LLM default: assistant answers stay structured/rule-based."""

    def complete(self, system: str, prompt: str) -> str:
        raise RuntimeError(
            "LLM provider is not configured (DTB ships rule-based answers only; "
            "plug an LLMProvider to enable natural-language rendering)"
        )


@dataclass
class ProposedChange:
    """A model mutation awaiting user confirmation (никогда не применяется молча)."""

    description: str
    snapshot_before: dict[str, Any]
    snapshot_after: dict[str, Any]
    diff: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)

    @property
    def safe_to_apply(self) -> bool:
        return self.validation.get("ok_to_simulate", False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "diff": self.diff,
            "validation": self.validation,
            "safe_to_apply": self.safe_to_apply,
        }


class AIAssistant:
    def __init__(self, model: EnterpriseModel, graph: KnowledgeGraph | None = None,
                 llm: LLMProvider | None = None):
        self.model = model
        self.graph = graph or project_model(model)
        self.llm = llm or NullLLMProvider()

    # ------------------------------------------------------------------ explain --

    def explain_entity(self, node_id: str) -> dict[str, Any]:
        node = self.graph.nodes.get(node_id)
        if node is None:
            raise KeyError(f"unknown node {node_id!r}")
        statements: list[str] = []
        for rel in self.graph.relations_of(node_id, "out"):
            phrase = _RELATION_PHRASES.get(rel.type.value, rel.type.value)
            target = self.graph.nodes.get(rel.target)
            if target:
                qty = rel.properties.get("quantity")
                suffix = f" ({qty:g})" if isinstance(qty, (int, float)) else ""
                statements.append(f"{phrase} «{target.label}»{suffix}")
        incoming: list[str] = []
        for rel in self.graph.relations_of(node_id, "in"):
            phrase = _RELATION_PHRASES.get(rel.type.value, rel.type.value)
            source = self.graph.nodes.get(rel.source)
            if source:
                incoming.append(f"«{source.label}» {phrase} эту сущность")
        entity = None
        if node.ref_id and node.type != "enterprise":
            try:
                found = self.model.get_entity(node.type, node.ref_id)
                entity = found.model_dump(mode="json") if found else None
            except KeyError:
                entity = None
        return {
            "node": node.model_dump(),
            "entity": entity,
            "statements": statements,
            "incoming": incoming,
            "impact_size": max(0, len(self.graph.impact(node_id)["nodes"]) - 1),
        }

    # ------------------------------------------------------------------- search --

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        q = query.strip().lower()
        if not q:
            return []
        scored: list[tuple[float, dict[str, Any]]] = []
        for node in self.graph.active_nodes():
            hay = f"{node.label} {node.id} {node.ref_id or ''}".lower()
            if q in hay:
                score = 2.0 if node.label.lower().startswith(q) else 1.0
                scored.append((score, node.model_dump()))
            else:
                terms = q.split()
                hits = sum(1 for t in terms if t in hay)
                if hits and hits == len(terms):
                    scored.append((0.5, node.model_dump()))
        scored.sort(key=lambda p: (-p[0], p[1]["label"]))
        return [n for _, n in scored[:limit]]

    def find_dependencies(self, node_id: str) -> dict[str, Any]:
        return self.graph.dependencies(node_id)

    # -------------------------------------------------------------- deviations --

    def analyze_deviations(self, forecast: dict[str, Any]) -> list[dict[str, Any]]:
        """Rank deviation causes from an existing forecast report (evidence-based)."""
        causes: list[dict[str, Any]] = []
        for kpi_id, report in forecast.get("kpis", {}).items():
            deviation = report.get("deviation_from_plan")
            if deviation is None or deviation >= 0:
                continue
            causes.append(
                {
                    "kind": "kpi_below_plan",
                    "kpi_id": kpi_id,
                    "name": report.get("name", kpi_id),
                    "deviation": deviation,
                    "explanation": f"Прогноз «{report.get('name', kpi_id)}» ниже плана на "
                    f"{abs(deviation):g} {report.get('unit', '')}".strip(),
                }
            )
        for b in forecast.get("bottlenecks", [])[:10]:
            causes.append(
                {
                    "kind": "bottleneck",
                    "type": b.get("type"),
                    "explanation": b.get("evidence", ""),
                    "severity": b.get("severity"),
                }
            )
        for goal_id, goal in forecast.get("goals", {}).items():
            p = goal.get("achievement_probability")
            if p is not None and p < 0.8:
                causes.append(
                    {
                        "kind": "goal_at_risk",
                        "goal_id": goal_id,
                        "probability": p,
                        "explanation": f"Цель «{goal.get('name', goal_id)}» достигается "
                        f"с вероятностью {p * 100:.0f}%",
                    }
                )
        return causes

    # ----------------------------------------------------------- missing links --

    def suggest_missing_links(self) -> list[dict[str, Any]]:
        m = self.model
        suggestions: list[dict[str, Any]] = []
        produced = {
            f.utility_type_id
            for s in m.seds.values()
            for b in s.effective_branches()
            for f in b.outputs
        }
        event_supplied = {
            e.utility_type_id
            for k in m.event_kinds.values()
            for e in k.effects
            if e.utility_type_id
        }
        for sed in m.seds.values():
            for flow in sed.inputs:
                if (
                    flow.utility_type_id not in produced
                    and flow.utility_type_id not in event_supplied
                    and flow.utility_type_id not in m.initial_state.utility_stocks
                ):
                    suggestions.append(
                        {
                            "kind": "unsourced_input",
                            "sed_id": sed.id,
                            "utility_type_id": flow.utility_type_id,
                            "suggestion": f"СЭД «{sed.name}» потребляет "
                            f"«{flow.utility_type_id}», но её никто не производит: добавьте "
                            "производящий СЭД, событие поставки или начальный запас",
                        }
                    )
            if not sed.goal_id:
                suggestions.append(
                    {
                        "kind": "sed_without_goal",
                        "sed_id": sed.id,
                        "suggestion": f"СЭД «{sed.name}» не связан с целью — укажите goal_id",
                    }
                )
        implemented_goals = {s.goal_id for s in m.seds.values() if s.goal_id}
        for goal in m.goals.values():
            has_children = any(g.parent_id == goal.id for g in m.goals.values())
            if goal.id not in implemented_goals and not has_children and not goal.sed_ids:
                suggestions.append(
                    {
                        "kind": "goal_without_sed",
                        "goal_id": goal.id,
                        "suggestion": f"Цель «{goal.name}» не реализуется ни одним СЭД",
                    }
                )
            if not goal.responsible_org_unit_id:
                suggestions.append(
                    {
                        "kind": "goal_without_center",
                        "goal_id": goal.id,
                        "suggestion": f"У цели «{goal.name}» нет центра ответственности",
                    }
                )
        used = {r.resource_id for s in m.seds.values() for r in s.resources}
        for res in m.resources.values():
            if res.id not in used:
                suggestions.append(
                    {
                        "kind": "unused_resource",
                        "resource_id": res.id,
                        "suggestion": f"Ресурс «{res.name}» не используется ни одним СЭД",
                    }
                )
        mapped = {(mm.entity_type, mm.entity_id) for mm in m.erp_mappings}
        for utype in m.utility_types.values():
            if utype.kind.value in ("material", "raw_material", "component") and (
                "utility_type",
                utype.id,
            ) not in mapped:
                suggestions.append(
                    {
                        "kind": "unmapped_material",
                        "utility_type_id": utype.id,
                        "suggestion": f"Материал «{utype.name}» не связан с ERP-объектом",
                    }
                )
        return suggestions

    # ---------------------------------------------------------------- questions --

    def generate_questions(self) -> list[str]:
        m = self.model
        questions: list[str] = []
        for sed in m.seds.values():
            if sed.duration.kind.value == "fixed":
                questions.append(
                    f"Длительность СЭД «{sed.name}» задана константой "
                    f"({sed.duration.value:g} ч) — известна ли её реальная вариативность?"
                )
        for kind in m.event_kinds.values():
            if kind.stream.kind.value == "poisson":
                questions.append(
                    f"Интенсивность события «{kind.name}» = {kind.stream.rate_per_hour:g}/ч — "
                    "подтверждена ли она историческими данными?"
                )
        for goal in m.goals.values():
            if not goal.target_metrics:
                questions.append(f"Как измерить достижение цели «{goal.name}»? Добавьте KPI.")
        if not m.actuals:
            questions.append("Фактические значения KPI не загружены — импортируйте их из ERP.")
        return questions[:25]

    # ------------------------------------------------------------------ changes --

    def propose_change(
        self, description: str, mutate: Any
    ) -> ProposedChange:
        """Build a change proposal: apply `mutate(model_copy)` to a copy, diff,
        validate. The caller (user) decides whether to apply the returned
        snapshot_after to the real model."""
        before = self.model.to_snapshot()
        candidate = self.model.clone()
        mutate(candidate)
        after = candidate.to_snapshot()
        return ProposedChange(
            description=description,
            snapshot_before=before,
            snapshot_after=after,
            diff=diff_snapshots(before, after),
            validation=validate_model(candidate).to_dict(),
        )
