"""Optimization problem definition: decision variables, objective, constraints.

Decision variables address model/scenario parameters through typed paths:

- ``resource:<id>``            — resource capacity (управляющее воздействие u)
- ``event_rate:<kind_id>``     — NP-event stream rate multiplier
- ``sed_duration:<sed_id>``    — SED duration scale
- ``stock:<utility_type_id>``  — initial utility stock
- ``param:<entity_type>:<id>.<field>`` — arbitrary model parameter
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from dtb.analysis.montecarlo import MonteCarloResult
from dtb.common.expr import SafeExpression
from dtb.domain.kpi import KPIDirection
from dtb.domain.scenario import Scenario, SEDOverride

_PREFIXES = ("resource:", "event_rate:", "sed_duration:", "stock:", "param:")


class DecisionVariable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    values: list[float] = Field(min_length=1)

    def validate_path(self) -> None:
        if not self.path.startswith(_PREFIXES):
            raise ValueError(
                f"decision variable {self.name}: path must start with one of {_PREFIXES}"
            )

    def apply(self, scenario: Scenario, value: float) -> None:
        prefix, _, ref = self.path.partition(":")
        if prefix == "resource":
            scenario.resource_capacity_overrides[ref] = value
        elif prefix == "event_rate":
            scenario.event_rate_multipliers[ref] = value
        elif prefix == "sed_duration":
            override = scenario.sed_overrides.get(ref) or SEDOverride()
            override.duration_scale = value
            scenario.sed_overrides[ref] = override
        elif prefix == "stock":
            scenario.initial_stock_overrides[ref] = value
        elif prefix == "param":
            scenario.parameter_overrides[ref] = value
        else:  # pragma: no cover — validate_path guards this
            raise ValueError(f"unknown decision path {self.path!r}")


class ObjectiveFunction(Protocol):
    """Objective over a Monte-Carlo evaluation; larger is always better here —
    minimisation objectives return negated values."""

    def evaluate(self, mc: MonteCarloResult) -> float: ...

    def describe(self) -> dict[str, Any]: ...


class KPIObjective:
    """Expected final value of one KPI (revenue, profit, cost, service level…)."""

    def __init__(
        self,
        kpi_id: str,
        direction: KPIDirection = KPIDirection.MAXIMIZE,
        target: float | None = None,
    ):
        self.kpi_id = kpi_id
        self.direction = direction
        self.target = target

    def evaluate(self, mc: MonteCarloResult) -> float:
        mean = mc.final_kpis.get(self.kpi_id, {}).get("mean", 0.0)
        return mean if self.direction == KPIDirection.MAXIMIZE else -mean

    def describe(self) -> dict[str, Any]:
        return {
            "type": "kpi",
            "kpi_id": self.kpi_id,
            "direction": self.direction.value,
            "target": self.target,
        }


class ExpressionObjective:
    """Custom metric: safe expression over final KPI expectations.

    Context: ``kpi('<id>')`` → expected final value, ``prob('<goal_id>')`` →
    goal achievement probability.
    """

    def __init__(self, expression: str, direction: KPIDirection = KPIDirection.MAXIMIZE):
        self.expression = expression
        self.direction = direction
        self._expr = SafeExpression(expression)

    def evaluate(self, mc: MonteCarloResult) -> float:
        value = float(
            self._expr.evaluate(
                {
                    "kpi": lambda k: mc.final_kpis.get(k, {}).get("mean", 0.0),
                    "prob": lambda g: mc.goal_achievement_probability.get(g, 0.0),
                }
            )
        )
        return value if self.direction == KPIDirection.MAXIMIZE else -value

    def describe(self) -> dict[str, Any]:
        return {
            "type": "expression",
            "expression": self.expression,
            "direction": self.direction.value,
        }


class Constraint(BaseModel):
    """Feasibility constraint over an evaluation, e.g. ``kpi('service') >= 90``."""

    model_config = ConfigDict(extra="forbid")

    expression: str
    label: str = ""

    def is_satisfied(self, mc: MonteCarloResult) -> bool:
        expr = SafeExpression(self.expression)
        return bool(
            expr.evaluate(
                {
                    "kpi": lambda k: mc.final_kpis.get(k, {}).get("mean", 0.0),
                    "prob": lambda g: mc.goal_achievement_probability.get(g, 0.0),
                }
            )
        )


class OptimizationProblem(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    variables: list[DecisionVariable] = Field(min_length=1)
    objective_kpi_id: str | None = None
    objective_expression: str | None = None
    direction: KPIDirection = KPIDirection.MAXIMIZE
    constraints: list[Constraint] = Field(default_factory=list)
    runs_per_evaluation: int = Field(default=5, ge=1)
    seed: int = 0
    max_evaluations: int = Field(default=200, ge=1)

    def build_objective(self) -> ObjectiveFunction:
        if self.objective_expression:
            return ExpressionObjective(self.objective_expression, self.direction)
        if self.objective_kpi_id:
            return KPIObjective(self.objective_kpi_id, self.direction)
        raise ValueError("problem needs objective_kpi_id or objective_expression")
