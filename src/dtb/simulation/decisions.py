"""Моменты принятия решений (МР) и отбор политик.

Модуль не знает про движок: он только расписывает, когда наступает МР, и
какие политики в нём участвуют. Применение действий выполняет движок.

Виды МР:

* ``timer``   — регламент: t = timer_start + k·timer_period;
* ``event``   — наступление события заданного вида с requires_decision;
* ``watchdog``— сторожевое условие: срабатывает по переднему фронту, то есть
  при первом выполнении условия, и не повторяется, пока условие не станет
  ложным (сброс) и снова истинным.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from dtb.domain.control import DecisionPoint, DecisionPointKind, Policy

_EPS = 1e-9


@dataclass
class DecisionTrigger:
    """Сработавшая точка принятия решений."""

    decision_point_id: str
    kind: str
    source: str = ""


@dataclass
class _TimerState:
    next_time: float


@dataclass
class DecisionScheduler:
    decision_points: dict[str, DecisionPoint]
    horizon: float
    _timers: dict[str, _TimerState] = field(default_factory=dict)
    _watchdog_fired: dict[str, bool] = field(default_factory=dict)
    _by_event: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for dp_id, dp in sorted(self.decision_points.items()):
            if not dp.enabled:
                continue
            if dp.kind == DecisionPointKind.TIMER and dp.timer_period:
                self._timers[dp_id] = _TimerState(next_time=dp.timer_start)
            elif dp.kind == DecisionPointKind.EVENT and dp.event_ref:
                self._by_event.setdefault(dp.event_ref, []).append(dp_id)
            elif dp.kind == DecisionPointKind.WATCHDOG:
                self._watchdog_fired[dp_id] = False

    @property
    def active(self) -> bool:
        return bool(self._timers or self._by_event or self._watchdog_fired)

    # ------------------------------------------------------------- регламент --

    def next_timer_time(self, now: float) -> float | None:
        times = [
            st.next_time
            for st in self._timers.values()
            if st.next_time >= now - _EPS and st.next_time <= self.horizon + _EPS
        ]
        return min(times) if times else None

    def timers_due(self, t: float) -> list[DecisionTrigger]:
        """Сработавшие регламентные точки; сдвигает их на следующий период."""
        out: list[DecisionTrigger] = []
        for dp_id, st in sorted(self._timers.items()):
            if st.next_time <= t + _EPS:
                dp = self.decision_points[dp_id]
                period = dp.timer_period or 0.0
                # не зацикливаемся: период гарантированно положителен по валидации
                while st.next_time <= t + _EPS and period > 0:
                    st.next_time += period
                # requires_decision=false: момент переключения создаётся,
                # но моментом принятия решений не становится
                if dp.requires_decision:
                    out.append(DecisionTrigger(dp_id, "timer", source="timer"))
        return out

    # --------------------------------------------------------------- события --

    def event_triggers(self, event_kind_id: str) -> list[DecisionTrigger]:
        out = []
        for dp_id in self._by_event.get(event_kind_id, []):
            dp = self.decision_points[dp_id]
            if dp.requires_decision:
                out.append(DecisionTrigger(dp_id, "event", source=f"event:{event_kind_id}"))
        return out

    # ------------------------------------------------------ сторожевое условие --

    def watchdog_triggers(
        self, evaluate: Callable[[str], bool]
    ) -> list[DecisionTrigger]:
        """Проверить сторожевые условия; сработка только по переднему фронту."""
        out: list[DecisionTrigger] = []
        for dp_id in sorted(self._watchdog_fired):
            dp = self.decision_points[dp_id]
            value = bool(evaluate(dp.watchdog_condition or "false"))
            if value and not self._watchdog_fired[dp_id]:
                self._watchdog_fired[dp_id] = True
                if dp.requires_decision:
                    out.append(DecisionTrigger(dp_id, "watchdog", source="watchdog"))
            elif not value:
                self._watchdog_fired[dp_id] = False  # сброс
        return out


def select_policies(
    policies: dict[str, Policy],
    decision_point: DecisionPoint,
    t: float,
    cooldown_until: dict[str, float],
) -> list[Policy]:
    """Политики, участвующие в данном МР, в порядке приоритета."""
    selected: list[Policy] = []
    for pid, policy in policies.items():
        if not policy.enabled:
            continue
        if decision_point.policy_ref and decision_point.policy_ref != pid:
            continue
        if t + _EPS < policy.active_from:
            continue
        if policy.active_to is not None and t > policy.active_to + _EPS:
            continue
        if t + _EPS < cooldown_until.get(pid, 0.0):
            continue
        selected.append(policy)
    selected.sort(key=lambda p: (p.priority, p.id))
    return selected
