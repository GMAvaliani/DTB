"""Фаза 4: отдельный контур верификации — сверка с аналитическими эталонами.

Этот набор отвечает не на вопрос «работает ли код», а на вопрос «совпадает ли
расчёт с теорией». Прогоны длинные, поэтому набор вынесен в отдельный каталог
и помечен маркером ``verification``: его можно запускать отдельно от
основных тестов.
"""

import math

import pytest

from dtb.verification import run_benchmarks
from dtb.verification.benchmarks import (
    _erlang_c_queue_length,
    benchmark_dd1,
    benchmark_discounting,
    benchmark_little,
    benchmark_mm1,
    benchmark_mmc,
    build_queue_model,
)

pytestmark = pytest.mark.verification


def assert_all_passed(checks):
    failed = [
        f"{c.id}: ожидалось {c.expected}, получено {c.observed} (допуск {c.tolerance})"
        for c in checks
        if not c.passed
    ]
    assert not failed, "; ".join(failed)
    return {c.id: c for c in checks}


# ------------------------------------------------------------------- M/M/1 --


def test_mm1_matches_theory():
    """ρ=0.8: загрузка, очередь, ожидание и время в системе — по формулам."""
    checks = assert_all_passed(benchmark_mm1(arrival_rate=0.8, service_rate=1.0))
    assert checks["mm1_queue_length"].expected == pytest.approx(3.2)
    assert checks["mm1_waiting_time"].expected == pytest.approx(4.0)
    assert checks["mm1_system_time"].expected == pytest.approx(5.0)


def test_mm1_light_load():
    """Малая загрузка: очередь почти пуста, ожидание близко к нулю."""
    checks = assert_all_passed(
        benchmark_mm1(arrival_rate=0.3, service_rate=1.0, tolerance=0.12,
                      horizon=10_000.0)
    )
    assert checks["mm1_queue_length"].observed < 0.3
    assert checks["mm1_utilization"].observed == pytest.approx(0.3, abs=0.03)


def test_mm1_service_rate_scaling():
    """Удвоение темпа обслуживания вдвое сокращает время в системе."""
    slow = assert_all_passed(benchmark_mm1(arrival_rate=0.8, service_rate=1.0))
    fast = assert_all_passed(
        benchmark_mm1(arrival_rate=1.6, service_rate=2.0, horizon=10_000.0)
    )  # та же ρ, вдвое быстрее
    assert fast["mm1_system_time"].observed == pytest.approx(
        slow["mm1_system_time"].observed / 2, rel=0.1
    )


def test_mm1_is_reproducible():
    first = benchmark_mm1(seed=777)
    second = benchmark_mm1(seed=777)
    assert [c.observed for c in first] == [c.observed for c in second]


def test_single_replication_is_noisier_than_the_average():
    """По одному прогону оценка очереди отклоняется сильнее: нужны повторения."""
    single = {c.id: c for c in benchmark_mm1(seed=101, replications=1, tolerance=0.3)}
    averaged = {c.id: c for c in benchmark_mm1(seed=101, replications=5)}
    assert single["mm1_queue_length"].observed != averaged["mm1_queue_length"].observed
    assert all(c.passed for c in averaged.values()), [
        c.to_dict() for c in averaged.values() if not c.passed
    ]
    # одиночный прогон остаётся в пределах широкого допуска, но точнее — среднее
    assert abs(averaged["mm1_queue_length"].observed - 3.2) < abs(
        single["mm1_queue_length"].observed - 3.2
    )


def test_unstable_queue_is_rejected():
    """λ ≥ μ: стационарного режима нет, эталон отказывается считать."""
    with pytest.raises(ValueError, match="неустойчива"):
        benchmark_mm1(arrival_rate=1.2, service_rate=1.0)
    with pytest.raises(ValueError, match="неустойчива"):
        _erlang_c_queue_length(2.5, 1.0, 2)


# ------------------------------------------------------------------- M/M/c --


def test_mmc_matches_erlang_c():
    checks = assert_all_passed(benchmark_mmc(arrival_rate=1.6, service_rate=1.0, servers=2))
    assert checks["mmc_utilization"].expected == pytest.approx(0.8)


def test_erlang_c_formula_sanity():
    """Эталонная формула сама согласована: при c=1 даёт M/M/1."""
    assert _erlang_c_queue_length(0.8, 1.0, 1) == pytest.approx(3.2, rel=1e-9)
    # при одинаковой загрузке ρ=0.8 два канала дают очередь короче одного
    assert _erlang_c_queue_length(1.6, 1.0, 2) < _erlang_c_queue_length(0.8, 1.0, 1)


def test_more_servers_shorten_queue():
    two = assert_all_passed(benchmark_mmc(arrival_rate=1.6, service_rate=1.0, servers=2))
    three = assert_all_passed(
        benchmark_mmc(arrival_rate=1.6, service_rate=1.0, servers=3, tolerance=0.3)
    )
    assert three["mmc_queue_length"].observed < two["mmc_queue_length"].observed


# -------------------------------------------------------------------- D/D/1 --


def test_dd1_is_exact():
    """Детерминированная система: совпадение точное, без статистики."""
    checks = assert_all_passed(benchmark_dd1(interval=2.0, service_time=1.0))
    assert checks["dd1_throughput"].observed == checks["dd1_throughput"].expected
    assert checks["dd1_utilization"].observed == pytest.approx(0.5, abs=1e-6)


def test_dd1_saturated_server():
    """Интервал равен времени обслуживания — загрузка 100%, очередь не копится."""
    checks = assert_all_passed(benchmark_dd1(interval=1.0, service_time=1.0))
    assert checks["dd1_utilization"].observed == pytest.approx(1.0, abs=1e-3)


# ------------------------------------------------------------ дисконтирование --


def test_discounting_matches_integral_exactly():
    checks = assert_all_passed(benchmark_discounting(rate=1000.0, annual=0.2))
    check = checks["discounting_integral"]
    assert check.observed == pytest.approx(check.expected, rel=1e-12)


def test_discounting_zero_rate_equals_nominal():
    checks = benchmark_discounting(rate=500.0, annual=0.0, horizon=1000.0)
    by_id = {c.id: c for c in checks}
    assert by_id["discounting_nominal"].observed == pytest.approx(500_000.0)


def test_discounting_higher_rate_lowers_present_value():
    low = {c.id: c for c in benchmark_discounting(annual=0.05)}
    high = {c.id: c for c in benchmark_discounting(annual=0.5)}
    assert high["discounting_integral"].observed < low["discounting_integral"].observed


# ------------------------------------------------------------- закон Литтла --


def test_little_law_holds():
    checks = assert_all_passed(benchmark_little(arrival_rate=0.9, service_rate=1.0))
    assert checks["little_throughput"].observed == pytest.approx(0.9, abs=0.05)


def test_little_law_holds_under_light_load():
    assert_all_passed(benchmark_little(arrival_rate=0.4, service_rate=1.0))


# ------------------------------------------------------------- набор целиком --


def test_full_benchmark_suite_passes():
    report = run_benchmarks()
    data = report.to_dict()
    assert data["passed"], [c for c in data["checks"] if not c["passed"]]
    assert data["total"] >= 13
    assert data["by_kind"]["analytic"]["failed"] == 0
    assert data["context"]["seed"]


def test_benchmark_report_is_serializable():
    data = run_benchmarks(["dd1"]).to_dict()
    import json

    assert json.loads(json.dumps(data, ensure_ascii=False))["total"] == 3


def test_queue_model_is_a_normal_model():
    """Эталонная модель — обычная модель предприятия и проходит валидацию."""
    from dtb.validation import validate_model

    model = build_queue_model(0.8, 1.0, 1)
    assert validate_model(model).ok_to_simulate
    assert model.seds["service"].duration.kind.value == "exponential"
    assert math.isclose(model.event_kinds["arrival"].stream.rate_per_hour, 0.8)
