"""Фаза 1.1: разделение моментов переключения (МП) и принятия решений (МР)."""

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
    Scenario,
    SEDDefinition,
    StreamKind,
    StreamSpec,
    TriggerKind,
    TriggerSpec,
    UtilityFlow,
    UtilityType,
)
from dtb.domain.control import (
    ControlActionDef,
    DecisionPoint,
    DecisionPointKind,
    Policy,
)
from dtb.simulation import simulate
from tests.conftest import build_control_model

SCN = Scenario(id="s", name="s", enterprise_id="ctl", seed=11)


def test_timer_decisions_land_on_the_grid():
    """kind=timer: решения приходят ровно в моменты t0 + k·period."""
    model = build_control_model(horizon=100.0, timer_period=10.0)
    result = simulate(model, SCN, seed=11)
    times = sorted({entry["t"] for entry in result.decision_log})
    assert times == [float(10 * k) for k in range(11)]


def test_timer_start_offset_respected():
    model = build_control_model(horizon=60.0, timer_period=10.0)
    model.decision_points["dp"].timer_start = 5.0
    result = simulate(model, SCN, seed=11)
    times = sorted({entry["t"] for entry in result.decision_log})
    assert times == [5.0, 15.0, 25.0, 35.0, 45.0, 55.0]


def test_requires_decision_false_suppresses_policy():
    """При requires_decision=false политика не вызывается."""
    model = build_control_model(horizon=200.0, requires_decision=False, rule="true")
    result = simulate(model, SCN, seed=11)
    assert result.metrics["decision_moments"] == 0
    assert result.policy_trace == []
    assert result.metrics["policy_firings"] == 0


def test_without_decision_points_no_decisions():
    """Регресс: сценарии без точек принятия решений работают как раньше."""
    model = build_control_model(with_decision_point=False, rule="true")
    result = simulate(model, SCN, seed=11)
    assert result.metrics["decision_moments"] == 0
    assert result.policy_trace == []
    assert result.status == "completed"


def test_decision_moments_are_fewer_than_switching_moments():
    """Большинство МП решения не требует: разделение потоков экономит вычисления."""
    model = build_control_model(horizon=200.0, timer_period=20.0)
    result = simulate(model, SCN, seed=11)
    assert result.metrics["decision_moments"] < result.metrics["switching_moments"]


def test_decision_log_records_interval_between_decisions():
    """Между МР вознаграждение накапливается: журнал хранит длину интервала."""
    model = build_control_model(horizon=50.0, timer_period=10.0)
    result = simulate(model, SCN, seed=11)
    intervals = [entry["interval"] for entry in result.decision_log]
    assert intervals[0] == 0.0
    assert all(value == pytest.approx(10.0) for value in intervals[1:])


def test_event_decision_point_fires_on_event():
    model = build_control_model(horizon=100.0, with_decision_point=False, rule="true")
    model.decision_points["dp_ev"] = DecisionPoint(
        id="dp_ev", name="По событию", kind=DecisionPointKind.EVENT, event_ref="arrive"
    )
    result = simulate(model, SCN, seed=11)
    assert result.metrics["decision_moments"] > 0
    kinds = {t["kind"] for entry in result.decision_log for t in entry["triggers"]}
    assert kinds == {"event"}
    # число МР не превосходит числа событий этого вида
    assert result.metrics["decision_moments"] <= result.metrics["np_events"]


def build_watchdog_model() -> EnterpriseModel:
    """Запас растёт событием и обнуляется СЭДом — условие успевает сброситься."""
    m = EnterpriseModel(
        enterprise=Enterprise(id="wd", name="Сторож", planning_horizon_hours=60.0)
    )
    m.utility_types["u"] = UtilityType(id="u", name="Полезность")
    m.event_kinds["fill"] = EventKind(
        id="fill", name="Пополнение",
        stream=StreamSpec(kind=StreamKind.SCHEDULED, times=[10.0, 40.0]),
        effects=[
            EventEffect(
                kind=EffectKind.ADD_UTILITY, utility_type_id="u", quantity_expression="10"
            )
        ],
    )
    m.seds["drain"] = SEDDefinition(
        id="drain", name="Расход",
        inputs=[UtilityFlow(utility_type_id="u", quantity=10)],
        duration=DurationModel(kind=DurationKind.FIXED, value=1.0),
        triggers=[
            TriggerSpec(kind=TriggerKind.AT_TIME, at=25.0),
            TriggerSpec(kind=TriggerKind.AT_TIME, at=55.0),
        ],
    )
    m.kpis["k"] = KPIDefinition(id="k", name="k", expression="stock('u')")
    m.control_actions["noop"] = ControlActionDef(
        id="noop", name="Пустое действие",
        effect=EventEffect(
            kind=EffectKind.ADD_UTILITY, utility_type_id="u", quantity_expression="0"
        ),
    )
    m.policies["pol"] = Policy(id="pol", name="Пустая", rule="true", action_ref="noop")
    # частый регламент, чтобы сторожевое условие проверялось часто
    m.decision_points["tick"] = DecisionPoint(
        id="tick", name="Тик", kind=DecisionPointKind.TIMER, timer_period=1.0,
        requires_decision=False,
    )
    m.decision_points["wd"] = DecisionPoint(
        id="wd", name="Сторож", kind=DecisionPointKind.WATCHDOG,
        watchdog_condition="stock('u') >= 5",
    )
    return m


def test_watchdog_fires_on_rising_edge_only():
    """Сторож срабатывает при первом выполнении условия и не повторяется без сброса."""
    model = build_watchdog_model()
    scenario = Scenario(id="w", name="w", enterprise_id="wd", seed=3)
    result = simulate(model, scenario, seed=3)
    fires = [
        entry["t"]
        for entry in result.decision_log
        for trig in entry["triggers"]
        if trig["kind"] == "watchdog"
    ]
    # условие истинно с 10 до 25 и с 40 до 55 — ровно два передних фронта
    assert fires == [10.0, 40.0]


def test_watchdog_without_reset_fires_once():
    """Если условие не возвращается в ложь, сработка остаётся единственной."""
    model = build_watchdog_model()
    model.seds.pop("drain")  # запас больше не расходуется
    scenario = Scenario(id="w", name="w", enterprise_id="wd", seed=3)
    result = simulate(model, scenario, seed=3)
    fires = [
        entry["t"]
        for entry in result.decision_log
        for trig in entry["triggers"]
        if trig["kind"] == "watchdog"
    ]
    assert fires == [10.0]


def test_trajectory_differs_with_and_without_decision_moments():
    """Траектория с МР отличается от траектории без них, и различие объяснимо."""
    open_loop = simulate(build_control_model(with_decision_point=False), SCN, seed=11)
    closed_loop = simulate(build_control_model(), SCN, seed=11)
    assert closed_loop.final_kpis["done"] > open_loop.final_kpis["done"]
    # объяснение: политика расширяла ресурс, и загрузка распределилась на большую ёмкость
    assert closed_loop.metrics["policy_firings"] > 0
    assert closed_loop.resource_stats["line"]["final_capacity"] >= 1


def test_decision_point_validation():
    with pytest.raises(ValueError):
        DecisionPoint(id="x", kind=DecisionPointKind.EVENT)
    with pytest.raises(ValueError):
        DecisionPoint(id="x", kind=DecisionPointKind.TIMER)
    with pytest.raises(ValueError):
        DecisionPoint(id="x", kind=DecisionPointKind.WATCHDOG)
