"""Проекты и пакеты работ — основа метода освоенного объёма (EVM).

Проект — это набор пакетов работ (позиций сметы) с бюджетом, базовым планом и
фактом выполнения. Каждый пакет может быть связан с СЭД, который его
выполняет: тогда освоение и фактические затраты возникают в прогоне сами —
из продолжительности работ, загрузки бригад, погоды, цен. Пакет без СЭД
участвует только в отчёте по фактическому статусу.

Время пакетов — в часах модельного времени от начала проекта (t = 0).

Термины EVM:

* **BAC** (budget at completion) — бюджет пакета: материалы + работы +
  доставка + техника;
* **PV** (planned value) — сколько бюджета должно быть освоено к моменту t по
  базовому плану;
* **EV** (earned value) — сколько бюджета фактически освоено: BAC × доля
  выполненного объёма;
* **AC** (actual cost) — сколько фактически потрачено на выполненное.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from dtb.domain.base import EntityBase


class CostRecognition(str, Enum):
    """Когда стоимость материалов признаётся фактическими затратами (AC)."""

    ON_PROGRESS = "on_progress"  # по мере монтажа: AC сопоставим с EV
    ON_PURCHASE = "on_purchase"  # в момент закупки: как в бухгалтерии заказчика


class Project(EntityBase):
    """Проект: базовый план, условия оплаты, поправки цен."""

    planned_start: float = Field(default=0.0, ge=0)
    planned_finish: float | None = Field(default=None, gt=0)
    #: момент, на который зафиксирован фактический статус (часы от начала)
    status_date: float | None = Field(default=None, ge=0)
    status_label: str = ""  # «Выполнение на 29.09.2026»
    contract_value: float | None = Field(default=None, ge=0)
    #: аванс подрядчику в начале проекта и доля его зачёта в каждом акте
    advance_payment: float = Field(default=0.0, ge=0)
    advance_offset_share: float = Field(default=0.0, ge=0, le=1)
    advance_paid_at: float = Field(default=0.0, ge=0)
    #: период закрытия актов: работы оплачиваются по итогам периода
    billing_period_hours: float = Field(default=720.0, gt=0)
    #: строка отчёта о движении денежных средств: стройка — инвестиционная
    cash_category: str = "investing"
    #: капитальные затраты не входят в отчёт о прибылях и убытках
    capitalize: bool = True
    material_recognition: CostRecognition = CostRecognition.ON_PROGRESS
    #: поправки к ценам сметы: 1,15 — материалы подорожали на 15%
    material_price_factor: float = Field(default=1.0, gt=0)
    labor_rate_factor: float = Field(default=1.0, gt=0)
    #: не начинать пакет раньше планового старта
    start_not_before_planned: bool = False
    #: очерёдность проекта у общих исполнителей: меньше — раньше (при равном
    #: приоритете СЭД пакеты проекта с меньшим числом получают бригаду первыми)
    priority: int = 0
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> "Project":
        if self.planned_finish is not None and self.planned_finish <= self.planned_start:
            raise ValueError(
                f"проект {self.id}: planned_finish должен быть больше planned_start"
            )
        return self


class WorkPackage(EntityBase):
    """Пакет работ — позиция сметы с бюджетом, планом и фактом."""

    project_ref: str
    object_name: str = ""  # объект: «Скотный двор с сыроварней»
    section: str = ""  # раздел: «Нулевой цикл», «Кровля»
    unit: str = ""
    planned_quantity: float = Field(default=0.0, ge=0)
    material_unit_cost: float = Field(default=0.0, ge=0)
    labor_unit_cost: float = Field(default=0.0, ge=0)
    #: итоги по смете; пусто — объём × цена единицы
    material_total: float | None = Field(default=None, ge=0)
    labor_total: float | None = Field(default=None, ge=0)
    delivery_cost: float = Field(default=0.0, ge=0)
    equipment_cost: float = Field(default=0.0, ge=0)
    planned_start: float | None = Field(default=None, ge=0)
    planned_finish: float | None = Field(default=None, ge=0)
    predecessors: list[str] = Field(default_factory=list)
    #: СЭД, который выполняет пакет в прогоне
    sed_ref: str | None = None
    #: плановая трудоёмкость пакета в рабочих часах исполнителя; СЭД задаёт
    #: исполнителя (ресурсы, график) и разброс, пакет — объём. Пусто —
    #: длительность берётся из СЭД целиком
    work_hours: float | None = Field(default=None, ge=0)
    #: работа «по времени» (геодезия, бытовки, проживание): осваивается
    #: пропорционально времени проекта, как принято в EVM для level of effort
    level_of_effort: bool = False
    #: факт на дату статуса проекта
    actual_quantity: float = Field(default=0.0, ge=0)
    actual_material_cost: float = Field(default=0.0, ge=0)
    actual_labor_cost: float = Field(default=0.0, ge=0)
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> "WorkPackage":
        if (
            self.planned_start is not None
            and self.planned_finish is not None
            and self.planned_finish < self.planned_start
        ):
            raise ValueError(
                f"пакет {self.id}: planned_finish раньше planned_start"
            )
        if self.id in self.predecessors:
            raise ValueError(f"пакет {self.id} не может предшествовать сам себе")
        return self

    def material_budget(self) -> float:
        if self.material_total is not None:
            return float(self.material_total)
        return float(self.planned_quantity * self.material_unit_cost)

    def labor_budget(self) -> float:
        if self.labor_total is not None:
            return float(self.labor_total)
        return float(self.planned_quantity * self.labor_unit_cost)

    def bac(self) -> float:
        """Бюджет пакета: материалы + работы + доставка + техника."""
        return (
            self.material_budget()
            + self.labor_budget()
            + self.delivery_cost
            + self.equipment_cost
        )

    def actual_fraction(self) -> float:
        """Доля выполненного объёма на дату статуса (0…1)."""
        if self.planned_quantity > 0:
            return max(0.0, min(1.0, self.actual_quantity / self.planned_quantity))
        return 1.0 if (self.actual_material_cost + self.actual_labor_cost) > 0 else 0.0

    def actual_cost(self) -> float:
        return self.actual_material_cost + self.actual_labor_cost

    def materials_on_site(self) -> float:
        """Материалы, закупленные сверх смонтированного (по ценам сметы)."""
        installed = self.material_budget() * self.actual_fraction()
        return max(0.0, self.actual_material_cost - installed)

    def material_to_purchase(self) -> float:
        """Сколько материалов осталось закупить на остаток работ (по смете)."""
        remaining = self.material_budget() * (1.0 - self.actual_fraction())
        return max(0.0, remaining - self.materials_on_site())
