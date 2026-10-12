"""Reproducibility invariant (acceptance criterion 20)."""

from dtb.analysis import run_monte_carlo
from dtb.simulation import simulate
from tests.conftest import build_small_model


def test_same_seed_same_digest(small_model, base_scenario):
    r1 = simulate(small_model, base_scenario, seed=42)
    r2 = simulate(small_model, base_scenario, seed=42)
    assert r1.content_digest() == r2.content_digest()


def test_different_seed_different_result(small_model, base_scenario):
    r1 = simulate(small_model, base_scenario, seed=42)
    r2 = simulate(small_model, base_scenario, seed=43)
    assert r1.content_digest() != r2.content_digest()


def test_rebuilt_model_same_digest(base_scenario):
    """A model rebuilt from scratch (or restored from snapshot) replays identically."""
    r1 = simulate(build_small_model(), base_scenario, seed=7)
    r2 = simulate(build_small_model(), base_scenario, seed=7)
    assert r1.content_digest() == r2.content_digest()


def test_monte_carlo_reproducible(small_model, base_scenario):
    mc1 = run_monte_carlo(small_model, base_scenario, runs=5, master_seed=11)
    mc2 = run_monte_carlo(small_model, base_scenario, runs=5, master_seed=11)
    assert mc1.run_digests == mc2.run_digests
    assert mc1.final_kpis == mc2.final_kpis
    # runs differ among themselves (independent substreams)
    assert len(set(mc1.run_digests)) == 5


def test_demo_model_deterministic():
    from dtb.demo import create_demo

    model, scenarios = create_demo()
    base = scenarios[0]
    r1 = simulate(model, base, seed=base.seed)
    r2 = simulate(model, base, seed=base.seed)
    assert r1.content_digest() == r2.content_digest()
    assert r1.status == "completed"


def test_structurally_identical_models_hash_the_same():
    """Служебная отметка времени не влияет на хеш содержания модели.

    Иначе две одинаковые модели, созданные в разные секунды, давали бы разные
    provenance, и воспроизводимость нельзя было бы проверить сравнением.
    """
    first = build_small_model()
    second = build_small_model()
    second.enterprise.created_at = "2000-01-01T00:00:00+00:00"
    assert first.content_hash() == second.content_hash()


def test_model_hash_still_reacts_to_content():
    first = build_small_model()
    second = build_small_model()
    second.resources["line"].capacity = 99
    assert first.content_hash() != second.content_hash()
