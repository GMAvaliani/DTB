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
from dtb.domain.control import (
    ControlActionDef,
    ObservationMode,
    Policy,
    PolicyRule,
)
from dtb.domain.calendars import Calendar
from dtb.domain.economics import EconomicBasis, EconomicItem
from dtb.domain.events import EffectKind, EventEffect, EventKind, StreamKind
from dtb.domain.kpi import KPIDefinition
from dtb.domain.model import (
    EnterpriseModel,
    _apply_parameter,
    _read_parameter,
    apply_scenario_overrides,
)
from dtb.domain.scenario import Scenario
from dtb.domain.resources import QueueDiscipline
from dtb.domain.seds import (
    DurationKind,
    DurationModel,
    InterruptionMode,
    SEDDefinition,
    SEDStatus,
    TriggerKind,
)
from dtb.simulation.adapters import AdapterRegistry, ExternalModelError, default_registry
from dtb.simulation.decisions import DecisionScheduler, DecisionTrigger, select_policies
from dtb.simulation.economics import EconomicsLedger
from dtb.simulation.observations import ObservationLayer
from dtb.simulation.result import Provenance, SimulationResult
from dtb.simulation.state import SEDInstance, SimulationState
from dtb.simulation.streams import InjectedSampler, PoissonSampler, StreamSampler, build_sampler

_EPS = 1e-9


@dataclass
class _PendingActivation:
    """Решение принято, эффект ещё не наступил (задержка активации)."""

    time: float
    seq: int
    action_id: str
    effect: EventEffect
    source: str
    policy_id: str | None
    subject_id: str | None


@dataclass
class _PendingReversion:
    """Срок обратимого действия истёк — эффект откатывается."""

    time: float
    seq: int
    action_id: str
    parameter_path: str
    previous_value: Any


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
        # --- контур управления: МР, политики, наблюдения ---
        mode_override = (
            ObservationMode(scenario.observation_mode) if scenario.observation_mode else None
        )
        self.observations = ObservationLayer(
            self.model.observations, self.seed, mode_override=mode_override
        )
        self.decisions = DecisionScheduler(self.model.decision_points, self.horizon)
        self._policy_cooldown: dict[str, float] = {}
        self._pending_event_decisions: list[DecisionTrigger] = []
        self._decision_log: list[dict[str, Any]] = []
        self._policy_trace: list[dict[str, Any]] = []
        self._n_decisions = 0
        self._last_decision_time = 0.0
        self._applied_by_policy_at: set[tuple[float, str]] = set()
        # --- управляющее действие как объект (фаза 2) ---
        self._action_rng = {
            aid: substream(self.seed, "control_action", aid)
            for aid in sorted(self.model.control_actions)
        }
        self._pending_activations: list[_PendingActivation] = []
        self._pending_reversions: list[_PendingReversion] = []
        self._action_seq = 0
        self._rejected_actions: list[dict[str, Any]] = []
        # --- экономика и разогрев (фаза 3) ---
        econ_settings = self.model.economics
        overrides: dict[str, Any] = {}
        if scenario.warmup_hours is not None:
            overrides["warmup_hours"] = float(scenario.warmup_hours)
        if scenario.discount_rate_annual is not None:
            overrides["discount_rate_annual"] = float(scenario.discount_rate_annual)
        if overrides:
            econ_settings = econ_settings.model_copy(update=overrides)
        self.economics = EconomicsLedger(
            self.model.economic_items, econ_settings, self.horizon
        )
        self._warmup_hours = float(econ_settings.warmup_hours or 0.0)
        self._warmup_report: dict[str, Any] = {
            "hours": self._warmup_hours,
            "applied": False,
        }
        # --- режимы работы: календари, дисциплины очередей (фаза 5) ---
        self._weekday_offset = int(self.model.enterprise.calendar_start_weekday)
        self._default_discipline = QueueDiscipline(
            self.model.enterprise.default_queue_discipline
        )
        self._resource_calendar: dict[str, Calendar] = {}
        default_cal = self.model.calendars.get(
            self.model.enterprise.default_calendar_ref or ""
        )
        for res in self.model.resources.values():
            calendar = self.model.calendars.get(res.calendar_ref or "") or default_cal
            if calendar is not None and calendar.enabled:
                self._resource_calendar[res.id] = calendar
        self._priority_wait: set[str] = set()  # экземпляры, ждущие возврата ресурса
        self._restarted = 0
        self._failed_instances = 0
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
            state = ResourceState(res.id, res.capacity)
            calendar = self._resource_calendar.get(res.id)
            if calendar is not None:
                state.calendar_factor = calendar.factor_at(0.0, self._weekday_offset)
            self.state.resources[res.id] = state
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
        # начальное состояние известно точно: снимаем нулевое наблюдение сразу
        if self.observations.active:
            self.observations.take_samples(0.0, self._observation_true_value)

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

    def _apply_calendar(self, res_id: str) -> None:
        """Пересчитать доступную ёмкость ресурса по его графику работы."""
        calendar = self._resource_calendar.get(res_id)
        res = self.state.resources.get(res_id)
        if calendar is None or res is None:
            return
        factor = calendar.factor_at(self.state.time, self._weekday_offset)
        if abs(factor - res.calendar_factor) <= _EPS:
            return
        previous = res.calendar_factor
        res.calendar_factor = factor
        self._log_entry(
            "calendar_change", res_id, factor=factor, previous=previous,
            capacity=res.capacity(self.state.time),
        )

    def _discipline_for(self, inst: SEDInstance) -> QueueDiscipline:
        """Дисциплина очереди экземпляра: по первому требуемому ресурсу."""
        sed = self.model.seds.get(inst.sed_id)
        if sed is not None:
            for req in sed.resources:
                resource = self.model.resources.get(req.resource_id)
                if resource is not None and resource.queue_discipline is not None:
                    return resource.queue_discipline
        return self._default_discipline

    def _expected_duration(self, sed: SEDDefinition | None) -> float:
        """Ожидаемая длительность СЭД — для дисциплины «сначала короткие»."""
        if sed is None:
            return 0.0
        d = sed.duration
        if d.kind == DurationKind.FIXED:
            return float(d.value or 0.0)
        if d.kind == DurationKind.UNIFORM:
            return (float(d.low or 0.0) + float(d.high or 0.0)) / 2
        if d.kind == DurationKind.TRIANGULAR:
            return (
                float(d.low or 0.0) + float(d.high or 0.0) + float(d.mode or 0.0)
            ) / 3
        if d.kind in (DurationKind.NORMAL, DurationKind.EXPONENTIAL):
            return float(d.mean or 0.0)
        if d.kind == DurationKind.LOGNORMAL:
            import math as _math

            return _math.exp(float(d.mean or 0.0) + float(d.std or 0.0) ** 2 / 2)
        return 0.0

    def _queue_sort_key(self, inst: SEDInstance) -> tuple:
        """Ключ сортировки очереди по действующей дисциплине.

        Экземпляры в режиме ``wait`` (ждут возврата прерванного ресурса)
        получают преимущество: они удерживают место в очереди.
        """
        sed = self.model.seds.get(inst.sed_id)
        held = 0 if inst.instance_id in self._priority_wait else 1
        discipline = self._discipline_for(inst)
        if discipline == QueueDiscipline.FIFO:
            return (held, inst.created_at, inst.seq)
        if discipline == QueueDiscipline.LIFO:
            return (held, -inst.created_at, -inst.seq)
        if discipline == QueueDiscipline.SPT:
            remaining = (
                inst.remaining_duration
                if inst.remaining_duration is not None
                else self._expected_duration(sed)
            )
            return (held, remaining, inst.seq)
        priority = sed.priority if sed is not None else 10**9
        return (held, priority, inst.seq)

    def _try_start_waiting(self) -> None:
        t = self.state.time
        progress = True
        while progress:
            progress = False
            order = sorted(self.state.waiting, key=self._queue_sort_key)
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
                self.economics.on_event(
                    EconomicBasis.UTILITY_CONSUMED, flow.utility_type_id, flow.quantity, t
                )
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
        self._priority_wait.discard(inst.instance_id)
        if not resumed:
            self.state.started_count[sed.id] = self.state.started_count.get(sed.id, 0) + 1
            self.economics.on_event(EconomicBasis.SED_STARTED, sed.id, 1.0, t)
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
            self.economics.on_event(
                EconomicBasis.UTILITY_PRODUCED, utype_id, qty, self.state.time
            )
        self.economics.on_event(
            EconomicBasis.SED_COMPLETED, inst.sed_id, 1.0, self.state.time
        )
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

    def _release_resources(self, inst: SEDInstance) -> None:
        for res_id, qty in inst.allocated.items():
            res = self.state.resources.get(res_id)
            if res:
                res.allocated = max(0.0, res.allocated - qty)
        inst.allocated = {}

    def _suspend_instance(self, inst: SEDInstance, reason: str) -> None:
        """Обработать прерывание экземпляра согласно режиму его СЭД.

        ``resume`` — остаток длительности сохраняется, экземпляр возвращается в
        общую очередь; ``wait`` — то же, но экземпляр удерживает место в
        очереди и получает преимущество при возврате ресурса; ``restart`` —
        наработка теряется и длительность будет сэмплирована заново; ``fail`` —
        экземпляр снимается как отказ.
        """
        t = self.state.time
        sed = self.model.seds.get(inst.sed_id)
        mode = sed.interruption_mode if sed is not None else InterruptionMode.RESUME
        elapsed = max(0.0, t - (inst.started_at or t))
        remaining = max(0.0, (inst.completion_time or t) - t)
        inst.completion_time = None
        self._release_resources(inst)
        del self.state.executing[inst.instance_id]

        if mode == InterruptionMode.FAIL:
            inst.status = SEDStatus.FAILED
            inst.remaining_duration = None
            inst.block_reason = reason
            self.state.failed_count[inst.sed_id] = (
                self.state.failed_count.get(inst.sed_id, 0) + 1
            )
            self._failed_instances += 1
            self._priority_wait.discard(inst.instance_id)
            self._log_entry(
                "sed_failed", inst.sed_id, instance=inst.instance_id, reason=reason,
                mode=mode.value, elapsed=round(elapsed, 6),
            )
            return

        if mode == InterruptionMode.RESTART:
            inst.remaining_duration = None  # длительность будет сэмплирована заново
            inst.started_at = None
            self._restarted += 1
        else:
            inst.remaining_duration = remaining
        inst.status = SEDStatus.WAITING
        inst.suspend_count += 1
        inst.waiting_since = t
        inst.block_reason = reason
        self.state.waiting.append(inst)
        if mode == InterruptionMode.WAIT:
            self._priority_wait.add(inst.instance_id)
        self._log_entry(
            "sed_suspended", inst.sed_id, instance=inst.instance_id, reason=reason,
            mode=mode.value, lost_progress=round(elapsed, 6) if mode == InterruptionMode.RESTART else 0.0,
        )

    # -------------------------------------------- наблюдения и контур решений --

    def _observation_true_value(self, obs) -> float:
        """Истинное значение наблюдаемой компоненты состояния."""
        kind = obs.target_kind.value
        ref = obs.target_ref
        metric = obs.effective_metric()
        st = self.state
        if kind == "utility_type":
            if metric == "produced":
                return st.produced.get(ref, 0.0)
            if metric == "consumed":
                return st.consumed.get(ref, 0.0)
            return st.stock(ref)
        if kind == "sed":
            if metric == "executing":
                return float(sum(1 for i in st.executing.values() if i.sed_id == ref))
            if metric == "completed":
                return float(st.completed_count.get(ref, 0))
            if metric == "started":
                return float(st.started_count.get(ref, 0))
            return float(sum(1 for i in st.waiting if i.sed_id == ref))
        if kind == "resource":
            res = st.resources.get(ref)
            if res is None:
                return 0.0
            if metric == "allocated":
                return res.allocated
            if metric == "available":
                return res.available(st.time)
            return res.utilization()
        if kind == "kpi":
            expr = self._kpi_exprs.get(ref)
            if expr is None:
                return 0.0
            try:
                return float(expr.evaluate(st.kpi_context()))
            except (ExpressionError, TypeError, ValueError):
                return 0.0
        return 0.0  # link: наблюдение связи NOT IMPLEMENTED

    def _decision_context(self) -> dict[str, Any]:
        """Контекст, который видит политика: оценка Ŷ либо истина Y."""
        base = self.state.kpi_context()
        context = dict(base)
        context["kpi"] = self._kpi_value
        if not self.observations.active:
            return context
        t = self.state.time
        layer = self.observations

        def wrap(kind: str, metric: str, fn):
            def wrapped(ref: str) -> float:
                return layer.observed_value(kind, ref, metric, t, float(fn(ref)))

            return wrapped

        context["stock"] = wrap("utility_type", "stock", base["stock"])
        context["produced"] = wrap("utility_type", "produced", base["produced"])
        context["consumed"] = wrap("utility_type", "consumed", base["consumed"])
        context["utilization"] = wrap("resource", "utilization", base["utilization"])
        context["waiting_count"] = wrap("sed", "queue", base["waiting_count"])
        context["executing_count"] = wrap("sed", "executing", base["executing_count"])
        context["completed"] = wrap("sed", "completed", base["completed"])
        context["kpi"] = wrap("kpi", "value", self._kpi_value)
        return context

    def _kpi_value(self, kpi_id: str) -> float:
        expr = self._kpi_exprs.get(kpi_id)
        if expr is None:
            return 0.0
        try:
            return float(expr.evaluate(self.state.kpi_context()))
        except (ExpressionError, TypeError, ValueError):
            return 0.0

    def _eval_in_context(self, source: str, context: dict[str, Any]) -> Any:
        return self._expr(source).evaluate(context)

    def _watchdog_evaluate(self, condition: str) -> bool:
        try:
            return bool(self._eval_in_context(condition, self._decision_context()))
        except ExpressionError:
            return False

    def _collect_decision_triggers(self) -> list[DecisionTrigger]:
        """Какие точки принятия решений сработали в текущий МП."""
        triggers: list[DecisionTrigger] = []
        triggers.extend(self.decisions.timers_due(self.state.time))
        triggers.extend(self._pending_event_decisions)
        self._pending_event_decisions = []
        triggers.extend(self.decisions.watchdog_triggers(self._watchdog_evaluate))
        return triggers

    def _run_decisions(self, triggers: list[DecisionTrigger]) -> None:
        """Обработать момент принятия решений: вызвать политики, применить действия."""
        if not triggers:
            return
        t = self.state.time
        interval = max(0.0, t - self._last_decision_time)
        self._n_decisions += 1
        applied_total = 0
        for trig in triggers:
            dp = self.model.decision_points.get(trig.decision_point_id)
            if dp is None or not dp.requires_decision:
                continue
            policies = select_policies(
                self.model.policies, dp, t, self._policy_cooldown
            )
            for policy in policies:
                applied_total += self._evaluate_policy(policy, dp, trig)
        self._decision_log.append(
            {
                "t": round(t, 6),
                "interval": round(interval, 6),
                "triggers": [
                    {"decision_point": x.decision_point_id, "kind": x.kind, "source": x.source}
                    for x in triggers
                ],
                "actions_applied": applied_total,
            }
        )
        self._last_decision_time = t

    def _evaluate_policy(self, policy: Policy, dp, trig: DecisionTrigger) -> int:
        """Проверить правила политики и применить действие при истинном условии."""
        context = self._decision_context()
        applied = 0
        for rule in policy.effective_rules():
            try:
                fired = bool(self._eval_in_context(rule.condition, context))
            except ExpressionError as exc:
                self._policy_trace.append(
                    {
                        "t": round(self.state.time, 6),
                        "policy": policy.id,
                        "loop": policy.loop.value,
                        "decision_point": dp.id,
                        "fired": False,
                        "error": str(exc),
                    }
                )
                continue
            entry = {
                "t": round(self.state.time, 6),
                "policy": policy.id,
                "loop": policy.loop.value,
                "decision_point": dp.id,
                "trigger": trig.kind,
                "condition": rule.condition,
                "fired": fired,
                "action": rule.action_ref,
            }
            if fired:
                magnitude = None
                if rule.magnitude_expression:
                    try:
                        magnitude = float(
                            self._eval_in_context(rule.magnitude_expression, context)
                        )
                    except (ExpressionError, TypeError, ValueError):
                        magnitude = None
                outcome = self._apply_control_action(
                    rule.action_ref, source=f"policy:{policy.id}", magnitude=magnitude,
                    policy=policy,
                )
                entry["outcome"] = outcome
                if outcome in ("applied", "scheduled"):
                    applied += 1
                    if policy.cooldown_hours:
                        self._policy_cooldown[policy.id] = (
                            self.state.time + policy.cooldown_hours
                        )
            self._policy_trace.append(entry)
            if fired:
                break  # первая сработавшая ветвь правила
        return applied

    def _effect_with_magnitude(self, effect: EventEffect, magnitude: float | None) -> EventEffect:
        """Подставить величину действия в эффект, не меняя исходный объект."""
        if magnitude is None:
            return effect
        updated = effect.model_copy(deep=True)
        if updated.kind == EffectKind.RESOURCE_CAPACITY_DELTA:
            updated.delta = magnitude
        else:
            updated.quantity_expression = repr(float(magnitude))
        return updated

    def _resolve_subject(
        self, action: ControlActionDef, policy: Policy | None, subject_ref: str | None
    ) -> tuple[str | None, int]:
        """Кто применяет действие и каков его уровень полномочий.

        Приоритет: субъект вызова (сценарий) → субъект политики → субъект,
        заданный у самого действия.
        """
        for candidate in (subject_ref, policy.subject_ref if policy else None,
                          action.subject_ref):
            if candidate:
                subject = self.model.subjects.get(candidate)
                return candidate, (subject.authority_level if subject else -1)
        return None, 0

    def _reject_action(
        self, action_id: str, reason: str, source: str, **details: Any
    ) -> str:
        """Зафиксировать отклонённое действие: оно не применяется."""
        entry = {
            "t": round(self.state.time, 6),
            "action": action_id,
            "reason": reason,
            "source": source,
            **details,
        }
        self._rejected_actions.append(entry)
        self._log_entry("control_action_rejected", action_id, reason=reason, source=source,
                        **details)
        return "rejected"

    def _apply_control_action(
        self,
        action_ref: str | None,
        source: str,
        magnitude: float | None = None,
        policy: Policy | None = None,
        subject_ref: str | None = None,
    ) -> str:
        """Применить каталожное управляющее действие.

        Порядок проверок: полномочия субъекта → розыгрыш успеха → задержка
        активации. Возвращает исход: ``applied``, ``scheduled``, ``failed``,
        ``rejected``, ``unknown_action`` или ``no_action``.
        """
        if not action_ref:
            return "no_action"
        action = self.model.control_actions.get(action_ref)
        if action is None:
            self._log_entry("warning", "unknown_control_action", action=action_ref, source=source)
            return "unknown_action"

        subject_id, level = self._resolve_subject(action, policy, subject_ref)
        if subject_id is not None and level < 0:
            return self._reject_action(
                action_ref, "unknown_subject", source, subject=subject_id
            )
        if action.min_authority_level > 0:
            if subject_id is None:
                return self._reject_action(
                    action_ref, "no_subject", source,
                    required_authority=action.min_authority_level,
                )
            if level < action.min_authority_level:
                return self._reject_action(
                    action_ref, "insufficient_authority", source, subject=subject_id,
                    authority_level=level, required_authority=action.min_authority_level,
                )

        # разовая стоимость списывается за попытку, в том числе неудачную
        if action.one_time_cost:
            self.economics.on_control_action(
                action_ref, action.loop.value if action.loop else "",
                action.one_time_cost, self.state.time,
            )

        if action.success_probability < 1.0:
            draw = self._action_rng[action_ref].random()
            if draw > action.success_probability:
                self._log_entry(
                    "control_action_failed", action_ref, source=source,
                    probability=action.success_probability,
                    subject=subject_id,
                    policy=policy.id if policy else None,
                )
                return "failed"

        effect = self._effect_with_magnitude(
            action.effect, magnitude if magnitude is not None else action.magnitude
        )
        self._applied_by_policy_at.add((round(self.state.time, 6), action_ref))
        if action.activation_delay > 0:
            self._action_seq += 1
            self._pending_activations.append(
                _PendingActivation(
                    time=self.state.time + action.activation_delay,
                    seq=self._action_seq,
                    action_id=action_ref,
                    effect=effect,
                    source=source,
                    policy_id=policy.id if policy else None,
                    subject_id=subject_id,
                )
            )
            self._log_entry(
                "control_action_scheduled", action_ref, source=source,
                activates_at=round(self.state.time + action.activation_delay, 6),
                subject=subject_id, policy=policy.id if policy else None,
            )
            return "scheduled"

        self._activate_action(action, effect, source, policy.id if policy else None, subject_id)
        return "applied"

    def _activate_action(
        self,
        action: ControlActionDef,
        effect: EventEffect,
        source: str,
        policy_id: str | None,
        subject_id: str | None,
    ) -> None:
        """Ввести эффект действия в силу: срок, откат, повременная стоимость."""
        t = self.state.time
        term = action.term if action.reversible else None
        applied = effect
        if (
            term
            and effect.kind == EffectKind.RESOURCE_CAPACITY_DELTA
            and not effect.duration
        ):
            applied = effect.model_copy(deep=True)
            applied.duration = term
        previous_value = None
        if term and effect.kind == EffectKind.SET_PARAMETER and effect.parameter_path:
            previous_value = _read_parameter(self.model, effect.parameter_path)
        self._apply_effect(applied, {}, source)
        if term and effect.kind == EffectKind.SET_PARAMETER and effect.parameter_path:
            self._action_seq += 1
            self._pending_reversions.append(
                _PendingReversion(
                    time=t + term, seq=self._action_seq, action_id=action.id,
                    parameter_path=effect.parameter_path, previous_value=previous_value,
                )
            )
        if action.recurring_cost:
            self.economics.open_control_window(
                action.id, action.loop.value if action.loop else "",
                action.recurring_cost, t, (t + term) if term else None,
            )
        self._log_entry(
            "control_action_applied", action.id, source=source, policy=policy_id,
            subject=subject_id, loop=action.loop.value if action.loop else None,
            term=term, reversible=action.reversible,
        )

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
                self.economics.on_event(
                    EconomicBasis.UTILITY_PRODUCED, effect.utility_type_id or "", qty, t
                )
        elif effect.kind == EffectKind.REMOVE_UTILITY:
            qty = float(self._eval(effect.quantity_expression, payload) or 0.0)
            have = self.state.stock(effect.utility_type_id or "")
            take = min(qty, have)
            if take > 0:
                self.state.take_stock(effect.utility_type_id or "", take)
                self.economics.on_event(
                    EconomicBasis.UTILITY_CONSUMED, effect.utility_type_id or "", take, t
                )
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
        self.state.last_event_time[kind.id] = self.state.time
        self._n_np_events += 1
        self._pending_event_decisions.extend(self.decisions.event_triggers(kind.id))
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
        obs_t = self.observations.next_sample_time(now)
        if obs_t is not None and obs_t > now - _EPS:
            out.append((obs_t, 5, "observation", ("obs_sample", None)))
        timer_t = self.decisions.next_timer_time(now)
        if timer_t is not None:
            out.append((timer_t, 6, "decision_timer", ("decision_timer", None)))
        for res_id, calendar in sorted(self._resource_calendar.items()):
            change = calendar.next_change(now, self.horizon, self._weekday_offset)
            if change is not None:
                out.append((change, 0, f"cal:{res_id}", ("calendar", res_id)))
        if self._warmup_hours and not self._warmup_report["applied"]:
            out.append((self._warmup_hours, -1, "warmup", ("warmup", None)))
        for pending in self._pending_activations:
            out.append(
                (pending.time, 7, f"{pending.seq:012d}", ("activation", pending.seq))
            )
        for reversion in self._pending_reversions:
            out.append(
                (reversion.time, 8, f"{reversion.seq:012d}", ("reversion", reversion.seq))
            )
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
            # сценарные действия применяются после политик: приоритет у контура
            deferred: list[tuple[str, Any]] = []
            for _, order_class, _, token in batch:
                if token[0] == "control" and self.decisions.active:
                    deferred.append(token)
                    continue
                self._dispatch(token)
            self._check_continuation()
            self._try_start_waiting()
            # момент принятия решений: политики вызываются только здесь
            triggers = self._collect_decision_triggers()
            if triggers:
                self._run_decisions(triggers)
            for token in deferred:
                self._dispatch(token)
            if triggers or deferred:
                self._check_continuation()
                self._try_start_waiting()  # ТЗ: повторить попытку запуска
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
                stamp = (round(self.state.time, 6), action.action_ref or "")
                if action.action_ref and stamp in self._applied_by_policy_at:
                    # приоритет у политики: то же действие в тот же момент не дублируем
                    self._log_entry(
                        "control_action_skipped", action.action_ref, reason="policy_priority"
                    )
                elif action.action_ref:
                    self._log_entry("control_action", action.label or action.action_ref)
                    self._apply_control_action(
                        action.action_ref, source="scenario",
                        subject_ref=action.subject_ref,
                    )
                elif action.effect is not None:
                    self._log_entry("control_action", action.label or action.effect.kind.value)
                    self._apply_effect(action.effect, {}, "control")
        elif kind == "activation":
            pending = next(
                (x for x in self._pending_activations if x.seq == ref), None
            )
            if pending is not None and pending.time <= self.state.time + _EPS:
                self._pending_activations.remove(pending)
                action = self.model.control_actions.get(pending.action_id)
                if action is not None:
                    self._activate_action(
                        action, pending.effect, pending.source, pending.policy_id,
                        pending.subject_id,
                    )
        elif kind == "reversion":
            reversion = next(
                (x for x in self._pending_reversions if x.seq == ref), None
            )
            if reversion is not None and reversion.time <= self.state.time + _EPS:
                self._pending_reversions.remove(reversion)
                try:
                    _apply_parameter(
                        self.model, reversion.parameter_path, reversion.previous_value
                    )
                    self._after_parameter_change(reversion.parameter_path)
                    self._log_entry(
                        "control_action_reverted", reversion.action_id,
                        path=reversion.parameter_path, value=reversion.previous_value,
                    )
                except (KeyError, AttributeError, ValueError) as exc:
                    self._log_entry(
                        "warning", "revert_failed", action=reversion.action_id,
                        error=str(exc),
                    )
        elif kind == "calendar":
            self._apply_calendar(ref)
        elif kind == "warmup":
            if not self._warmup_report["applied"]:
                self._apply_warmup()
        elif kind == "obs_sample":
            self.observations.take_samples(self.state.time, self._observation_true_value)
        elif kind == "decision_timer":
            pass  # сам МР обрабатывается после батча, в _loop

    def _advance_to(self, t: float) -> None:
        dt = t - self.state.time
        if dt > 0:
            if self.economics.active:
                # ставки начисляются на интервале, где состояние постоянно
                self.economics.accrue_interval(
                    self.state.time, t, self._economic_basis_value
                )
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
        result.decision_log = self._decision_log
        result.policy_trace = self._policy_trace
        result.observation_error = self.observations.error_report()
        result.rejected_actions = self._rejected_actions
        result.control_costs = self._control_cost_report()
        self.economics.accrue_terminal(self.state.time, self._economic_basis_value)
        result.economics = self.economics.report(self.state.time)
        result.warmup = self._warmup_report
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
                "failed": self.state.failed_count.get(sed_id, 0),
                "suspensions": suspend_totals.get(sed_id, 0),
                "waiting_hours": round(waiting_totals.get(sed_id, 0.0), 4),
                "queue_peak": self.state.queue_peak.get(sed_id, 0),
                "still_waiting": sum(1 for i in self.state.waiting if i.sed_id == sed_id),
                "still_executing": sum(
                    1 for i in self.state.executing.values() if i.sed_id == sed_id
                ),
                "blocked_hours": blocked.get(sed_id, {}),
                "executing_hours": round(self.state.executing_hours.get(sed_id, 0.0), 4),
                "mean_waiting": round(
                    waiting_totals.get(sed_id, 0.0)
                    / max(1, self.state.completed_count.get(sed_id, 0)), 6
                ),
                "mean_service": round(
                    self.state.executing_hours.get(sed_id, 0.0)
                    / max(1, self.state.completed_count.get(sed_id, 0)), 6
                ),
            }
        for res_id, res in self.state.resources.items():
            result.resource_stats[res_id] = {
                "utilization": round(res.utilization(), 4),
                "busy_hours": round(res.busy_integral, 4),
                "capacity_hours": round(res.capacity_integral, 4),
                "peak_allocated": res.peak_allocated,
                "final_capacity": res.capacity(self.state.time),
                "nominal_capacity": res.nominal_capacity(self.state.time),
                "off_shift_hours": round(res.off_shift_hours, 4),
                "calendar_factor": res.calendar_factor,
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
            "decision_moments": self._n_decisions,
            "policy_firings": sum(1 for e in self._policy_trace if e.get("fired")),
            "observation_mode": (
                self.observations.mode_override.value
                if self.observations.mode_override
                else "per_observation"
            ),
            "sed_restarts": self._restarted,
            "sed_failures": self._failed_instances,
            "rejected_actions": len(self._rejected_actions),
            "control_cost_total": result.control_costs.get("total", 0.0),
            "measured_hours": round(max(0.0, self.state.time - self.state.stat_start_time), 6),
            "avg_wip": round(
                self.state.wip_integral
                / max(_EPS, self.state.time - self.state.stat_start_time), 6
            ),
            "avg_queue": round(
                self.state.queue_integral
                / max(_EPS, self.state.time - self.state.stat_start_time), 6
            ),
            "npv": result.economics.get("npv", 0.0),
            "warmup_hours": self._warmup_hours,
        }

    def _economic_basis_value(self, item: EconomicItem) -> float:
        """Значение базы начисления на текущем интервале состояния."""
        st = self.state
        basis = item.basis
        if basis == EconomicBasis.CONSTANT:
            return 1.0
        if basis == EconomicBasis.RESOURCE_CAPACITY:
            # номинальная ёмкость: платится независимо от графика смен
            res = st.resources.get(item.ref or "")
            return res.nominal_capacity(st.time) if res else 0.0
        if basis == EconomicBasis.RESOURCE_AVAILABLE:
            # доступная по календарю ёмкость: ФОТ по сменам
            res = st.resources.get(item.ref or "")
            return res.capacity(st.time) if res else 0.0
        if basis == EconomicBasis.RESOURCE_BUSY:
            res = st.resources.get(item.ref or "")
            return res.allocated if res else 0.0
        if basis == EconomicBasis.UTILITY_STOCK:
            return st.stock(item.ref or "")
        if basis == EconomicBasis.WIP:
            if item.ref:
                return float(
                    sum(1 for i in st.executing.values() if i.sed_id == item.ref)
                    + sum(1 for i in st.waiting if i.sed_id == item.ref)
                )
            return float(len(st.executing) + len(st.waiting))
        if basis == EconomicBasis.QUEUE:
            if item.ref:
                return float(sum(1 for i in st.waiting if i.sed_id == item.ref))
            return float(len(st.waiting))
        if basis == EconomicBasis.EXPRESSION:
            try:
                return float(self._eval(item.expression, {}) or 0.0)
            except (ExpressionError, TypeError, ValueError):
                return 0.0
        return 0.0

    def _apply_warmup(self) -> None:
        """Удалить переходный режим: статистика считается от момента разогрева."""
        st = self.state
        self._warmup_report = {
            "hours": self._warmup_hours,
            "applied": True,
            "dropped": {
                "np_events": self._n_np_events,
                "switching_moments": self._n_moments,
                "decision_moments": self._n_decisions,
                "policy_firings": sum(1 for e in self._policy_trace if e.get("fired")),
                "instances_completed": sum(st.completed_count.values()),
            },
            "stocks_at_warmup": {k: round(v, 6) for k, v in sorted(st.stocks.items())},
        }
        st.reset_statistics()
        self.observations.reset_error_stats()
        self._n_np_events = 0
        self._n_moments = 0
        self._n_decisions = 0
        self._policy_trace = []
        self._decision_log = []
        self._last_decision_time = st.time
        self._kpi_series = {k: [] for k in self.model.kpis}
        self._stock_series = {u: [] for u in self.model.utility_types}
        self._pool_series = []
        self._last_record_t = None
        self._log_entry("warmup_completed", "warmup", hours=self._warmup_hours)
        self._record_series(force=True)

    def _control_cost_report(self) -> dict[str, Any]:
        """Стоимость управления: разовая за попытки + повременная за часы."""
        return self.economics.control_cost_report(self.state.time)

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
