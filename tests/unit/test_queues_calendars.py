"""Фаза 5: дисциплины очередей, режимы прерывания, календари работы."""

import pytest

from dtb.domain import (
    DurationKind,
    DurationModel,
    EffectKind,
    Enterprise,
    EnterpriseModel,
    EventEffect,
    EventKind,
    KPIDefinition,
    Resource,
    ResourceRequirement,
    Scenario,
    SEDDefinition,
    StreamKind,
    StreamSpec,
    TriggerKind,
    TriggerSpec,
    UtilityFlow,
    UtilityType,
)
from dtb.domain.calendars import Calendar, CalendarException, Shift
from dtb.domain.resources import QueueDiscipline
from dtb.domain.seds import InterruptionMode
from dtb.simulation import simulate
from tests.conftest import build_control_model

SCN = Scenario(id="s", name="s", enterprise_id="ctl", seed=11)


# ------------------------------------------------------ дисциплины очередей --


def build_mixed_queue(discipline: QueueDiscipline | None, horizon: float = 60.0):
    """Короткие и длинные работы соперничают за один канал обслуживания."""
    m = EnterpriseModel(
        enterprise=Enterprise(id="q", name="Очередь", planning_horizon_hours=horizon)
    )
    m.utility_types["job"] = UtilityType(id="job", name="Заявка")
    m.resources["srv"] = Resource(
        id="srv", name="Канал", capacity=1, queue_discipline=discipline
    )
    m.event_kinds["arrive"] = EventKind(
        id="arrive", name="Приход",
        stream=StreamSpec(kind=StreamKind.POISSON, rate_per_hour=1.0),
        effects=[
            EventEffect(
                kind=EffectKind.ADD_UTILITY, utility_type_id="job",
                quantity_expression="1",
            )
        ],
    )
    for sid, duration in (("short", 1.0), ("long", 6.0)):
        m.seds[sid] = SEDDefinition(
            id=sid, name=sid,
            inputs=[UtilityFlow(utility_type_id="job", quantity=1)],
            resources=[ResourceRequirement(resource_id="srv", quantity=1)],
            duration=DurationModel(kind=DurationKind.FIXED, value=duration),
            triggers=[TriggerSpec(kind=TriggerKind.ON_EVENT, event_kind_id="arrive")],
        )
    m.kpis["done"] = KPIDefinition(
        id="done", name="Выполнено",
        expression="completed('short') + completed('long')",
    )
    return m


def run_discipline(discipline, horizon: float = 60.0):
    scenario = Scenario(id="s", name="s", enterprise_id="q", seed=5)
    return simulate(build_mixed_queue(discipline, horizon), scenario, seed=5)


def trajectory(result) -> tuple:
    """Сравнимый след прогона: итоги и статистика без привязки к хешу модели."""
    return (
        result.final_kpis,
        {sid: (st["completed"], round(st["waiting_hours"], 6)) for sid, st in result.sed_stats.items()},
        [(e["t"], e["type"], e["name"]) for e in result.event_log],
    )


def test_default_discipline_matches_previous_behaviour():
    """Без указания дисциплины порядок тот же, что и до фазы 5 (приоритет СЭД)."""
    assert trajectory(run_discipline(None)) == trajectory(
        run_discipline(QueueDiscipline.PRIORITY)
    )


def test_spt_maximizes_throughput():
    """Сначала короткие: пропускная способность выше, ожидание коротких меньше."""
    fifo = run_discipline(QueueDiscipline.FIFO)
    spt = run_discipline(QueueDiscipline.SPT)
    assert spt.final_kpis["done"] > fifo.final_kpis["done"]
    assert spt.sed_stats["short"]["mean_waiting"] < fifo.sed_stats["short"]["mean_waiting"]
    # цена: длинные работы ждут дольше
    assert spt.sed_stats["long"]["mean_waiting"] > fifo.sed_stats["long"]["mean_waiting"]


def test_lifo_differs_from_fifo():
    fifo = run_discipline(QueueDiscipline.FIFO)
    lifo = run_discipline(QueueDiscipline.LIFO)
    assert trajectory(lifo) != trajectory(fifo)
    assert lifo.final_kpis["done"] != fifo.final_kpis["done"]


def test_discipline_is_reproducible():
    first, second = run_discipline(QueueDiscipline.SPT), run_discipline(QueueDiscipline.SPT)
    assert first.content_digest() == second.content_digest()


def test_model_level_discipline_applies_to_all_resources():
    model = build_mixed_queue(None)
    model.enterprise.default_queue_discipline = "spt"
    scenario = Scenario(id="s", name="s", enterprise_id="q", seed=5)
    by_model = simulate(model, scenario, seed=5)
    by_resource = run_discipline(QueueDiscipline.SPT)
    assert by_model.final_kpis["done"] == by_resource.final_kpis["done"]


def test_resource_discipline_overrides_model_default():
    model = build_mixed_queue(QueueDiscipline.FIFO)
    model.enterprise.default_queue_discipline = "spt"
    scenario = Scenario(id="s", name="s", enterprise_id="q", seed=5)
    result = simulate(model, scenario, seed=5)
    assert trajectory(result) == trajectory(run_discipline(QueueDiscipline.FIFO))


# -------------------------------------------------------- режимы прерывания --


def build_interrupted(mode: InterruptionMode, horizon: float = 80.0):
    """Смена заканчивается посреди работы: экземпляр прерывается."""
    model = build_control_model(
        horizon=horizon, with_policy=False, with_decision_point=False
    )
    model.seds["work"].duration = DurationModel(kind=DurationKind.FIXED, value=6.0)
    model.seds["work"].interruption_mode = mode
    model.calendars["day"] = Calendar(
        id="day", name="Смена", shifts=[Shift(start_hour=8.0, end_hour=18.0)]
    )
    model.resources["line"].calendar_ref = "day"
    return model


def test_resume_keeps_progress():
    result = simulate(build_interrupted(InterruptionMode.RESUME), SCN, seed=11)
    suspended = [e for e in result.event_log if e["type"] == "sed_suspended"]
    resumed = [e for e in result.event_log if e["type"] == "sed_resumed"]
    assert suspended and resumed
    assert suspended[0]["details"]["mode"] == "resume"
    assert suspended[0]["details"]["lost_progress"] == 0.0
    assert result.metrics["sed_restarts"] == 0


def test_restart_loses_progress():
    result = simulate(build_interrupted(InterruptionMode.RESTART), SCN, seed=11)
    suspended = [e for e in result.event_log if e["type"] == "sed_suspended"]
    assert suspended and suspended[0]["details"]["mode"] == "restart"
    assert suspended[0]["details"]["lost_progress"] > 0
    assert result.metrics["sed_restarts"] == len(suspended)
    # потеря наработки стоит пропускной способности
    resume = simulate(build_interrupted(InterruptionMode.RESUME), SCN, seed=11)
    assert result.final_kpis["done"] < resume.final_kpis["done"]


def test_fail_drops_instance():
    result = simulate(build_interrupted(InterruptionMode.FAIL), SCN, seed=11)
    failed = [e for e in result.event_log if e["type"] == "sed_failed"]
    assert failed and failed[0]["details"]["mode"] == "fail"
    assert result.metrics["sed_failures"] == len(failed)
    assert result.sed_stats["work"]["failed"] == len(failed)
    # отказавший экземпляр не возвращается в очередь
    assert not any(e["type"] == "sed_resumed" for e in result.event_log)


def test_wait_mode_holds_place_in_queue():
    """Режим wait даёт прерванному экземпляру преимущество при возврате ресурса."""
    model = build_interrupted(InterruptionMode.WAIT, horizon=40.0)
    model.resources["line"].queue_discipline = QueueDiscipline.LIFO
    waited = simulate(model, SCN, seed=11)
    resume_model = build_interrupted(InterruptionMode.RESUME, horizon=40.0)
    resume_model.resources["line"].queue_discipline = QueueDiscipline.LIFO
    resumed = simulate(resume_model, SCN, seed=11)
    # при LIFO прерванный экземпляр в режиме resume уступает новым заявкам,
    # а в режиме wait — нет
    first_wait = next(e for e in waited.event_log if e["type"] == "sed_resumed")
    first_resume = next(
        (e for e in resumed.event_log if e["type"] == "sed_resumed"), None
    )
    assert first_wait["details"]["instance"]
    assert first_resume is None or first_wait["t"] <= first_resume["t"]


def test_interruption_mode_default_is_backwards_compatible():
    """По умолчанию режим resume: прежнее поведение сохранено."""
    model = build_control_model(horizon=60.0, with_policy=False, with_decision_point=False)
    assert model.seds["work"].interruption_mode == InterruptionMode.RESUME


# ----------------------------------------------------------------- календари --


def test_calendar_blocks_work_outside_shift():
    model = build_control_model(horizon=100.0, with_policy=False, with_decision_point=False)
    without = simulate(model, SCN, seed=11)
    model.calendars["day"] = Calendar(
        id="day", name="Смена", shifts=[Shift(start_hour=8.0, end_hour=20.0)]
    )
    model.resources["line"].calendar_ref = "day"
    with_calendar = simulate(model, SCN, seed=11)
    assert with_calendar.final_kpis["done"] < without.final_kpis["done"]
    stats = with_calendar.resource_stats["line"]
    assert stats["off_shift_hours"] == pytest.approx(52.0)  # 100 ч − 4 смены по 12 ч
    assert stats["nominal_capacity"] == 1.0


def test_shift_boundaries_are_switching_moments():
    """Начало и конец смены — моменты переключения, замечаемые точно."""
    model = build_control_model(horizon=50.0, with_policy=False, with_decision_point=False)
    model.calendars["day"] = Calendar(
        id="day", name="Смена", shifts=[Shift(start_hour=8.0, end_hour=18.0)]
    )
    model.resources["line"].calendar_ref = "day"
    result = simulate(model, SCN, seed=11)
    changes = [e["t"] for e in result.event_log if e["type"] == "calendar_change"]
    assert changes == [8.0, 18.0, 32.0, 42.0]


def test_weekday_shifts_respect_start_weekday():
    """Будние смены: выходные выпадают там, где их ждёт календарь предприятия."""
    model = build_control_model(horizon=200.0, with_policy=False, with_decision_point=False)
    model.calendars["weekdays"] = Calendar(
        id="weekdays", name="Будни",
        shifts=[Shift(day_of_week=d, start_hour=0.0, end_hour=24.0) for d in range(5)],
    )
    model.resources["line"].calendar_ref = "weekdays"
    monday = simulate(model, SCN, seed=11)
    # горизонт 200 ч от понедельника: нерабочие — суббота и воскресенье (48 ч)
    assert monday.resource_stats["line"]["off_shift_hours"] == pytest.approx(48.0)
    first_change_monday = next(
        e["t"] for e in monday.event_log if e["type"] == "calendar_change"
    )
    assert first_change_monday == pytest.approx(120.0)  # конец пятницы

    model.enterprise.calendar_start_weekday = 5  # прогон начинается в субботу
    saturday = simulate(model, SCN, seed=11)
    # сб + вс в начале (48 ч), сб и часть вс в конце (24 + 8 ч)
    assert saturday.resource_stats["line"]["off_shift_hours"] == pytest.approx(80.0)
    first_change_saturday = next(
        e["t"] for e in saturday.event_log if e["type"] == "calendar_change"
    )
    assert first_change_saturday == pytest.approx(48.0)  # начало понедельника


def test_calendar_exception_stops_work():
    model = build_control_model(horizon=60.0, with_policy=False, with_decision_point=False)
    model.calendars["stop"] = Calendar(
        id="stop", name="Остановка",
        exceptions=[
            CalendarException(
                from_hour=20.0, to_hour=40.0, working=False, label="техобслуживание"
            )
        ],
    )
    model.resources["line"].calendar_ref = "stop"
    result = simulate(model, SCN, seed=11)
    assert result.resource_stats["line"]["off_shift_hours"] == pytest.approx(20.0)
    changes = [e["t"] for e in result.event_log if e["type"] == "calendar_change"]
    assert changes == [20.0, 40.0]


def test_partial_capacity_factor():
    """Смена с долей ёмкости 0.5 уменьшает доступную ёмкость, не обнуляя её."""
    model = build_control_model(horizon=50.0, with_policy=False, with_decision_point=False)
    model.resources["line"].capacity = 2
    model.calendars["half"] = Calendar(
        id="half", name="Половина",
        shifts=[Shift(start_hour=0.0, end_hour=24.0, capacity_factor=0.5)],
    )
    model.resources["line"].calendar_ref = "half"
    result = simulate(model, SCN, seed=11)
    assert result.resource_stats["line"]["final_capacity"] == 1.0
    assert result.resource_stats["line"]["nominal_capacity"] == 2.0
    assert result.resource_stats["line"]["off_shift_hours"] == 0.0


def test_default_calendar_applies_to_all_resources():
    model = build_control_model(horizon=60.0, with_policy=False, with_decision_point=False)
    model.calendars["day"] = Calendar(
        id="day", name="Смена", shifts=[Shift(start_hour=8.0, end_hour=18.0)]
    )
    model.enterprise.default_calendar_ref = "day"
    by_default = simulate(model, SCN, seed=11)
    model.enterprise.default_calendar_ref = None
    model.resources["line"].calendar_ref = "day"
    by_resource = simulate(model, SCN, seed=11)
    assert trajectory(by_default) == trajectory(by_resource)


def test_disabled_calendar_is_ignored():
    model = build_control_model(horizon=60.0, with_policy=False, with_decision_point=False)
    model.calendars["day"] = Calendar(
        id="day", name="Смена", enabled=False,
        shifts=[Shift(start_hour=8.0, end_hour=18.0)],
    )
    model.resources["line"].calendar_ref = "day"
    with_disabled = simulate(model, SCN, seed=11)
    plain = simulate(
        build_control_model(horizon=60.0, with_policy=False, with_decision_point=False),
        SCN, seed=11,
    )
    assert trajectory(with_disabled) == trajectory(plain)


def test_calendar_factor_at_boundaries():
    calendar = Calendar(
        id="c", name="c", shifts=[Shift(start_hour=8.0, end_hour=20.0)]
    )
    assert calendar.factor_at(7.999) == 0.0
    assert calendar.factor_at(8.0) == 1.0
    assert calendar.factor_at(19.999) == 1.0
    assert calendar.factor_at(20.0) == 0.0
    assert calendar.next_change(0.0, 100.0) == 8.0
    assert calendar.next_change(8.0, 100.0) == 20.0
    assert calendar.next_change(20.0, 100.0) == 32.0
    # полночь при дневной смене долю не меняет — такой момент не возвращается
    assert calendar.next_change(95.0, 100.0) is None


def test_shift_validation():
    with pytest.raises(ValueError):
        Shift(start_hour=20.0, end_hour=8.0)  # ночная смена задаётся двумя интервалами
    with pytest.raises(ValueError):
        CalendarException(from_hour=10.0, to_hour=10.0)
