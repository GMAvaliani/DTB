"""Аналитические эталоны: сверка движка с задачами, решёнными в теории.

Это отдельный контур доверия к расчёту. Движок не знает, что его сверяют:
строятся обычные модели предприятия, запускаются обычные прогоны, а итог
сравнивается с формулой, известной из теории массового обслуживания и
финансовой математики.

Эталоны:

* **M/M/1** — пуассоновский поток λ, экспоненциальное обслуживание μ, один
  канал: ρ = λ/μ, L_q = ρ²/(1−ρ), W_q = ρ/(μ−λ), W = 1/(μ−λ), загрузка = ρ.
* **M/M/c** — тот же поток, c каналов: загрузка = λ/(cμ), L_q по формуле
  Эрланга C.
* **D/D/1** — детерминированный поток и обслуживание: пропускная способность
  равна min(λ, μ), очередь не растёт при λ < μ.
* **Дисконтирование** — постоянная ставка r на горизонте T при непрерывной
  ставке ρ: ∫ r e^{-ρt} dt = r(1 − e^{-ρT})/ρ.
* **Закон Литтла** — L = λ·W на прогоне с высокой загрузкой.

Прогоны длинные и с разогревом: сравнивается стационарный режим, а переходный
отбрасывается. Допуск задан в долях от эталона и отражает статистическую
погрешность конечного прогона, а не «подгонку под ответ».
"""

from __future__ import annotations

import math

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
    ResourceKind,
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
from dtb.common.seeding import derive_seed
from dtb.domain.economics import EconomicBasis, EconomicItem, EconomicKind, EconomicsSettings
from dtb.simulation import simulate
from dtb.verification.checks import VerificationCheck, VerificationReport, _close

#: Длина прогона и разогрев по умолчанию: стационарный режим M/M/1 с ρ=0.8
#: устанавливается медленно, поэтому горизонт большой.
DEFAULT_HORIZON = 20_000.0
DEFAULT_WARMUP = 2_000.0
DEFAULT_SEED = 20260101
#: Число независимых повторений: сравнивается среднее по повторениям, потому
#: что оценка длины очереди в M/M/1 сама случайна и по одному прогону
#: отклоняется от теории на величину статистической погрешности.
DEFAULT_REPLICATIONS = 3


def build_queue_model(
    arrival_rate: float,
    service_rate: float,
    servers: int = 1,
    horizon: float = DEFAULT_HORIZON,
) -> EnterpriseModel:
    """Модель системы массового обслуживания M/M/c в терминах СЭД и ресурса."""
    m = EnterpriseModel(
        enterprise=Enterprise(
            id="mmc", name=f"M/M/{servers}", planning_horizon_hours=horizon
        )
    )
    m.utility_types["job"] = UtilityType(id="job", name="Заявка")
    m.resources["server"] = Resource(
        id="server", name="Канал обслуживания", kind=ResourceKind.EQUIPMENT,
        capacity=servers,
    )
    m.event_kinds["arrival"] = EventKind(
        id="arrival", name="Поступление заявки",
        stream=StreamSpec(kind=StreamKind.POISSON, rate_per_hour=arrival_rate),
        effects=[
            EventEffect(
                kind=EffectKind.ADD_UTILITY, utility_type_id="job",
                quantity_expression="1",
            )
        ],
    )
    m.seds["service"] = SEDDefinition(
        id="service", name="Обслуживание",
        inputs=[UtilityFlow(utility_type_id="job", quantity=1)],
        resources=[ResourceRequirement(resource_id="server", quantity=1)],
        duration=DurationModel(kind=DurationKind.EXPONENTIAL, mean=1.0 / service_rate),
        triggers=[TriggerSpec(kind=TriggerKind.ON_EVENT, event_kind_id="arrival")],
    )
    m.kpis["served"] = KPIDefinition(
        id="served", name="Обслужено", expression="completed('service')"
    )
    return m


def _run(model: EnterpriseModel, seed: int, warmup: float, horizon: float):
    scenario = Scenario(
        id="verify", name="Верификация", enterprise_id=model.enterprise.id,
        seed=seed, warmup_hours=warmup, horizon_hours=horizon,
    )
    return simulate(model, scenario, seed=seed)


def _replicate(
    model: EnterpriseModel,
    seed: int,
    warmup: float,
    horizon: float,
    observe,
    replications: int,
) -> dict[str, float]:
    """Средние значения наблюдаемых величин по независимым повторениям.

    Сиды повторений выводятся из основного через SHA-256: новых независимых
    генераторов не заводится, повторения воспроизводимы.
    """
    totals: dict[str, float] = {}
    count = max(1, replications)
    for index in range(count):
        run_seed = seed if count == 1 else derive_seed(seed, "replication", index)
        values = observe(_run(model, run_seed, warmup, horizon))
        for key, value in values.items():
            totals[key] = totals.get(key, 0.0) + value
    return {key: value / count for key, value in totals.items()}


def benchmark_mm1(
    arrival_rate: float = 0.8,
    service_rate: float = 1.0,
    seed: int = DEFAULT_SEED,
    horizon: float = DEFAULT_HORIZON,
    warmup: float = DEFAULT_WARMUP,
    tolerance: float = 0.1,
    replications: int = DEFAULT_REPLICATIONS,
) -> list[VerificationCheck]:
    """Сверка с M/M/1: загрузка, средняя очередь, ожидание, время в системе."""
    if arrival_rate >= service_rate:
        raise ValueError(
            f"M/M/1 неустойчива при λ={arrival_rate:g} ≥ μ={service_rate:g}: "
            f"очередь растёт неограниченно, стационарного режима нет"
        )
    rho = arrival_rate / service_rate
    model = build_queue_model(arrival_rate, service_rate, 1, horizon)

    def observe(result) -> dict[str, float]:
        stats = result.sed_stats["service"]
        hours = float(result.metrics["measured_hours"])
        return {
            "rho": float(result.resource_stats["server"]["utilization"]),
            "lq": float(stats["waiting_hours"]) / hours,
            "wq": float(stats["mean_waiting"]),
            "w": float(stats["mean_waiting"]) + float(stats["mean_service"]),
            "hours": hours,
        }

    observed = _replicate(model, seed, warmup, horizon, observe, replications)
    expected_lq = rho * rho / (1 - rho)
    expected_wq = rho / (service_rate - arrival_rate)
    expected_w = 1.0 / (service_rate - arrival_rate)
    scope = f"M/M/1 λ={arrival_rate:g} μ={service_rate:g}"
    detail = (
        f"{max(1, replications)} повторений по {observed['hours']:g} ч "
        f"после разогрева {warmup:g} ч, seed {seed}"
    )
    return [
        VerificationCheck(
            id="mm1_utilization", name="M/M/1: загрузка канала ρ = λ/μ",
            expected=round(rho, 6), observed=round(observed["rho"], 6),
            tolerance=tolerance, passed=_close(observed["rho"], rho, tolerance),
            kind="analytic", scope=scope, detail=detail,
        ),
        VerificationCheck(
            id="mm1_queue_length", name="M/M/1: средняя очередь L_q = ρ²/(1−ρ)",
            expected=round(expected_lq, 6), observed=round(observed["lq"], 6),
            tolerance=tolerance, passed=_close(observed["lq"], expected_lq, tolerance),
            kind="analytic", scope=scope,
            detail="L_q — интеграл длины очереди, делённый на измеренные часы",
        ),
        VerificationCheck(
            id="mm1_waiting_time", name="M/M/1: ожидание W_q = ρ/(μ−λ)",
            expected=round(expected_wq, 6), observed=round(observed["wq"], 6),
            tolerance=tolerance, passed=_close(observed["wq"], expected_wq, tolerance),
            kind="analytic", scope=scope, detail=detail,
        ),
        VerificationCheck(
            id="mm1_system_time", name="M/M/1: время в системе W = 1/(μ−λ)",
            expected=round(expected_w, 6), observed=round(observed["w"], 6),
            tolerance=tolerance, passed=_close(observed["w"], expected_w, tolerance),
            kind="analytic", scope=scope, detail=detail,
        ),
    ]


def _erlang_c_queue_length(arrival_rate: float, service_rate: float, c: int) -> float:
    """Средняя длина очереди M/M/c по формуле Эрланга C."""
    a = arrival_rate / service_rate
    rho = a / c
    if rho >= 1:
        raise ValueError(
            f"M/M/{c} неустойчива при ρ={rho:g} ≥ 1: стационарного режима нет"
        )
    terms = sum(a**k / math.factorial(k) for k in range(c))
    last = a**c / (math.factorial(c) * (1 - rho))
    p0 = 1.0 / (terms + last)
    lq = p0 * (a**c) * rho / (math.factorial(c) * (1 - rho) ** 2)
    return lq


def benchmark_mmc(
    arrival_rate: float = 1.6,
    service_rate: float = 1.0,
    servers: int = 2,
    seed: int = DEFAULT_SEED,
    horizon: float = DEFAULT_HORIZON,
    warmup: float = DEFAULT_WARMUP,
    tolerance: float = 0.15,
    replications: int = DEFAULT_REPLICATIONS,
) -> list[VerificationCheck]:
    """Сверка с M/M/c: загрузка и средняя очередь по формуле Эрланга C."""
    expected_lq = _erlang_c_queue_length(arrival_rate, service_rate, servers)
    rho = arrival_rate / (servers * service_rate)
    model = build_queue_model(arrival_rate, service_rate, servers, horizon)

    def observe(result) -> dict[str, float]:
        hours = float(result.metrics["measured_hours"])
        return {
            "rho": float(result.resource_stats["server"]["utilization"]),
            "lq": float(result.sed_stats["service"]["waiting_hours"]) / hours,
        }

    observed = _replicate(model, seed, warmup, horizon, observe, replications)
    scope = f"M/M/{servers} λ={arrival_rate:g} μ={service_rate:g}"
    detail = f"{max(1, replications)} повторений по {horizon:g} ч, seed {seed}"
    return [
        VerificationCheck(
            id="mmc_utilization", name=f"M/M/{servers}: загрузка ρ = λ/(cμ)",
            expected=round(rho, 6), observed=round(observed["rho"], 6),
            tolerance=tolerance, passed=_close(observed["rho"], rho, tolerance),
            kind="analytic", scope=scope, detail=detail,
        ),
        VerificationCheck(
            id="mmc_queue_length", name=f"M/M/{servers}: очередь по Эрлангу C",
            expected=round(expected_lq, 6), observed=round(observed["lq"], 6),
            tolerance=tolerance, passed=_close(observed["lq"], expected_lq, tolerance),
            kind="analytic", scope=scope, detail=detail,
        ),
    ]


def benchmark_dd1(
    interval: float = 2.0,
    service_time: float = 1.0,
    horizon: float = 1_000.0,
    seed: int = DEFAULT_SEED,
    tolerance: float = 0.01,
) -> list[VerificationCheck]:
    """Детерминированная система D/D/1: пропускная способность и пустая очередь."""
    m = EnterpriseModel(
        enterprise=Enterprise(id="dd1", name="D/D/1", planning_horizon_hours=horizon)
    )
    m.utility_types["job"] = UtilityType(id="job", name="Заявка")
    m.resources["server"] = Resource(id="server", name="Канал", capacity=1)
    m.seds["service"] = SEDDefinition(
        id="service", name="Обслуживание",
        resources=[ResourceRequirement(resource_id="server", quantity=1)],
        duration=DurationModel(kind=DurationKind.FIXED, value=service_time),
        triggers=[TriggerSpec(kind=TriggerKind.RECURRING, interval=interval, start=0.0)],
    )
    m.kpis["served"] = KPIDefinition(
        id="served", name="Обслужено", expression="completed('service')"
    )
    result = _run(m, seed, 0.0, horizon)
    expected_completed = math.floor((horizon - service_time) / interval) + 1
    observed_completed = float(result.sed_stats["service"]["completed"])
    expected_rho = service_time / interval
    observed_rho = float(result.resource_stats["server"]["utilization"])
    return [
        VerificationCheck(
            id="dd1_throughput", name="D/D/1: число обслуженных заявок",
            expected=float(expected_completed), observed=observed_completed,
            tolerance=tolerance,
            passed=abs(observed_completed - expected_completed) <= 1,
            kind="analytic", scope=f"D/D/1 интервал={interval:g} обслуживание={service_time:g}",
            detail="детерминированная система: результат точный, без статистики",
        ),
        VerificationCheck(
            id="dd1_utilization", name="D/D/1: загрузка = время обслуживания / интервал",
            expected=round(expected_rho, 6), observed=round(observed_rho, 6),
            tolerance=tolerance, passed=_close(observed_rho, expected_rho, tolerance),
            kind="analytic", scope="D/D/1",
        ),
        VerificationCheck(
            id="dd1_no_queue", name="D/D/1: при λ < μ очередь не накапливается",
            expected=0.0,
            observed=float(result.sed_stats["service"]["queue_peak"]),
            tolerance=0.0,
            passed=float(result.sed_stats["service"]["queue_peak"]) <= 1,
            kind="analytic", scope="D/D/1",
        ),
    ]


def benchmark_discounting(
    rate: float = 1_000.0,
    annual: float = 0.2,
    horizon: float = 5_000.0,
    seed: int = DEFAULT_SEED,
    tolerance: float = 1e-9,
) -> list[VerificationCheck]:
    """Непрерывное дисконтирование постоянной ставки против интеграла."""
    m = EnterpriseModel(
        enterprise=Enterprise(
            id="disc", name="Дисконтирование", planning_horizon_hours=horizon
        )
    )
    m.utility_types["unit"] = UtilityType(id="unit", name="Единица")
    m.kpis["zero"] = KPIDefinition(id="zero", name="Ноль", expression="0")
    m.economics = EconomicsSettings(discount_rate_annual=annual)
    m.economic_items["fixed"] = EconomicItem(
        id="fixed", name="Постоянная ставка", article="постоянные",
        kind=EconomicKind.RATE, basis=EconomicBasis.CONSTANT, rate=rate,
    )
    result = _run(m, seed, 0.0, horizon)
    rho = annual / m.economics.hours_per_year
    # при нулевой ставке интеграл дисконтного множителя равен длине горизонта
    expected = rate * (1 - math.exp(-rho * horizon)) / rho if rho else rate * horizon
    observed = float(result.economics["discounted_expense"])
    nominal_expected = rate * horizon
    nominal_observed = float(result.economics["expense"])
    return [
        VerificationCheck(
            id="discounting_integral",
            name="Дисконтирование: r(1−e^{−ρT})/ρ",
            expected=round(expected, 6), observed=round(observed, 6),
            tolerance=tolerance, passed=_close(observed, expected, tolerance),
            kind="analytic", scope=f"ρ={rho:.3e} 1/ч, T={horizon:g} ч",
            detail="интеграл дисконтного множителя берётся точно, не по средней точке",
        ),
        VerificationCheck(
            id="discounting_nominal", name="Номинальная сумма ставки = r·T",
            expected=round(nominal_expected, 6), observed=round(nominal_observed, 6),
            tolerance=1e-9, passed=_close(nominal_observed, nominal_expected, 1e-9),
            kind="analytic", scope="проверка интегрирования по интервалам МП",
        ),
    ]


def benchmark_little(
    arrival_rate: float = 0.9,
    service_rate: float = 1.0,
    seed: int = DEFAULT_SEED,
    horizon: float = DEFAULT_HORIZON,
    warmup: float = DEFAULT_WARMUP,
    tolerance: float = 0.05,
    replications: int = DEFAULT_REPLICATIONS,
) -> list[VerificationCheck]:
    """Закон Литтла на загруженной системе: L = λ·W."""
    model = build_queue_model(arrival_rate, service_rate, 1, horizon)

    def observe(result) -> dict[str, float]:
        stats = result.sed_stats["service"]
        hours = float(result.metrics["measured_hours"])
        mean_time = float(stats["mean_waiting"]) + float(stats["mean_service"])
        return {
            "lam": float(stats["completed"]) / hours,
            "w": mean_time,
            "l": (float(stats["waiting_hours"]) + float(stats["executing_hours"])) / hours,
            "lw": (float(stats["completed"]) / hours) * mean_time,
        }

    observed = _replicate(model, seed, warmup, horizon, observe, replications)
    detail = f"{max(1, replications)} повторений по {horizon:g} ч, seed {seed}"
    return [
        VerificationCheck(
            id="little_law", name="Закон Литтла: L = λ·W",
            expected=round(observed["lw"], 6), observed=round(observed["l"], 6),
            tolerance=tolerance, passed=_close(observed["l"], observed["lw"], tolerance),
            kind="analytic",
            scope=f"λ={observed['lam']:.6g} 1/ч, W={observed['w']:.6g} ч",
            detail="уровень в системе — интеграл, λ и W — по завершённым; " + detail,
        ),
        VerificationCheck(
            id="little_throughput",
            name="Пропускная способность равна интенсивности входа",
            expected=round(arrival_rate, 6), observed=round(observed["lam"], 6),
            tolerance=tolerance, passed=_close(observed["lam"], arrival_rate, tolerance),
            kind="analytic", scope="стационарный режим: что вошло, то и вышло",
            detail=detail,
        ),
    ]


#: Набор эталонов: имя → функция, возвращающая проверки.
BENCHMARKS = {
    "mm1": benchmark_mm1,
    "mmc": benchmark_mmc,
    "dd1": benchmark_dd1,
    "discounting": benchmark_discounting,
    "little": benchmark_little,
}


#: Эталоны со статистической оценкой: им осмысленны повторения.
STOCHASTIC_BENCHMARKS = ("mm1", "mmc", "little")


def run_benchmarks(
    names: list[str] | None = None,
    seed: int = DEFAULT_SEED,
    horizon: float | None = None,
    replications: int = DEFAULT_REPLICATIONS,
) -> VerificationReport:
    """Прогнать аналитические эталоны и собрать отчёт."""
    selected = names or list(BENCHMARKS)
    report = VerificationReport(
        context={
            "seed": seed,
            "benchmarks": selected,
            "horizon_hours": horizon or DEFAULT_HORIZON,
            "warmup_hours": DEFAULT_WARMUP,
            "replications": max(1, replications),
        }
    )
    for name in selected:
        fn = BENCHMARKS.get(name)
        if fn is None:
            continue
        kwargs: dict[str, float] = {"seed": seed}
        if name in STOCHASTIC_BENCHMARKS:
            kwargs["replications"] = max(1, replications)
            if horizon is not None:
                kwargs["horizon"] = horizon
        for check in fn(**kwargs):  # type: ignore[arg-type]
            report.add(check)
    return report
