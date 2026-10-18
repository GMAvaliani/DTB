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


def _control_category(loop: str) -> str:
    """Раздел ОДДС для стоимости управления: развитие — инвестиции."""
    return "investing" if loop == "development" else "operating"


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
        # --- отчётность по времени: начисления и деньги по шагам хранения ---
        self.bucket_hours = float(settings.report_bucket_hours or 24.0)
        #: (шаг, статья, доход?, капвложение?) → сумма начислений
        self._accrual_buckets: dict[tuple[int, str, bool, bool], float] = {}
        #: (шаг, статья, поступление?, раздел ОДДС) → сумма денег
        self._cash_buckets: dict[tuple[int, str, bool, str], float] = {}
        #: деньги, срок которых наступает после горизонта (отсрочки платежа):
        #: (статья, поступление?, раздел ОДДС) → сумма
        self._cash_after: dict[tuple[str, bool, str], float] = {}
        #: денежные движения с точным временем: (t0, t1, сумма со знаком)
        self._cash_events: list[tuple[float, float, float]] = []
        # остаток считается часто (мониторинг, KPI), поэтому движения, целиком
        # оставшиеся в прошлом, сворачиваются в одно число
        self._open_cash: list[tuple[float, float, float]] = []
        self._settled_cash = 0.0
        self._settled_until = 0.0
        self._npv_cash = 0.0

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

    def _split(
        self, store: dict, key_rest: tuple, amount: float, t0: float, t1: float
    ) -> None:
        """Разнести сумму по шагам хранения пропорционально времени."""
        step = self.bucket_hours
        if t1 <= t0 + _EPS:
            index = int(max(0.0, t0) // step)
            store[(index, *key_rest)] = store.get((index, *key_rest), 0.0) + amount
            return
        span = t1 - t0
        cursor = t0
        while cursor < t1 - _EPS:
            index = int(max(0.0, cursor) // step)
            boundary = min(t1, (index + 1) * step)
            if boundary <= cursor + _EPS:  # защита от зацикливания на границе
                boundary = min(t1, cursor + step)
            share = amount * (boundary - cursor) / span
            store[(index, *key_rest)] = store.get((index, *key_rest), 0.0) + share
            cursor = boundary

    def _cash(
        self,
        article: str,
        amount: float,
        income: bool,
        category: str,
        t0: float,
        t1: float | None = None,
    ) -> None:
        """Денежное движение: поступление либо выплата в момент или на интервале."""
        if amount == 0.0:
            return
        end = t0 if t1 is None else t1
        key = (article, income, category)
        horizon = self.horizon
        if t0 >= horizon - _EPS and not (end <= t0 + _EPS and t0 <= horizon + _EPS):
            # целиком после горизонта
            self._cash_after[key] = self._cash_after.get(key, 0.0) + amount
        elif end > horizon + _EPS:
            # часть интервала после горизонта: делится пропорционально времени
            inside = amount * (horizon - t0) / (end - t0)
            self._split(self._cash_buckets, key, inside, t0, horizon)
            self._cash_after[key] = self._cash_after.get(key, 0.0) + (amount - inside)
        else:
            self._split(self._cash_buckets, key, amount, t0, end)
        signed = amount if income else -amount
        self._cash_events.append((t0, end, signed))
        self._open_cash.append((t0, end, signed))
        if end > t0 + _EPS:
            factor = self._integral_factor(t0, end) / (end - t0)
        else:
            factor = self.discount_factor(t0)
        self._npv_cash += signed * factor

    def _charge(
        self,
        item_id: str,
        article: str,
        kind: str,
        amount: float,
        discounted: float,
        income: bool,
        t: float,
        t_end: float | None = None,
        capex: bool = False,
        cash_category: str = "operating",
        payment_delay: float = 0.0,
        cash: bool = True,
    ) -> None:
        if amount == 0.0:
            return
        self._entries += 1
        end = t if t_end is None else t_end
        # отчётность по времени ведётся всегда, в том числе в разогреве:
        # отчёт сам выбирает окно, а итог прогона разогрев исключает.
        # Финансовая деятельность (кредиты, взносы собственников) — движение
        # денег, а не доход или расход: в начисления она не попадает
        if cash_category != "financing":
            self._split(self._accrual_buckets, (article, income, capex), amount, t, end)
        if cash:
            self._cash(
                article, amount, income, cash_category,
                t + payment_delay, end + payment_delay,
            )
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
                item.direction == EconomicDirection.INCOME, t0, t_end=t1,
                cash_category=item.cash_category,
                payment_delay=item.payment_delay_hours,
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
                    "rate", amount, discounted, False, start, t_end=stop,
                    cash_category=_control_category(window.loop),
                    capex=window.loop == "development",
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
                cash_category=item.cash_category,
                payment_delay=item.payment_delay_hours,
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
                cash_category=_control_category(loop),
                capex=loop == "development",
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
            # остаточная стоимость — оценка, а не движение денег
            self._charge(
                item.id, item.article, "terminal", amount, amount * factor,
                item.direction == EconomicDirection.INCOME, t, cash=False,
            )

    # ------------------------------------------------- проекты и деньги --

    def post_accrual(
        self,
        item_id: str,
        article: str,
        amount: float,
        t0: float,
        t1: float,
        income: bool = False,
        capex: bool = False,
        kind: str = "project",
    ) -> None:
        """Начисление без движения денег (оплата идёт отдельно, например актами)."""
        if amount == 0.0:
            return
        if t1 > t0 + _EPS:
            discounted = amount * self._integral_factor(t0, t1) / (t1 - t0)
        else:
            discounted = amount * self.discount_factor(t0)
        self._charge(
            item_id, article, kind, amount, discounted, income, t0,
            t_end=t1, capex=capex, cash=False,
        )

    def post_cash(
        self,
        article: str,
        amount: float,
        t: float,
        income: bool = False,
        category: str = "operating",
        t_end: float | None = None,
    ) -> None:
        """Движение денег без начисления: аванс, оплата акта, кредит."""
        self._cash(article, amount, income, category, t, t_end)

    def cash_at(self, t: float) -> float:
        """Остаток денежных средств на момент t (точное время, не шаг хранения)."""
        if t + _EPS < self._settled_until:
            # запрос в прошлое: полный пересчёт по всем движениям
            return float(self.settings.opening_cash) + self._partial_sum(
                self._cash_events, t
            )
        still_open: list[tuple[float, float, float]] = []
        for event in self._open_cash:
            if event[1] <= t + _EPS and event[0] <= t + _EPS:
                self._settled_cash += event[2]
            else:
                still_open.append(event)
        self._open_cash = still_open
        self._settled_until = t
        return (
            float(self.settings.opening_cash)
            + self._settled_cash
            + self._partial_sum(still_open, t)
        )

    @staticmethod
    def _partial_sum(events: list[tuple[float, float, float]], t: float) -> float:
        total = 0.0
        for t0, t1, signed in events:
            if t0 > t + _EPS:
                continue
            if t1 <= t + _EPS or t1 <= t0 + _EPS:
                total += signed
            else:
                total += signed * (t - t0) / (t1 - t0)
        return total

    def net_cash_between(self, t0: float, t1: float) -> float:
        """Чистый денежный поток за интервал [t0, t1]."""
        return self.cash_at(t1) - self.cash_at(t0)

    def finance_report(self, end_time: float) -> dict[str, Any]:
        """Сырьё для отчётности: начисления и деньги по шагам хранения."""
        return {
            "bucket_hours": self.bucket_hours,
            "opening_cash": float(self.settings.opening_cash),
            "currency": self.settings.currency,
            "end_time": round(end_time, 6),
            "accrual": [
                [index, article, income, capex, round(amount, 6)]
                for (index, article, income, capex), amount in sorted(
                    self._accrual_buckets.items()
                )
                if abs(amount) > 1e-9
            ],
            "cash": [
                [index, article, income, category, round(amount, 6)]
                for (index, article, income, category), amount in sorted(
                    self._cash_buckets.items()
                )
                if abs(amount) > 1e-9
            ],
            "cash_after_horizon": [
                [article, income, category, round(amount, 6)]
                for (article, income, category), amount in sorted(self._cash_after.items())
                if abs(amount) > 1e-9
            ],
            "closing_cash": round(self.cash_at(end_time), 6),
            "npv_cash": round(self._npv_cash, 6),
        }

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
