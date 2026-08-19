"""Deterministic seed derivation.

Python's builtin ``hash`` of strings is salted per process, so substream seeds
are derived with SHA-256 instead: identical (master seed, path) always yields
the identical substream on any machine/process — the foundation of reproducible
simulation (engineering rule 6 / acceptance criterion 20).
"""

from __future__ import annotations

import hashlib
import random


def derive_seed(master_seed: int, *path: str | int) -> int:
    material = f"{master_seed}|" + "|".join(str(p) for p in path)
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def substream(master_seed: int, *path: str | int) -> random.Random:
    return random.Random(derive_seed(master_seed, *path))
