"""Исполнение проектов в прогоне: сетевой запуск пакетов работ и освоение.

Трекер не знает про устройство движка: движок сообщает ему о запуске,
остановке и завершении экземпляров СЭД, связанных с пакетами работ, а трекер
решает, когда пакет можно начать (все предшественники завершены), сколько
работы осталось и сколько освоено и потрачено к любому моменту.

Освоение и затраты хранятся как кусочно-линейные функции времени, поэтому PV,
EV и AC восстанавливаются точно в любой момент прогона — и для онлайн-функций
``cpi('проект')`` / ``spi('проект')``, и для итоговых графиков.

Два режима:

* ``plan`` — проект с нуля по базовому плану (что будет, если начать так);
* ``status`` — продолжение от даты статуса: выполненное на дату статуса
  считается сделанным, затраченное — потраченным, прогон моделирует только
  остаток работ. Время отчётов при этом — время проекта: t + дата статуса.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Callable

from dtb.domain.projects import CostRecognition, Project, WorkPackage

_EPS = 1e-9

#: Часов в единице «работ по времени»: месяц — 730 ч (8760 / 12).
LOE_UNIT_HOURS = {
    "мес": 730.0, "мес.": 730.0, "месяц": 730.0,
    "нед": 168.0, "нед.": 168.0, "неделя": 168.0,
    "дн": 24.0, "дн.": 24.0, "день": 24.0, "сут": 24.0, "сут.": 24.0,
}


def loe_hours_per_unit(unit: str) -> float | None:
    return LOE_UNIT_HOURS.get(unit.strip().lower())


class PiecewiseLinear:
    """Кусочно-линейная функция времени, накапливаемая по событиям."""

    __slots__ = ("times", "values", "slopes")

    def __init__(self, value: float = 0.0, t: float = 0.0) -> None:
        self.times = [t]
        self.values = [value]
        self.slopes = [0.0]

    def at(self, t: float) -> float:
        i = bisect_right(self.times, t + _EPS) - 1
        if i < 0:
            return self.values[0]
        return self.values[i] + self.slopes[i] * max(0.0, t - self.times[i])

    def set(self, t: float, value: float | None = None, slope: float | None = None) -> None:
        """С момента t: значение (по умолчанию текущее) и наклон (по умолчанию прежний)."""
        current = self.at(t)
        new_value = current if value is None else value
        new_slope = self.slopes[-1] if slope is None else slope
        if abs(t - self.times[-1]) <= _EPS:
            self.values[-1] = new_value
            self.slopes[-1] = new_slope
        else:
            self.times.append(t)
            self.values.append(new_value)
            self.slopes.append(new_slope)

    def add(self, t: float, delta: float) -> None:
        self.set(t, value=self.at(t) + delta)


@dataclass
class _WPState:
    wp: WorkPackage
    initial_fraction: float = 0.0
    initial_cost: float = 0.0
    status: str = "pending"  # pending | released | running | suspended | done | static
    progress: PiecewiseLinear = field(default_factory=PiecewiseLinear)
    labor_hours: PiecewiseLinear = field(default_factory=PiecewiseLinear)
    extras: PiecewiseLinear = field(default_factory=PiecewiseLinear)  # материалы при закупке, доставка
    attempt_total: float = 0.0
    planned_work_hours: float = 0.0
    labor_rate: float = 0.0  # ₽ за рабочий час остатка работ
    material_remaining: float = 0.0  # стоимость материалов остатка по цене закупки
    started_at: float | None = None
    finished_at: float | None = None
    attempts: int = 0
    lost_hours: float = 0.0  # часы работы, потерянные при перезапусках
    purchased: bool = False
    billed_cost: float = 0.0
    pace: float = 1.0  # темп исполнителя в текущей попытке (множитель длительности)
    budget: float = 0.0  # BAC пакета (смета не меняется в прогоне)
    done_cost: float | None = None  # затраты завершённого пакета


@dataclass
class _ProjectState:
    project: Project
    offset: float = 0.0  # время проекта = время прогона + offset
    advance_remaining: float = 0.0
    next_billing: float | None = None
    last_billed_at: float = 0.0
    advance_paid: bool = False
    finished_at: float | None = None


class ProjectTracker:
    """Сетевой запуск пакетов работ и учёт освоения в прогоне."""

    def __init__(
        self,
        projects: dict[str, Project],
        work_packages: dict[str, WorkPackage],
        expected_durations: dict[str, float],
        mode: str = "plan",
    ) -> None:
        self.mode = mode
        #: ожидаемая длительность СЭД — масштаб, к которому приводится разброс
        self.expected_durations = dict(expected_durations)
        self._pv_cache: dict[str, list[tuple[float, float]]] = {}
        self._indicator_cache: dict[tuple[str, float], dict[str, float]] = {}
        self.projects: dict[str, _ProjectState] = {}
        self.wps: dict[str, _WPState] = {}
        self._successors: dict[str, list[str]] = {}
        self._releases: list[tuple[float, str]] = []
        self._by_project: dict[str, list[_WPState]] = {}
        for pid, project in sorted(projects.items()):
            if not project.enabled:
                continue
            offset = float(project.status_date or 0.0) if mode == "status" else 0.0
            self.projects[pid] = _ProjectState(project=project, offset=offset)
        for wid, wp in sorted(work_packages.items()):
            if wp.project_ref not in self.projects:
                continue
            state = _WPState(wp=wp, budget=wp.bac())
            if mode == "status":
                state.initial_fraction = wp.actual_fraction()
                state.initial_cost = wp.actual_cost()
            state.progress = PiecewiseLinear(0.0)
            state.labor_hours = PiecewiseLinear(0.0)
            state.extras = PiecewiseLinear(0.0)
            self.wps[wid] = state
            self._by_project.setdefault(wp.project_ref, []).append(state)
            for pred in wp.predecessors:
                self._successors.setdefault(pred, []).append(wid)

    @property
    def active(self) -> bool:
        return bool(self.wps)

    def all_finished(self) -> bool:
        """Все пакеты всех проектов выполнены (работы «по времени» не в счёт)."""
        return bool(self.wps) and all(
            s.status in ("done", "static") for s in self.wps.values()
        )

    # ------------------------------------------------------------ планирование --

    def _project_time(self, wp_state: _WPState, t: float) -> float:
        return t + self.projects[wp_state.wp.project_ref].offset

    def _ready(self, wid: str) -> bool:
        for pred in self.wps[wid].wp.predecessors:
            pred_state = self.wps.get(pred)
            if pred_state is not None and pred_state.status not in ("done", "static"):
                return False
        return True

    def _release_time(self, wid: str, now: float) -> float:
        state = self.wps[wid]
        project = self.projects[state.wp.project_ref]
        if project.project.start_not_before_planned and state.wp.planned_start is not None:
            return max(now, state.wp.planned_start - project.offset)
        return now

    def initialize(self, t: float = 0.0) -> None:
        """Отметить выполненное к статусу и выпустить готовые к старту пакеты."""
        for state in self.wps.values():
            wp = state.wp
            if state.initial_fraction >= 1.0 - _EPS:
                state.status = "done"
                state.finished_at = t
                state.progress.set(t, value=1.0, slope=0.0)
                if not wp.level_of_effort:
                    state.done_cost = self._wp_cost(state, t)
            elif wp.level_of_effort or not wp.sed_ref:
                state.status = "static"
        for wid, state in self.wps.items():
            if state.status == "pending" and self._ready(wid):
                self._schedule_release(wid, t)

    def _schedule_release(self, wid: str, now: float) -> None:
        self.wps[wid].status = "released"
        self._releases.append((self._release_time(wid, now), wid))
        self._releases.sort()

    def next_release_time(self) -> float | None:
        return self._releases[0][0] if self._releases else None

    def pop_releases(self, t: float) -> list[str]:
        """Пакеты, которые пора запускать: движок создаёт для них экземпляры СЭД."""
        out: list[str] = []
        while self._releases and self._releases[0][0] <= t + _EPS:
            out.append(self._releases.pop(0)[1])
        return out

    def sed_of(self, wid: str) -> str | None:
        state = self.wps.get(wid)
        return state.wp.sed_ref if state else None

    def priority_of(self, wid: str) -> int:
        state = self.wps.get(wid)
        if state is None:
            return 0
        return self.projects[state.wp.project_ref].project.priority

    # ------------------------------------------------------- жизненный цикл --

    def scale_duration(self, wid: str, sampled: float, expected: float) -> float:
        """Длительность попытки: объём остатка × случайный множитель СЭД."""
        state = self.wps[wid]
        remaining = max(0.0, 1.0 - state.initial_fraction)
        factor = sampled / expected if expected > _EPS else 1.0
        base = state.wp.work_hours if state.wp.work_hours is not None else expected
        return max(0.0, base * remaining * factor)

    def on_start(
        self, wid: str, t: float, duration: float, resumed: bool,
        material_price_factor: float, labor_rate_factor: float, pace: float = 1.0,
    ) -> None:
        """Запуск или возобновление пакета.

        ``pace`` — темп исполнителя (множитель длительности СЭД): усиленная
        бригада работает быстрее, но человеко-часов на объём тратит столько
        же, поэтому стоимость работ в час растёт как 1 / pace.
        """
        self._indicator_cache.clear()
        state = self.wps[wid]
        wp = state.wp
        project = self.projects[wp.project_ref].project
        if not resumed:
            state.attempts += 1
            state.attempt_total = max(duration, _EPS)
            state.pace = pace if pace > _EPS else 1.0
            state.progress.set(t, value=0.0)
            if state.started_at is None:
                state.started_at = t
                remaining = max(0.0, 1.0 - state.initial_fraction)
                base = (
                    wp.work_hours
                    if wp.work_hours is not None
                    else self.expected_durations.get(wp.sed_ref or "", 0.0)
                )
                state.planned_work_hours = max(base * remaining, _EPS)
                # цены фиксируются на момент старта пакета: подорожание после
                # старта на него уже не влияет
                state.labor_rate = (
                    wp.labor_budget() * remaining * labor_rate_factor
                    / state.planned_work_hours
                )
                # в режиме статуса материалы, уже лежащие на объекте, второй
                # раз не закупаются
                to_buy = (
                    wp.material_to_purchase()
                    if self.mode == "status"
                    else wp.material_budget() * remaining
                )
                state.material_remaining = to_buy * material_price_factor
                if state.initial_fraction <= _EPS:
                    extras = wp.delivery_cost + wp.equipment_cost
                    if extras:
                        state.extras.add(t, extras)
                if (
                    project.material_recognition == CostRecognition.ON_PURCHASE
                    and state.material_remaining
                ):
                    state.extras.add(t, state.material_remaining)
                    state.purchased = True
        state.status = "running"
        state.progress.set(t, slope=1.0 / state.attempt_total)
        state.labor_hours.set(t, slope=1.0 / state.pace)

    def on_stop(self, wid: str, t: float, mode: str) -> bool:
        """Прерывание. Возвращает True, если пакет нужно запустить заново (fail)."""
        self._indicator_cache.clear()
        state = self.wps[wid]
        state.labor_hours.set(t, slope=0.0)
        if mode == "restart":
            done = state.progress.at(t) * state.attempt_total
            state.lost_hours += done
            state.progress.set(t, value=0.0, slope=0.0)
        elif mode == "fail":
            done = state.progress.at(t) * state.attempt_total
            state.lost_hours += done
            state.progress.set(t, value=0.0, slope=0.0)
            state.status = "released"
            return True
        else:
            state.progress.set(t, slope=0.0)
        state.status = "suspended"
        return False

    def on_complete(self, wid: str, t: float) -> list[str]:
        """Завершение пакета; возвращает пакеты, ставшие готовыми к старту."""
        self._indicator_cache.clear()
        state = self.wps[wid]
        state.progress.set(t, value=1.0, slope=0.0)
        state.labor_hours.set(t, slope=0.0)
        state.status = "done"
        state.finished_at = t
        if not state.wp.level_of_effort:
            state.done_cost = self._wp_cost(state, t)
        released: list[str] = []
        for succ in self._successors.get(wid, []):
            succ_state = self.wps.get(succ)
            if succ_state is not None and succ_state.status == "pending" and self._ready(succ):
                self._schedule_release(succ, t)
                released.append(succ)
        project_state = self.projects[state.wp.project_ref]
        if project_state.finished_at is None and all(
            s.status in ("done", "static")
            for s in self.wps.values()
            if s.wp.project_ref == state.wp.project_ref
        ):
            project_state.finished_at = t
        return released

    # ---------------------------------------------------------------- освоение --

    def _loe_rate(self, state: _WPState) -> float:
        """Стоимость часа «работы по времени»: бюджет ÷ плановая продолжительность."""
        wp = state.wp
        per_unit = loe_hours_per_unit(wp.unit)
        if per_unit and wp.planned_quantity > 0:
            return wp.bac() / (wp.planned_quantity * per_unit)
        if wp.planned_start is not None and wp.planned_finish is not None:
            span = wp.planned_finish - wp.planned_start
            if span > _EPS:
                return wp.bac() / span
        return 0.0

    def _loe_active_hours(self, state: _WPState, t: float) -> float:
        """Сколько часов «работа по времени» шла в прогоне к моменту t.

        Она идёт, пока не завершён проект: задержка проекта продлевает
        аренду бытовок, проживание и сопровождение — и это реальные затраты.
        """
        project_state = self.projects[state.wp.project_ref]
        start = 0.0
        if self.mode == "plan" and state.wp.planned_start is not None:
            start = state.wp.planned_start
        end = t if project_state.finished_at is None else min(t, project_state.finished_at)
        return max(0.0, end - start)

    def _progress_fraction(self, state: _WPState, t: float) -> float:
        """Доля выполненного объёма пакета к моменту t прогона."""
        wp = state.wp
        if wp.level_of_effort:
            bac = wp.bac()
            if bac <= _EPS:
                return state.initial_fraction
            added = self._loe_rate(state) * self._loe_active_hours(state, t) / bac
            return min(1.0, state.initial_fraction + added)
        if state.status == "static":
            return state.initial_fraction
        remaining = 1.0 - state.initial_fraction
        return state.initial_fraction + remaining * max(0.0, min(1.0, state.progress.at(t)))

    @staticmethod
    def _pv_fraction(wp: WorkPackage, project_t: float) -> float:
        """Доля бюджета пакета, которая по базовому плану освоена к моменту."""
        if wp.planned_start is None or wp.planned_finish is None:
            return 0.0
        if wp.planned_finish <= wp.planned_start + _EPS:
            return 1.0 if project_t >= wp.planned_start - _EPS else 0.0
        if project_t <= wp.planned_start:
            return 0.0
        if project_t >= wp.planned_finish:
            return 1.0
        return (project_t - wp.planned_start) / (wp.planned_finish - wp.planned_start)

    def _wp_cost(self, state: _WPState, t: float) -> float:
        wp = state.wp
        if wp.level_of_effort:
            # затраты идут всё время работы проекта, освоение — не выше бюджета
            project = self.projects[wp.project_ref].project
            return state.initial_cost + (
                self._loe_rate(state) * self._loe_active_hours(state, t)
                * project.labor_rate_factor
            )
        if state.status == "static":
            return state.initial_cost
        project = self.projects[wp.project_ref].project
        labor = state.labor_rate * state.labor_hours.at(t)
        if project.material_recognition == CostRecognition.ON_PROGRESS:
            remaining = 1.0 - state.initial_fraction
            progress = 0.0
            if remaining > _EPS:
                progress = (self._progress_fraction(state, t) - state.initial_fraction) / remaining
            material = state.material_remaining * max(0.0, min(1.0, progress))
        else:
            material = 0.0  # уже учтено в extras при закупке
        return state.initial_cost + labor + material + state.extras.at(t)

    def indicators(self, pid: str, t: float) -> dict[str, float]:
        """PV, EV, AC и производные показатели проекта в момент t прогона."""
        project_state = self.projects.get(pid)
        if project_state is None:
            return {}
        key = (pid, round(t, 9))
        cached = self._indicator_cache.get(key)
        if cached is not None:
            return cached
        if len(self._indicator_cache) > 64:
            self._indicator_cache.clear()
        project_t = t + project_state.offset
        bac = ev = ac = 0.0
        for state in self._by_project.get(pid, []):
            budget = state.budget
            bac += budget
            if (
                state.done_cost is not None
                and state.finished_at is not None
                and t >= state.finished_at - _EPS
            ):
                # завершённый пакет: освоен целиком, затраты зафиксированы
                ev += budget
                ac += state.done_cost
            elif state.started_at is None and not state.wp.level_of_effort:
                # ещё не начатый пакет: только факт на дату статуса
                ev += budget * state.initial_fraction
                ac += state.initial_cost
            else:
                ev += budget * self._progress_fraction(state, t)
                ac += self._wp_cost(state, t)
        # PV кусочно-линейна между плановыми сроками пакетов: интерполяция по
        # точкам излома кривой точна
        pv = _interpolate(self.pv_curve(pid), project_t)
        finished_t = (
            None
            if project_state.finished_at is None or project_state.finished_at > t + _EPS
            else project_state.finished_at + project_state.offset
        )
        result = evm_indicators(
            bac, pv, ev, ac, project_t, project_state.project,
            self.pv_curve(pid), finished_t=finished_t,
        )
        self._indicator_cache[key] = result
        return result

    def pv_curve(self, pid: str) -> list[tuple[float, float]]:
        """Кривая PV проекта по точкам излома (время проекта, PV)."""
        cached = self._pv_cache.get(pid)
        if cached is not None:
            return cached
        points: set[float] = set()
        for state in self.wps.values():
            wp = state.wp
            if wp.project_ref != pid or wp.planned_start is None or wp.planned_finish is None:
                continue
            points.add(wp.planned_start)
            points.add(wp.planned_finish)
        curve = []
        for point in sorted(points):
            value = sum(
                s.wp.bac() * self._pv_fraction(s.wp, point)
                for s in self.wps.values()
                if s.wp.project_ref == pid
            )
            curve.append((point, value))
        self._pv_cache[pid] = curve
        return curve

    # -------------------------------------------------------------- деньги --

    def next_billing_time(self) -> float | None:
        """Ближайший денежный момент проекта: выплата аванса либо закрытие актов."""
        times: list[float] = []
        for project_state in self.projects.values():
            project = project_state.project
            if not project_state.advance_paid and project.advance_payment:
                times.append(project.advance_paid_at - project_state.offset)
            if project_state.next_billing is not None:
                times.append(project_state.next_billing)
        return min(times) if times else None

    def start_finance(self, t: float) -> None:
        """Состояние аванса и расписание актов на старте прогона."""
        for pid, project_state in self.projects.items():
            project = project_state.project
            project_state.last_billed_at = t
            if self.mode == "status":
                # аванс выплачен до даты статуса; к статусу зачтена доля факта
                spent = sum(
                    s.initial_cost for s in self.wps.values() if s.wp.project_ref == pid
                )
                project_state.advance_remaining = max(
                    0.0, project.advance_payment - spent * project.advance_offset_share
                )
                for s in self.wps.values():
                    if s.wp.project_ref == pid:
                        s.billed_cost = s.initial_cost
                project_state.advance_paid = True
            else:
                project_state.advance_remaining = project.advance_payment
                project_state.advance_paid = not project.advance_payment
            project_state.next_billing = t + project.billing_period_hours

    def bill(
        self, t: float, post_cash: Callable[..., None], post_accrual: Callable[..., None]
    ) -> None:
        """Выплатить аванс и закрыть акты, срок которых наступил."""
        for pid, project_state in self.projects.items():
            project = project_state.project
            if (
                not project_state.advance_paid
                and project.advance_payment
                and project.advance_paid_at - project_state.offset <= t + _EPS
            ):
                post_cash(pid, "аванс подрядчику", project.advance_payment, t)
                project_state.advance_paid = True
            if project_state.next_billing is not None and project_state.next_billing <= t + _EPS:
                self._close_period(pid, project_state, t, post_cash, post_accrual)
                project_state.next_billing = t + project.billing_period_hours

    def _close_period(self, pid, project_state, t, post_cash, post_accrual) -> None:
        project = project_state.project
        period_cost = 0.0
        for state in self.wps.values():
            if state.wp.project_ref != pid:
                continue
            cost = self._wp_cost(state, t)
            delta = cost - state.billed_cost
            if delta > _EPS:
                post_accrual(pid, state.wp, delta, project_state.last_billed_at, t)
                period_cost += delta
                state.billed_cost = cost
        if period_cost > _EPS:
            offset = min(project_state.advance_remaining, period_cost * project.advance_offset_share)
            project_state.advance_remaining -= offset
            payable = period_cost - offset
            post_cash(pid, "оплата актов подрядчику", payable, t)
        project_state.last_billed_at = t

    def close(self, t: float, post_cash: Callable[..., None], post_accrual: Callable[..., None]) -> None:
        """Финальный акт на конце прогона: всё начисленное попадает в отчётность."""
        for pid, project_state in self.projects.items():
            self._close_period(pid, project_state, t, post_cash, post_accrual)

    # ---------------------------------------------------------------- итог --

    def report(self, end_time: float, grid_points: int = 80) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for pid, project_state in self.projects.items():
            project = project_state.project
            grid = [end_time * i / grid_points for i in range(grid_points + 1)]
            series: dict[str, list[float]] = {"t": [], "pv": [], "ev": [], "ac": []}
            for t in grid:
                ind = self.indicators(pid, t)
                series["t"].append(round(t + project_state.offset, 4))
                series["pv"].append(round(ind["pv"], 2))
                series["ev"].append(round(ind["ev"], 2))
                series["ac"].append(round(ind["ac"], 2))
            final = self.indicators(pid, end_time)
            packages = {}
            for wid, state in self.wps.items():
                if state.wp.project_ref != pid:
                    continue
                packages[wid] = {
                    "name": state.wp.name,
                    "object": state.wp.object_name,
                    "section": state.wp.section,
                    "status": state.status,
                    "bac": round(state.wp.bac(), 2),
                    "ev": round(state.wp.bac() * self._progress_fraction(state, end_time), 2),
                    "ac": round(self._wp_cost(state, end_time), 2),
                    "progress": round(self._progress_fraction(state, end_time), 4),
                    "planned_start": state.wp.planned_start,
                    "planned_finish": state.wp.planned_finish,
                    "start": None if state.started_at is None else round(state.started_at + project_state.offset, 4),
                    "finish": None if state.finished_at is None else round(state.finished_at + project_state.offset, 4),
                    "attempts": state.attempts,
                    "lost_hours": round(state.lost_hours, 4),
                }
            out[pid] = {
                "name": project.name,
                "mode": self.mode,
                "time_offset": project_state.offset,
                "planned_start": project.planned_start,
                "planned_finish": project.planned_finish,
                "finished_at": None
                if project_state.finished_at is None
                else round(project_state.finished_at + project_state.offset, 4),
                "series": series,
                "final": final,
                "advance_remaining": round(project_state.advance_remaining, 2),
                "work_packages": packages,
            }
        return out


def _interpolate(curve: list[tuple[float, float]], t: float) -> float:
    """Значение кусочно-линейной кривой (точки излома) в момент t."""
    if not curve or t <= curve[0][0]:
        return 0.0 if not curve or t < curve[0][0] else curve[0][1]
    if t >= curve[-1][0]:
        return curve[-1][1]
    i = bisect_right([point[0] for point in curve], t) - 1
    t0, v0 = curve[i]
    t1, v1 = curve[i + 1]
    if t1 - t0 <= _EPS:
        return v1
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def earned_schedule(pv_curve: list[tuple[float, float]], ev: float, planned_start: float) -> float:
    """Время, к которому по плану должен был быть освоен объём EV."""
    if not pv_curve or ev <= 0:
        return planned_start
    previous_t, previous_v = planned_start, 0.0
    for t, value in pv_curve:
        if value + _EPS >= ev:
            if value - previous_v <= _EPS:
                return t
            return previous_t + (t - previous_t) * (ev - previous_v) / (value - previous_v)
        previous_t, previous_v = t, value
    return pv_curve[-1][0]


def evm_indicators(
    bac: float,
    pv: float,
    ev: float,
    ac: float,
    project_t: float,
    project: Project,
    pv_curve: list[tuple[float, float]] | None = None,
    finished_t: float | None = None,
) -> dict[str, float]:
    """Показатели освоенного объёма по четвёрке BAC, PV, EV, AC.

    Формулы — по стандарту PMI (PMBOK) и практике Earned Schedule:

    * SV = EV − PV, CV = EV − AC, SPI = EV / PV, CPI = EV / AC;
    * EAC (типичный) = BAC / CPI — отклонения продолжатся;
    * EAC (нетипичный) = AC + (BAC − EV) — отклонения разовые;
    * EAC (с учётом сроков) = AC + (BAC − EV) / (CPI × SPI);
    * ETC = EAC − AC, VAC = BAC − EAC;
    * TCPI = (BAC − EV) / (BAC − AC) — какую эффективность нужно показать на
      остатке, чтобы уложиться в бюджет;
    * ES — время, к которому по плану должен был быть освоен EV;
      SPI(t) = ES / AT, SV(t) = ES − AT; прогноз длительности = PD / SPI(t).
      После завершения проекта (``finished_t``) фактическое время AT
      останавливается на моменте завершения: отставание больше не растёт.
    """
    spi = ev / pv if pv > _EPS else (1.0 if ev <= _EPS else 0.0)
    cpi = ev / ac if ac > _EPS else (1.0 if ev <= _EPS else 0.0)
    eac_typical = bac / cpi if cpi > _EPS else float("inf")
    eac_atypical = ac + (bac - ev)
    eac_combined = (
        ac + (bac - ev) / (cpi * spi) if cpi > _EPS and spi > _EPS else float("inf")
    )
    tcpi = (bac - ev) / (bac - ac) if abs(bac - ac) > _EPS else float("inf")
    planned_start = project.planned_start
    actual_t = project_t if finished_t is None else min(project_t, finished_t)
    at = max(0.0, actual_t - planned_start)
    es_time = earned_schedule(pv_curve or [], ev, planned_start) if pv_curve else planned_start
    es = max(0.0, es_time - planned_start)
    spi_t = es / at if at > _EPS else 1.0
    planned_duration = (
        (project.planned_finish - planned_start) if project.planned_finish else None
    )
    forecast_duration = (
        planned_duration / spi_t if planned_duration is not None and spi_t > _EPS else None
    )

    def r(value: float | None, digits: int = 6) -> float | None:
        if value is None or value != value or value in (float("inf"), float("-inf")):
            return None
        return round(value, digits)

    return {
        "t": r(project_t, 4),
        "bac": r(bac, 2),
        "pv": r(pv, 2),
        "ev": r(ev, 2),
        "ac": r(ac, 2),
        "sv": r(ev - pv, 2),
        "cv": r(ev - ac, 2),
        "spi": r(spi, 4),
        "cpi": r(cpi, 4),
        "eac": r(eac_typical, 2),
        "eac_atypical": r(eac_atypical, 2),
        "eac_cpi_spi": r(eac_combined, 2),
        "etc": r(eac_typical - ac, 2),
        "vac": r(bac - eac_typical, 2),
        "tcpi": r(tcpi, 4),
        "percent_complete": r(ev / bac if bac > _EPS else 0.0, 4),
        "percent_spent": r(ac / bac if bac > _EPS else 0.0, 4),
        "es": r(es, 4),
        "at": r(at, 4),
        "spi_t": r(spi_t, 4),
        "sv_t": r(es - at, 4),
        "planned_duration": r(planned_duration, 4),
        "forecast_duration": r(forecast_duration, 4),
        "forecast_finish": r(
            planned_start + forecast_duration if forecast_duration is not None else None, 4
        ),
    }
