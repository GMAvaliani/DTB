"""Контур управления: моменты принятия решений, политики, наблюдения, субъекты.

Деление времени на два потока моментов (референсная механика):

* **МП — момент переключения.** Продвигает модельное время и накапливает
  вознаграждение на интервале. Большинство МП решения не требуют.
* **МР — момент принятия решений.** Дискретная точка, в которой вызывается
  политика. Между МР вознаграждение накапливается, выбор действия происходит
  только в МР, что делает задачу полумарковской.

Политика читает состояние (истинное либо наблюдаемое) и применяет
управляющее действие; результат действия виден следующему МР — контур
замкнут внутри модели.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dtb.domain.base import EntityBase
from dtb.domain.events import EventEffect

# --------------------------------------------------------------------- субъект --


class Subject(EntityBase):
    """Субъект, применяющий управляющее действие (МКД: «тот, кто действует»).

    Уровень полномочий сопоставляется с ``min_authority_level`` действия:
    действие вне полномочий отклоняется и попадает в журнал отклонений.
    """

    authority_level: int = Field(default=1, ge=0)
    org_unit_ref: str | None = None
    role_ref: str | None = None  # соответствие роли RBAC: viewer | editor | admin
    external: bool = False


# ------------------------------------------------------- управляющее действие --


class ControlActionDef(EntityBase):
    """Управляющее действие как объект каталога модели.

    Сценарий может применить его по времени, политика — по состоянию.
    Поля стоимости, задержки, срока, обратимости и вероятности успеха
    задействуются движком (фаза 2), экономика начисляется по правилам фазы 3.
    """

    loop: "ControlLoop | None" = None  # к какому контуру относится действие
    effect: EventEffect  # что именно действие делает
    goal_ref: str | None = None  # цель, ради которой действие вводится
    magnitude: float | None = None  # величина (переопределяет величину эффекта)
    activation_delay: float = Field(default=0.0, ge=0)  # задержка вступления в силу, ч
    one_time_cost: float = Field(default=0.0, ge=0)  # разовая стоимость
    recurring_cost: float = Field(default=0.0, ge=0)  # повременная стоимость, за час
    term: float | None = Field(default=None, gt=0)  # срок действия, ч (None — бессрочно)
    reversible: bool = True  # можно ли отменить досрочно
    success_probability: float = Field(default=1.0, ge=0.0, le=1.0)
    min_authority_level: int = Field(default=0, ge=0)
    subject_ref: str | None = None  # субъект по умолчанию


# -------------------------------------------------------------------- политика --


class ControlLoop(str, Enum):
    """Три контура управления, различающиеся горизонтом воздействия."""

    REGULATION = "regulation"  # быстрый: поддержание нормы
    ORGANIZATION = "organization"  # средний: перераспределение мощностей
    DEVELOPMENT = "development"  # длинный: порождение технологии


class PolicyRule(BaseModel):
    """Ветвь правила: при истинном условии применяется действие."""

    model_config = ConfigDict(extra="forbid")

    condition: str = "true"
    action_ref: str
    magnitude_expression: str | None = None  # переопределение величины


class Policy(EntityBase):
    """Правило выбора управляющего действия, декларативное, не код."""

    loop: ControlLoop = ControlLoop.REGULATION
    state_selector: list[str] = Field(default_factory=list)  # какие поля состояния читает
    rule: str | None = None  # простая форма: условие …
    action_ref: str | None = None  # … и применяемое действие
    rules: list[PolicyRule] = Field(default_factory=list)  # расширенная форма
    subject_ref: str | None = None
    priority: int = 100  # меньше — раньше
    active_from: float = Field(default=0.0, ge=0)
    active_to: float | None = Field(default=None, gt=0)
    cooldown_hours: float | None = Field(default=None, gt=0)
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> "Policy":
        if not self.rules and not self.action_ref:
            raise ValueError(
                f"политика {self.id}: нужно задать action_ref (с rule) либо непустой rules"
            )
        if self.active_to is not None and self.active_to <= self.active_from:
            raise ValueError(f"политика {self.id}: active_to должно быть больше active_from")
        return self

    def effective_rules(self) -> list[PolicyRule]:
        """Единая форма: расширенная, если задана, иначе простая."""
        if self.rules:
            return list(self.rules)
        return [PolicyRule(condition=self.rule or "true", action_ref=self.action_ref or "")]


# ----------------------------------------------------- момент принятия решений --


class DecisionPointKind(str, Enum):
    EVENT = "event"  # по наступлению события заданного вида
    TIMER = "timer"  # по регламенту
    WATCHDOG = "watchdog"  # по сторожевому условию


class DecisionScope(BaseModel):
    """Что наблюдаемо в этой точке принятия решений."""

    model_config = ConfigDict(extra="forbid")

    all: bool = True
    sed_ids: list[str] = Field(default_factory=list)
    utility_type_ids: list[str] = Field(default_factory=list)
    resource_ids: list[str] = Field(default_factory=list)
    link_ids: list[str] = Field(default_factory=list)  # заготовка под связи

    def allows(self, kind: str, ref: str) -> bool:
        if self.all:
            return True
        bucket = {
            "sed": self.sed_ids,
            "utility_type": self.utility_type_ids,
            "resource": self.resource_ids,
            "link": self.link_ids,
        }.get(kind)
        return bool(bucket) and ref in bucket


class DecisionPoint(EntityBase):
    """Момент принятия решений: когда вызывается политика."""

    kind: DecisionPointKind
    event_ref: str | None = None  # kind=event
    timer_period: float | None = Field(default=None, gt=0)  # kind=timer
    timer_start: float = Field(default=0.0, ge=0)
    watchdog_condition: str | None = None  # kind=watchdog
    requires_decision: bool = True
    policy_ref: str | None = None  # конкретная политика; пусто — все подходящие
    scope: DecisionScope = Field(default_factory=DecisionScope)
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> "DecisionPoint":
        if self.kind == DecisionPointKind.EVENT and not self.event_ref:
            raise ValueError(f"точка решений {self.id}: kind=event требует event_ref")
        if self.kind == DecisionPointKind.TIMER and not self.timer_period:
            raise ValueError(f"точка решений {self.id}: kind=timer требует timer_period > 0")
        if self.kind == DecisionPointKind.WATCHDOG and not self.watchdog_condition:
            raise ValueError(
                f"точка решений {self.id}: kind=watchdog требует watchdog_condition"
            )
        return self


# ------------------------------------------------------------------ наблюдение --


class Estimator(str, Enum):
    LAST = "last"  # последнее доступное значение
    SMOOTH = "smooth"  # экспоненциальное сглаживание
    EXTRAPOLATE = "extrapolate"  # предсказание по тренду двух последних


class ObservationMode(str, Enum):
    TWIN = "twin"  # политика видит оценку Ŷ
    PURE_SIMULATION = "pure_simulation"  # политика видит истинное Y


class ObservationTarget(str, Enum):
    UTILITY_TYPE = "utility_type"
    SED = "sed"
    RESOURCE = "resource"
    KPI = "kpi"
    LINK = "link"  # заготовка под связи: наблюдение не реализовано


#: Метрика по умолчанию для каждого вида цели наблюдения.
DEFAULT_OBSERVED_METRIC = {
    ObservationTarget.UTILITY_TYPE: "stock",
    ObservationTarget.SED: "queue",
    ObservationTarget.RESOURCE: "utilization",
    ObservationTarget.KPI: "value",
    ObservationTarget.LINK: "health",
}


#: Синонимы метрик: как их пишут в Excel → как их называет движок.
METRIC_ALIASES = {
    "waiting_count": "queue",
    "waiting": "queue",
    "queue_length": "queue",
    "executing_count": "executing",
    "load": "utilization",
    "completed_count": "completed",
}


class Observation(EntityBase):
    """Наблюдаемая компонента состояния: период, задержка, погрешность, оценка."""

    target_kind: ObservationTarget = ObservationTarget.UTILITY_TYPE
    target_ref: str
    metric: str | None = None  # None — метрика по умолчанию для вида цели
    period: float = Field(default=1.0, gt=0)
    delay: float = Field(default=0.0, ge=0)
    relative_error: float = Field(default=0.0, ge=0)
    estimator: Estimator = Estimator.LAST
    mode: ObservationMode = ObservationMode.TWIN
    alpha: float = Field(default=0.5, gt=0, le=1)  # для smooth
    enabled: bool = True

    def effective_metric(self) -> str:
        """Канонический идентификатор метрики (синонимы приводятся к одному виду)."""
        metric = self.metric or DEFAULT_OBSERVED_METRIC[self.target_kind]
        return METRIC_ALIASES.get(metric.strip().lower(), metric.strip().lower())


ControlActionDef.model_rebuild()
