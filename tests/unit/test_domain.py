import pytest
from pydantic import ValidationError

from dtb.domain import (
    DurationKind,
    DurationModel,
    EnterpriseModel,
    Scenario,
    SEDDefinition,
    SEDOverride,
    OutcomeBranch,
    TriggerKind,
    TriggerSpec,
    UtilityFlow,
)
from dtb.domain.model import apply_scenario_overrides
from tests.conftest import build_small_model


def test_snapshot_roundtrip_preserves_hash(small_model):
    snapshot = small_model.to_snapshot()
    restored = EnterpriseModel.from_snapshot(snapshot)
    assert restored.content_hash() == small_model.content_hash()


def test_duration_model_requires_params():
    with pytest.raises(ValidationError):
        DurationModel(kind=DurationKind.UNIFORM, low=1)  # high missing
    with pytest.raises(ValidationError):
        DurationModel(kind=DurationKind.NORMAL, mean=5)  # std missing


def test_trigger_requires_reference():
    with pytest.raises(ValidationError):
        TriggerSpec(kind=TriggerKind.ON_EVENT)
    with pytest.raises(ValidationError):
        TriggerSpec(kind=TriggerKind.RECURRING)


def test_branch_probabilities_cannot_exceed_one():
    with pytest.raises(ValidationError):
        SEDDefinition(
            id="x", name="x",
            outcome_branches=[
                OutcomeBranch(probability=0.7, outputs=[]),
                OutcomeBranch(probability=0.6, outputs=[]),
            ],
        )


def test_effective_branches_fill_missing_probability():
    sed = SEDDefinition(
        id="x", name="x",
        outcome_branches=[OutcomeBranch(label="ok", probability=0.7,
                                        outputs=[UtilityFlow(utility_type_id="u", quantity=1)])],
    )
    branches = sed.effective_branches()
    assert len(branches) == 2
    assert abs(sum(b.probability for b in branches) - 1.0) < 1e-9
    assert branches[1].label == "no_output"


def test_scenario_overrides_do_not_mutate_base():
    model = build_small_model()
    scenario = Scenario(
        id="s", name="s", enterprise_id="acme",
        sed_overrides={"production": SEDOverride(duration_scale=2.0)},
        resource_capacity_overrides={"line": 5},
        initial_stock_overrides={"material": 10},
        event_rate_multipliers={"customer_order": 0.5},
        parameter_overrides={"sed:sale.priority": 5},
    )
    derived = apply_scenario_overrides(model, scenario)
    assert derived.seds["production"].duration.low == 8
    assert model.seds["production"].duration.low == 4
    assert derived.resources["line"].capacity == 5
    assert derived.initial_state.utility_stocks["material"] == 10
    assert derived.event_kinds["customer_order"].stream.rate_per_hour == 0.1
    assert derived.seds["sale"].priority == 5
    assert model.seds["sale"].priority == 100


def test_zero_rate_multiplier_disables_stream():
    model = build_small_model()
    scenario = Scenario(id="s", name="s", enterprise_id="acme",
                        event_rate_multipliers={"customer_order": 0.0})
    derived = apply_scenario_overrides(model, scenario)
    assert derived.event_kinds["customer_order"].stream.kind.value == "none"


def test_entity_collections_api(small_model):
    assert small_model.get_entity("sed", "production") is not None
    assert small_model.get_entity("sed", "missing") is None
    with pytest.raises(KeyError):
        small_model.collection("nope")
    assert {etype for etype, _ in small_model.iter_entities()} >= {"sed", "goal", "kpi"}
