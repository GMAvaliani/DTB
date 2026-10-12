"""Shared fixtures."""

from __future__ import annotations

import pytest

from dtb.domain import (
    DurationKind,
    DurationModel,
    EffectKind,
    Enterprise,
    EnterpriseModel,
    EventCategory,
    EventEffect,
    EventKind,
    Goal,
    KPIDefinition,
    Resource,
    ResourceKind,
    ResourceRequirement,
    Scenario,
    SEDDefinition,
    StreamKind,
    StreamSpec,
    TargetMetric,
    TriggerKind,
    TriggerSpec,
    UtilityFlow,
    UtilityKind,
    UtilityType,
)


def build_small_model(horizon: float = 240.0) -> EnterpriseModel:
    """Order stream → production (consumes material) → sale (produces money)."""
    m = EnterpriseModel(
        enterprise=Enterprise(id="acme", name="ACME", planning_horizon_hours=horizon)
    )
    for uid, kind in [
        ("order", UtilityKind.INFORMATION),
        ("material", UtilityKind.MATERIAL),
        ("product", UtilityKind.PRODUCT),
        ("money", UtilityKind.MONEY),
    ]:
        m.utility_types[uid] = UtilityType(id=uid, name=uid, kind=kind)
    m.resources["line"] = Resource(
        id="line", name="Line", kind=ResourceKind.EQUIPMENT, capacity=2
    )
    m.event_kinds["customer_order"] = EventKind(
        id="customer_order",
        name="Order",
        category=EventCategory.EXTERNAL,
        stream=StreamSpec(kind=StreamKind.POISSON, rate_per_hour=0.2),
        effects=[
            EventEffect(
                kind=EffectKind.ADD_UTILITY,
                utility_type_id="order",
                quantity_expression="1",
            )
        ],
    )
    m.seds["production"] = SEDDefinition(
        id="production",
        name="Production",
        goal_id="g1",
        inputs=[
            UtilityFlow(utility_type_id="order", quantity=1),
            UtilityFlow(utility_type_id="material", quantity=5),
        ],
        outputs=[UtilityFlow(utility_type_id="product", quantity=1)],
        resources=[ResourceRequirement(resource_id="line", quantity=1)],
        duration=DurationModel(kind=DurationKind.TRIANGULAR, low=4, high=10, mode=6),
        triggers=[TriggerSpec(kind=TriggerKind.ON_EVENT, event_kind_id="customer_order")],
    )
    m.seds["sale"] = SEDDefinition(
        id="sale",
        name="Sale",
        goal_id="g1",
        inputs=[UtilityFlow(utility_type_id="product", quantity=1)],
        outputs=[UtilityFlow(utility_type_id="money", quantity=100)],
        duration=DurationModel(kind=DurationKind.FIXED, value=1.0),
        triggers=[TriggerSpec(kind=TriggerKind.ON_SED_COMPLETED, sed_id="production")],
    )
    m.kpis["revenue"] = KPIDefinition(id="revenue", name="Revenue", expression="produced('money')")
    m.goals["g1"] = Goal(
        id="g1",
        name="Revenue goal",
        target_metrics=[TargetMetric(kpi_id="revenue", target_value=1000)],
    )
    m.initial_state.utility_stocks = {"material": 500.0}
    return m


@pytest.fixture()
def small_model() -> EnterpriseModel:
    return build_small_model()


@pytest.fixture()
def base_scenario() -> Scenario:
    return Scenario(id="base", name="Baseline", enterprise_id="acme", seed=42)


# --------------------------------------------------------- контур управления --

def build_control_model(
    horizon: float = 200.0,
    *,
    with_policy: bool = True,
    with_decision_point: bool = True,
    timer_period: float = 10.0,
    rule: str = "waiting_count('work') >= 2",
    requires_decision: bool = True,
    cooldown: float | None = None,
) -> EnterpriseModel:
    """Модель с замкнутым контуром: заказы → работа, политика расширяет ресурс."""
    from dtb.domain.control import (
        ControlActionDef,
        ControlLoop,
        DecisionPoint,
        DecisionPointKind,
        Policy,
    )

    m = EnterpriseModel(
        enterprise=Enterprise(id="ctl", name="Контур", planning_horizon_hours=horizon)
    )
    m.utility_types["order"] = UtilityType(id="order", name="Заказ")
    m.resources["line"] = Resource(id="line", name="Линия", capacity=1)
    m.event_kinds["arrive"] = EventKind(
        id="arrive", name="Поступление заказа",
        stream=StreamSpec(kind=StreamKind.POISSON, rate_per_hour=0.5),
        effects=[
            EventEffect(
                kind=EffectKind.ADD_UTILITY, utility_type_id="order", quantity_expression="1"
            )
        ],
    )
    m.seds["work"] = SEDDefinition(
        id="work", name="Работа",
        inputs=[UtilityFlow(utility_type_id="order", quantity=1)],
        resources=[ResourceRequirement(resource_id="line", quantity=1)],
        duration=DurationModel(kind=DurationKind.FIXED, value=3.0),
        triggers=[TriggerSpec(kind=TriggerKind.ON_EVENT, event_kind_id="arrive")],
    )
    m.kpis["done"] = KPIDefinition(id="done", name="Выполнено", expression="completed('work')")
    m.control_actions["add_line"] = ControlActionDef(
        id="add_line", name="Дополнительная линия", loop=ControlLoop.REGULATION,
        effect=EventEffect(
            kind=EffectKind.RESOURCE_CAPACITY_DELTA, resource_id="line", delta=1, duration=24
        ),
    )
    if with_policy:
        m.policies["pol"] = Policy(
            id="pol", name="Регулирование", loop=ControlLoop.REGULATION,
            rule=rule, action_ref="add_line", cooldown_hours=cooldown,
        )
    if with_decision_point:
        m.decision_points["dp"] = DecisionPoint(
            id="dp", name="Регламент", kind=DecisionPointKind.TIMER,
            timer_period=timer_period, requires_decision=requires_decision,
        )
    return m
