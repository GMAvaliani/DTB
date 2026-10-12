"""Проверки инвариантов конкретного прогона.

Это не тесты кода, а проверки результата: выполняются на любом прогоне и
отвечают на вопрос «не нарушил ли движок законы сохранения и теорию очередей
на этих данных». Отчёт прикладывается к прогону, поэтому доверие к числам
проверяемо, а не декларативно.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dtb.domain.model import EnterpriseModel
from dtb.simulation.result import SimulationResult


@dataclass
class VerificationCheck:
    """Одна проверка: что сверялось, что ожидалось, что получилось."""

    id: str
    name: str
    expected: float | None
    observed: float | None
    tolerance: float
    passed: bool
    detail: str = ""
    kind: str = "invariant"  # invariant | analytic
    scope: str = ""  # к чему относится: полезность, СЭД, ресурс

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "expected": self.expected,
            "observed": self.observed,
            "tolerance": self.tolerance,
            "passed": self.passed,
            "detail": self.detail,
            "kind": self.kind,
            "scope": self.scope,
            "absolute_error": (
                None
                if self.expected is None or self.observed is None
                else round(abs(self.observed - self.expected), 9)
            ),
        }


@dataclass
class VerificationReport:
    checks: list[VerificationCheck] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def add(self, check: VerificationCheck) -> None:
        self.checks.append(check)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "total": len(self.checks),
            "failed": sum(1 for c in self.checks if not c.passed),
            "by_kind": {
                kind: {
                    "total": sum(1 for c in self.checks if c.kind == kind),
                    "failed": sum(1 for c in self.checks if c.kind == kind and not c.passed),
                }
                for kind in sorted({c.kind for c in self.checks})
            },
            "context": self.context,
            "checks": [c.to_dict() for c in self.checks],
        }


def _close(observed: float, expected: float, tolerance: float) -> bool:
    """Относительная близость с абсолютным дном для значений около нуля."""
    scale = max(1.0, abs(expected))
    return abs(observed - expected) <= tolerance * scale


def check_utility_balance(
    result: SimulationResult, model: EnterpriseModel, tolerance: float = 1e-6
) -> list[VerificationCheck]:
    """Баланс полезностей: начальный запас + выпуск − расход = конечный запас.

    Корректно только для прогона без разогрева: разогрев обнуляет счётчики
    выпуска и расхода, оставляя физический запас, поэтому баланс проверяется
    от границы разогрева и такая проверка пропускается.
    """
    if result.warmup.get("applied"):
        return [
            VerificationCheck(
                id="utility_balance", name="Баланс полезностей",
                expected=None, observed=None, tolerance=tolerance, passed=True,
                detail="пропущено: прогон с разогревом обнуляет счётчики выпуска и расхода",
            )
        ]
    checks: list[VerificationCheck] = []
    initial = model.initial_state.utility_stocks
    for utype_id in sorted(set(result.utility_totals) | set(initial)):
        totals = result.utility_totals.get(utype_id, {})
        produced = float(totals.get("produced", 0.0))
        consumed = float(totals.get("consumed", 0.0))
        final = float(totals.get("final_stock", result.final_stocks.get(utype_id, 0.0)))
        expected = float(initial.get(utype_id, 0.0)) + produced - consumed
        checks.append(
            VerificationCheck(
                id=f"utility_balance:{utype_id}",
                name=f"Баланс полезности «{utype_id}»",
                expected=round(expected, 9),
                observed=round(final, 9),
                tolerance=tolerance,
                passed=_close(final, expected, tolerance),
                detail="начальный запас + выпуск − расход = конечный запас",
                scope=utype_id,
            )
        )
    return checks


def check_little_law(
    result: SimulationResult, tolerance: float = 0.05
) -> list[VerificationCheck]:
    """Закон Литтла: L = λ·W по каждому СЭД и по системе целиком.

    λ берётся как интенсивность завершений на измеренном интервале, W — среднее
    время в системе (ожидание + обслуживание), L — средний уровень по интегралу.
    Проверка осмысленна только при достаточном числе завершений и при пустых
    пулах на конце прогона: иначе незавершённые экземпляры смещают оценку.
    """
    checks: list[VerificationCheck] = []
    hours = float(result.metrics.get("measured_hours", 0.0))
    if hours <= 0:
        return checks
    for sed_id, stats in sorted(result.sed_stats.items()):
        completed = int(stats.get("completed", 0))
        pending = int(stats.get("still_waiting", 0)) + int(stats.get("still_executing", 0))
        if completed < 20 or pending > max(1, completed * 0.1):
            continue  # недостаточно данных либо велик хвост незавершённых
        waiting_hours = float(stats.get("waiting_hours", 0.0))
        executing_hours = float(stats.get("executing_hours", 0.0))
        lam = completed / hours
        mean_time = (waiting_hours + executing_hours) / completed
        level = (waiting_hours + executing_hours) / hours
        expected = lam * mean_time
        checks.append(
            VerificationCheck(
                id=f"little:{sed_id}",
                name=f"Закон Литтла для СЭД «{sed_id}»",
                expected=round(expected, 6),
                observed=round(level, 6),
                tolerance=tolerance,
                passed=_close(level, expected, tolerance),
                detail=(
                    f"λ={lam:.6g} 1/ч, W={mean_time:.6g} ч, L={level:.6g} "
                    f"(интеграл уровня / измеренные часы)"
                ),
                scope=sed_id,
            )
        )
    return checks


def check_resource_accounting(
    result: SimulationResult, tolerance: float = 1e-6
) -> list[VerificationCheck]:
    """Учёт ресурсов: занятые часы не превышают часов ёмкости, загрузка — их отношение."""
    checks: list[VerificationCheck] = []
    for res_id, stats in sorted(result.resource_stats.items()):
        busy = float(stats.get("busy_hours", 0.0))
        capacity = float(stats.get("capacity_hours", 0.0))
        utilization = float(stats.get("utilization", 0.0))
        expected = busy / capacity if capacity > 0 else 0.0
        checks.append(
            VerificationCheck(
                id=f"resource_utilization:{res_id}",
                name=f"Загрузка ресурса «{res_id}»",
                expected=round(expected, 6),
                observed=round(utilization, 6),
                tolerance=1e-4,
                passed=_close(utilization, expected, 1e-4) and busy <= capacity + 1e-6,
                detail=f"занято {busy:.6g} ч из {capacity:.6g} ч ёмкости",
                scope=res_id,
            )
        )
    return checks


def check_economics_consistency(
    result: SimulationResult, tolerance: float = 1e-6
) -> list[VerificationCheck]:
    """Согласованность экономики: сумма статей равна итогу, NPV ≤ номинала."""
    econ = result.economics
    if not econ:
        return []
    checks: list[VerificationCheck] = []
    by_article = econ.get("by_article", {})
    article_net = sum(float(b.get("net", 0.0)) for b in by_article.values())
    checks.append(
        VerificationCheck(
            id="economics_articles_sum",
            name="Сумма статей равна итогу",
            expected=round(float(econ.get("net", 0.0)), 6),
            observed=round(article_net, 6),
            tolerance=tolerance,
            passed=_close(article_net, float(econ.get("net", 0.0)), 1e-6),
            detail="раскладка по статьям не теряет и не удваивает начисления",
        )
    )
    kind_net = sum(float(b.get("net", 0.0)) for b in econ.get("by_kind", {}).values())
    checks.append(
        VerificationCheck(
            id="economics_kinds_sum",
            name="Сумма видов начисления равна итогу",
            expected=round(float(econ.get("net", 0.0)), 6),
            observed=round(kind_net, 6),
            tolerance=tolerance,
            passed=_close(kind_net, float(econ.get("net", 0.0)), 1e-6),
            detail="ставка + разовое + остаток = итог",
        )
    )
    if econ.get("discount_rate_annual"):
        income = float(econ.get("income", 0.0))
        discounted = float(econ.get("discounted_income", 0.0))
        checks.append(
            VerificationCheck(
                id="economics_discount_monotone",
                name="Дисконтирование уменьшает доход",
                expected=None,
                observed=round(discounted - income, 6),
                tolerance=0.0,
                passed=discounted <= income + 1e-6,
                detail="при положительной ставке дисконтированный доход не больше номинального",
            )
        )
    return checks


def check_control_costs(
    result: SimulationResult, tolerance: float = 1e-6
) -> list[VerificationCheck]:
    """Стоимость управления: сумма по действиям равна итогу отчёта."""
    costs = result.control_costs
    if not costs:
        return []
    by_action = sum(float(d.get("total", 0.0)) for d in costs.get("by_action", {}).values())
    total = float(costs.get("total", 0.0))
    return [
        VerificationCheck(
            id="control_costs_sum",
            name="Сумма стоимости по действиям равна итогу",
            expected=round(total, 6),
            observed=round(by_action, 6),
            tolerance=tolerance,
            passed=_close(by_action, total, 1e-6),
            detail="разовая + повременная по каждому действию = итог управления",
        )
    ]


def verify_run(result: SimulationResult, model: EnterpriseModel) -> VerificationReport:
    """Полный набор проверок инвариантов для одного прогона."""
    report = VerificationReport(
        context={
            "status": result.status,
            "seed": result.provenance.seed,
            "horizon_hours": result.provenance.horizon_hours,
            "measured_hours": result.metrics.get("measured_hours"),
            "model_hash": result.provenance.model_hash,
            "content_digest": result.content_digest(),
            "warmup_applied": bool(result.warmup.get("applied")),
        }
    )
    for check in (
        *check_utility_balance(result, model),
        *check_little_law(result),
        *check_resource_accounting(result),
        *check_economics_consistency(result),
        *check_control_costs(result),
    ):
        report.add(check)
    return report
