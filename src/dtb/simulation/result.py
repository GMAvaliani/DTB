"""Simulation results with full provenance (engineering rule 6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dtb import ENGINE_VERSION


@dataclass
class Provenance:
    enterprise_id: str
    model_version: int
    model_hash: str
    scenario_id: str
    scenario_version: int
    initial_state_hash: str
    seed: int
    horizon_hours: float
    engine_version: str = ENGINE_VERSION
    started_at: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class SimulationResult:
    provenance: Provenance
    status: str = "completed"  # completed | cancelled | failed
    error: str | None = None
    # trajectories (sampled at switching moments)
    kpi_series: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    stock_series: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    pool_series: list[tuple[float, int, int]] = field(default_factory=list)  # t, waiting, executing
    # logs & stats
    event_log: list[dict[str, Any]] = field(default_factory=list)
    event_log_truncated: int = 0
    # контур управления
    decision_log: list[dict[str, Any]] = field(default_factory=list)
    policy_trace: list[dict[str, Any]] = field(default_factory=list)
    observation_error: dict[str, Any] = field(default_factory=dict)
    rejected_actions: list[dict[str, Any]] = field(default_factory=list)
    control_costs: dict[str, Any] = field(default_factory=dict)
    sed_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    resource_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    utility_totals: dict[str, dict[str, float]] = field(default_factory=dict)
    goal_achievement: dict[str, dict[str, Any]] = field(default_factory=dict)
    final_kpis: dict[str, float] = field(default_factory=dict)
    final_stocks: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provenance": self.provenance.to_dict(),
            "status": self.status,
            "error": self.error,
            "kpi_series": {k: [[t, v] for t, v in s] for k, s in self.kpi_series.items()},
            "stock_series": {
                k: [[t, v] for t, v in s] for k, s in self.stock_series.items()
            },
            "pool_series": [[t, w, e] for t, w, e in self.pool_series],
            "event_log": self.event_log,
            "event_log_truncated": self.event_log_truncated,
            "decision_log": self.decision_log,
            "policy_trace": self.policy_trace,
            "observation_error": self.observation_error,
            "rejected_actions": self.rejected_actions,
            "control_costs": self.control_costs,
            "sed_stats": self.sed_stats,
            "resource_stats": self.resource_stats,
            "utility_totals": self.utility_totals,
            "goal_achievement": self.goal_achievement,
            "final_kpis": self.final_kpis,
            "final_stocks": self.final_stocks,
            "metrics": self.metrics,
        }

    def content_digest(self) -> str:
        """Hash of the simulation content, excluding wall-clock metadata.

        Identical (model, scenario, initial state, seed) ⇒ identical digest —
        the reproducibility invariant (acceptance criterion 20).
        """
        import hashlib
        import json

        data = self.to_dict()
        data["provenance"].pop("started_at", None)
        data["metrics"].pop("wall_time_seconds", None)
        payload = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SimulationResult":
        prov = Provenance(**data["provenance"])
        result = cls(provenance=prov, status=data.get("status", "completed"))
        result.error = data.get("error")
        result.kpi_series = {
            k: [(p[0], p[1]) for p in s] for k, s in data.get("kpi_series", {}).items()
        }
        result.stock_series = {
            k: [(p[0], p[1]) for p in s] for k, s in data.get("stock_series", {}).items()
        }
        result.pool_series = [(p[0], p[1], p[2]) for p in data.get("pool_series", [])]
        result.event_log = data.get("event_log", [])
        result.event_log_truncated = data.get("event_log_truncated", 0)
        result.decision_log = data.get("decision_log", [])
        result.policy_trace = data.get("policy_trace", [])
        result.observation_error = data.get("observation_error", {})
        result.rejected_actions = data.get("rejected_actions", [])
        result.control_costs = data.get("control_costs", {})
        result.sed_stats = data.get("sed_stats", {})
        result.resource_stats = data.get("resource_stats", {})
        result.utility_totals = data.get("utility_totals", {})
        result.goal_achievement = data.get("goal_achievement", {})
        result.final_kpis = data.get("final_kpis", {})
        result.final_stocks = data.get("final_stocks", {})
        result.metrics = data.get("metrics", {})
        return result
