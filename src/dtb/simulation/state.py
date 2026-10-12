"""Runtime simulation state — plain Python for the hot loop (no pydantic here).

Holds utility stocks, SED instance pools, resource capacities/allocations and
the counters that KPI expressions read. Definition-level objects stay immutable;
everything mutable during a run lives in this module.
"""

from __future__ import annotations

from typing import Any, Callable

from dtb.domain.seds import SEDStatus


class SEDInstance:
    """One agent: a concrete occurrence of a SED definition."""

    __slots__ = (
        "instance_id",
        "seq",
        "sed_id",
        "status",
        "created_at",
        "payload",
        "started_at",
        "completion_time",
        "remaining_duration",
        "sampled_duration",
        "allocated",
        "inputs_consumed",
        "branch_label",
        "external_outputs",
        "waiting_since",
        "total_waiting",
        "block_reason",
        "suspend_count",
        "trigger_source",
    )

    def __init__(
        self,
        instance_id: str,
        seq: int,
        sed_id: str,
        created_at: float,
        payload: dict[str, Any] | None = None,
        trigger_source: str = "",
    ):
        self.instance_id = instance_id
        self.seq = seq  # global creation sequence — deterministic ordering
        self.sed_id = sed_id
        self.status = SEDStatus.WAITING
        self.created_at = created_at
        self.payload = payload or {}
        self.started_at: float | None = None
        self.completion_time: float | None = None
        self.remaining_duration: float | None = None  # set when suspended
        self.sampled_duration: float | None = None
        self.allocated: dict[str, float] = {}
        self.inputs_consumed = False
        self.branch_label: str | None = None
        self.external_outputs: list[dict[str, Any]] | None = None
        self.waiting_since: float = created_at
        self.total_waiting: float = 0.0
        self.block_reason: str | None = None
        self.suspend_count = 0
        self.trigger_source = trigger_source

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "sed_id": self.sed_id,
            "status": self.status.value,
            "created_at": round(self.created_at, 6),
            "started_at": self.started_at,
            "completion_time": self.completion_time,
            "block_reason": self.block_reason,
            "suspend_count": self.suspend_count,
            "trigger_source": self.trigger_source,
        }


class ResourceState:
    """Capacity pool: base capacity ± temporary event deltas, minus allocations."""

    __slots__ = (
        "resource_id",
        "base_capacity",
        "allocated",
        "temp_deltas",
        "busy_integral",
        "capacity_integral",
        "peak_allocated",
    )

    def __init__(self, resource_id: str, capacity: float):
        self.resource_id = resource_id
        self.base_capacity = capacity
        self.allocated = 0.0
        self.temp_deltas: list[tuple[float, float]] = []  # (expire_time, delta)
        self.busy_integral = 0.0
        self.capacity_integral = 0.0
        self.peak_allocated = 0.0

    def capacity(self, t: float) -> float:
        return max(
            0.0,
            self.base_capacity + sum(d for exp, d in self.temp_deltas if exp > t),
        )

    def available(self, t: float) -> float:
        return self.capacity(t) - self.allocated

    def next_delta_expiry(self, t: float) -> float | None:
        expiries = [exp for exp, _ in self.temp_deltas if exp > t]
        return min(expiries) if expiries else None

    def expire_deltas(self, t: float) -> None:
        self.temp_deltas = [(exp, d) for exp, d in self.temp_deltas if exp > t]

    def accumulate(self, dt: float, t: float) -> None:
        self.busy_integral += self.allocated * dt
        self.capacity_integral += self.capacity(t) * dt
        self.peak_allocated = max(self.peak_allocated, self.allocated)

    def utilization(self) -> float:
        if self.capacity_integral <= 0:
            return 0.0
        return self.busy_integral / self.capacity_integral


class SimulationState:
    """Everything mutable during one run; also the KPI evaluation source."""

    def __init__(self) -> None:
        self.time = 0.0
        self.stocks: dict[str, float] = {}
        self.resources: dict[str, ResourceState] = {}
        self.waiting: list[SEDInstance] = []
        self.executing: dict[str, SEDInstance] = {}
        self.instances: dict[str, SEDInstance] = {}  # all, by id
        # counters
        self.produced: dict[str, float] = {}
        self.consumed: dict[str, float] = {}
        self.started_count: dict[str, int] = {}
        self.completed_count: dict[str, int] = {}
        self.cancelled_count: dict[str, int] = {}
        self.failed_count: dict[str, int] = {}
        self.event_counts: dict[str, int] = {}
        self.last_event_time: dict[str, float] = {}  # для сторожевых условий
        # bottleneck accounting: waiting hours per (sed_id, cause)
        self.blocked_hours: dict[tuple[str, str], float] = {}
        self.queue_peak: dict[str, int] = {}
        self._seq = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # -- stocks -------------------------------------------------------------------

    def stock(self, utility_type_id: str) -> float:
        return self.stocks.get(utility_type_id, 0.0)

    def add_stock(self, utility_type_id: str, qty: float) -> None:
        self.stocks[utility_type_id] = self.stock(utility_type_id) + qty
        if qty >= 0:
            self.produced[utility_type_id] = self.produced.get(utility_type_id, 0.0) + qty
        else:
            self.consumed[utility_type_id] = self.consumed.get(utility_type_id, 0.0) - qty

    def take_stock(self, utility_type_id: str, qty: float) -> bool:
        if self.stock(utility_type_id) + 1e-9 < qty:
            return False
        self.stocks[utility_type_id] = self.stock(utility_type_id) - qty
        self.consumed[utility_type_id] = self.consumed.get(utility_type_id, 0.0) + qty
        return True

    def seed_stock(self, utility_type_id: str, qty: float) -> None:
        """Initial state load — not counted as production."""
        self.stocks[utility_type_id] = qty

    # -- time accounting ----------------------------------------------------------

    def accumulate(self, dt: float) -> None:
        """Advance integrals over [time, time+dt) — call before moving the clock."""
        if dt <= 0:
            return
        for res in self.resources.values():
            res.accumulate(dt, self.time)
        queue_by_sed: dict[str, int] = {}
        for inst in self.waiting:
            inst.total_waiting += dt
            cause = inst.block_reason or "not_yet_checked"
            key = (inst.sed_id, cause)
            self.blocked_hours[key] = self.blocked_hours.get(key, 0.0) + dt
            queue_by_sed[inst.sed_id] = queue_by_sed.get(inst.sed_id, 0) + 1
        for sed_id, n in queue_by_sed.items():
            self.queue_peak[sed_id] = max(self.queue_peak.get(sed_id, 0), n)

    def reset_statistics(self) -> None:
        """Удалить переходный режим: счётчики и интегралы — с нуля.

        Физическое состояние (запасы, пулы СЭД, ёмкости ресурсов) сохраняется:
        обнуляется только статистика, чтобы итог прогона измерял стационарный
        режим. Вызывается один раз, в момент окончания разогрева.
        """
        self.produced.clear()
        self.consumed.clear()
        self.started_count.clear()
        self.completed_count.clear()
        self.cancelled_count.clear()
        self.failed_count.clear()
        self.event_counts.clear()
        self.blocked_hours.clear()
        self.queue_peak.clear()
        for res in self.resources.values():
            res.busy_integral = 0.0
            res.capacity_integral = 0.0
            res.peak_allocated = res.allocated
        for inst in self.instances.values():
            inst.total_waiting = 0.0
            inst.suspend_count = 0

    # -- KPI evaluation context -----------------------------------------------------

    def kpi_context(self) -> dict[str, Callable[..., float] | float]:
        return {
            "stock": lambda u: self.stock(u),
            "produced": lambda u: self.produced.get(u, 0.0),
            "consumed": lambda u: self.consumed.get(u, 0.0),
            "completed": lambda s: float(self.completed_count.get(s, 0)),
            "started": lambda s: float(self.started_count.get(s, 0)),
            "events": lambda k: float(self.event_counts.get(k, 0)),
            "time_since_event": lambda k: (
                self.time - self.last_event_time[k]
                if k in self.last_event_time
                else self.time
            ),
            "utilization": lambda r: (
                self.resources[r].utilization() if r in self.resources else 0.0
            ),
            "waiting_count": lambda s: float(
                sum(1 for i in self.waiting if i.sed_id == s)
            ),
            "executing_count": lambda s: float(
                sum(1 for i in self.executing.values() if i.sed_id == s)
            ),
            "t": lambda: self.time,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "time": round(self.time, 6),
            "stocks": {k: round(v, 6) for k, v in self.stocks.items()},
            "waiting": len(self.waiting),
            "executing": len(self.executing),
            "resource_allocated": {
                rid: r.allocated for rid, r in self.resources.items()
            },
        }
