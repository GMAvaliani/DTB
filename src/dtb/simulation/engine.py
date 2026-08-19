"""Event-driven continuous-time simulation engine (ADR-0003).

Implements the switching-moment (МП) loop of the source specification:

1. for every NP-event kind, sample the next occurrence of its stream;
2. take the earliest completion among executing SEDs;
3. the next МП is the minimum of those (plus scheduled SED triggers, timed
   control actions and temporary-capacity expiries);
4. advance model time to the МП and process every occurrence at that instant in
   a deterministic total order;
5. spawn newly triggered SEDs into the waiting pool and start every waiting SED
   whose input utilities and resource capacities are available (inputs are
   consumed at start, outputs are produced at completion);
6. re-check continuation of executing SEDs — those invalidated by events are
   suspended back to the waiting pool with their remaining duration;
7. repeat until the horizon.

Deterministic order of simultaneous occurrences:
capacity-delta expiries → SED completions (by creation seq) → NP-events
(by kind id; scenario-injected before stream-generated) → scheduled SED
triggers (by SED id) → control actions (by list order). Reproducibility: all
randomness flows from SHA-256-derived substreams of the master seed.
"""

from __future__ import annotations

import threading
import time as wall_time
from dataclasses import dataclass, field
from typing import Any, Callable

from dtb.common.expr import ExpressionError, SafeExpression
from dtb.common.seeding import derive_seed, substream
from dtb.domain.base import utcnow_iso
from dtb.domain.events import EffectKind, EventEffect, EventKind, StreamKind
from dtb.domain.kpi import KPIDefinition
from dtb.domain.model import EnterpriseModel, apply_scenario_overrides, _apply_parameter
from dtb.domain.scenario import Scenario
from dtb.domain.seds import (
    DurationKind,
    DurationModel,
    SEDDefinition,
    SEDStatus,
    TriggerKind,
)
from dtb.simulation.adapters import AdapterRegistry, ExternalModelError, default_registry
from dtb.simulation.result import Provenance, SimulationResult
from dtb.simulation.state import SEDInstance, SimulationState
from dtb.simulation.streams import InjectedSampler, PoissonSampler, StreamSampler, build_sampler

_EPS = 1e-9


@dataclass
class SimulationConfig:
    max_events: int = 200_000
    max_log_entries: int = 5_000
    series_max_points: int = 2_000
    progress_every: int = 200


class SimControl:
    """Cooperative pause/resume/cancel; checkpoints never touch the RNG."""

    def __init__(self) -> None:
        self._resume = threading.Event()
        self._resume.set()
        self.cancelled = False

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    def cancel(self) -> None:
        self.cancelled = True
        self._resume.set()

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    def checkpoint(self) -> bool:
        """Blocks while paused; returns False when the run must stop."""
        self._resume.wait()
        return not self.cancelled


@dataclass
class _ScheduledTrigger:
    time: float
    sed_id: str
    count_expression: str
    condition: str | None


class SimulationEngine:
    def __init__(
        self,
        model: EnterpriseModel,
        scenario: Scenario,
        seed: int | None = None,
        registry: AdapterRegistry | None = None,
        config: SimulationConfig | None = None,
        control: SimControl | None = None,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.base_model = model
        self.scenario = scenario
        self.model = apply_scenario_overrides(model, scenario)
        self.horizon = float(
            scenario.horizon_hours or self.model.enterprise.planning_horizon_hours
        )
        self.seed = int(seed if seed is not None else (scenario.seed or 0))
        self.registry = registry or default_registry()
        self.config = config or SimulationConfig()
        self.control = control or SimControl()
        self.progress_cb = progress_cb

        self.state = SimulationState()
        self._expr_cache: dict[str, SafeExpression] = {}
        self._kpi_exprs: dict[str, SafeExpression] = {
            kid: SafeExpression(k.expression) for kid, k in self.model.kpis.items()
        }
        self._duration_rng = {
            sid: substream(self.seed, "duration", sid) for sid in self.model.seds
        }
        self._branch_rng = {
            sid: substream(self.seed, "branch", sid) for sid in self.model.seds
        }
        self._samplers: dict[str, StreamSampler] = {}
        self._injected: dict[str, InjectedSampler] = {}
        self._sched_triggers: list[_ScheduledTrigger] = []
        self._sched_index = 0
        self._controls = sorted(
            enumerate(scenario.control_actions), key=lambda p: (p[1].time, p[0])
        )
        self._control_index = 0
        # trigger indexes
        self._on_event: dict[str, list[SEDDefinition]] = {}
        self._on_completed: dict[str, list[SEDDefinition]] = {}
        self._build_indexes()

        # bookkeeping
        self._n_np_events = 0
        self._n_moments = 0
        self._log: list[dict[str, Any]] = []
        self._log_dropped = 0
        self._kpi_series: dict[str, list[tuple[float, float]]] = {
            k: [] for k in self.model.kpis
        }
        self._stock_series: dict[str, list[tuple[float, float]]] = {
            u: [] for u in self.model.utility_types
        }
        self._pool_series: list[tuple[float, int, int]] = []
        self._last_record_t: float | None = None
        self._record_dt = self.horizon / max(1, self.config.series_max_points)

    # ------------------------------------------------------------------ setup --

    def _build_indexes(self) -> None:
        for sed in sorted(self.model.seds.values(), key=lambda s: s.id):
            for trig in sed.triggers:
                if trig.kind == TriggerKind.ON_EVENT and trig.event_kind_id:
                    self._on_event.setdefault(trig.event_kind_id, []).append(sed)
                elif trig.kind == TriggerKind.ON_SED_COMPLETED and trig.sed_id:
                    self._on_completed.setdefault(trig.sed_id, []).append(sed)
                elif trig.kind == TriggerKind.AT_TIME and trig.at is not None:
                    if trig.at <= self.horizon:
                        self._sched_triggers.append(
                            _ScheduledTrigger(trig.at, sed.id, trig.count_expression, trig.condition)
                        )
                elif trig.kind == TriggerKind.RECURRING and trig.interval:
                    t = trig.start
                    while t <= self.horizon + _EPS:
                        self._sched_triggers.append(
                            _ScheduledTrigger(t, sed.id, trig.count_expression, trig.condition)
                        )
                        t += trig.interval
        self._sched_triggers.sort(key=lambda s: (s.time, s.sed_id))

    def _init_state(self) -> None:
        for utype_id, qty in self.model.initial_state.utility_stocks.items():
            self.state.seed_stock(utype_id, qty)
        from dtb.simulation.state import ResourceState

        for res in self.model.resources.values():
            self.state.resources[res.id] = ResourceState(res.id, res.capacity)
        for kind_id, kind in sorted(self.model.event_kinds.items()):
            sampler = build_sampler(
                kind_id,
                kind.stream,
                self.horizon,
                substream(self.seed, "stream", kind_id),
                self.registry,
                derive_seed(self.seed, "ext_stream", kind_id),
            )
            if sampler is not None:
                self._samplers[kind_id] = sampler
        injected: dict[str, list[tuple[float, dict[str, Any]]]] = {}
        for ev in self.scenario.injected_events:
            if ev.time <= self.horizon:
                injected.setdefault(ev.event_kind_id, []).append((ev.time, dict(ev.payload)))
        for kind_id, occs in sorted(injected.items()):
            self._injected[kind_id] = InjectedSampler(kind_id, occs)

    # ------------------------------------------------------------- expressions --

    def _expr(self, source: str) -> SafeExpression:
        cached = self._expr_cache.get(source)
        if cached is None:
            cached = SafeExpression(source)
            self._expr_cache[source] = cached
        return cached

    def _eval(self, source: str, payload: dict[str, Any] | None = None) -> Any:
        context = self.state.kpi_context()
        if payload:
            context.update(
                {k: v for k, v in payload.items() if isinstance(k, str) and k.isidentifier()}
            )
        return self._expr(source).evaluate(context)

    def _eval_count(self, source: str, payload: dict[str, Any] | None = None) -> int:
        try:
            return max(0, int(round(float(self._eval(source, payload)))))
        except (ExpressionError, TypeError, ValueError):
            return 0

    def _condition_ok(self, source: str | None, payload: dict[str, Any] | None = None) -> bool:
        if not source:
            return True
        try:
            return bool(self._eval(source, payload))
        except ExpressionError:
            return False

    # ------------------------------------------------------------------- logging --

    def _log_entry(self, type_: str, name: str, **details: Any) -> None:
        if len(self._log) >= self.config.max_log_entries:
            self._log_dropped += 1
            return
        self._log.append(
            {"t": round(self.state.time, 6), "type": type_, "name": name, "details": details}
        )

    def _record_series(self, force: bool = False) -> None:
        t = self.state.time
        if (
            not force
            and self._last_record_t is not None
            and t - self._last_record_t < self._record_dt - _EPS
        ):
            return
        self._last_record_t = t
        context = self.state.kpi_context()
        for kid, expr in self._kpi_exprs.items():
            try:
                value = float(expr.evaluate(context))
            except (ExpressionError, TypeError, ValueError):
                value = 0.0
            self._kpi_series[kid].append((round(t, 6), value))
        for utype_id in self._stock_series:
            self._stock_series[utype_id].append((round(t, 6), self.state.stock(utype_id)))
        self._pool_series.append((round(t, 6), len(self.state.waiting), len(self.state.executing)))

    # ------------------------------------------------------------ SED lifecycle --

    def _spawn(self, sed_id: str, payload: dict[str, Any], source: str, count: int = 1) -> None:
        sed = self.model.seds.get(sed_id)
        if sed is None:
            self._log_entry("warning", "unknown_sed", sed_id=sed_id, source=source)
            return
        for _ in range(count):
            seq = self.state.next_seq()
            inst = SEDInstance(
                instance_id=f"{sed_id}#{seq}",
                seq=seq,
                sed_id=sed_id,
                created_at=self.state.time,
                payload=dict(payload),
                trigger_source=source,
            )
            self.state.instances[inst.instance_id] = inst
            self.state.waiting.append(inst)
            self._log_entry("sed_created", sed_id, instance=inst.instance_id, source=source)

    def _sample_duration(self, sed: SEDDefinition) -> float:
        d: DurationModel = sed.duration
        rng = self._duration_rng[sed.id]
        if d.kind == DurationKind.FIXED:
            return max(0.0, float(d.value or 0.0))
        if d.kind == DurationKind.UNIFORM:
            return max(0.0, rng.uniform(d.low or 0.0, d.high or 0.0))
        if d.kind == DurationKind.TRIANGULAR:
            return max(0.0, rng.triangular(d.low or 0.0, d.high or 0.0, d.mode))
        if d.kind == DurationKind.NORMAL:
            return max(0.0, rng.normalvariate(d.mean or 0.0, d.std or 0.0))
        if d.kind == DurationKind.LOGNORMAL:
            return max(0.0, rng.lognormvariate(d.mean or 0.0, d.std or 1.0))
        if d.kind == DurationKind.EXPONENTIAL:
            mean = d.mean or 0.0
            return rng.expovariate(1.0 / mean) if mean > 0 else 0.0
        raise ValueError(f"unknown duration kind {d.kind}")  # pragma: no cover

    def _try_start_waiting(self) -> None:
        t = self.state.time
        progress = True
        while progress:
            progress = False
            order = sorted(
                self.state.waiting,
                key=lambda i: (self.model.seds[i.sed_id].priority, i.seq)
                if i.sed_id in self.model.seds
                else (10**9, i.seq),
            )
            for inst in order:
                sed = self.model.seds.get(inst.sed_id)
                if sed is None:
                    continue
                if sed.max_concurrent is not None:
                    running = sum(
                        1 for e in self.state.executing.values() if e.sed_id == sed.id
                    )
                    if running >= sed.max_concurrent:
                        inst.block_reason = "max_concurrent"
                        continue
                if not self._condition_ok(sed.start_condition, inst.payload):
                    inst.block_reason = "start_condition"
                    continue
                blocked = None
                if not inst.inputs_consumed:
                    for flow in sed.inputs:
                        if self.state.stock(flow.utility_type_id) + _EPS < flow.quantity:
                            blocked = f"utility:{flow.utility_type_id}"
                            break
                if blocked is None:
                    for req in sed.resources:
                        res = self.state.resources.get(req.resource_id)
                        if res is None or res.available(t) + _EPS < req.quantity:
                            blocked = f"resource:{req.resource_id}"
                            break
                if blocked is not None:
                    inst.block_reason = blocked
                    continue
                self._start_instance(inst, sed)
                progress = True

    def _start_instance(self, inst: SEDInstance, sed: SEDDefinition) -> None:
        t = self.state.time
        if not inst.inputs_consumed:
            for flow in sed.inputs:
                ok = self.state.take_stock(flow.utility_type_id, flow.quantity)
                assert ok, "input availability was checked before start"
            inst.inputs_consumed = True
        for req in sed.resources:
            res = self.state.resources[req.resource_id]
            res.allocated += req.quantity
            inst.allocated[req.resource_id] = (
                inst.allocated.get(req.resource_id, 0.0) + req.quantity
            )
        resumed = inst.remaining_duration is not None
        if resumed:
            duration = inst.remaining_duration or 0.0
            inst.remaining_duration = None
        elif sed.external_model is not None:
            duration = self._execute_external(inst, sed)
        else:
            duration = self._sample_duration(sed)
            inst.sampled_duration = duration
        inst.status = SEDStatus.EXECUTING
        inst.started_at = t if inst.started_at is None else inst.started_at
        inst.completion_time = t + duration
        inst.block_reason = None
        self.state.waiting.remove(inst)
        self.state.executing[inst.instance_id] = inst
        if not resumed:
            self.state.started_count[sed.id] = self.state.started_count.get(sed.id, 0) + 1
        self._log_entry(
            "sed_resumed" if resumed else "sed_started",
            sed.id,
            instance=inst.instance_id,
            duration=round(duration, 6),
        )

    def _execute_external(self, inst: SEDInstance, sed: SEDDefinition) -> float:
        ref = sed.external_model
        assert ref is not None
        adapter = self.registry.get(ref.adapter)
        response = adapter.execute_sed(
            ref,
            {
                "sed_id": sed.id,
                "instance_id": inst.instance_id,
                "time": self.state.time,
                "payload": inst.payload,
                "seed": derive_seed(self.seed, "ext_sed", sed.id, inst.seq),
                "inputs": [
                    {"utility_type_id": f.utility_type_id, "quantity": f.quantity}
                    for f in sed.inputs
                ],
            },
        )
        duration = response.get("duration")
        if not isinstance(duration, (int, float)) or duration < 0:
            raise ExternalModelError(
                f"external model for SED {sed.id}: invalid duration {duration!r}"
            )
        outputs = response.get("outputs")
        if outputs is not None:
            if not isinstance(outputs, list):
                raise ExternalModelError(f"external model for SED {sed.id}: outputs must be a list")
            inst.external_outputs = outputs
            inst.branch_label = str(response.get("branch_label") or "external")
        return float(duration)

    def _complete_instance(self, inst: SEDInstance) -> None:
        sed = self.model.seds.get(inst.sed_id)
        del self.state.executing[inst.instance_id]
        inst.status = SEDStatus.COMPLETED
        self.state.completed_count[inst.sed_id] = (
            self.state.completed_count.get(inst.sed_id, 0) + 1
        )
        for res_id, qty in inst.allocated.items():
            res = self.state.resources.get(res_id)
            if res:
                res.allocated = max(0.0, res.allocated - qty)
        inst.allocated = {}
        outputs: list[tuple[str, float]] = []
        spawn_after: list[str] = []
        if inst.external_outputs is not None:
            for out in inst.external_outputs:
                utype = out.get("utility_type_id")
                qty = out.get("quantity")
                if isinstance(utype, str) and isinstance(qty, (int, float)) and qty > 0:
                    outputs.append((utype, float(qty)))
        elif sed is not None:
            branches = sed.effective_branches()
            if len(branches) == 1:
                branch = branches[0]
            else:
                r = self._branch_rng[sed.id].random()
                acc = 0.0
                branch = branches[-1]
                for b in branches:
                    acc += b.probability
                    if r <= acc + _EPS:
                        branch = b
                        break
            inst.branch_label = branch.label
            outputs = [(f.utility_type_id, f.quantity) for f in branch.outputs]
            spawn_after = list(branch.spawn_sed_ids)
        for utype_id, qty in outputs:
            self.state.add_stock(utype_id, qty)
        self._log_entry(
            "sed_completed",
            inst.sed_id,
            instance=inst.instance_id,
            branch=inst.branch_label,
            outputs={u: q for u, q in outputs},
        )
        completion_payload = {
            **inst.payload,
            "branch": inst.branch_label or "success",
            "source_sed": inst.sed_id,
        }
        for sed_id in spawn_after:
            self._spawn(sed_id, completion_payload, f"branch:{inst.sed_id}")
        for dep in self._on_completed.get(inst.sed_id, []):
            for trig in dep.triggers:
                if trig.kind != TriggerKind.ON_SED_COMPLETED or trig.sed_id != inst.sed_id:
                    continue
                if not self._condition_ok(trig.condition, completion_payload):
                    continue
                count = self._eval_count(trig.count_expression, completion_payload)
                if count > 0:
                    self._spawn(dep.id, completion_payload, f"sed_completed:{inst.sed_id}", count)

    def _check_continuation(self) -> None:
        t = self.state.time
        # capacity violations: suspend most recently started first (deterministic LIFO)
        for res_id, res in sorted(self.state.resources.items()):
            while res.allocated > res.capacity(t) + _EPS:
                users = sorted(
                    (
                        inst
                        for inst in self.state.executing.values()
                        if inst.allocated.get(res_id, 0.0) > 0
                    ),
                    key=lambda i: (-(i.started_at or 0.0), -i.seq),
                )
                if not users:
                    break
                self._suspend_instance(users[0], f"resource:{res_id}")
        for inst in sorted(self.state.executing.values(), key=lambda i: i.seq):
            sed = self.model.seds.get(inst.sed_id)
            if sed and sed.continuation_condition:
                if not self._condition_ok(sed.continuation_condition, inst.payload):
                    self._suspend_instance(inst, "continuation_condition")

    def _suspend_instance(self, inst: SEDInstance, reason: str) -> None:
        t = self.state.time
        inst.remaining_duration = max(0.0, (inst.completion_time or t) - t)
        inst.completion_time = None
        for res_id, qty in inst.allocated.items():
            res = self.state.resources.get(res_id)
            if res:
                res.allocated = max(0.0, res.allocated - qty)
        inst.allocated = {}
        inst.status = SEDStatus.WAITING
        inst.suspend_count += 1
        inst.waiting_since = t
        inst.block_reason = reason
        del self.state.executing[inst.instance_id]
        self.state.waiting.append(inst)
        self._log_entry("sed_suspended", inst.sed_id, instance=inst.instance_id, reason=reason)

    # ------------------------------------------------------------------ effects --

    def _apply_effect(self, effect: EventEffect, payload: dict[str, Any], source: str) -> None:
        if not self._condition_ok(effect.condition, payload):
            return
        t = self.state.time
        if effect.kind == EffectKind.SPAWN_SED:
            count = self._eval_count(effect.quantity_expression, payload)
            if count > 0:
                self._spawn(effect.sed_id or "", payload, source, count)
        elif effect.kind == EffectKind.ADD_UTILITY:
            qty = float(self._eval(effect.quantity_expression, payload) or 0.0)
            if qty > 0:
                self.state.add_stock(effect.utility_type_id or "", qty)
        elif effect.kind == EffectKind.REMOVE_UTILITY:
            qty = float(self._eval(effect.quantity_expression, payload) or 0.0)
            have = self.state.stock(effect.utility_type_id or "")
            take = min(qty, have)
            if take > 0:
                self.state.take_stock(effect.utility_type_id or "", take)
        elif effect.kind == EffectKind.RESOURCE_CAPACITY_DELTA:
            res = self.state.resources.get(effect.resource_id or "")
            if res is not None and effect.delta is not None:
                if effect.duration:
                    res.temp_deltas.append((t + effect.duration, effect.delta))
                else:
                    res.base_capacity = max(0.0, res.base_capacity + effect.delta)
                self._log_entry(
                    "resource_capacity_change",
                    effect.resource_id or "",
                    delta=effect.delta,
                    until=(t + effect.duration) if effect.duration else None,
                    source=source,
                )
        elif effect.kind == EffectKind.SET_PARAMETER:
            try:
                _apply_parameter(self.model, effect.parameter_path or "", effect.value)
                self._log_entry(
                    "parameter_set", effect.parameter_path or "", value=effect.value, source=source
                )
                self._after_parameter_change(effect.parameter_path or "")
            except (KeyError, AttributeError, ValueError) as exc:
                self._log_entry("warning", "set_parameter_failed", path=effect.parameter_path, error=str(exc))
        elif effect.kind == EffectKind.CANCEL_SED_INSTANCES:
            sed_id = effect.sed_id or ""
            remaining: list[SEDInstance] = []
            for inst in self.state.waiting:
                if inst.sed_id == sed_id:
                    inst.status = SEDStatus.CANCELLED
                    self.state.cancelled_count[sed_id] = (
                        self.state.cancelled_count.get(sed_id, 0) + 1
                    )
                else:
                    remaining.append(inst)
            self.state.waiting = remaining
            self._log_entry("sed_cancelled", sed_id, source=source)

    def _after_parameter_change(self, path: str) -> None:
        """Runtime stream parameters take effect immediately."""
        if path.startswith("event_kind:"):
            kind_id = path.split(":", 1)[1].partition(".")[0]
            kind = self.model.event_kinds.get(kind_id)
            sampler = self._samplers.get(kind_id)
            if kind and isinstance(sampler, PoissonSampler):
                sampler.rate = kind.stream.rate_per_hour or 0.0
                sampler.invalidate()

    def _process_event(self, kind: EventKind, payload: dict[str, Any], source: str) -> None:
        merged = {**kind.payload_defaults, **payload}
        self.state.event_counts[kind.id] = self.state.event_counts.get(kind.id, 0) + 1
        self._n_np_events += 1
        self._log_entry("np_event", kind.id, category=kind.category.value, source=source, payload=merged)
        for effect in kind.effects:
            self._apply_effect(effect, merged, f"event:{kind.id}")
        for sed in self._on_event.get(kind.id, []):
            for trig in sed.triggers:
                if trig.kind != TriggerKind.ON_EVENT or trig.event_kind_id != kind.id:
                    continue
                if not self._condition_ok(trig.condition, merged):
                    continue
                count = self._eval_count(trig.count_expression, merged)
                if count > 0:
                    self._spawn(sed.id, merged, f"event:{kind.id}", count)

    # -------------------------------------------------------------------- loop --

    def run(self) -> SimulationResult:
        started_wall = wall_time.monotonic()
        provenance = Provenance(
            enterprise_id=self.model.enterprise.id,
            model_version=self.model.enterprise.model_version,
            model_hash=self.base_model.content_hash(),
            scenario_id=self.scenario.id,
            scenario_version=self.scenario.version,
            initial_state_hash=_hash_dict(self.model.initial_state.utility_stocks),
            seed=self.seed,
            horizon_hours=self.horizon,
            started_at=utcnow_iso(),
            parameters={"max_events": self.config.max_events},
        )
        result = SimulationResult(provenance=provenance)
        try:
            self._init_state()
            self._record_series(force=True)
            self._try_start_waiting()
            status = self._loop()
            result.status = status
        except ExternalModelError as exc:
            result.status = "failed"
            result.error = str(exc)
        except Exception as exc:  # noqa: BLE001 — surfaced in the result, never hidden
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
        self._finalize(result, started_wall)
        return result

    def _candidates(self) -> list[tuple[float, int, str, Any]]:
        """(time, order_class, sort_key, payload) for every possible next occurrence."""
        now = self.state.time
        out: list[tuple[float, int, str, Any]] = []
        for res_id, res in self.state.resources.items():
            expiry = res.next_delta_expiry(now)
            if expiry is not None:
                out.append((expiry, 0, res_id, ("expiry", res_id)))
        for inst in self.state.executing.values():
            if inst.completion_time is not None:
                out.append(
                    (inst.completion_time, 1, f"{inst.seq:012d}", ("completion", inst.instance_id))
                )
        for kind_id, sampler in self._injected.items():
            occ = sampler.peek(now)
            if occ is not None:
                out.append((occ[0], 2, f"0:{kind_id}", ("injected", kind_id)))
        for kind_id, sampler in self._samplers.items():
            occ = sampler.peek(now)
            if occ is not None:
                out.append((occ[0], 2, f"1:{kind_id}", ("stream", kind_id)))
        if self._sched_index < len(self._sched_triggers):
            trig = self._sched_triggers[self._sched_index]
            out.append((trig.time, 3, trig.sed_id, ("sched", self._sched_index)))
        if self._control_index < len(self._controls):
            idx, action = self._controls[self._control_index]
            out.append((action.time, 4, f"{idx:06d}", ("control", self._control_index)))
        return out

    def _loop(self) -> str:
        moments_since_progress = 0
        while True:
            if not self.control.checkpoint():
                return "cancelled"
            if (
                self._n_np_events >= self.config.max_events
                or self._n_moments >= self.config.max_events * 4
            ):
                self._log_entry("warning", "max_events_reached", limit=self.config.max_events)
                self._advance_to(self.horizon)
                return "truncated"
            candidates = self._candidates()
            upcoming = [c for c in candidates if c[0] <= self.horizon + _EPS]
            if not upcoming:
                self._advance_to(self.horizon)
                return "completed"
            t_next = min(c[0] for c in upcoming)
            if t_next > self.horizon + _EPS:
                self._advance_to(self.horizon)
                return "completed"
            self._advance_to(t_next)
            batch = sorted(
                (c for c in upcoming if abs(c[0] - t_next) <= _EPS),
                key=lambda c: (c[1], c[2]),
            )
            self._n_moments += 1
            for _, order_class, _, token in batch:
                self._dispatch(token)
            self._check_continuation()
            self._try_start_waiting()
            self._record_series()
            moments_since_progress += 1
            if self.progress_cb and moments_since_progress >= self.config.progress_every:
                moments_since_progress = 0
                self._emit_progress()
            if self.state.time >= self.horizon - _EPS and not self._pending_now():
                return "completed"

    def _pending_now(self) -> bool:
        return any(c[0] <= self.horizon + _EPS for c in self._candidates())

    def _dispatch(self, token: tuple[str, Any]) -> None:
        kind, ref = token
        if kind == "expiry":
            res = self.state.resources.get(ref)
            if res:
                res.expire_deltas(self.state.time)
        elif kind == "completion":
            inst = self.state.executing.get(ref)
            if inst is not None and inst.completion_time is not None:
                if inst.completion_time <= self.state.time + _EPS:
                    self._complete_instance(inst)
        elif kind in ("injected", "stream"):
            sampler = (self._injected if kind == "injected" else self._samplers)[ref]
            occ = sampler.peek(self.state.time)
            if occ is not None and occ[0] <= self.state.time + _EPS:
                _, payload = sampler.pop()
                event_kind = self.model.event_kinds.get(ref)
                if event_kind is not None:
                    self._process_event(event_kind, payload, kind)
        elif kind == "sched":
            trig = self._sched_triggers[ref]
            if ref == self._sched_index:
                self._sched_index += 1
                if self._condition_ok(trig.condition, {}):
                    count = self._eval_count(trig.count_expression, {})
                    if count > 0:
                        self._spawn(trig.sed_id, {}, "schedule", count)
        elif kind == "control":
            if ref == self._control_index:
                self._control_index += 1
                _, action = self._controls[ref]
                self._log_entry("control_action", action.label or action.effect.kind.value)
                self._apply_effect(action.effect, {}, "control")

    def _advance_to(self, t: float) -> None:
        dt = t - self.state.time
        if dt > 0:
            self.state.accumulate(dt)
            self.state.time = t

    def _emit_progress(self) -> None:
        if self.progress_cb:
            self.progress_cb(
                {
                    "time": round(self.state.time, 3),
                    "horizon": self.horizon,
                    "progress": min(1.0, self.state.time / self.horizon) if self.horizon else 1.0,
                    "events": self._n_np_events,
                    "moments": self._n_moments,
                    "waiting": len(self.state.waiting),
                    "executing": len(self.state.executing),
                }
            )

    # ---------------------------------------------------------------- finalize --

    def _finalize(self, result: SimulationResult, started_wall: float) -> None:
        self._record_series(force=True)
        if self.progress_cb:
            self._emit_progress()
        result.kpi_series = self._kpi_series
        result.stock_series = self._stock_series
        result.pool_series = self._pool_series
        result.event_log = self._log
        result.event_log_truncated = self._log_dropped
        result.final_stocks = {k: round(v, 6) for k, v in self.state.stocks.items()}
        context = self.state.kpi_context()
        for kid, expr in self._kpi_exprs.items():
            try:
                result.final_kpis[kid] = float(expr.evaluate(context))
            except (ExpressionError, TypeError, ValueError):
                result.final_kpis[kid] = 0.0
        # per-SED stats
        created: dict[str, int] = {}
        suspend_totals: dict[str, int] = {}
        waiting_totals: dict[str, float] = {}
        for inst in self.state.instances.values():
            created[inst.sed_id] = created.get(inst.sed_id, 0) + 1
            suspend_totals[inst.sed_id] = suspend_totals.get(inst.sed_id, 0) + inst.suspend_count
            waiting_totals[inst.sed_id] = waiting_totals.get(inst.sed_id, 0.0) + inst.total_waiting
        blocked: dict[str, dict[str, float]] = {}
        for (sed_id, cause), hours in self.state.blocked_hours.items():
            blocked.setdefault(sed_id, {})[cause] = round(hours, 4)
        for sed_id in sorted(set(created) | set(self.model.seds)):
            result.sed_stats[sed_id] = {
                "created": created.get(sed_id, 0),
                "started": self.state.started_count.get(sed_id, 0),
                "completed": self.state.completed_count.get(sed_id, 0),
                "cancelled": self.state.cancelled_count.get(sed_id, 0),
                "suspensions": suspend_totals.get(sed_id, 0),
                "waiting_hours": round(waiting_totals.get(sed_id, 0.0), 4),
                "queue_peak": self.state.queue_peak.get(sed_id, 0),
                "still_waiting": sum(1 for i in self.state.waiting if i.sed_id == sed_id),
                "still_executing": sum(
                    1 for i in self.state.executing.values() if i.sed_id == sed_id
                ),
                "blocked_hours": blocked.get(sed_id, {}),
            }
        for res_id, res in self.state.resources.items():
            result.resource_stats[res_id] = {
                "utilization": round(res.utilization(), 4),
                "busy_hours": round(res.busy_integral, 4),
                "capacity_hours": round(res.capacity_integral, 4),
                "peak_allocated": res.peak_allocated,
                "final_capacity": res.capacity(self.state.time),
            }
        for utype_id in sorted(
            set(self.model.utility_types)
            | set(self.state.produced)
            | set(self.state.consumed)
        ):
            result.utility_totals[utype_id] = {
                "produced": round(self.state.produced.get(utype_id, 0.0), 6),
                "consumed": round(self.state.consumed.get(utype_id, 0.0), 6),
                "final_stock": round(self.state.stock(utype_id), 6),
            }
        result.goal_achievement = self._goal_achievement(result)
        result.metrics = {
            "np_events": self._n_np_events,
            "switching_moments": self._n_moments,
            "instances_created": len(self.state.instances),
            "instances_completed": sum(self.state.completed_count.values()),
            "instances_waiting": len(self.state.waiting),
            "instances_executing": len(self.state.executing),
            "sim_time_hours": round(self.state.time, 6),
            "wall_time_seconds": round(wall_time.monotonic() - started_wall, 4),
            "seed": self.seed,
        }

    def _goal_achievement(self, result: SimulationResult) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for goal in self.model.goals.values():
            metrics = []
            achieved_all = True
            for tm in goal.target_metrics:
                series = result.kpi_series.get(tm.kpi_id, [])
                deadline = tm.deadline_hours if tm.deadline_hours is not None else self.horizon
                value = 0.0
                for t, v in series:
                    if t <= deadline + _EPS:
                        value = v
                    else:
                        break
                ok = value >= tm.target_value if tm.comparison == ">=" else value <= tm.target_value
                achieved_all = achieved_all and ok
                metrics.append(
                    {
                        "kpi_id": tm.kpi_id,
                        "target": tm.target_value,
                        "comparison": tm.comparison,
                        "deadline_hours": deadline,
                        "value": round(value, 6),
                        "achieved": ok,
                        "deviation": round(value - tm.target_value, 6),
                        "weight": tm.weight,
                    }
                )
            if goal.target_metrics:
                out[goal.id] = {"achieved": achieved_all, "metrics": metrics}
        return out


def _hash_dict(data: dict[str, float]) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(data, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def simulate(
    model: EnterpriseModel,
    scenario: Scenario,
    seed: int | None = None,
    registry: AdapterRegistry | None = None,
    config: SimulationConfig | None = None,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
    control: SimControl | None = None,
) -> SimulationResult:
    return SimulationEngine(
        model, scenario, seed=seed, registry=registry, config=config,
        control=control, progress_cb=progress_cb,
    ).run()
