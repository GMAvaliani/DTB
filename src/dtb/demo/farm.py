"""Демо «Агроферма: строительство» — смета стройки как проект EVM.

Строительство фермы по реальной смете со статусом выполнения на 29.09.2026
(данные — :mod:`dtb.demo.farm_estimate`). Каждая позиция сметы — пакет работ
с бюджетом, плановым и фактическим объёмом. Пакеты выполняют СЭД по видам
работ — «Земляные работы», «Бетонные работы», «Монтаж металлоконструкций»,
«Каркас и обрешётка», «Кровля», «Утепление и отделка», «Общестрой»,
«Поставка материалов». За каждым видом работ стоит бригада со своим графиком
(шестидневка по 10 часов), поэтому сроки определяются не только сметой, но и
загрузкой бригад, погодой и праздниками.

Объём пакета задаётся трудоёмкостью: сумма «Работа всего» по смете ÷ часовая
выработка бригады. Разброс сроков и риски (непогода, срыв поставки) дают СЭД и
события модели.

Финансы заказчика: собственные средства на старте, кредитная линия (транши —
финансовая деятельность, проценты — текущая), выручка действующего хозяйства с
отсрочкой оплаты покупателями, аванс подрядчику 14 млн ₽ с зачётом 51% в
каждом акте, оплата актов раз в месяц. Отчёт о движении денежных средств,
остаток денег и показатели освоенного объёма доступны на любой момент прогона,
а правила мониторинга проверяют их ежедневно.

Допущения демо (в смете их нет, при работе с реальным объектом их нужно
заменить своими данными):

* начало стройки — 01.07.2026, дата статуса 29.09.2026 — 2160-й час;
* плановая продолжительность — 4 месяца (по позициям «мес» в смете);
* часовая выработка бригад — :data:`CREW_RATES`;
* здание убоя — второй этап (отдельный проект без аванса);
* финансовые параметры хозяйства — :func:`_add_finance`.
"""

from __future__ import annotations

from datetime import date

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
    ObjectiveSpec,
    OrgUnit,
    OrgUnitKind,
    Resource,
    ResourceKind,
    ResourceRequirement,
    Scenario,
    SEDDefinition,
    StreamKind,
    StreamSpec,
    TargetMetric,
    UtilityKind,
    UtilityType,
)
from dtb.domain.calendars import Calendar, CalendarException, Shift
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
from dtb.domain.economics import (
    EconomicBasis,
    EconomicDirection,
    EconomicItem,
    EconomicKind,
    EconomicsSettings,
)
from dtb.domain.monitoring import AlertSeverity, MonitorRule
from dtb.domain.projects import Project, WorkPackage
from dtb.domain.seds import InterruptionMode
from dtb.demo.farm_estimate import (
    ADVANCE_OFFSET_SHARE,
    ADVANCE_PAYMENT,
    ESTIMATE,
    OBJECTS,
    STATUS_LABEL,
)

FARM_ID = "demo_farm"
FARM_SEED = 20260929
PROJECT_ID = "farm"
STAGE2_ID = "farm_slaughter"
START_DATE = "2026-07-01"
STATUS_DATE = "2026-09-29"
#: Часы от начала стройки до даты статуса.
STATUS_HOURS = float((date.fromisoformat(STATUS_DATE) - date.fromisoformat(START_DATE)).days * 24)
MONTH_HOURS = 730.0

#: Вид работ → (бригада, название СЭД, часовая выработка бригады, ₽ работ в час).
#: Выработка — допущение демо: сколько рублей сметной стоимости работ бригада
#: осваивает за час на объекте.
CREW_RATES: dict[str, tuple[str, str, float]] = {
    "earth": ("crew_earth", "Земляные работы", 8_500.0),
    "concrete": ("crew_concrete", "Бетонные работы и основания", 11_000.0),
    "steel": ("crew_steel", "Монтаж металлоконструкций", 11_000.0),
    "carpentry": ("crew_carpentry", "Каркас и обрешётка", 8_000.0),
    "roofing": ("crew_roof", "Кровля и утепление ППУ", 12_500.0),
    "finishing": ("crew_finish", "Отделка фасадов", 7_000.0),
    "general": ("crew_general", "Общестроительные работы", 5_500.0),
}
#: Позиции без стоимости работ — поставка материала: срок поставки, часы.
SUPPLY_HOURS = 24.0
#: Минимальная трудоёмкость позиции, часы.
MIN_WORK_HOURS = 2.0

_EARTH_WORDS = ("разработка", "перевозка грунта", "вывоз", "выемка", "уплотнение грунта")
_SECTION_WORK = {
    "Раздел КМ": "steel",
    "Обрешетка": "carpentry",
    "Кровля": "roofing",
    "Утепление": "finishing",
}


def work_type(object_key: str, section: str, name: str, labor: float) -> str:
    """Вид работ позиции сметы — по разделу и наименованию."""
    if labor <= 0:
        return "supply"
    lowered = name.lower()
    if object_key == "prep":
        return "general"
    if any(word in lowered for word in _EARTH_WORDS):
        return "earth"
    for prefix, kind in _SECTION_WORK.items():
        if section.startswith(prefix):
            return kind
    return "concrete"


def _is_level_of_effort(object_key: str, name: str, unit: str) -> bool:
    """Работы «по времени»: помесячные и накладные позиции подготовки."""
    if unit.strip().lower().startswith("мес"):
        return True
    lowered = name.lower()
    return object_key == "prep" and ("транспортные" in lowered or "расходник" in lowered)


def _labor_total(row: tuple) -> float:
    _, _, _, _, qty, _, lab_unit, _, lab_total, *_ = row
    return float(lab_total) if lab_total is not None else float(qty * lab_unit)


def _is_side_section(section: str) -> bool:
    """Отмостку делают параллельно каркасу — сразу после нулевого цикла."""
    return section.startswith("Отмостка")


def build_work_packages() -> dict[str, WorkPackage]:
    """Пакеты работ из сметы: бюджет, факт на статус, вид работ, связи.

    Сетевой график по объекту: разделы идут друг за другом (нулевой цикл →
    металлоконструкции → обрешётка → кровля → отделка), позиции внутри
    раздела — по порядку сметы, отмостка — параллельно каркасу после нулевого
    цикла. Поставки материалов (позиции без стоимости работ) заказываются в
    начале раздела и цепочку не задерживают. Объекты стартуют после
    мобилизации, здание убоя — после фундамента скотного двора (бетонщики
    переходят с него).
    """
    packages: dict[str, WorkPackage] = {}
    counters: dict[str, int] = {}
    mobilization: str | None = None
    #: последний пакет основной цепочки объекта и последний пакет нулевого цикла
    chain_end: dict[str, str | None] = {}
    zero_cycle_end: dict[str, str | None] = {}
    #: (объект, раздел) → вход раздела и последний пакет в его цепочке
    section_entry: dict[tuple[str, str], str | None] = {}
    section_last: dict[tuple[str, str], str | None] = {}
    for row in ESTIMATE:
        (obj, section, name, unit, qty, mat_unit, lab_unit, mat_total, lab_total,
         delivery, equipment, act_qty, act_mat, act_lab) = row
        counters[obj] = counters.get(obj, 0) + 1
        project_ref = STAGE2_ID if obj == "slaughter" else PROJECT_ID
        wid = f"{obj}_{counters[obj]:03d}"
        labor = _labor_total(row)
        loe = _is_level_of_effort(obj, name, unit)
        kind = work_type(obj, section, name, labor)
        key = (obj, section)
        if key not in section_entry and obj != "prep":
            if _is_side_section(section):
                entry = zero_cycle_end.get(obj)
            elif obj in chain_end:
                entry = chain_end[obj]
            elif obj == "slaughter":
                entry = zero_cycle_end.get("barn") or mobilization
            else:
                entry = mobilization
            section_entry[key] = entry
            section_last[key] = entry
        predecessors: list[str] = []
        if loe:
            hours = None
        elif obj == "prep":
            hours = SUPPLY_HOURS if kind == "supply" else round(
                max(MIN_WORK_HOURS, labor / CREW_RATES[kind][2]), 2
            )
        elif kind == "supply":
            hours = SUPPLY_HOURS
            entry = section_entry[key]
            predecessors = [entry] if entry else []
        else:
            hours = round(max(MIN_WORK_HOURS, labor / CREW_RATES[kind][2]), 2)
            last = section_last[key]
            predecessors = [last] if last else []
            section_last[key] = wid
            if not _is_side_section(section):
                chain_end[obj] = wid
            if section.startswith("Нулевой цикл"):
                zero_cycle_end[obj] = wid
        packages[wid] = WorkPackage(
            id=wid, name=name, project_ref=project_ref,
            object_name=OBJECTS[obj][0], section=section, unit=unit,
            planned_quantity=qty, material_unit_cost=mat_unit, labor_unit_cost=lab_unit,
            material_total=mat_total, labor_total=lab_total,
            delivery_cost=delivery, equipment_cost=equipment,
            predecessors=predecessors, sed_ref=None if loe else f"work_{kind}",
            work_hours=hours, level_of_effort=loe,
            actual_quantity=act_qty, actual_material_cost=act_mat,
            actual_labor_cost=act_lab,
        )
        if obj == "prep" and "мобилизация" in name.lower():
            mobilization = wid
    return packages


def build_farm_model(with_baseline: bool = True) -> EnterpriseModel:
    """Модель стройки фермы; ``with_baseline`` — сразу построить базовый план."""
    m = EnterpriseModel(
        enterprise=Enterprise(
            id=FARM_ID,
            name="Агроферма: строительство",
            description="Строительство скотного двора с сыроварней, птичника, навеса "
            "и здания убоя по смете со статусом выполнения на 29.09.2026",
            planning_horizon_hours=24.0 * 240,
            time_zone="Europe/Moscow",
            start_date=START_DATE,
            simulation_calendar="6 дней × 10 часов",
        )
    )
    for uid, name, kind, unit in [
        ("money", "Денежные средства", UtilityKind.MONEY, "руб"),
        ("loan_tranche", "Транш кредита", UtilityKind.MONEY, "руб"),
    ]:
        m.utility_types[uid] = UtilityType(id=uid, name=name, kind=kind, unit=unit)
    for oid, name, kind, parent in [
        ("farm", "Агроферма", OrgUnitKind.ENTERPRISE, None),
        ("ou_construction", "Служба заказчика (стройка)", OrgUnitKind.DEPARTMENT, "farm"),
        ("ou_contractor", "Генподрядчик", OrgUnitKind.DEPARTMENT, "farm"),
        ("ou_finance", "Финансовая служба", OrgUnitKind.RESPONSIBILITY_CENTER, "farm"),
    ]:
        m.org_units[oid] = OrgUnit(id=oid, name=name, kind=kind, parent_id=parent)

    _add_calendars(m)
    _add_crews_and_seds(m)
    _add_projects(m)
    _add_risks(m)
    _add_finance(m)
    _add_kpis_and_goals(m)
    _add_monitoring(m)
    _add_control(m)
    if with_baseline:
        from dtb.analysis.evm import build_baseline

        build_baseline(m)
    return m


def _hours_from_start(day: str) -> float:
    return float((date.fromisoformat(day) - date.fromisoformat(START_DATE)).days * 24)


def _add_calendars(m: EnterpriseModel) -> None:
    """Шестидневка 08:00–18:00, государственные праздники — нерабочие."""
    holidays = [
        ("2026-11-04", "2026-11-05", "День народного единства"),
        ("2026-12-31", "2027-01-09", "Новогодние праздники"),
        ("2027-02-23", "2027-02-24", "День защитника Отечества"),
    ]
    m.calendars["six_days"] = Calendar(
        id="six_days", name="Шестидневка, 10 часов",
        description="08:00–18:00 с понедельника по субботу; праздники — нерабочие",
        shifts=[
            Shift(day_of_week=day, start_hour=8.0, end_hour=18.0, label="смена")
            for day in range(6)
        ],
        exceptions=[
            CalendarException(
                from_hour=_hours_from_start(a), to_hour=_hours_from_start(b),
                working=False, label=label,
            )
            for a, b, label in holidays
        ],
    )


def _add_crews_and_seds(m: EnterpriseModel) -> None:
    for kind, (crew, name, rate) in CREW_RATES.items():
        m.resources[crew] = Resource(
            id=crew, name=f"Бригада: {name.lower()}", kind=ResourceKind.PERSONNEL,
            capacity=1, unit="бригада", org_unit_id="ou_contractor",
            calendar_ref="six_days",
            description=f"Выработка {rate:,.0f} ₽ сметной стоимости работ в час".replace(",", " "),
        )
        m.seds[f"work_{kind}"] = SEDDefinition(
            id=f"work_{kind}", name=name, org_unit_id="ou_contractor",
            description="Выполняет пакеты работ проекта: длительность — трудоёмкость "
            "пакета × случайный множитель (производительность, мелкие простои)",
            resources=[ResourceRequirement(resource_id=crew, quantity=1)],
            duration=DurationModel(kind=DurationKind.TRIANGULAR, low=0.9, mode=1.0, high=1.35),
            interruption_mode=InterruptionMode.RESUME,
            priority=10,
        )
    m.seds["work_supply"] = SEDDefinition(
        id="work_supply", name="Поставка материалов", org_unit_id="ou_construction",
        description="Позиции сметы без стоимости работ: заказ и доставка материала",
        duration=DurationModel(kind=DurationKind.TRIANGULAR, low=0.5, mode=1.0, high=3.0),
        priority=5,
    )


def _add_projects(m: EnterpriseModel) -> None:
    m.projects[PROJECT_ID] = Project(
        id=PROJECT_ID, name="Строительство фермы",
        description="Подготовка, скотный двор с сыроварней, птичник, навес",
        planned_start=0.0, planned_finish=4 * MONTH_HOURS,
        status_date=STATUS_HOURS, status_label=STATUS_LABEL,
        advance_payment=ADVANCE_PAYMENT, advance_offset_share=ADVANCE_OFFSET_SHARE,
        billing_period_hours=MONTH_HOURS,
    )
    m.projects[STAGE2_ID] = Project(
        id=STAGE2_ID, name="Здание убоя (второй этап)",
        description="Отдельный этап: в итог основной сметы не входит, оплата по актам",
        planned_start=0.0, status_date=STATUS_HOURS, status_label=STATUS_LABEL,
        billing_period_hours=MONTH_HOURS, priority=10,
    )
    m.work_packages.update(build_work_packages())


def _add_risks(m: EnterpriseModel) -> None:
    """Риски стройки: непогода и срыв поставки — категория «неопределённые»."""
    for crew in ("crew_earth", "crew_concrete", "crew_roof"):
        m.event_kinds[f"weather_{crew}"] = EventKind(
            id=f"weather_{crew}", name=f"Непогода: {m.resources[crew].name.lower()}",
            category=EventCategory.UNCERTAIN,
            description="Дождь или мороз останавливает работы на открытом воздухе на сутки",
            stream=StreamSpec(kind=StreamKind.POISSON, rate_per_hour=1 / 240),
            effects=[EventEffect(kind=EffectKind.RESOURCE_CAPACITY_DELTA,
                                 resource_id=crew, delta=-1, duration=24)],
            affected_entity_ids=[f"resource:{crew}"],
        )
    m.event_kinds["supply_failure"] = EventKind(
        id="supply_failure", name="Срыв поставки металла",
        category=EventCategory.UNCERTAIN,
        description="Металлопрокат не пришёл в срок: монтажники простаивают трое суток",
        stream=StreamSpec(kind=StreamKind.POISSON, rate_per_hour=1 / 1000),
        effects=[EventEffect(kind=EffectKind.RESOURCE_CAPACITY_DELTA,
                             resource_id="crew_steel", delta=-1, duration=72)],
        affected_entity_ids=["resource:crew_steel"],
    )
    m.event_kinds["price_rise"] = EventKind(
        id="price_rise", name="Подорожание материалов",
        category=EventCategory.EXTERNAL,
        description="Пакеты, начатые после события, закупают материалы на 15% дороже",
        stream=StreamSpec(kind=StreamKind.NONE),  # подаётся сценарием
        effects=[EventEffect(kind=EffectKind.SET_PARAMETER,
                             parameter_path=f"project:{PROJECT_ID}.material_price_factor",
                             value=1.15)],
    )


#: Транши кредитной линии: даты выдачи и сумма транша.
LOAN_TRANCHES = ("2026-07-01", "2026-08-15", "2026-10-01")
LOAN_TRANCHE = 8_000_000.0
#: Остаток денег и долг по кредиту на дату статуса (допущение демо: так
#: выглядели бы данные казначейства на 29.09.2026).
STATUS_OPENING_CASH = 6_750_000.0
STATUS_LOAN_BALANCE = 16_000_000.0


def _add_finance(m: EnterpriseModel) -> None:
    """Деньги хозяйства: свои средства, кредит, выручка, накладные расходы.

    Стройка — инвестиционная деятельность (аванс и акты подрядчику), выручка
    действующего хозяйства и его расходы — текущая, транши кредита —
    финансовая. Проценты по кредиту относятся к текущей деятельности, как в
    форме отчёта о движении денежных средств по РСБУ.
    """
    m.economics = EconomicsSettings(
        currency="руб", discount_rate_annual=0.18, opening_cash=10_000_000.0,
        report_bucket_hours=24.0,
    )
    m.initial_state.utility_stocks = {"money": 0.0, "loan_tranche": 0.0}
    m.event_kinds["loan_tranche"] = EventKind(
        id="loan_tranche", name="Транш кредитной линии",
        category=EventCategory.PLANNED,
        description="Банк выдаёт три транша по 8 млн ₽: 01.07, 15.08 и 01.10.2026",
        stream=StreamSpec(
            kind=StreamKind.SCHEDULED,
            times=[_hours_from_start(day) for day in LOAN_TRANCHES],
        ),
        effects=[EventEffect(kind=EffectKind.ADD_UTILITY, utility_type_id="loan_tranche",
                             quantity_expression=f"{LOAN_TRANCHE:.0f}")],
    )
    m.economic_items["loan_in"] = EconomicItem(
        id="loan_in", name="Получение кредита", article="кредиты и займы",
        direction=EconomicDirection.INCOME, kind=EconomicKind.ONE_TIME,
        basis=EconomicBasis.UTILITY_PRODUCED, ref="loan_tranche", rate=1.0,
        cash_category="financing",
    )
    m.economic_items["loan_interest"] = EconomicItem(
        id="loan_interest", name="Проценты по кредиту (16% годовых)",
        article="проценты по кредитам",
        description="Начисляются на сумму полученных траншей, платятся раз в месяц",
        kind=EconomicKind.RATE, basis=EconomicBasis.UTILITY_STOCK, ref="loan_tranche",
        rate=0.16 / 8760.0, cash_category="operating", payment_delay_hours=MONTH_HOURS,
    )
    m.economic_items["farm_revenue"] = EconomicItem(
        id="farm_revenue", name="Выручка действующего хозяйства", article="выручка",
        description="Продажа молока и мяса: 3,2 млн ₽ в месяц, покупатели платят через 14 дней",
        direction=EconomicDirection.INCOME, kind=EconomicKind.RATE,
        basis=EconomicBasis.CONSTANT, rate=3_200_000.0 / MONTH_HOURS,
        cash_category="operating", payment_delay_hours=336.0,
    )
    m.economic_items["farm_costs"] = EconomicItem(
        id="farm_costs", name="Расходы хозяйства", article="себестоимость и накладные",
        description="Корма, ФОТ, энергия: 2,3 млн ₽ в месяц",
        kind=EconomicKind.RATE, basis=EconomicBasis.CONSTANT,
        rate=2_300_000.0 / MONTH_HOURS, cash_category="operating",
    )
    m.economic_items["owner_supervision"] = EconomicItem(
        id="owner_supervision", name="Технический надзор заказчика",
        article="содержание службы заказчика",
        kind=EconomicKind.RATE, basis=EconomicBasis.CONSTANT,
        rate=150_000.0 / MONTH_HOURS, cash_category="operating",
    )


def _add_kpis_and_goals(m: EnterpriseModel) -> None:
    kpis = [
        ("cash_balance", "Остаток денежных средств", "cash()", "руб"),
        ("net_cash_30d", "Чистый денежный поток за 30 дней", "net_cash_flow(30)", "руб"),
        ("farm_progress", "Готовность стройки", f"100 * progress('{PROJECT_ID}')", "%"),
        ("farm_cpi", "CPI стройки", f"cpi('{PROJECT_ID}')", ""),
        ("farm_spi", "SPI стройки", f"spi('{PROJECT_ID}')", ""),
        ("farm_spi_t", "SPI(t) стройки", f"spi_t('{PROJECT_ID}')", ""),
        ("farm_eac", "Прогноз стоимости стройки (EAC)", f"eac('{PROJECT_ID}')", "руб"),
        ("farm_ac", "Фактические затраты стройки (AC)", f"ac('{PROJECT_ID}')", "руб"),
        ("farm_delay_days", "Отставание стройки, дни",
         f"project_delay('{PROJECT_ID}') / 24", "дн"),
        ("stage2_progress", "Готовность здания убоя", f"100 * progress('{STAGE2_ID}')", "%"),
    ]
    for kid, name, expr, unit in kpis:
        m.kpis[kid] = KPIDefinition(id=kid, name=name, expression=expr, unit=unit)
    m.goals["g_build"] = Goal(
        id="g_build", name="Построить ферму в срок и в бюджете",
        responsible_org_unit_id="ou_construction", priority=1,
        target_metrics=[
            TargetMetric(kpi_id="farm_progress", target_value=100, comparison=">="),
            TargetMetric(kpi_id="farm_cpi", target_value=0.95, comparison=">="),
        ],
        sed_ids=[f"work_{kind}" for kind in CREW_RATES] + ["work_supply"],
    )
    m.goals["g_liquidity"] = Goal(
        id="g_liquidity", name="Не допустить кассового разрыва", parent_id="g_build",
        responsible_org_unit_id="ou_finance", priority=5,
        target_metrics=[TargetMetric(kpi_id="cash_balance", target_value=0, comparison=">=")],
    )


def _add_monitoring(m: EnterpriseModel) -> None:
    """Ежедневные правила: деньги, освоение, сроки — сигнал в день отклонения."""
    rules = [
        ("mon_cash_gap", "Кассовый разрыв", "cash() < 0",
         AlertSeverity.CRITICAL, "Денег на счетах не хватает на платежи"),
        ("mon_cash_low", "Риск кассового разрыва", "cash() < 1500000",
         AlertSeverity.WARNING, "Остаток денег ниже 1,5 млн ₽"),
        ("mon_cash_burn", "Отток денег за 30 дней", "net_cash_flow(30) < -6000000",
         AlertSeverity.WARNING, "Чистый отток за последние 30 дней больше 6 млн ₽"),
        ("mon_cpi", "Затраты выше освоенного",
         f"ac('{PROJECT_ID}') > 0 and cpi('{PROJECT_ID}') < 0.9",
         AlertSeverity.WARNING, "CPI стройки ниже 0,9"),
        ("mon_spi", "Отставание от графика",
         f"pv('{PROJECT_ID}') > 0 and spi('{PROJECT_ID}') < 0.85",
         AlertSeverity.WARNING, "SPI стройки ниже 0,85"),
        ("mon_delay", "Отставание больше двух недель",
         f"project_delay('{PROJECT_ID}') > 336",
         AlertSeverity.CRITICAL, "По Earned Schedule стройка отстаёт больше чем на 14 дней"),
        ("mon_eac", "Прогноз выше бюджета",
         f"ac('{PROJECT_ID}') > 0 and eac('{PROJECT_ID}') > 1.05 * bac('{PROJECT_ID}')",
         AlertSeverity.WARNING, "Прогноз стоимости (EAC) выше бюджета больше чем на 5%"),
    ]
    for rid, name, condition, severity, message in rules:
        m.monitor_rules[rid] = MonitorRule(
            id=rid, name=name, condition=condition, severity=severity,
            check_period_hours=24.0, check_start=12.0, message=message,
        )


#: Бригады, которые штаб может усилить: вид работ → подпись.
REINFORCEABLE = {
    "concrete": "бетонщиков",
    "steel": "монтажников",
    "carpentry": "плотников",
    "roofing": "кровельщиков",
}


def _add_control(m: EnterpriseModel) -> None:
    """Контур организации: штаб стройки усиливает бригады при отставании.

    Усиление (вторая смена, дополнительные люди в бригаде) ускоряет работы
    вида на 30% на шесть недель; человеко-часов на объём уходит столько же,
    а мобилизация и проживание людей — за счёт заказчика.

    Штаб видит SPI не напрямую, а через наблюдение — ежедневную сводку за
    вчерашний день. Чтобы оценить ежемесячную отчётность, достаточно задать
    наблюдению период 730 ч и задержку 240 ч (отчёт готов через 10 дней после
    закрытия месяца) и сравнить сценарии.
    """
    m.subjects["construction_manager"] = Subject(
        id="construction_manager", name="Руководитель проекта", authority_level=2,
        org_unit_ref="ou_construction", role_ref="editor",
    )
    for kind, label in REINFORCEABLE.items():
        sed_id = f"work_{kind}"
        m.control_actions[f"reinforce_{kind}"] = ControlActionDef(
            id=f"reinforce_{kind}", name=f"Усиление бригады {label}",
            loop=ControlLoop.ORGANIZATION, goal_ref="g_build",
            description="Вторая смена и дополнительные люди на 6 недель: работы этого "
            "вида идут на 30% быстрее; мобилизация и проживание — за счёт заказчика",
            effect=EventEffect(kind=EffectKind.SET_PARAMETER,
                               parameter_path=f"sed:{sed_id}.duration_factor", value=0.7),
            activation_delay=72, one_time_cost=150_000, recurring_cost=400,
            term=1008, min_authority_level=2, subject_ref="construction_manager",
        )
        m.policies[f"pol_reinforce_{kind}"] = Policy(
            id=f"pol_reinforce_{kind}", name=f"Отставание: усилить {label}",
            description="Если SPI по отчёту ниже порога, а у бригады есть работа — усилить её",
            loop=ControlLoop.ORGANIZATION, subject_ref="construction_manager",
            state_selector=["kpi('farm_spi')", f"executing_count('{sed_id}')",
                            f"waiting_count('{sed_id}')"],
            rule=(
                f"kpi('farm_spi') < threshold('spi') and "
                f"executing_count('{sed_id}') + waiting_count('{sed_id}') > 0"
            ),
            thresholds={"spi": 0.95},
            action_ref=f"reinforce_{kind}", cooldown_hours=1008, priority=20,
        )
    m.decision_points["dp_site_meeting"] = DecisionPoint(
        id="dp_site_meeting", name="Штаб стройки", kind=DecisionPointKind.TIMER,
        description="Еженедельный штаб по понедельникам, 10:00",
        timer_period=168, timer_start=130, policy_ref=None,
    )
    m.observations["obs_farm_spi"] = Observation(
        id="obs_farm_spi", name="SPI стройки в ежедневной сводке",
        description="Сводка об освоении за вчерашний день",
        target_kind=ObservationTarget.KPI, target_ref="farm_spi",
        period=24.0, delay=24.0, estimator=Estimator.LAST,
    )


def build_farm_scenarios() -> list[Scenario]:
    plan = Scenario(
        id="farm_plan", name="Стройка с начала: план с рисками",
        description="Проект с 01.07.2026: бригады, погода, праздники; штаб по "
        "ежедневной сводке усиливает отстающие бригады",
        enterprise_id=FARM_ID, seed=FARM_SEED, monte_carlo_runs=20,
        horizon_hours=24.0 * 240,
        objective=ObjectiveSpec(kpi_id="farm_progress", target=100),
    )
    no_control = Scenario(
        id="farm_plan_no_control", name="Стройка с начала: без реакции штаба",
        description="Те же риски, но штаб не усиливает бригады: цена отсутствия "
        "управляющего контура",
        enterprise_id=FARM_ID, base_scenario_id="farm_plan", seed=FARM_SEED,
        monte_carlo_runs=20, horizon_hours=24.0 * 240,
        parameter_overrides={
            f"policy:pol_reinforce_{kind}.enabled": False for kind in REINFORCEABLE
        },
        objective=ObjectiveSpec(kpi_id="farm_progress", target=100),
    )
    status = Scenario(
        id="farm_status", name="Прогноз от статуса 29.09.2026",
        description="Выполненное на 29.09 считается сделанным, моделируется остаток "
        "работ: прогноз срока и стоимости завершения",
        enterprise_id=FARM_ID, seed=FARM_SEED, monte_carlo_runs=20,
        horizon_hours=24.0 * 200, project_mode="status", opening_cash=STATUS_OPENING_CASH,
        initial_stock_overrides={"loan_tranche": STATUS_LOAN_BALANCE},
        objective=ObjectiveSpec(kpi_id="farm_progress", target=100),
    )
    status_crews = Scenario(
        id="farm_status_crews", name="От статуса: вторые бригады сразу",
        description="С даты статуса работают по две бригады монтажников и кровельщиков",
        enterprise_id=FARM_ID, base_scenario_id="farm_status", seed=FARM_SEED,
        monte_carlo_runs=20, horizon_hours=24.0 * 200, project_mode="status",
        opening_cash=STATUS_OPENING_CASH,
        initial_stock_overrides={"loan_tranche": STATUS_LOAN_BALANCE},
        resource_capacity_overrides={"crew_steel": 2, "crew_roof": 2},
        objective=ObjectiveSpec(kpi_id="farm_progress", target=100),
    )
    status_prices = Scenario(
        id="farm_status_prices", name="От статуса: материалы +15%",
        description="Цены материалов на остаток работ выросли на 15%",
        enterprise_id=FARM_ID, base_scenario_id="farm_status", seed=FARM_SEED,
        monte_carlo_runs=20, horizon_hours=24.0 * 200, project_mode="status",
        opening_cash=STATUS_OPENING_CASH,
        initial_stock_overrides={"loan_tranche": STATUS_LOAN_BALANCE},
        parameter_overrides={f"project:{PROJECT_ID}.material_price_factor": 1.15},
        objective=ObjectiveSpec(kpi_id="farm_progress", target=100),
    )
    status_no_stage2 = Scenario(
        id="farm_status_no_stage2", name="От статуса: убой переносится",
        description="Второй этап (здание убоя) откладывается: бригады работают "
        "только на основной стройке",
        enterprise_id=FARM_ID, base_scenario_id="farm_status", seed=FARM_SEED,
        monte_carlo_runs=20, horizon_hours=24.0 * 200, project_mode="status",
        opening_cash=STATUS_OPENING_CASH,
        initial_stock_overrides={"loan_tranche": STATUS_LOAN_BALANCE},
        parameter_overrides={f"project:{STAGE2_ID}.enabled": False},
        objective=ObjectiveSpec(kpi_id="farm_progress", target=100),
    )
    return [plan, no_control, status, status_crews, status_prices, status_no_stage2]


def create_farm_demo() -> tuple[EnterpriseModel, list[Scenario]]:
    return build_farm_model(), build_farm_scenarios()
