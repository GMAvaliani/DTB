"""Demo enterprise: «Демо-Завод» — full order-to-cash activity model.

Customer Order → Sales → Production Planning → Production → Quality Control →
(Rework) → Warehouse → Delivery → Revenue, plus human-capital provisioning,
material replenishment, equipment failures and maintenance. Goals, SEDs,
utilities, org structure, employees, equipment, resources, events and ERP
objects are linked into one coherent model that actually runs.

The engine knows nothing about these business names (engineering rule 8) —
this module is pure model content.
"""

from __future__ import annotations

from dtb.domain import (
    ControlAction,
    DurationKind,
    DurationModel,
    EffectKind,
    Enterprise,
    EnterpriseModel,
    EventCategory,
    EventEffect,
    EventKind,
    Goal,
    InjectedEvent,
    KPIDefinition,
    ObjectiveSpec,
    OrgUnit,
    OrgUnitKind,
    OutcomeBranch,
    Resource,
    ResourceKind,
    ResourceRequirement,
    Scenario,
    SEDDefinition,
    SeriesPoint,
    StreamKind,
    StreamSpec,
    TargetMetric,
    TriggerKind,
    TriggerSpec,
    UtilityFlow,
    UtilityKind,
    UtilityType,
)
from dtb.domain.control import (
    ControlActionDef,
    ControlLoop,
    DecisionPoint,
    DecisionPointKind,
    Estimator,
    Observation,
    ObservationTarget,
    Policy,
    Subject,
)
from dtb.integration.base import sync_connector
from dtb.integration.mock_erp import MockERPConnector

DEMO_ID = "demo_plant"
DEMO_SEED = 20250819


def build_demo_model() -> EnterpriseModel:
    m = EnterpriseModel(
        enterprise=Enterprise(
            id=DEMO_ID,
            name="Демо-Завод",
            description="Демонстрационный цифровой двойник производственного предприятия: "
            "заказ → продажи → планирование → производство → ОТК → склад → доставка → выручка",
            planning_horizon_hours=720.0,
            time_zone="Europe/Moscow",
        )
    )

    # -- utilities (полезности) ---------------------------------------------------
    for args in [
        ("customer_order", "Заказ клиента", UtilityKind.INFORMATION, "шт"),
        ("sales_contract", "Контракт", UtilityKind.INFORMATION, "шт"),
        ("production_plan", "Производственное задание", UtilityKind.WORK, "шт"),
        ("raw_material", "Сырьё", UtilityKind.RAW_MATERIAL, "кг"),
        ("components", "Комплектующие", UtilityKind.COMPONENT, "шт"),
        ("finished_product", "Готовое изделие", UtilityKind.PRODUCT, "шт"),
        ("defect_product", "Изделие с дефектом", UtilityKind.PRODUCT, "шт"),
        ("qc_passed_product", "Изделие, прошедшее ОТК", UtilityKind.PRODUCT, "шт"),
        ("stored_product", "Изделие на складе", UtilityKind.MOVED_STOCK, "шт"),
        ("delivered_product", "Доставленное изделие", UtilityKind.PRODUCT, "шт"),
        ("money", "Денежные средства", UtilityKind.MONEY, "руб"),
        ("trained_operator", "Обученный оператор", UtilityKind.TRAINED_EMPLOYEE, "чел"),
    ]:
        uid, name, kind, unit = args
        m.utility_types[uid] = UtilityType(id=uid, name=name, kind=kind, unit=unit)

    # -- organization (View 4) ----------------------------------------------------
    org_units = [
        ("company", "Демо-Завод", OrgUnitKind.ENTERPRISE, None),
        ("ou_sales", "Отдел продаж", OrgUnitKind.DEPARTMENT, "company"),
        ("ou_planning", "Планово-производственный отдел", OrgUnitKind.DEPARTMENT, "company"),
        ("ou_production", "Производство", OrgUnitKind.DEPARTMENT, "company"),
        ("ou_quality", "Служба качества", OrgUnitKind.DEPARTMENT, "company"),
        ("ou_warehouse", "Склад", OrgUnitKind.DEPARTMENT, "company"),
        ("ou_logistics", "Логистика", OrgUnitKind.DEPARTMENT, "company"),
        ("ou_finance", "Финансовая служба", OrgUnitKind.RESPONSIBILITY_CENTER, "company"),
        ("ou_hr", "Служба персонала", OrgUnitKind.DEPARTMENT, "company"),
    ]
    for oid, name, kind, parent in org_units:
        m.org_units[oid] = OrgUnit(id=oid, name=name, kind=kind, parent_id=parent)

    # -- resources (View 6) -------------------------------------------------------
    resources = [
        ("sales_team", "Менеджеры продаж", ResourceKind.PERSONNEL, 2, "чел", "ou_sales", 1200),
        ("planners", "Планировщики", ResourceKind.PERSONNEL, 1, "чел", "ou_planning", 1100),
        ("production_line", "Сборочные линии", ResourceKind.EQUIPMENT, 2, "линия", "ou_production", 3500),
        ("qc_station", "Пост ОТК", ResourceKind.EQUIPMENT, 1, "пост", "ou_quality", 900),
        ("warehouse_team", "Кладовщики", ResourceKind.PERSONNEL, 1, "чел", "ou_warehouse", 800),
        ("delivery_trucks", "Автопарк доставки", ResourceKind.VEHICLE, 2, "машина", "ou_logistics", 1500),
        ("trainers", "Наставники", ResourceKind.PERSONNEL, 1, "чел", "ou_hr", 1000),
    ]
    for rid, name, kind, capacity, unit, org, cost in resources:
        m.resources[rid] = Resource(
            id=rid, name=name, kind=kind, capacity=capacity, unit=unit,
            org_unit_id=org, cost_per_hour=cost,
        )

    # -- KPIs (View 3 definitions) ------------------------------------------------
    kpis = [
        ("revenue", "Выручка", "produced('money')", "руб"),
        ("orders_received", "Получено заказов", "events('customer_order')", "шт"),
        ("orders_delivered", "Доставлено заказов", "completed('delivery')", "шт"),
        ("service_level", "Уровень сервиса",
         "100 * completed('delivery') / max(1, events('customer_order'))", "%"),
        ("wip", "Незавершённое производство",
         "completed('production_planning') - completed('delivery')", "шт"),
        ("defect_rate", "Уровень дефектов",
         "100 * produced('defect_product') / max(1, produced('finished_product'))", "%"),
        ("material_stock", "Запас сырья", "stock('raw_material')", "кг"),
        ("line_utilization", "Загрузка линий", "100 * utilization('production_line')", "%"),
        ("trained_staff", "Подготовлено операторов", "produced('trained_operator')", "чел"),
    ]
    for kid, name, expr, unit in kpis:
        m.kpis[kid] = KPIDefinition(id=kid, name=name, expression=expr, unit=unit)

    # -- goals (View 1) -----------------------------------------------------------
    m.goals["g_root"] = Goal(
        id="g_root", name="Рост бизнеса",
        description="Головная цель: выручка и уровень сервиса за горизонт планирования",
        responsible_org_unit_id="company", priority=1,
        target_metrics=[
            TargetMetric(kpi_id="revenue", target_value=2_000_000, comparison=">="),
            TargetMetric(kpi_id="service_level", target_value=75, comparison=">="),
        ],
    )
    m.goals["g_sales"] = Goal(
        id="g_sales", name="Обеспечить приток заказов", parent_id="g_root",
        responsible_org_unit_id="ou_sales", priority=10,
        target_metrics=[TargetMetric(kpi_id="orders_received", target_value=90, comparison=">=")],
        sed_ids=["sales"],
    )
    m.goals["g_production"] = Goal(
        id="g_production", name="Выполнять заказы в срок", parent_id="g_root",
        responsible_org_unit_id="ou_production", priority=10,
        target_metrics=[
            TargetMetric(kpi_id="service_level", target_value=75, comparison=">="),
            TargetMetric(kpi_id="defect_rate", target_value=10, comparison="<="),
        ],
        sed_ids=["production", "quality_control"],
        resource_ids=["production_line", "qc_station"],
    )
    m.goals["g_finance"] = Goal(
        id="g_finance", name="Собрать выручку", parent_id="g_root",
        responsible_org_unit_id="ou_finance", priority=20,
        target_metrics=[TargetMetric(kpi_id="revenue", target_value=2_000_000, comparison=">=")],
        sed_ids=["revenue_collection"],
    )
    m.goals["g_hr"] = Goal(
        id="g_hr", name="Готовить кадры", parent_id="g_root",
        responsible_org_unit_id="ou_hr", priority=30,
        target_metrics=[TargetMetric(kpi_id="trained_staff", target_value=3, comparison=">=")],
        sed_ids=["staff_training"],
    )

    # -- НП-события ---------------------------------------------------------------
    m.event_kinds["customer_order"] = EventKind(
        id="customer_order", name="Поступление заказа",
        category=EventCategory.EXTERNAL,
        stream=StreamSpec(kind=StreamKind.POISSON, rate_per_hour=0.15),
        payload_defaults={"quantity": 1},
        effects=[EventEffect(kind=EffectKind.ADD_UTILITY, utility_type_id="customer_order",
                             quantity_expression="quantity")],
    )
    m.event_kinds["equipment_failure"] = EventKind(
        id="equipment_failure", name="Отказ оборудования",
        category=EventCategory.UNCERTAIN,
        stream=StreamSpec(kind=StreamKind.POISSON, rate_per_hour=1 / 200),
        effects=[EventEffect(kind=EffectKind.RESOURCE_CAPACITY_DELTA,
                             resource_id="production_line", delta=-1, duration=24)],
        affected_entity_ids=["resource:production_line"],
    )
    m.event_kinds["material_delivery"] = EventKind(
        id="material_delivery", name="Поставка материалов",
        category=EventCategory.PLANNED,
        stream=StreamSpec(kind=StreamKind.SCHEDULED, interval=84, start=12),
        effects=[
            EventEffect(kind=EffectKind.ADD_UTILITY, utility_type_id="raw_material",
                        quantity_expression="400"),
            EventEffect(kind=EffectKind.ADD_UTILITY, utility_type_id="components",
                        quantity_expression="200"),
        ],
    )
    m.event_kinds["maintenance"] = EventKind(
        id="maintenance", name="Регламентное обслуживание",
        category=EventCategory.PLANNED,
        stream=StreamSpec(kind=StreamKind.SCHEDULED, interval=168, start=100),
        effects=[EventEffect(kind=EffectKind.RESOURCE_CAPACITY_DELTA,
                             resource_id="production_line", delta=-1, duration=8)],
        affected_entity_ids=["resource:production_line"],
    )
    m.event_kinds["employee_resignation"] = EventKind(
        id="employee_resignation", name="Увольнение сотрудника",
        category=EventCategory.UNCERTAIN,
        stream=StreamSpec(kind=StreamKind.POISSON, rate_per_hour=1 / 400),
        effects=[EventEffect(kind=EffectKind.RESOURCE_CAPACITY_DELTA,
                             resource_id="sales_team", delta=-1, duration=168)],
        affected_entity_ids=["resource:sales_team"],
    )
    m.event_kinds["demand_shift"] = EventKind(
        id="demand_shift", name="Изменение спроса",
        category=EventCategory.EXTERNAL,
        stream=StreamSpec(kind=StreamKind.NONE),  # scenario-injected
        payload_defaults={"new_rate": 0.15},
        effects=[EventEffect(kind=EffectKind.SET_PARAMETER,
                             parameter_path="event_kind:customer_order.stream.rate_per_hour",
                             value=0.25)],
    )

    # -- СЭДы (View 2, причинно-следственная цепочка) -----------------------------
    m.seds["sales"] = SEDDefinition(
        id="sales", name="Продажи",
        description="Обработка заказа, подготовка и заключение контракта",
        goal_id="g_sales", org_unit_id="ou_sales",
        inputs=[UtilityFlow(utility_type_id="customer_order", quantity=1)],
        outputs=[UtilityFlow(utility_type_id="sales_contract", quantity=1)],
        resources=[ResourceRequirement(resource_id="sales_team", quantity=1)],
        duration=DurationModel(kind=DurationKind.TRIANGULAR, low=2, high=8, mode=4),
        triggers=[TriggerSpec(kind=TriggerKind.ON_EVENT, event_kind_id="customer_order")],
        priority=10,
    )
    m.seds["production_planning"] = SEDDefinition(
        id="production_planning", name="Планирование производства",
        goal_id="g_production", org_unit_id="ou_planning",
        inputs=[UtilityFlow(utility_type_id="sales_contract", quantity=1)],
        outputs=[UtilityFlow(utility_type_id="production_plan", quantity=1)],
        resources=[ResourceRequirement(resource_id="planners", quantity=1)],
        duration=DurationModel(kind=DurationKind.FIXED, value=2),
        triggers=[TriggerSpec(kind=TriggerKind.ON_SED_COMPLETED, sed_id="sales")],
        priority=20,
    )
    m.seds["production"] = SEDDefinition(
        id="production", name="Производство",
        description="Изготовление изделия по производственному заданию",
        goal_id="g_production", org_unit_id="ou_production",
        inputs=[
            UtilityFlow(utility_type_id="production_plan", quantity=1),
            UtilityFlow(utility_type_id="raw_material", quantity=10),
            UtilityFlow(utility_type_id="components", quantity=4),
        ],
        outputs=[UtilityFlow(utility_type_id="finished_product", quantity=1)],
        resources=[ResourceRequirement(resource_id="production_line", quantity=1)],
        equipment_ids=["eq_line_1", "eq_line_2"],
        duration=DurationModel(kind=DurationKind.TRIANGULAR, low=6, high=16, mode=10),
        triggers=[TriggerSpec(kind=TriggerKind.ON_SED_COMPLETED, sed_id="production_planning")],
        priority=30,
    )
    m.seds["quality_control"] = SEDDefinition(
        id="quality_control", name="Контроль качества (ОТК)",
        goal_id="g_production", org_unit_id="ou_quality",
        inputs=[UtilityFlow(utility_type_id="finished_product", quantity=1)],
        outcome_branches=[
            OutcomeBranch(label="pass", probability=0.93,
                          outputs=[UtilityFlow(utility_type_id="qc_passed_product", quantity=1)]),
            OutcomeBranch(label="fail", probability=0.07,
                          outputs=[UtilityFlow(utility_type_id="defect_product", quantity=1)],
                          spawn_sed_ids=["rework"]),
        ],
        resources=[ResourceRequirement(resource_id="qc_station", quantity=1)],
        duration=DurationModel(kind=DurationKind.FIXED, value=1.5),
        triggers=[
            TriggerSpec(kind=TriggerKind.ON_SED_COMPLETED, sed_id="production"),
            TriggerSpec(kind=TriggerKind.ON_SED_COMPLETED, sed_id="rework"),
        ],
        priority=40,
    )
    m.seds["rework"] = SEDDefinition(
        id="rework", name="Устранение дефектов",
        goal_id="g_production", org_unit_id="ou_production",
        inputs=[
            UtilityFlow(utility_type_id="defect_product", quantity=1),
            UtilityFlow(utility_type_id="raw_material", quantity=2),
        ],
        outputs=[UtilityFlow(utility_type_id="finished_product", quantity=1)],
        resources=[ResourceRequirement(resource_id="production_line", quantity=1)],
        duration=DurationModel(kind=DurationKind.FIXED, value=4),
        priority=25,  # rework jumps the production queue
    )
    m.seds["warehousing"] = SEDDefinition(
        id="warehousing", name="Приёмка на склад",
        org_unit_id="ou_warehouse",
        inputs=[UtilityFlow(utility_type_id="qc_passed_product", quantity=1)],
        outputs=[UtilityFlow(utility_type_id="stored_product", quantity=1)],
        resources=[ResourceRequirement(resource_id="warehouse_team", quantity=1)],
        duration=DurationModel(kind=DurationKind.FIXED, value=1),
        triggers=[TriggerSpec(kind=TriggerKind.ON_SED_COMPLETED, sed_id="quality_control",
                              condition="branch == 'pass'")],
        priority=50,
    )
    m.seds["delivery"] = SEDDefinition(
        id="delivery", name="Доставка клиенту",
        org_unit_id="ou_logistics",
        inputs=[UtilityFlow(utility_type_id="stored_product", quantity=1)],
        outputs=[UtilityFlow(utility_type_id="delivered_product", quantity=1)],
        resources=[ResourceRequirement(resource_id="delivery_trucks", quantity=1)],
        duration=DurationModel(kind=DurationKind.TRIANGULAR, low=4, high=12, mode=6),
        triggers=[TriggerSpec(kind=TriggerKind.ON_SED_COMPLETED, sed_id="warehousing")],
        priority=60,
    )
    m.seds["revenue_collection"] = SEDDefinition(
        id="revenue_collection", name="Получение оплаты",
        goal_id="g_finance", org_unit_id="ou_finance",
        inputs=[UtilityFlow(utility_type_id="delivered_product", quantity=1)],
        outputs=[UtilityFlow(utility_type_id="money", quantity=25_000)],
        duration=DurationModel(kind=DurationKind.FIXED, value=24),  # отсрочка платежа
        triggers=[TriggerSpec(kind=TriggerKind.ON_SED_COMPLETED, sed_id="delivery")],
        priority=70,
    )
    m.seds["staff_training"] = SEDDefinition(
        id="staff_training", name="Подготовка операторов",
        description="Обеспечение человеческого капитала",
        goal_id="g_hr", org_unit_id="ou_hr",
        outputs=[UtilityFlow(utility_type_id="trained_operator", quantity=1)],
        resources=[ResourceRequirement(resource_id="trainers", quantity=1)],
        duration=DurationModel(kind=DurationKind.FIXED, value=16),
        triggers=[TriggerSpec(kind=TriggerKind.RECURRING, interval=168, start=40)],
        priority=80,
    )

    # -- initial state (View 7) + plans/actuals (View 3) --------------------------
    m.initial_state.utility_stocks = {
        "raw_material": 800.0,
        "components": 400.0,
        "money": 0.0,
    }
    m.plans["revenue"] = [
        SeriesPoint(t=168, value=500_000),
        SeriesPoint(t=336, value=1_000_000),
        SeriesPoint(t=504, value=1_500_000),
        SeriesPoint(t=720, value=2_200_000),
    ]
    m.plans["service_level"] = [SeriesPoint(t=360, value=75), SeriesPoint(t=720, value=80)]
    m.actuals["revenue"] = [
        SeriesPoint(t=-336, value=1_950_000),
        SeriesPoint(t=-168, value=2_050_000),
        SeriesPoint(t=0, value=0),
    ]

    # -- контур управления: субъекты, действия, политики, МР, наблюдения ---------
    _add_control_loop(m)

    # -- ERP micro-level (views 4–7) through the real connector pipeline ----------
    sync_connector(m, MockERPConnector())
    return m


def _add_control_loop(m: EnterpriseModel) -> None:
    """Три контура управления демо-завода: регулирование, организация, развитие.

    Политики читают наблюдаемое состояние (с периодом, задержкой и
    погрешностью) и применяют действия из каталога. Решения принимаются только
    в моменты принятия решений: смена (24 ч), неделя (168 ч), квартал (240 ч) и
    отказ оборудования.
    """
    m.subjects["shift_supervisor"] = Subject(
        id="shift_supervisor", name="Начальник смены", authority_level=1,
        org_unit_ref="ou_production", role_ref="editor",
        description="Принимает решения контура регулирования",
    )
    m.subjects["plant_manager"] = Subject(
        id="plant_manager", name="Директор по производству", authority_level=2,
        org_unit_ref="ou_production", role_ref="editor",
        description="Принимает решения контура организации",
    )
    m.subjects["ceo"] = Subject(
        id="ceo", name="Генеральный директор", authority_level=3,
        org_unit_ref="company", role_ref="admin",
        description="Принимает решения контура развития",
    )

    m.control_actions["overtime_shift"] = ControlActionDef(
        id="overtime_shift", name="Сверхурочная смена", loop=ControlLoop.REGULATION,
        description="Временное расширение сборочной мощности на сутки",
        goal_ref="g_production",
        effect=EventEffect(
            kind=EffectKind.RESOURCE_CAPACITY_DELTA,
            resource_id="production_line", delta=1, duration=24,
        ),
        one_time_cost=45_000, term=24, min_authority_level=1,
        subject_ref="shift_supervisor",
    )
    m.control_actions["second_shift_crew"] = ControlActionDef(
        id="second_shift_crew", name="Бригада второй смены", loop=ControlLoop.ORGANIZATION,
        description="Набор бригады на две недели: перераспределение мощности, "
        "вступает в силу через двое суток после решения",
        goal_ref="g_production",
        effect=EventEffect(
            kind=EffectKind.RESOURCE_CAPACITY_DELTA,
            resource_id="production_line", delta=1, duration=336,
        ),
        activation_delay=48, one_time_cost=120_000, recurring_cost=900,
        term=336, min_authority_level=2, subject_ref="plant_manager",
    )
    m.control_actions["third_line"] = ControlActionDef(
        id="third_line", name="Третья сборочная линия", loop=ControlLoop.DEVELOPMENT,
        description="Инвестиция в постоянное расширение производственной мощности",
        goal_ref="g_root",
        effect=EventEffect(
            kind=EffectKind.RESOURCE_CAPACITY_DELTA, resource_id="production_line", delta=1,
        ),
        activation_delay=168, one_time_cost=4_500_000, recurring_cost=3_500,
        reversible=False, success_probability=0.85,
        min_authority_level=3, subject_ref="ceo",
    )

    m.policies["pol_overtime"] = Policy(
        id="pol_overtime", name="Регулирование: очередь на сборке",
        description="Если на сборку ждёт три и более задания — сверхурочная смена",
        loop=ControlLoop.REGULATION, subject_ref="shift_supervisor",
        state_selector=["waiting_count('production')"],
        rule="waiting_count('production') >= 3", action_ref="overtime_shift",
        cooldown_hours=48, priority=10,
    )
    m.policies["pol_second_shift"] = Policy(
        id="pol_second_shift", name="Организация: вторая смена",
        description="Если наблюдаемая загрузка линий выше 70% — набираем бригаду",
        loop=ControlLoop.ORGANIZATION, subject_ref="plant_manager",
        state_selector=["utilization('production_line')"],
        rule="utilization('production_line') >= 0.7", action_ref="second_shift_crew",
        cooldown_hours=336, priority=20,
    )
    m.policies["pol_invest"] = Policy(
        id="pol_invest", name="Развитие: третья линия",
        description="Если линии устойчиво загружены и накоплены средства — инвестируем",
        loop=ControlLoop.DEVELOPMENT, subject_ref="ceo",
        state_selector=["utilization('production_line')", "stock('money')"],
        rule="utilization('production_line') >= 0.6 and stock('money') >= 800000",
        action_ref="third_line", cooldown_hours=480, priority=30,
    )

    m.decision_points["dp_shift"] = DecisionPoint(
        id="dp_shift", name="Пересмотр по смене", kind=DecisionPointKind.TIMER,
        description="Регламентный момент решения контура регулирования",
        timer_period=24, timer_start=8, policy_ref="pol_overtime",
    )
    m.decision_points["dp_week"] = DecisionPoint(
        id="dp_week", name="Недельный штаб", kind=DecisionPointKind.TIMER,
        description="Регламентный момент решения контура организации",
        timer_period=168, timer_start=168, policy_ref="pol_second_shift",
    )
    m.decision_points["dp_quarter"] = DecisionPoint(
        id="dp_quarter", name="Инвестиционный комитет", kind=DecisionPointKind.TIMER,
        description="Регламентный момент решения контура развития",
        timer_period=240, timer_start=240, policy_ref="pol_invest",
    )
    m.decision_points["dp_failure"] = DecisionPoint(
        id="dp_failure", name="Решение по отказу оборудования",
        kind=DecisionPointKind.EVENT, event_ref="equipment_failure",
        description="Внеплановый момент решения: отказ требует реакции смены",
        policy_ref="pol_overtime",
    )

    m.observations["obs_line_load"] = Observation(
        id="obs_line_load", name="Загрузка линий (наблюдаемая)",
        description="Снимается раз в смену со сглаживанием и погрешностью 10%",
        target_kind=ObservationTarget.RESOURCE, target_ref="production_line",
        metric="utilization", period=8, delay=8, relative_error=0.1,
        estimator=Estimator.SMOOTH, alpha=0.5,
    )
    m.observations["obs_qc_load"] = Observation(
        id="obs_qc_load", name="Загрузка ОТК (наблюдаемая)",
        target_kind=ObservationTarget.RESOURCE, target_ref="qc_station",
        metric="utilization", period=24, delay=24, relative_error=0.05,
        estimator=Estimator.LAST,
    )
    m.observations["obs_prod_queue"] = Observation(
        id="obs_prod_queue", name="Очередь на сборке (наблюдаемая)",
        description="Диспетчер видит очередь с запаздыванием в четыре часа",
        target_kind=ObservationTarget.SED, target_ref="production",
        metric="waiting_count", period=4, delay=4, relative_error=0.0,
        estimator=Estimator.LAST,
    )
    m.observations["obs_money"] = Observation(
        id="obs_money", name="Денежные средства (наблюдаемые)",
        description="Финансовый учёт закрывается раз в неделю",
        target_kind=ObservationTarget.UTILITY_TYPE, target_ref="money",
        metric="stock", period=168, delay=24, relative_error=0.02,
        estimator=Estimator.LAST,
    )


def build_demo_scenarios() -> list[Scenario]:
    base = Scenario(
        id="demo_baseline", name="Базовый сценарий",
        description="Текущая структура деятельности и внешней среды без изменений",
        enterprise_id=DEMO_ID, seed=DEMO_SEED, monte_carlo_runs=20,
        objective=ObjectiveSpec(kpi_id="revenue", target=2_000_000),
    )
    demand_growth = Scenario(
        id="demo_demand_growth", name="Рост спроса +40%",
        description="Интенсивность потока заказов выше на 40%",
        enterprise_id=DEMO_ID, base_scenario_id="demo_baseline",
        seed=DEMO_SEED, monte_carlo_runs=20,
        event_rate_multipliers={"customer_order": 1.4},
        objective=ObjectiveSpec(kpi_id="revenue", target=2_000_000),
    )
    extra_line = Scenario(
        id="demo_extra_line", name="Третья линия + рост спроса",
        description="Инвестиция в третью сборочную линию при выросшем спросе",
        enterprise_id=DEMO_ID, base_scenario_id="demo_demand_growth",
        seed=DEMO_SEED, monte_carlo_runs=20,
        event_rate_multipliers={"customer_order": 1.4},
        resource_capacity_overrides={"production_line": 3},
        objective=ObjectiveSpec(kpi_id="revenue", target=2_000_000),
    )
    failures = Scenario(
        id="demo_failure_stress", name="Стресс: отказы оборудования",
        description="Утроенная интенсивность отказов и две плановых остановки",
        enterprise_id=DEMO_ID, base_scenario_id="demo_baseline",
        seed=DEMO_SEED, monte_carlo_runs=20,
        event_rate_multipliers={"equipment_failure": 3.0},
        injected_events=[
            InjectedEvent(time=120, event_kind_id="equipment_failure"),
            InjectedEvent(time=400, event_kind_id="equipment_failure"),
        ],
        objective=ObjectiveSpec(kpi_id="service_level", target=75),
    )
    managed = Scenario(
        id="demo_managed_demand", name="Управление: скачок спроса с t=240",
        description="Управляющее воздействие: маркетинговая кампания повышает "
        "интенсивность заказов с 240-го часа",
        enterprise_id=DEMO_ID, base_scenario_id="demo_baseline",
        seed=DEMO_SEED, monte_carlo_runs=20,
        control_actions=[
            ControlAction(
                time=240,
                label="Маркетинговая кампания",
                effect=EventEffect(
                    kind=EffectKind.SET_PARAMETER,
                    parameter_path="event_kind:customer_order.stream.rate_per_hour",
                    value=0.22,
                ),
            )
        ],
        objective=ObjectiveSpec(kpi_id="revenue", target=2_000_000),
    )
    pure_observation = Scenario(
        id="demo_pure_observation", name="Полная наблюдаемость (эталон)",
        description="Те же политики, но решения принимаются по истинному состоянию: "
        "разность с базовым сценарием — цена неполноты наблюдений",
        enterprise_id=DEMO_ID, base_scenario_id="demo_baseline",
        seed=DEMO_SEED, monte_carlo_runs=20,
        observation_mode="pure_simulation",
        objective=ObjectiveSpec(kpi_id="revenue", target=2_000_000),
    )
    return [base, demand_growth, extra_line, failures, managed, pure_observation]


def create_demo() -> tuple[EnterpriseModel, list[Scenario]]:
    return build_demo_model(), build_demo_scenarios()
