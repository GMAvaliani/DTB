"""Фаза 4: инварианты, проверяемые на данных конкретного прогона."""

import pytest

from dtb.demo import create_demo
from dtb.domain import Scenario
from dtb.simulation import simulate
from dtb.verification import (
    check_little_law,
    check_utility_balance,
    verify_run,
)
from tests.conftest import build_small_model

pytestmark = pytest.mark.verification


def test_demo_run_passes_all_invariants():
    model, scenarios = create_demo()
    result = simulate(model, scenarios[0], seed=20250819)
    report = verify_run(result, model)
    failed = [c.to_dict() for c in report.checks if not c.passed]
    assert not failed, failed
    assert report.to_dict()["total"] > 10
    assert report.context["content_digest"]


def test_utility_balance_detects_tampering():
    """Проверка не декоративна: искажённый результат её не проходит."""
    model = build_small_model()
    scenario = Scenario(id="s", name="s", enterprise_id="acme", seed=42)
    result = simulate(model, scenario, seed=42)
    assert all(c.passed for c in check_utility_balance(result, model))
    result.utility_totals["money"]["final_stock"] += 1000.0
    assert any(
        not c.passed and c.scope == "money"
        for c in check_utility_balance(result, model)
    )


def test_balance_check_skipped_for_warmup_runs():
    model = build_small_model()
    scenario = Scenario(id="s", name="s", enterprise_id="acme", seed=42, warmup_hours=50.0)
    result = simulate(model, scenario, seed=42)
    checks = check_utility_balance(result, model)
    assert len(checks) == 1 and checks[0].passed
    assert "разогрев" in checks[0].detail


def test_little_law_check_skips_short_runs():
    """При малом числе завершений проверка не делает выводов."""
    model = build_small_model(horizon=48.0)
    scenario = Scenario(id="s", name="s", enterprise_id="acme", seed=42)
    result = simulate(model, scenario, seed=42)
    assert check_little_law(result) == []


def test_resource_and_economics_checks_on_demo():
    model, scenarios = create_demo()
    steady = next(s for s in scenarios if s.id == "demo_steady_state")
    result = simulate(model, steady, seed=20250819)
    report = verify_run(result, model)
    ids = {c.id for c in report.checks}
    assert "economics_articles_sum" in ids
    assert "economics_kinds_sum" in ids
    assert "control_costs_sum" in ids
    assert any(i.startswith("resource_utilization:") for i in ids)
    assert report.passed
