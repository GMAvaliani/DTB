from dtb.common.seeding import derive_seed, substream


def test_derivation_is_stable():
    assert derive_seed(42, "stream", "orders") == derive_seed(42, "stream", "orders")
    assert derive_seed(42, "stream", "orders") != derive_seed(42, "stream", "failures")
    assert derive_seed(42, "a") != derive_seed(43, "a")


def test_substreams_are_independent_and_reproducible():
    a1 = [substream(7, "x").random() for _ in range(3)]
    a2 = [substream(7, "x").random() for _ in range(3)]
    b = [substream(7, "y").random() for _ in range(3)]
    assert a1 == a2
    assert a1 != b
