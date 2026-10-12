"""Фаза 3: экономика деятельности, дисконтирование, разогрев."""

import math

import pytest

from dtb.domain import Scenario
from dtb.domain.economics import (
    EconomicBasis,
    EconomicDirection,
    EconomicItem,
    EconomicKind,
    EconomicsSettings,
)
from dtb.simulation import simulate
from tests.conftest import build_control_model

SCN = Scenario(id="s", name="s", enterprise_id="ctl", seed=11)


def build_economic_model(horizon: float = 100.0, **settings):
    model = build_control_model(
        horizon=horizon, with_policy=False, with_decision_point=False
    )
    model.economics = EconomicsSettings(**settings)
    return model


def rate_item(**kw) -> EconomicItem:
    kw.setdefault("id", "item")
    kw.setdefault("name", "статья")
    return EconomicItem(**kw)


# ----------------------------------------------------------------- ставки --


def test_constant_rate_integrates_over_horizon():
    """Постоянная ставка даёт rate × горизонт."""
    model = build_economic_model(horizon=50.0)
    model.economic_items["fixed"] = rate_item(
        id="fixed", article="постоянные", basis=EconomicBasis.CONSTANT, rate=20.0
    )
    result = simulate(model, SCN, seed=11)
    assert result.economics["expense"] == pytest.approx(50.0 * 20.0)
    assert result.economics["net"] == pytest.approx(-1000.0)
    assert result.economics["by_article"]["постоянные"]["expense"] == pytest.approx(1000.0)


def test_capacity_rate_follows_capacity_changes():
    """Ставка по ёмкости учитывает её изменение во времени."""
    from dtb.domain import EffectKind, EventEffect
    from dtb.domain.scenario import ControlAction

    model = build_economic_model(horizon=100.0)
    model.economic_items["hold"] = rate_item(
        id="hold", basis=EconomicBasis.RESOURCE_CAPACITY, ref="line", rate=10.0
    )
    scenario = SCN.model_copy(deep=True)
    scenario.control_actions = [
        ControlAction(
            id="c", time=40.0,
            effect=EventEffect(
                kind=EffectKind.RESOURCE_CAPACITY_DELTA, resource_id="line", delta=1
            ),
        )
    ]
    result = simulate(model, scenario, seed=11)
    # 40 ч по одной линии + 60 ч по двум
    assert result.economics["expense"] == pytest.approx(40 * 10 + 60 * 20)


def test_busy_rate_matches_resource_busy_hours():
    """Ставка за занятость совпадает с интегралом занятости из статистики."""
    model = build_economic_model(horizon=100.0)
    model.economic_items["busy"] = rate_item(
        id="busy", basis=EconomicBasis.RESOURCE_BUSY, ref="line", rate=7.0
    )
    result = simulate(model, SCN, seed=11)
    busy_hours = result.resource_stats["line"]["busy_hours"]
    assert result.economics["expense"] == pytest.approx(busy_hours * 7.0, rel=1e-6)


def test_stock_rate_prices_storage():
    model = build_economic_model(horizon=60.0)
    model.economic_items["storage"] = rate_item(
        id="storage", basis=EconomicBasis.UTILITY_STOCK, ref="order", rate=1.0
    )
    result = simulate(model, SCN, seed=11)
    assert result.economics["expense"] > 0


# ------------------------------------------------------- разовые начисления --


def test_one_time_per_completed_sed():
    model = build_economic_model(horizon=100.0)
    model.economic_items["fee"] = rate_item(
        id="fee", article="выручка", kind=EconomicKind.ONE_TIME,
        basis=EconomicBasis.SED_COMPLETED, ref="work", rate=500.0,
        direction=EconomicDirection.INCOME,
    )
    result = simulate(model, SCN, seed=11)
    completed = result.sed_stats["work"]["completed"]
    assert completed > 0
    assert result.economics["income"] == pytest.approx(completed * 500.0)
    assert result.economics["by_kind"]["one_time"]["income"] == pytest.approx(
        completed * 500.0
    )


def test_one_time_per_consumed_utility():
    model = build_economic_model(horizon=100.0)
    model.economic_items["mat"] = rate_item(
        id="mat", kind=EconomicKind.ONE_TIME, basis=EconomicBasis.UTILITY_CONSUMED,
        ref="order", rate=3.0,
    )
    result = simulate(model, SCN, seed=11)
    consumed = result.utility_totals["order"]["consumed"]
    assert result.economics["expense"] == pytest.approx(consumed * 3.0)


# --------------------------------------------------- остаточная стоимость --


def test_terminal_value_priced_at_horizon():
    model = build_economic_model(horizon=80.0)
    model.economic_items["rest"] = rate_item(
        id="rest", article="остаток", kind=EconomicKind.TERMINAL,
        basis=EconomicBasis.UTILITY_STOCK, ref="order", rate=100.0,
        direction=EconomicDirection.INCOME,
    )
    result = simulate(model, SCN, seed=11)
    stock = result.final_stocks.get("order", 0.0)
    assert result.economics["by_kind"]["terminal"]["income"] == pytest.approx(
        stock * 100.0
    )


# ------------------------------------------------------- дисконтирование --


def test_continuous_discounting_matches_closed_form():
    """Дисконтированная постоянная ставка равна r(1 − e^{−ρT})/ρ."""
    horizon, rate, annual = 100.0, 50.0, 0.2
    model = build_economic_model(horizon=horizon, discount_rate_annual=annual)
    model.economic_items["fixed"] = rate_item(
        id="fixed", basis=EconomicBasis.CONSTANT, rate=rate
    )
    result = simulate(model, SCN, seed=11)
    rho = annual / 8760.0
    expected = rate * (1 - math.exp(-rho * horizon)) / rho
    assert result.economics["discounted_expense"] == pytest.approx(expected, rel=1e-9)
    assert result.economics["npv"] == pytest.approx(-expected, rel=1e-9)
    # дисконтированная сумма меньше номинальной
    assert result.economics["discounted_expense"] < result.economics["expense"]


def test_zero_discount_equals_nominal():
    model = build_economic_model(horizon=40.0, discount_rate_annual=0.0)
    model.economic_items["fixed"] = rate_item(
        id="fixed", basis=EconomicBasis.CONSTANT, rate=5.0
    )
    result = simulate(model, SCN, seed=11)
    assert result.economics["npv"] == pytest.approx(result.economics["net"])


def test_later_income_is_discounted_more():
    """Один и тот же доход позже стоит меньше."""
    from dtb.domain import EffectKind, EventEffect
    from dtb.domain.scenario import ControlAction

    def npv_of(time: float) -> float:
        model = build_economic_model(horizon=200.0, discount_rate_annual=0.5)
        model.economic_items["bonus"] = rate_item(
            id="bonus", kind=EconomicKind.ONE_TIME,
            basis=EconomicBasis.UTILITY_PRODUCED, ref="bonus_unit", rate=1000.0,
            direction=EconomicDirection.INCOME,
        )
        model.utility_types["bonus_unit"] = model.utility_types["order"].model_copy(
            update={"id": "bonus_unit", "name": "бонус"}
        )
        scenario = SCN.model_copy(deep=True)
        scenario.control_actions = [
            ControlAction(
                id="c", time=time,
                effect=EventEffect(
                    kind=EffectKind.ADD_UTILITY, utility_type_id="bonus_unit",
                    quantity_expression="1",
                ),
            )
        ]
        return simulate(model, scenario, seed=11).economics["npv"]

    assert npv_of(10.0) > npv_of(150.0) > 0


def test_scenario_discount_rate_overrides_model():
    model = build_economic_model(horizon=50.0, discount_rate_annual=0.0)
    model.economic_items["fixed"] = rate_item(
        id="fixed", basis=EconomicBasis.CONSTANT, rate=10.0
    )
    scenario = SCN.model_copy(deep=True)
    scenario.discount_rate_annual = 1.0
    discounted = simulate(model, scenario, seed=11)
    plain = simulate(model, SCN, seed=11)
    assert discounted.economics["discount_rate_annual"] == 1.0
    assert abs(discounted.economics["npv"]) < abs(plain.economics["npv"])


# ----------------------------------------------------------------- разогрев --


def test_warmup_excludes_transient_from_statistics():
    """Разогрев отбрасывает переходный режим: статистика идёт от warmup_hours."""
    model = build_economic_model(horizon=200.0)
    model.economic_items["fee"] = rate_item(
        id="fee", kind=EconomicKind.ONE_TIME, basis=EconomicBasis.SED_COMPLETED,
        ref="work", rate=100.0, direction=EconomicDirection.INCOME,
    )
    full = simulate(model, SCN, seed=11)
    warm = SCN.model_copy(deep=True)
    warm.warmup_hours = 100.0
    after = simulate(model, warm, seed=11)
    assert after.warmup["applied"] is True
    assert after.warmup["dropped"]["instances_completed"] > 0
    assert after.final_kpis["done"] < full.final_kpis["done"]
    assert after.economics["income"] < full.economics["income"]
    assert after.economics["warmup_excluded"]["income"] > 0
    assert after.economics["measured_from"] == 100.0


def test_warmup_keeps_physical_state():
    """Разогрев обнуляет статистику, но не состояние: запасы сохраняются."""
    model = build_economic_model(horizon=120.0)
    warm = SCN.model_copy(deep=True)
    warm.warmup_hours = 60.0
    result = simulate(model, warm, seed=11)
    assert result.warmup["stocks_at_warmup"]
    # очередь и исполняющиеся СЭД не сбрасываются, только счётчики
    assert result.metrics["switching_moments"] > 0
    assert result.resource_stats["line"]["capacity_hours"] == pytest.approx(60.0)


def test_no_warmup_by_default():
    model = build_economic_model(horizon=50.0)
    result = simulate(model, SCN, seed=11)
    assert result.warmup["applied"] is False
    assert result.metrics["warmup_hours"] == 0.0


def test_warmup_is_reproducible():
    model = build_economic_model(horizon=150.0)
    warm = SCN.model_copy(deep=True)
    warm.warmup_hours = 50.0
    a = simulate(model, warm, seed=11)
    b = simulate(model, warm, seed=11)
    assert a.content_digest() == b.content_digest()


# ------------------------------------------- стоимость управления в экономике --


def test_control_costs_enter_economics():
    model = build_control_model(horizon=100.0, timer_period=50.0, rule="true")
    model.economics = EconomicsSettings(control_article="управление")
    model.control_actions["add_line"].one_time_cost = 1000.0
    result = simulate(model, SCN, seed=11)
    assert result.economics["by_article"]["управление"]["expense"] > 0
    assert result.economics["control_total"] == pytest.approx(
        result.control_costs["total"]
    )


def test_control_costs_can_be_excluded():
    model = build_control_model(horizon=100.0, timer_period=50.0, rule="true")
    model.economics = EconomicsSettings(include_control_costs=False)
    model.control_actions["add_line"].one_time_cost = 1000.0
    result = simulate(model, SCN, seed=11)
    assert "управление" not in result.economics["by_article"]
    assert result.control_costs["total"] > 0  # отдельный отчёт остаётся
    assert result.economics["control_costs_included"] is False


# ------------------------------------------------------------- валидация --


def test_economic_item_validation():
    with pytest.raises(ValueError):
        rate_item(id="x", basis=EconomicBasis.RESOURCE_CAPACITY)  # нет ref
    with pytest.raises(ValueError):
        rate_item(id="x", basis=EconomicBasis.EXPRESSION)  # нет expression
    with pytest.raises(ValueError):
        rate_item(
            id="x", kind=EconomicKind.RATE, basis=EconomicBasis.SED_COMPLETED, ref="w"
        )  # событийная база в ставке
    with pytest.raises(ValueError):
        rate_item(
            id="x", kind=EconomicKind.ONE_TIME, basis=EconomicBasis.CONSTANT
        )  # непрерывная база в разовом начислении


def test_expression_basis_reads_state():
    model = build_economic_model(horizon=40.0)
    model.economic_items["flex"] = rate_item(
        id="flex", basis=EconomicBasis.EXPRESSION,
        expression="waiting_count('work') + 1", rate=2.0,
    )
    result = simulate(model, SCN, seed=11)
    assert result.economics["expense"] >= 40.0 * 2.0
