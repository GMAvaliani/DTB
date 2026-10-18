"""Регулярная проверка правил мониторинга в прогоне.

Каждое правило проверяется по своему расписанию (``check_period_hours``).
Сигнал поднимается по переднему фронту — в первой проверке, где условие стало
истинным, — и снимается, когда условие снова ложно. Так журнал сигналов
показывает, **когда** проблема возникла и сколько длилась, а не повторяет её
на каждой проверке.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from dtb.domain.monitoring import MonitorRule

_EPS = 1e-9


@dataclass
class _RuleState:
    rule: MonitorRule
    next_check: float
    active: bool = False
    raised_at: float | None = None
    checks: int = 0
    fired: int = 0
    active_hours: float = 0.0
    errors: int = 0


@dataclass
class MonitorScheduler:
    rules: dict[str, MonitorRule]
    horizon: float
    _state: dict[str, _RuleState] = field(default_factory=dict)
    alerts: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        for rid, rule in sorted(self.rules.items()):
            if rule.enabled:
                self._state[rid] = _RuleState(rule=rule, next_check=rule.check_start)

    @property
    def active(self) -> bool:
        return bool(self._state)

    def next_time(self) -> float | None:
        times = [
            s.next_check for s in self._state.values() if s.next_check <= self.horizon + _EPS
        ]
        return min(times) if times else None

    def check(self, t: float, evaluate: Callable[[str], bool]) -> list[dict[str, Any]]:
        """Проверить правила, срок которых наступил; вернуть новые сигналы."""
        raised: list[dict[str, Any]] = []
        for rid, st in self._state.items():
            if st.next_check > t + _EPS:
                continue
            while st.next_check <= t + _EPS:
                st.next_check += st.rule.check_period_hours
            st.checks += 1
            try:
                value = bool(evaluate(st.rule.condition))
            except Exception:  # noqa: BLE001 — ошибка условия не роняет прогон
                st.errors += 1
                value = False
            if value and not st.active:
                st.active = True
                st.raised_at = t
                st.fired += 1
                alert = {
                    "t": round(t, 6),
                    "rule": rid,
                    "name": st.rule.name or rid,
                    "severity": st.rule.severity.value,
                    "message": st.rule.message or st.rule.condition,
                    "condition": st.rule.condition,
                    "state": "raised",
                }
                self.alerts.append(alert)
                raised.append(alert)
            elif not value and st.active:
                st.active = False
                st.active_hours += t - (st.raised_at or t)
                self.alerts.append(
                    {
                        "t": round(t, 6),
                        "rule": rid,
                        "name": st.rule.name or rid,
                        "severity": st.rule.severity.value,
                        "message": "показатель вернулся в норму",
                        "condition": st.rule.condition,
                        "state": "cleared",
                        "duration_hours": round(t - (st.raised_at or t), 6),
                    }
                )
                st.raised_at = None
        return raised

    def report(self, end_time: float) -> dict[str, Any]:
        rules = {}
        for rid, st in self._state.items():
            active_hours = st.active_hours
            if st.active and st.raised_at is not None:
                active_hours += end_time - st.raised_at
            rules[rid] = {
                "name": st.rule.name or rid,
                "severity": st.rule.severity.value,
                "checks": st.checks,
                "fired": st.fired,
                "active_at_end": st.active,
                "active_hours": round(active_hours, 6),
                "errors": st.errors,
                "check_period_hours": st.rule.check_period_hours,
            }
        return {
            "alerts": self.alerts,
            "rules": rules,
            "raised": sum(1 for a in self.alerts if a["state"] == "raised"),
            "active_at_end": sorted(rid for rid, st in self._state.items() if st.active),
        }
