"""Экономика деятельности: непрерывная ставка, разовые начисления, остаточная
стоимость и непрерывное дисконтирование.

Разделение начислений (референсная механика):

* **ставка** — поток ₽/ч, который идёт всё время, пока держится состояние
  (аренда ёмкости, оплата занятых ресурсов, хранение запаса);
* **разовое** — начисление в момент события (завершение СЭД, выпуск единицы
  продукции, применение управляющего действия);
* **остаточная стоимость** — оценка на конце горизонта (незавершённое
  производство, остатки, введённая мощность).

Дисконтирование непрерывное: вес момента t равен ``exp(-ρ t)``, где
``ρ = discount_rate_annual / hours_per_year``. Для ставки на интервале
[t₀, t₁] вклад равен ``rate · (e^{-ρ t₀} − e^{-ρ t₁}) / ρ``, то есть интеграл
считается точно, а не по средней точке.

Каждое начисление отнесено к **статье** (``article``), поэтому итог
раскладывается по статьям, видам начисления и контурам управления.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dtb.domain.base import EntityBase


class EconomicKind(str, Enum):
    RATE = "rate"  # ₽/ч, интегрируется по времени
    ONE_TIME = "one_time"  # ₽ в момент события
    TERMINAL = "terminal"  # ₽ на конце горизонта


class EconomicBasis(str, Enum):
    """Чему пропорционально начисление."""

    CONSTANT = "constant"  # просто ставка в час (постоянные расходы)
    RESOURCE_CAPACITY = "resource_capacity"  # × ёмкость ресурса
    RESOURCE_BUSY = "resource_busy"  # × занятые единицы ресурса
    UTILITY_STOCK = "utility_stock"  # × запас полезности (хранение)
    WIP = "wip"  # × число СЭД в работе и в очереди
    QUEUE = "queue"  # × число СЭД в очереди (цена ожидания)
    SED_COMPLETED = "sed_completed"  # за каждый завершённый СЭД
    SED_STARTED = "sed_started"  # за каждый запущенный СЭД
    UTILITY_PRODUCED = "utility_produced"  # за единицу произведённой полезности
    UTILITY_CONSUMED = "utility_consumed"  # за единицу потреблённой полезности
    EXPRESSION = "expression"  # произвольное выражение по состоянию


class EconomicDirection(str, Enum):
    INCOME = "income"
    EXPENSE = "expense"


#: Базы, допустимые для непрерывной ставки (остальные — событийные).
RATE_BASES = {
    EconomicBasis.CONSTANT,
    EconomicBasis.RESOURCE_CAPACITY,
    EconomicBasis.RESOURCE_BUSY,
    EconomicBasis.UTILITY_STOCK,
    EconomicBasis.WIP,
    EconomicBasis.QUEUE,
    EconomicBasis.EXPRESSION,
}

#: Базы, начисляемые в момент события.
EVENT_BASES = {
    EconomicBasis.SED_COMPLETED,
    EconomicBasis.SED_STARTED,
    EconomicBasis.UTILITY_PRODUCED,
    EconomicBasis.UTILITY_CONSUMED,
}

#: Базы, требующие ссылки на сущность модели.
REF_BASES = {
    EconomicBasis.RESOURCE_CAPACITY: "resource",
    EconomicBasis.RESOURCE_BUSY: "resource",
    EconomicBasis.UTILITY_STOCK: "utility_type",
    EconomicBasis.UTILITY_PRODUCED: "utility_type",
    EconomicBasis.UTILITY_CONSUMED: "utility_type",
    EconomicBasis.SED_COMPLETED: "sed",
    EconomicBasis.SED_STARTED: "sed",
}


class EconomicItem(EntityBase):
    """Статья начисления: что, по какой базе, в какую сторону и сколько."""

    article: str = "прочее"  # статья для раскладки итога
    kind: EconomicKind = EconomicKind.RATE
    basis: EconomicBasis = EconomicBasis.CONSTANT
    direction: EconomicDirection = EconomicDirection.EXPENSE
    ref: str | None = None  # ресурс / полезность / СЭД — по базе
    rate: float = 0.0  # ₽/ч для ставки, ₽ за единицу для событийных
    expression: str | None = None  # для basis=expression
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> "EconomicItem":
        if self.basis == EconomicBasis.EXPRESSION and not self.expression:
            raise ValueError(f"статья {self.id}: basis=expression требует expression")
        if self.basis in REF_BASES and not self.ref:
            raise ValueError(
                f"статья {self.id}: база {self.basis.value} требует ссылки ref на "
                f"{REF_BASES[self.basis]}"
            )
        if self.kind == EconomicKind.RATE and self.basis in EVENT_BASES:
            raise ValueError(
                f"статья {self.id}: база {self.basis.value} событийная — "
                f"укажите kind=one_time"
            )
        if self.kind == EconomicKind.ONE_TIME and self.basis not in EVENT_BASES:
            raise ValueError(
                f"статья {self.id}: разовое начисление требует событийной базы "
                f"({', '.join(sorted(b.value for b in EVENT_BASES))})"
            )
        if self.kind == EconomicKind.TERMINAL and self.basis not in (
            EconomicBasis.UTILITY_STOCK,
            EconomicBasis.WIP,
            EconomicBasis.RESOURCE_CAPACITY,
            EconomicBasis.EXPRESSION,
        ):
            raise ValueError(
                f"статья {self.id}: остаточная стоимость считается по запасу, НЗП, "
                f"ёмкости ресурса либо выражению"
            )
        return self

    def signed_rate(self) -> float:
        """Ставка со знаком: расход отрицателен, доход положителен."""
        return self.rate if self.direction == EconomicDirection.INCOME else -self.rate


class EconomicsSettings(BaseModel):
    """Настройки экономики прогона: валюта, дисконт, разогрев."""

    model_config = ConfigDict(extra="forbid")

    currency: str = "руб"
    discount_rate_annual: float = Field(default=0.0, ge=0.0)
    hours_per_year: float = Field(default=8760.0, gt=0)
    #: период разогрева: статистика и экономика очищаются в этот момент,
    #: чтобы итог измерял стационарный режим, а не переходный
    warmup_hours: float = Field(default=0.0, ge=0.0)
    #: учитывать ли стоимость управляющих действий в экономике прогона
    include_control_costs: bool = True
    control_article: str = "управление"

    def hourly_rate(self) -> float:
        """Непрерывная часовая ставка дисконтирования ρ."""
        return self.discount_rate_annual / self.hours_per_year
