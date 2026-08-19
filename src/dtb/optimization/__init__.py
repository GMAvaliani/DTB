"""Multi-step optimization framework over the simulation core.

Follows the source spec: for practically complex models the objective
J(i; x) is evaluated algorithmically via Monte-Carlo simulation rather than
analytically. MVP ships grid search and random search over decision variables
(resource characteristics, event rates, SED parameters, initial stocks) with a
plugin interface for future optimizers (dynamic programming over МП steps,
stochastic/evolutionary/RL — see ROADMAP.md).
"""

from dtb.optimization.problem import (
    Constraint,
    DecisionVariable,
    ExpressionObjective,
    KPIObjective,
    ObjectiveFunction,
    OptimizationProblem,
)
from dtb.optimization.optimizers import (
    GridSearchOptimizer,
    OptimizationResult,
    Optimizer,
    RandomSearchOptimizer,
    get_optimizer,
    register_optimizer,
)

__all__ = [
    "DecisionVariable",
    "ObjectiveFunction",
    "KPIObjective",
    "ExpressionObjective",
    "Constraint",
    "OptimizationProblem",
    "Optimizer",
    "GridSearchOptimizer",
    "RandomSearchOptimizer",
    "OptimizationResult",
    "register_optimizer",
    "get_optimizer",
]
