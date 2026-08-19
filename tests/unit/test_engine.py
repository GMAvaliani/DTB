"""Simulation engine semantics: the МП loop, SED lifecycle, effects, Mode B."""

import pytest

from dtb.domain import (
    ControlAction,
    DurationKind,
    DurationModel,
    EffectKind,
    Enterprise,
    EnterpriseModel,
    EventEffect,
    EventKind,
    ExternalModelRef,
    InjectedEvent,
    OutcomeBranch,
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
from dtb.simulation import (
    CallableAdapter,
    ExternalModelError,
    SimulationEngine,
    default_registry,
    simulate,
)
from tests.conftest import build_small_model


def bare_model(horizon=100.0) -> EnterpriseModel:
    m = EnterpriseModel(enterprise=Enterprise(id="t", name="T", planning_horizon_hours=horizon))
    for uid in ("a", "b", "c"):
        m.utility_types[uid] = UtilityType(id=uid, name=uid)
    return m


def scn(**kw) -> Scenario:
    return Scenario(id="s", name="s", enterprise_id="t", **kw)


def test_consume_at_start_produce_at_completion():
    m = bare_model()
    m.seds["work"] = SEDDefinition(
        id="work", name="work",
        inputs=[UtilityFlow(utility_type_id="a", quantity=2)],
        outputs=[UtilityFlow(utility_type_id="b", quantity=1)],
        duration=DurationModel(kind=DurationKind.FIXED, value=10),
        triggers=[TriggerSpec(kind=TriggerKind.AT_TIME, at=5.0)],
    )
    m.initial_state.utility_stocks = {"a": 2}
    r = simulate(m, scn(), seed=1)
    assert r.status == "completed"
    assert r.final_stocks["a"] == 0
    assert r.final_stocks["b"] == 1
    log_types = [(e["t"], e["type"]) for e in r.event_log]
    assert (5.0, "sed_started") in log_types
    assert (15.0, "sed_completed") in log_types


def test_waiting_until_inputs_available():
    m = bare_model()
    m.seds["work"] = SEDDefinition(
        id="work", name="work",
        inputs=[UtilityFlow(utility_type_id="a", quantity=1)],
        outputs=[UtilityFlow(utility_type_id="b", quantity=1)],
        duration=DurationModel(kind=DurationKind.FIXED, value=1),
        triggers=[TriggerSpec(kind=TriggerKind.AT_TIME, at=0.0)],
    )
    m.event_kinds["supply"] = EventKind(
        id="supply", name="supply",
        stream=StreamSpec(kind=StreamKind.SCHEDULED, times=[30.0]),
        effects=[EventEffect(kind=EffectKind.ADD_UTILITY, utility_type_id="a",
                             quantity_expression="1")],
    )
    r = simulate(m, scn(), seed=1)
    started = [e for e in r.event_log if e["type"] == "sed_started"]
    assert started and started[0]["t"] == 30.0
    assert r.sed_stats["work"]["waiting_hours"] == pytest.approx(30.0)
    assert r.sed_stats["work"]["blocked_hours"].get("utility:a") == pytest.approx(30.0)


def test_resource_capacity_limits_concurrency():
    m = bare_model()
    m.resources["r"] = Resource(id="r", name="r", capacity=1)
    m.seds["work"] = SEDDefinition(
        id="work", name="work",
        outputs=[UtilityFlow(utility_type_id="b", quantity=1)],
        resources=[ResourceRequirement(resource_id="r", quantity=1)],
        duration=DurationModel(kind=DurationKind.FIXED, value=10),
        triggers=[TriggerSpec(kind=TriggerKind.AT_TIME, at=0.0, count_expression="3")],
    )
    r = simulate(m, scn(), seed=1)
    # capacity 1 → three sequential executions: completions at 10, 20, 30
    completions = [e["t"] for e in r.event_log if e["type"] == "sed_completed"]
    assert completions == [10.0, 20.0, 30.0]
    assert r.final_stocks["b"] == 3


def test_suspension_on_capacity_loss_and_resume():
    m = bare_model(horizon=200)
    m.resources["r"] = Resource(id="r", name="r", capacity=1)
    m.seds["work"] = SEDDefinition(
        id="work", name="work",
        outputs=[UtilityFlow(utility_type_id="b", quantity=1)],
        resources=[ResourceRequirement(resource_id="r", quantity=1)],
        duration=DurationModel(kind=DurationKind.FIXED, value=50),
        triggers=[TriggerSpec(kind=TriggerKind.AT_TIME, at=0.0)],
    )
    m.event_kinds["failure"] = EventKind(
        id="failure", name="failure",
        stream=StreamSpec(kind=StreamKind.SCHEDULED, times=[20.0]),
        effects=[EventEffect(kind=EffectKind.RESOURCE_CAPACITY_DELTA,
                             resource_id="r", delta=-1, duration=30.0)],
    )
    r = simulate(m, scn(), seed=1)
    suspended = [e for e in r.event_log if e["type"] == "sed_suspended"]
    resumed = [e for e in r.event_log if e["type"] == "sed_resumed"]
    completed = [e for e in r.event_log if e["type"] == "sed_completed"]
    assert suspended and suspended[0]["t"] == 20.0
    assert resumed and resumed[0]["t"] == 50.0  # capacity restored at 20+30
    # 20h done before suspension, 30h remaining after resume → completes at 80
    assert completed and completed[0]["t"] == pytest.approx(80.0)
    assert r.sed_stats["work"]["suspensions"] == 1


def test_outcome_branch_probabilities():
    m = bare_model(horizon=5000)
    m.seds["qc"] = SEDDefinition(
        id="qc", name="qc",
        outcome_branches=[
            OutcomeBranch(label="pass", probability=0.8,
                          outputs=[UtilityFlow(utility_type_id="b", quantity=1)]),
            OutcomeBranch(label="fail", probability=0.2,
                          outputs=[UtilityFlow(utility_type_id="c", quantity=1)]),
        ],
        duration=DurationModel(kind=DurationKind.FIXED, value=1),
        triggers=[TriggerSpec(kind=TriggerKind.RECURRING, interval=1.0)],
    )
    r = simulate(m, scn(horizon_hours=4000), seed=123)
    passes = r.final_stocks.get("b", 0)
    fails = r.final_stocks.get("c", 0)
    total = passes + fails
    assert total > 3000
    assert passes / total == pytest.approx(0.8, abs=0.03)


def test_chained_trigger_with_condition_on_branch():
    m = bare_model()
    m.seds["qc"] = SEDDefinition(
        id="qc", name="qc",
        outcome_branches=[
            OutcomeBranch(label="pass", probability=1.0,
                          outputs=[UtilityFlow(utility_type_id="b", quantity=1)]),
        ],
        duration=DurationModel(kind=DurationKind.FIXED, value=1),
        triggers=[TriggerSpec(kind=TriggerKind.AT_TIME, at=0.0)],
    )
    m.seds["ship"] = SEDDefinition(
        id="ship", name="ship",
        inputs=[UtilityFlow(utility_type_id="b", quantity=1)],
        outputs=[UtilityFlow(utility_type_id="c", quantity=1)],
        duration=DurationModel(kind=DurationKind.FIXED, value=1),
        triggers=[TriggerSpec(kind=TriggerKind.ON_SED_COMPLETED, sed_id="qc",
                              condition="branch == 'pass'")],
    )
    m.seds["scrap"] = SEDDefinition(
        id="scrap", name="scrap",
        duration=DurationModel(kind=DurationKind.FIXED, value=1),
        triggers=[TriggerSpec(kind=TriggerKind.ON_SED_COMPLETED, sed_id="qc",
                              condition="branch == 'fail'")],
    )
    r = simulate(m, scn(), seed=1)
    assert r.sed_stats["ship"]["completed"] == 1
    assert r.sed_stats["scrap"]["created"] == 0


def test_injected_events_and_payload_expressions():
    m = bare_model()
    m.event_kinds["order"] = EventKind(
        id="order", name="order",
        stream=StreamSpec(kind=StreamKind.NONE),
        payload_defaults={"quantity": 1},
        effects=[EventEffect(kind=EffectKind.ADD_UTILITY, utility_type_id="a",
                             quantity_expression="quantity * 2")],
    )
    scenario = scn(injected_events=[
        InjectedEvent(time=10.0, event_kind_id="order", payload={"quantity": 3}),
        InjectedEvent(time=20.0, event_kind_id="order"),
    ])
    r = simulate(m, scenario, seed=1)
    assert r.final_stocks["a"] == 3 * 2 + 1 * 2
    assert r.metrics["np_events"] == 2


def test_control_action_changes_poisson_rate_mid_run():
    m = bare_model(horizon=2000)
    m.event_kinds["order"] = EventKind(
        id="order", name="order",
        stream=StreamSpec(kind=StreamKind.POISSON, rate_per_hour=0.01),
        effects=[EventEffect(kind=EffectKind.ADD_UTILITY, utility_type_id="a",
                             quantity_expression="1")],
    )
    scenario = scn(
        horizon_hours=2000,
        control_actions=[ControlAction(
            time=1000.0,
            effect=EventEffect(kind=EffectKind.SET_PARAMETER,
                               parameter_path="event_kind:order.stream.rate_per_hour",
                               value=1.0),
        )],
    )
    r = simulate(m, scenario, seed=5)
    first_half = sum(1 for e in r.event_log if e["type"] == "np_event" and e["t"] <= 1000)
    # ~10 events expected before the boost; ~1000 after (log caps at 5000 entries)
    assert first_half < 50
    assert r.metrics["np_events"] > 500


def test_max_concurrent_and_priority():
    m = bare_model()
    m.resources["r"] = Resource(id="r", name="r", capacity=10)
    m.seds["low"] = SEDDefinition(
        id="low", name="low", priority=200,
        inputs=[UtilityFlow(utility_type_id="a", quantity=1)],
        outputs=[UtilityFlow(utility_type_id="b", quantity=1)],
        duration=DurationModel(kind=DurationKind.FIXED, value=1),
        triggers=[TriggerSpec(kind=TriggerKind.AT_TIME, at=0.0)],
    )
    m.seds["high"] = SEDDefinition(
        id="high", name="high", priority=1,
        inputs=[UtilityFlow(utility_type_id="a", quantity=1)],
        outputs=[UtilityFlow(utility_type_id="c", quantity=1)],
        duration=DurationModel(kind=DurationKind.FIXED, value=1),
        triggers=[TriggerSpec(kind=TriggerKind.AT_TIME, at=0.0)],
    )
    m.initial_state.utility_stocks = {"a": 1}  # only one can start
    r = simulate(m, scn(), seed=1)
    assert r.final_stocks.get("c") == 1  # high priority won the single input
    assert r.sed_stats["low"]["completed"] == 0


def test_mode_b_external_model_via_callable_adapter():
    m = bare_model()
    m.seds["ext"] = SEDDefinition(
        id="ext", name="ext",
        inputs=[UtilityFlow(utility_type_id="a", quantity=1)],
        duration=DurationModel(kind=DurationKind.FIXED, value=999),  # ignored in Mode B
        external_model=ExternalModelRef(uri="callable:machine", adapter="callable"),
        triggers=[TriggerSpec(kind=TriggerKind.AT_TIME, at=0.0)],
    )
    m.initial_state.utility_stocks = {"a": 1}
    registry = default_registry()
    adapter: CallableAdapter = registry.get("callable")
    calls = []

    def machine(request):
        calls.append(request)
        return {"duration": 7.5,
                "outputs": [{"utility_type_id": "b", "quantity": 4}],
                "branch_label": "external_ok"}

    adapter.register("machine", machine)
    r = simulate(m, scn(), seed=9, registry=registry)
    assert r.status == "completed"
    assert r.final_stocks["b"] == 4
    completed = [e for e in r.event_log if e["type"] == "sed_completed"]
    assert completed[0]["t"] == 7.5
    assert calls[0]["seed"] is not None  # engine passes a derived seed for determinism


def test_mode_b_failure_fails_run_honestly():
    m = bare_model()
    m.seds["ext"] = SEDDefinition(
        id="ext", name="ext",
        external_model=ExternalModelRef(uri="callable:missing", adapter="callable"),
        triggers=[TriggerSpec(kind=TriggerKind.AT_TIME, at=0.0)],
    )
    r = simulate(m, scn(), seed=1)
    assert r.status == "failed"
    assert "not registered" in (r.error or "")


def test_cancel_effect_cancels_waiting_instances():
    m = bare_model()
    m.seds["work"] = SEDDefinition(
        id="work", name="work",
        inputs=[UtilityFlow(utility_type_id="a", quantity=1)],  # never available
        duration=DurationModel(kind=DurationKind.FIXED, value=1),
        triggers=[TriggerSpec(kind=TriggerKind.AT_TIME, at=0.0, count_expression="4")],
    )
    m.event_kinds["stop"] = EventKind(
        id="stop", name="stop",
        stream=StreamSpec(kind=StreamKind.SCHEDULED, times=[10.0]),
        effects=[EventEffect(kind=EffectKind.CANCEL_SED_INSTANCES, sed_id="work")],
    )
    r = simulate(m, scn(), seed=1)
    assert r.sed_stats["work"]["cancelled"] == 4
    assert r.metrics["instances_waiting"] == 0


def test_goal_achievement_with_deadline(small_model, base_scenario):
    small_model.goals["g1"].target_metrics[0].deadline_hours = 1.0  # unreachably early
    r = simulate(small_model, base_scenario, seed=42)
    assert r.goal_achievement["g1"]["achieved"] is False
    small_model.goals["g1"].target_metrics[0].deadline_hours = None
    r2 = simulate(small_model, base_scenario, seed=42)
    assert r2.goal_achievement["g1"]["achieved"] is True


def test_pause_cancel_controls(small_model, base_scenario):
    from dtb.simulation.engine import SimControl

    control = SimControl()
    control.cancel()
    r = simulate(small_model, base_scenario, seed=42, control=control)
    assert r.status == "cancelled"


def test_provenance_recorded(small_model, base_scenario):
    r = simulate(small_model, base_scenario, seed=42)
    p = r.provenance
    assert p.seed == 42
    assert p.model_hash == small_model.content_hash()
    assert p.scenario_id == "base"
    assert p.horizon_hours == 240.0
    assert p.engine_version
    assert p.started_at
