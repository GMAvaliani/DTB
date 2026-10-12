"""Multi-step optimization framework over the simulation core.

Follows the source spec: for practically complex models the objective
J(i; x) is evaluated algorithmically via Monte-Carlo simulation rather than
analytically. Поставляются полный перебор, случайный поиск и покоординатный спуск по
переменным решения (характеристики ресурсов, интенсивности событий, параметры
СЭД, начальные запасы, **пороги политик контура управления**). Критерий —
среднее по прогонам либо нижний квантиль (осторожный выбор). Интерфейс
оптимизатора открыт для расширения (динамическое программирование по шагам
МП, эволюционные методы, RL — см. ROADMAP.md).
"""

from dtb.optimization.problem import (
    Constraint,
    DecisionVariable,
    ExpressionObjective,
    KPIObjective,
    ObjectiveFunction,
    OptimizationProblem,
    quantile_of,
)
from dtb.optimization.optimizers import (
    CoordinateSearchOptimizer,
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
    "CoordinateSearchOptimizer",
    "quantile_of",
    "OptimizationResult",
    "register_optimizer",
    "get_optimizer",
]
