"""Начисление экономики прогона: ставки, разовые суммы, остаточная стоимость.

Модуль не знает про движок: движок сообщает ему интервалы времени и события,
модуль считает суммы. Дисконтирование непрерывное; для ставки на интервале
интеграл берётся точно:

    ∫_{t₀}^{t₁} e^{-ρt} dt = (e^{-ρt₀} − e^{-ρt₁}) / ρ,  ρ > 0
                           = t₁ − t₀,                     ρ = 0

Разогрев: начисления до ``warmup_hours`` собираются отдельно и в итог не
входят — отчёт показывает их отдельной строкой, чтобы переходный режим был
виден, но не искажал оценку стационарного.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from dtb.domain.economics import (
    EVENT_BASES,
    EconomicBasis,
    EconomicDirection,
    EconomicItem,
    EconomicKind,
    EconomicsSettings,
)

_EPS = 1e-9


@dataclass
class _Bucket:
    """Накопитель по статье: номинал и дисконтированная сумма."""

    income: float = 0.0
    expense: float = 0.0
    discounted_income: float = 0.0
    discounted_expense: float = 0.0

    def add(self, amount: float, discounted: float, income: bool) -> None:
        if income:
            self.income += amount
            self.discounted_income += discounted
        else:
            self.expense += amount
            self.discounted_expense += discounted

    def to_dict(self) -> dict[str, float]:
        return {
            "income": round(self.income, 6),
            "expense": round(self.expense, 6),
            "net": round(self.income - self.expense, 6),
            "discounted_income": round(self.discounted_income, 6),
            "discounted_expense": round(self.discounted_expense, 6),
            "discounted_net": round(self.discounted_income - self.discounted_expense, 6),
        }


@dataclass
class ControlCostWindow:
    """Интервал, на котором действует повременная стоимость действия."""

    action_id: str
    loop: str
    rate: float
    start: float
    end: float | None = None


class EconomicsLedger:
    """Книга начислений прогона."""

    def __init__(
        self,
        items: dict[str, EconomicItem],
        settings: EconomicsSettings,
        horizon: float,
    ):
        self.settings = settings
        self.horizon = horizon
        self.rho = settings.hourly_rate()
        self.warmup = settings.warmup_hours
        self._rate_items = [
            item
            for _, item in sorted(items.items())
            if item.enabled and item.kind == EconomicKind.RATE
        ]
        self._event_items: dict[tuple[str, str], list[EconomicItem]] = {}
        for _, item in sorted(items.items()):
            if not item.enabled or item.kind != EconomicKind.ONE_TIME:
                continue
            if item.basis in EVENT_BASES:
                self._event_items.setdefault((item.basis.value, item.ref or ""), []).append(item)
        self._terminal_items = [
            item
            for _, item in sorted(items.items())
            if item.enabled and item.kind == EconomicKind.TERMINAL
        ]
        self._by_article: dict[str, _Bucket] = {}
        self._by_kind: dict[str, _Bucket] = {}
        self._by_item: dict[str, _Bucket] = {}
        self._warmup_bucket = _Bucket()
        self.control_windows: list[ControlCostWindow] = []
        self._control_one_time: list[dict[str, Any]] = []
        self._control_recurring: dict[str, float] = {}
        self._entries = 0

    # -------------------------------------------------------------- признаки --

    @property
    def active(self) -> bool:
        return bool(
            self._rate_items
            or self._event_items
            or self._terminal_items
            or self.settings.include_control_costs
        )

    def discount_factor(self, t: float) -> float:
        return math.exp(-self.rho * t) if self.rho else 1.0

    def _integral_factor(self, t0: float, t1: float) -> float:
        """Интеграл дисконтного множителя по интервалу, в часах."""
        if t1 <= t0:
            return 0.0
        if not self.rho:
            return t1 - t0
        return (math.exp(-self.rho * t0) - math.exp(-self.rho * t1)) / self.rho

    # -------------------------------------------------------------- начисления --

    def _charge(
        self,
        item_id: str,
        article: str,
        kind: str,
        amount: float,
        discounted: float,
        income: bool,
        t: float,
    ) -> None:
        if amount == 0.0:
            return
        self._entries += 1
        if t + _EPS < self.warmup:
            self._warmup_bucket.add(amount, discounted, income)
            return
        self._by_article.setdefault(article, _Bucket()).add(amount, discounted, income)
        self._by_kind.setdefault(kind, _Bucket()).add(amount, discounted, income)
        self._by_item.setdefault(item_id, _Bucket()).add(amount, discounted, income)

    def accrue_interval(
        self, t0: float, t1: float, basis_value: Callable[[EconomicItem], float]
    ) -> None:
        """Начислить ставки на интервале [t0, t1), состояние на нём постоянно."""
        if t1 <= t0 + _EPS:
            return
        hours = t1 - t0
        factor = self._integral_factor(t0, t1)
        for item in self._rate_items:
            value = basis_value(item)
            if value == 0.0:
                continue
            amount = abs(item.rate) * value * hours
            discounted = abs(item.rate) * value * factor
            self._charge(
                item.id, item.article, "rate", amount, discounted,
                item.direction == EconomicDirection.INCOME, t0,
            )
        # повременная стоимость управляющих действий
        if self.settings.include_control_costs:
            for window in self.control_windows:
                start = max(window.start, t0)
                stop = min(window.end if window.end is not None else t1, t1)
                if stop <= start + _EPS:
                    continue
                amount = window.rate * (stop - start)
                discounted = window.rate * self._integral_factor(start, stop)
                self._control_recurring[window.action_id] = (
                    self._control_recurring.get(window.action_id, 0.0) + amount
                )
                self._charge(
                    f"control:{window.action_id}", self.settings.control_article,
                    "rate", amount, discounted, False, start,
                )

    def on_event(self, basis: EconomicBasis, ref: str, quantity: float, t: float) -> None:
        """Разовое начисление по событию (завершение СЭД, выпуск полезности)."""
        if quantity == 0.0:
            return
        factor = self.discount_factor(t)
        for item in self._event_items.get((basis.value, ref), []):
            amount = abs(item.rate) * quantity
            self._charge(
                item.id, item.article, "one_time", amount, amount * factor,
                item.direction == EconomicDirection.INCOME, t,
            )

    def on_control_action(self, action_id: str, loop: str, amount: float, t: float) -> None:
        """Разовая стоимость управляющего действия (включая неудачную попытку)."""
        if not amount:
            return
        self._control_one_time.append(
            {"t": round(t, 6), "action": action_id, "loop": loop, "amount": amount}
        )
        if self.settings.include_control_costs:
            self._charge(
                f"control:{action_id}", self.settings.control_article, "one_time",
                amount, amount * self.discount_factor(t), False, t,
            )

    def open_control_window(
        self, action_id: str, loop: str, rate: float, start: float, end: float | None
    ) -> None:
        self.control_windows.append(ControlCostWindow(action_id, loop, rate, start, end))

    def accrue_terminal(self, t: float, basis_value: Callable[[EconomicItem], float]) -> None:
        """Остаточная стоимость на конце горизонта."""
        factor = self.discount_factor(t)
        for item in self._terminal_items:
            value = basis_value(item)
            if value == 0.0:
                continue
            amount = abs(item.rate) * value
            self._charge(
                item.id, item.article, "terminal", amount, amount * factor,
                item.direction == EconomicDirection.INCOME, t,
            )

    # ------------------------------------------------------------------ отчёт --

    def report(self, end_time: float) -> dict[str, Any]:
        income = sum(b.income for b in self._by_article.values())
        expense = sum(b.expense for b in self._by_article.values())
        d_income = sum(b.discounted_income for b in self._by_article.values())
        d_expense = sum(b.discounted_expense for b in self._by_article.values())
        control_total = sum(e["amount"] for e in self._control_one_time) + sum(
            self._control_recurring.values()
        )
        return {
            "currency": self.settings.currency,
            "discount_rate_annual": self.settings.discount_rate_annual,
            "discount_rate_hourly": round(self.rho, 12),
            "warmup_hours": self.warmup,
            "measured_from": self.warmup,
            "measured_to": round(end_time, 6),
            "income": round(income, 6),
            "expense": round(expense, 6),
            "net": round(income - expense, 6),
            "npv": round(d_income - d_expense, 6),
            "discounted_income": round(d_income, 6),
            "discounted_expense": round(d_expense, 6),
            "by_article": {k: v.to_dict() for k, v in sorted(self._by_article.items())},
            "by_kind": {k: v.to_dict() for k, v in sorted(self._by_kind.items())},
            "by_item": {k: v.to_dict() for k, v in sorted(self._by_item.items())},
            "warmup_excluded": self._warmup_bucket.to_dict(),
            "control_costs_included": self.settings.include_control_costs,
            "control_total": round(control_total, 6),
            "entries": self._entries,
        }

    def control_cost_report(self, end_time: float) -> dict[str, Any]:
        """Стоимость управления отдельно: разовая за попытки + повременная."""
        by_action: dict[str, dict[str, Any]] = {}
        by_loop: dict[str, float] = {}
        one_time_total = 0.0
        for entry in self._control_one_time:
            data = by_action.setdefault(
                entry["action"],
                {"one_time": 0.0, "recurring": 0.0, "total": 0.0, "attempts": 0,
                 "loop": entry["loop"]},
            )
            data["one_time"] += entry["amount"]
            data["attempts"] += 1
            one_time_total += entry["amount"]
            by_loop[entry["loop"]] = by_loop.get(entry["loop"], 0.0) + entry["amount"]
        recurring_total = 0.0
        for window in self.control_windows:
            stop = min(window.end if window.end is not None else end_time, end_time)
            amount = max(0.0, stop - window.start) * window.rate
            if not amount:
                continue
            data = by_action.setdefault(
                window.action_id,
                {"one_time": 0.0, "recurring": 0.0, "total": 0.0, "attempts": 0,
                 "loop": window.loop},
            )
            data["recurring"] += amount
            recurring_total += amount
            by_loop[window.loop] = by_loop.get(window.loop, 0.0) + amount
        for data in by_action.values():
            data["one_time"] = round(data["one_time"], 6)
            data["recurring"] = round(data["recurring"], 6)
            data["total"] = round(data["one_time"] + data["recurring"], 6)
        return {
            "one_time": round(one_time_total, 6),
            "recurring": round(recurring_total, 6),
            "total": round(one_time_total + recurring_total, 6),
            "by_action": by_action,
            "by_loop": {k: round(v, 6) for k, v in sorted(by_loop.items())},
        }
