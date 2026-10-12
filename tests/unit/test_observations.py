"""Фаза 1.3: слой наблюдений — задержка, погрешность, способы оценивания."""

import pytest

from dtb.domain.control import (
    Estimator,
    Observation,
    ObservationMode,
    ObservationTarget,
)
from dtb.simulation.observations import ObservationLayer


def layer(**kw) -> tuple[ObservationLayer, Observation]:
    obs = Observation(
        id="obs",
        name="наблюдение",
        target_kind=ObservationTarget.UTILITY_TYPE,
        target_ref="u",
        period=kw.pop("period", 1.0),
        delay=kw.pop("delay", 0.0),
        relative_error=kw.pop("relative_error", 0.0),
        estimator=kw.pop("estimator", Estimator.LAST),
        mode=kw.pop("mode", ObservationMode.TWIN),
        **kw,
    )
    return ObservationLayer({"obs": obs}, seed=1), obs


def feed(lay: ObservationLayer, values: dict[float, float]) -> None:
    """Снять наблюдения в заданные моменты с заданными истинными значениями."""
    for t in sorted(values):
        lay.take_samples(t, lambda _obs, _t=t: values[_t])


def test_exact_observation_equals_truth():
    """delay=0, relative_error=0, estimator=last ⇒ Ŷ совпадает с Y."""
    lay, _ = layer()
    feed(lay, {0.0: 10.0, 1.0: 11.0, 2.0: 12.0})
    assert lay.estimate("obs", 2.0) == 12.0
    assert lay.observed_value("utility_type", "u", "stock", 2.0, 12.0) == 12.0


def test_delay_shows_past_value():
    """При delay>0 политика в момент t видит значение, снятое в t-delay."""
    lay, _ = layer(delay=2.0)
    feed(lay, {0.0: 10.0, 1.0: 11.0, 2.0: 12.0, 3.0: 13.0})
    # в t=3 доступны снимки со временем доступности <= 3: t=0 (сразу) и t=1 (1+2=3)
    assert lay.estimate("obs", 3.0) == 11.0
    # в t=2 доступен только начальный снимок
    lay2, _ = layer(delay=2.0)
    feed(lay2, {0.0: 10.0, 1.0: 11.0, 2.0: 12.0})
    assert lay2.estimate("obs", 2.0) == 10.0


def test_estimator_last_holds_last_available():
    lay, _ = layer(estimator=Estimator.LAST)
    feed(lay, {0.0: 1.0, 1.0: 5.0, 2.0: 2.0})
    assert lay.estimate("obs", 2.5) == 2.0


def test_estimator_smooth_smooths():
    """Сглаживание тянет оценку к новым значениям, не повторяя их."""
    lay, _ = layer(estimator=Estimator.SMOOTH, alpha=0.5)
    feed(lay, {0.0: 0.0, 1.0: 10.0})
    value = lay.estimate("obs", 1.0)
    assert 0.0 < value < 10.0
    assert value == pytest.approx(5.0)


def test_estimator_extrapolate_follows_trend():
    """Экстраполяция продолжает тренд двух последних наблюдений."""
    lay, _ = layer(estimator=Estimator.EXTRAPOLATE)
    feed(lay, {0.0: 10.0, 1.0: 12.0})
    # наклон 2 за час: в t=2 ожидаем 14
    assert lay.estimate("obs", 2.0) == pytest.approx(14.0)


def test_pure_mode_returns_truth():
    """В режиме pure_simulation политика читает истину, а не оценку."""
    lay, _ = layer(delay=5.0, mode=ObservationMode.PURE_SIMULATION)
    feed(lay, {0.0: 1.0, 1.0: 99.0})
    assert lay.observed_value("utility_type", "u", "stock", 1.0, 99.0) == 99.0


def test_mode_override_wins():
    lay = ObservationLayer(
        {
            "obs": Observation(
                id="obs", name="o", target_kind=ObservationTarget.UTILITY_TYPE,
                target_ref="u", period=1.0, delay=5.0, mode=ObservationMode.TWIN,
            )
        },
        seed=1,
        mode_override=ObservationMode.PURE_SIMULATION,
    )
    feed(lay, {0.0: 1.0, 1.0: 99.0})
    assert lay.observed_value("utility_type", "u", "stock", 1.0, 99.0) == 99.0


def test_noise_is_reproducible_and_uses_substream():
    """Погрешность берётся из SHA-256-субпотока: одинаковый seed ⇒ одинаковый шум."""
    def run(seed: int) -> float:
        lay, _ = layer(relative_error=0.3)
        feed(lay, {0.0: 100.0, 1.0: 100.0})
        return lay.estimate("obs", 1.0)

    assert run(1) == run(1)
    lay_a, _ = layer(relative_error=0.3)
    feed(lay_a, {0.0: 100.0, 1.0: 100.0})
    noisy = lay_a.estimate("obs", 1.0)
    assert noisy != 100.0  # шум действительно применён


def test_error_report_accumulates():
    lay, _ = layer(delay=2.0)
    feed(lay, {0.0: 10.0, 1.0: 20.0, 2.0: 30.0})
    lay.observed_value("utility_type", "u", "stock", 2.0, 30.0)
    report = lay.error_report()["obs"]
    assert report["reads"] == 1
    assert report["max_abs_error"] == pytest.approx(20.0)  # видели 10 вместо 30
    assert report["mode"] == "twin"


def test_uncovered_target_falls_back_to_truth():
    lay, _ = layer()
    assert lay.observed_value("resource", "other", "utilization", 1.0, 0.75) == 0.75
    assert not lay.covers("resource", "other", "utilization")


# ------------------------------------------- движок: twin против pure (ТЗ 1.3) --


def build_observed_model(delay: float, relative_error: float = 0.0):
    """Модель, где политика читает очередь с задержкой: решение запаздывает."""
    from tests.conftest import build_control_model

    model = build_control_model(horizon=300.0, timer_period=8.0,
                                rule="waiting_count('work') >= 2")
    model.observations["obs"] = Observation(
        id="obs", name="Очередь", target_kind=ObservationTarget.SED,
        target_ref="work", metric="waiting_count", period=4.0, delay=delay,
        relative_error=relative_error, estimator=Estimator.LAST,
    )
    return model


def test_twin_and_pure_runs_differ_and_difference_is_reported():
    from dtb.domain import Scenario
    from dtb.simulation import simulate

    model = build_observed_model(delay=24.0)
    twin = Scenario(id="t", name="twin", enterprise_id="ctl", seed=7)
    pure = Scenario(id="p", name="pure", enterprise_id="ctl", seed=7,
                    observation_mode="pure_simulation")
    a = simulate(model, twin, seed=7)
    b = simulate(model, pure, seed=7)
    # разность итогов двух прогонов — цена неполноты наблюдений
    assert a.metrics["observation_mode"] == "per_observation"
    assert b.metrics["observation_mode"] == "pure_simulation"
    assert a.observation_error["obs"]["mean_abs_error"] > 0
    assert b.observation_error["obs"]["mean_abs_error"] == 0
    assert a.final_kpis["done"] != b.final_kpis["done"] or (
        a.metrics["policy_firings"] != b.metrics["policy_firings"]
    )


def test_observation_noise_does_not_break_reproducibility():
    from dtb.domain import Scenario
    from dtb.simulation import simulate

    model = build_observed_model(delay=8.0, relative_error=0.25)
    scenario = Scenario(id="t", name="twin", enterprise_id="ctl", seed=7)
    first = simulate(model, scenario, seed=7)
    second = simulate(model, scenario, seed=7)
    assert first.content_digest() == second.content_digest()
    assert first.observation_error == second.observation_error


def test_zero_delay_zero_error_matches_pure_simulation():
    """Идеальное наблюдение эквивалентно прогону по истинному состоянию."""
    from dtb.domain import Scenario
    from dtb.simulation import simulate

    model = build_observed_model(delay=0.0, relative_error=0.0)
    model.observations["obs"].period = 0.5
    twin = simulate(model, Scenario(id="t", name="t", enterprise_id="ctl", seed=7), seed=7)
    pure = simulate(
        model,
        Scenario(id="p", name="p", enterprise_id="ctl", seed=7,
                 observation_mode="pure_simulation"),
        seed=7,
    )
    assert twin.final_kpis["done"] == pure.final_kpis["done"]
    assert twin.metrics["policy_firings"] == pure.metrics["policy_firings"]
