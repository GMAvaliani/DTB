"""Связь между элементами деятельности и разрывы предпосылок контура — ЗАГОТОВКИ.

ВНИМАНИЕ. Этот модуль намеренно содержит только описание сущностей и места
для данных. Поведение НЕ РЕАЛИЗОВАНО и реализовываться здесь не должно:
это предмет отдельного исследования (связь как объект управления, обратный
поток F←, реестр передач результатов, типология разрывов, мера тяжести,
критический момент t* и горизонт реакции h).

Статус каждого элемента помечен NOT IMPLEMENTED. Движок симуляции эти
сущности читает только для того, чтобы они переживали загрузку/выгрузку
модели и попадали в снимки версий; на ход расчёта они не влияют.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field

from dtb.domain.base import EntityBase


class LinkFlowKind(str, Enum):
    """Направление потока связи (ст. 3.2 источника)."""

    FORWARD = "forward"  # F→ вещественно-результатный поток
    BACKWARD = "backward"  # F← информационно-регуляторный поток


class Link(EntityBase):
    """Связь между элементами деятельности как объект с состоянием.

    NOT IMPLEMENTED: ни критерий исправности, ни запаздывание, ни ответственный
    субъект не используются движком. Поля заведены, чтобы модель могла их
    хранить, а последующая работа — наполнить смыслом.
    """

    source_sed_id: str
    target_sed_id: str
    utility_type_id: str | None = None  # что передаётся по прямому потоку
    health_criterion: str | None = None  # NOT IMPLEMENTED: K_c, выражение
    delay: float | None = Field(default=None, ge=0)  # NOT IMPLEMENTED: τ_c, часы
    reaction_horizon: float | None = Field(default=None, ge=0)  # NOT IMPLEMENTED: h
    critical_moment: float | None = Field(default=None, ge=0)  # NOT IMPLEMENTED: t*
    responsible_subject_ref: str | None = None  # NOT IMPLEMENTED: σ
    implemented: bool = False  # признак того, что логика связи не задействована


class Feedback(EntityBase):
    """Обратный поток F←: подтверждение, приёмка или рекламация.

    NOT IMPLEMENTED: записи не порождаются движком и не читаются политиками.
    """

    id: str = "-"
    link_id: str | None = None
    from_sed_id: str
    to_sed_id: str
    result_ref: str | None = None  # на какую передачу отвечает
    kind: str = "acknowledgement"  # acknowledgement | acceptance | claim
    timestamp: float | None = None  # модельное время
    payload: dict[str, Any] = Field(default_factory=dict)


class TransferRegistry(EntityBase):
    """Реестр передач результатов между элементами деятельности.

    NOT IMPLEMENTED: движок ведёт собственный журнал производства/потребления
    полезностей, но не реестр передач с параметрами и подтверждениями.
    """

    id: str = "-"
    link_id: str | None = None
    source_sed_id: str
    target_sed_id: str
    utility_type_id: str | None = None
    quantity: float | None = None
    sent_at: float | None = None
    observed_at: float | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    acknowledged: bool = False


class GapType(EntityBase):
    """Справочник типов разрывов предпосылок контура управления.

    NOT IMPLEMENTED: типология (разрыв цели, критерия, ресурсной связи,
    технологии, информационной связи) заведена как справочник без логики
    локализации.
    """

    broken_object: str = ""  # goal | criterion | forward_flow | technology | backward_flow
    localization_check: str | None = None  # NOT IMPLEMENTED: проверка при локализации
    decision_class: str = ""  # класс управленческих решений
    closure_mode: str = ""  # regulatory | generative


class Gap(EntityBase):
    """Обнаруженный разрыв.

    NOT IMPLEMENTED: не обнаруживается и не оценивается. severity оставлена
    как место под количественную меру тяжести.
    """

    id: str = "-"
    gap_type_id: str | None = None
    link_id: str | None = None
    sed_id: str | None = None
    detected_at: float | None = None
    severity: float | None = None  # NOT IMPLEMENTED: мера тяжести разрыва
    critical_moment: float | None = None  # NOT IMPLEMENTED: t*
    reaction_horizon: float | None = None  # NOT IMPLEMENTED: h
    evidence: dict[str, Any] = Field(default_factory=dict)


#: Коллекции-заготовки: читаются и сохраняются, но не влияют на расчёт.
NOT_IMPLEMENTED_COLLECTIONS = ("link", "gap_type")
