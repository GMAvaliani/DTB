"""NP-event stream samplers.

Each event kind owns one sampler that lazily yields its next occurrence time.
Poisson streams (простейший поток) are sampled in-engine from a per-stream
substream RNG; scheduled streams follow explicit times / regular intervals;
external streams delegate to an adapter; injected occurrences come from the
scenario. Determinism: substream RNG derived from (master seed, kind id).
"""

from __future__ import annotations

import random
from typing import Any

from dtb.domain.events import StreamKind, StreamSpec
from dtb.domain.seds import ExternalModelRef
from dtb.simulation.adapters import AdapterRegistry, ExternalModelError


class StreamSampler:
    """Base: cache-one-occurrence-ahead sampler."""

    def __init__(self, kind_id: str):
        self.kind_id = kind_id
        self._next: tuple[float, dict[str, Any]] | None = None
        self.exhausted = False

    def peek(self, now: float) -> tuple[float, dict[str, Any]] | None:
        """Next occurrence at time >= now, or None if the stream is exhausted."""
        while not self.exhausted and (self._next is None or self._next[0] < now - 1e-12):
            self._next = self._sample_after(self._next[0] if self._next else now, now)
            if self._next is None:
                self.exhausted = True
        return None if self.exhausted else self._next

    def pop(self) -> tuple[float, dict[str, Any]]:
        assert self._next is not None
        occ = self._next
        self._next = None
        return occ

    def invalidate(self) -> None:
        """Drop the cached occurrence (e.g. after a runtime rate change)."""
        self._next = None
        self.exhausted = False

    def _sample_after(
        self, last: float, now: float
    ) -> tuple[float, dict[str, Any]] | None:
        raise NotImplementedError


class PoissonSampler(StreamSampler):
    def __init__(self, kind_id: str, rate_per_hour: float, rng: random.Random):
        super().__init__(kind_id)
        self.rate = rate_per_hour
        self.rng = rng

    def _sample_after(self, last: float, now: float) -> tuple[float, dict[str, Any]] | None:
        if self.rate <= 0:
            return None
        base = max(last, now)
        return (base + self.rng.expovariate(self.rate), {})


class ScheduledSampler(StreamSampler):
    def __init__(self, kind_id: str, spec: StreamSpec, horizon: float):
        super().__init__(kind_id)
        times = sorted(t for t in spec.times if 0 <= t <= horizon)
        if spec.interval:
            t = spec.start
            while t <= horizon:
                times.append(t)
                t += spec.interval
        self.times = sorted(set(round(t, 9) for t in times))
        self.index = 0

    def _sample_after(self, last: float, now: float) -> tuple[float, dict[str, Any]] | None:
        while self.index < len(self.times) and self.times[self.index] < now - 1e-12:
            self.index += 1
        if self.index >= len(self.times):
            return None
        t = self.times[self.index]
        self.index += 1
        return (t, {})


class InjectedSampler(StreamSampler):
    """Scenario-injected occurrences of one event kind."""

    def __init__(self, kind_id: str, occurrences: list[tuple[float, dict[str, Any]]]):
        super().__init__(kind_id)
        self.queue = sorted(occurrences, key=lambda o: o[0])
        self.index = 0

    def _sample_after(self, last: float, now: float) -> tuple[float, dict[str, Any]] | None:
        if self.index >= len(self.queue):
            return None
        occ = self.queue[self.index]
        self.index += 1
        return occ


class ExternalSampler(StreamSampler):
    """Stream produced by an external model via adapter (Mode B for events)."""

    def __init__(
        self,
        kind_id: str,
        ref: ExternalModelRef,
        registry: AdapterRegistry,
        seed: int,
    ):
        super().__init__(kind_id)
        self.ref = ref
        self.registry = registry
        self.seed = seed
        self._calls = 0

    def _sample_after(self, last: float, now: float) -> tuple[float, dict[str, Any]] | None:
        adapter = self.registry.get(self.ref.adapter)
        self._calls += 1
        resp = adapter.next_event(
            self.ref,
            {
                "event_kind_id": self.kind_id,
                "time": max(last, now),
                "seed": self.seed,
                "call": self._calls,
            },
        )
        t = resp.get("time")
        if t is None:
            return None
        if not isinstance(t, (int, float)) or t < now - 1e-9:
            raise ExternalModelError(
                f"external stream {self.kind_id}: invalid next time {t!r}"
            )
        payload = resp.get("payload") or {}
        if not isinstance(payload, dict):
            raise ExternalModelError(f"external stream {self.kind_id}: payload must be object")
        return (float(t), payload)


def build_sampler(
    kind_id: str,
    spec: StreamSpec,
    horizon: float,
    rng: random.Random,
    registry: AdapterRegistry,
    seed: int,
) -> StreamSampler | None:
    if spec.kind == StreamKind.POISSON:
        return PoissonSampler(kind_id, spec.rate_per_hour or 0.0, rng)
    if spec.kind == StreamKind.SCHEDULED:
        return ScheduledSampler(kind_id, spec, horizon)
    if spec.kind == StreamKind.EXTERNAL:
        assert spec.external is not None
        return ExternalSampler(kind_id, spec.external, registry, seed)
    return None  # StreamKind.NONE
