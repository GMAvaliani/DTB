"""Правила мониторинга: регулярное отслеживание показателей в прогоне.

Отчётность по итогам месяца запаздывает: к моменту, когда отчёт готов,
решение уже опоздало. Правило мониторинга проверяется с заданной частотой
(хоть каждый час модельного времени) и поднимает сигнал в момент, когда
условие впервые стало истинным, — а не в конце периода.

Условие пишется на языке выражений модели и может опираться на финансовые и
проектные функции: ``cash()`` — остаток денежных средств, ``cpi('проект')`` и
``spi('проект')`` — индексы освоенного объёма, ``kpi('id')`` — любой KPI.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from dtb.domain.base import EntityBase


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class MonitorRule(EntityBase):
    """Правило: что проверять, как часто и насколько это серьёзно."""

    condition: str  # истинно — тревога
    severity: AlertSeverity = AlertSeverity.WARNING
    check_period_hours: float = Field(default=24.0, gt=0)
    check_start: float = Field(default=0.0, ge=0)
    message: str = ""
    enabled: bool = True
