# DTB — Domain Model

Source of truth: `Алгоритм_2025_10_16.docx`, `Платформа_2026_06.docx`.

The enterprise is modelled as a dynamic system `dv(t) = f(v(t), u(t), t)dt + dξ(t)`:
state `v` is a vector of **utility parameters**, activity `f` is a hierarchy of
**SEDs** (структурные элементы деятельности), uncertainty `ξ` is realised as
streams of **NP-events**, and management control `u` is applied at decision
moments. In discrete decision steps: `x(i-1) = g(x(i), u(i), ζ(i), i)` with the
objective `J` optimised over multi-step decisions.

All definition-level entities are pydantic models in `dtb.domain` and serialise
to/from JSON (this is also the model-version snapshot format).

## Entity overview

```
Enterprise 1─* Goal (tree)        Goal *─* SED (IMPLEMENTS)
Enterprise 1─1 EnterpriseModel    Goal *─1 OrgUnit (responsible center)
EnterpriseModel:
  goals, utility_types, seds, resources, org_units, employees, equipment,
  erp_objects, erp_mappings, event_kinds, kpis, initial_state, plans, actuals
SED (tree) : inputs [UtilityFlow], outputs [UtilityFlow] / outcome_branches,
             resources [ResourceRequirement], duration model, triggers,
             external_model (Mode B), org_unit, goal
EventKind  : category (НП-классификация), stream (poisson|scheduled|external|none),
             effects [spawn_sed | add/remove_utility | resource_capacity_delta |
                      set_parameter | cancel_sed_instances]
Scenario   : horizon, seed, initial-stock / event-rate / SED / resource overrides,
             injected events, control actions u(t), objective, constraints
```

## Enterprise

`id, name, description, version, model_version, time_zone, simulation_calendar,
planning_horizon_hours, status (draft|active|archived), metadata`.
A digital twin may exist in many **model versions**; snapshots are stored by
`dtb.persistence.versioning` and can be diffed.

## Goal

Goals form a hierarchy (`parent_id`). A goal carries:
`responsible_org_unit_id` (centre of responsibility), `target_metrics`
(KPI id + target value + comparison + optional deadline in model hours + weight),
`constraints` (expressions), associated `sed_ids` and `resource_ids`, `priority`,
validity period, metadata. The goal tree simultaneously expresses the goal
structure, the SED hierarchy linkage (via IMPLEMENTS) and the responsibility-centre
structure (via responsible org units) — the three faces of View 1.

## Utility (полезность)

The fundamental entity. *Any* final or intermediate result of activity: products,
services, components, materials, moved stock, trained employees, money,
information, performed work, energy…

- `UtilityType` — catalogue entry: `id, name, kind, unit, quality_attributes,
  metadata`. Kinds: product, service, component, material, raw_material, energy,
  money, information, work, trained_employee, moved_stock, other.
- `UtilityFlow` — `(utility_type_id, quantity)` used as SED input/output.
- `Utility` — a concrete produced/consumed batch (instance record in the
  simulation ledger): quantity, owner, location, timestamp, source, target.
- At runtime the state keeps **stocks**: quantity per utility type (per location
  when used).

## SED — структурный элемент деятельности

Central entity; at runtime each SED instance is an **agent** with a lifecycle:

```
created → waiting → executing → completed
              ▲          │ suspended (continuation impossible) 
              └──────────┘        cancelled / failed
```

`SEDDefinition`: `id, name, goal_id, parent_id` (arbitrary-depth hierarchy:
Enterprise → Goal → Capability/СЭД → Sub-СЭД → Activity → Action is content, not a
fixed schema), `org_unit_id`, `inputs`, `outputs` or probabilistic
`outcome_branches`, `resources` (capacity requirements), `duration`
(fixed | uniform | triangular | normal | lognormal | exponential),
`triggers` (on_event | on_sed_completed | at_time | recurring, each with an
optional condition and count expression), `start_condition`,
`continuation_condition`, `max_concurrent`, `priority`, `external_model`
(Mode B URI reference), `equipment_ids`, `tags`, `metadata`.

Semantics (per the source algorithm): a triggered SED instance enters the
*waiting pool*; it starts when input utilities are in stock and required resource
capacity is free (inputs are consumed and resources allocated at start); it
completes after its sampled duration, producing outputs (branch sampled by
probability) and firing an internal completion event that can trigger dependent
SEDs; while executing it may be **suspended** back to waiting if an NP-event makes
continuation impossible (e.g. resource capacity lost), resuming later with its
remaining duration.

## Resource

Reusable capacity used by SEDs: personnel, equipment, facility, vehicle,
financial, energy, other. `capacity, unit, org_unit_id, cost_per_hour`.
Consumables are modelled as input **utilities**, not resources.
`Employee` and `Equipment` (with технологические места / tech places) are
micro-level entities that can point to the resource pool they contribute to and to
ERP objects they are imported from.

## Organization

`OrgUnit` tree (enterprise | division | department | team | responsibility_center
| position), employees with positions and skills, ERP references.

## ERP layer

`ERPObject` — an atomic object imported from an external system (`system,
object_type, external_id, payload`). `ERPMapping` links any domain entity to ERP
objects (`MAPPED_TO_ERP`). The macro model (views 1–3) is DTB-specific; views 4–7
are largely imported from ERP — see ERP_INTEGRATION.md.

## Events (НП-события)

`EventKind`: `category` (external | internal | planned | regulatory | uncertain |
sed_completion), `stream` — how occurrences arise in model time:

- `poisson(rate_per_hour)` — простейший поток, sampled in-engine;
- `scheduled(times | interval+start)` — плановые/регламентные события;
- `external(uri)` — a stream produced by an external model via adapter (Mode B for
  event flows);
- `none` — only injected by scenarios or internal logic.

`effects` describe what an occurrence does: spawn SED instances (count may be an
expression over the payload), add/remove utilities, change resource capacity
(temporarily — failures/repairs — or permanently), set model parameters, cancel
SED instances. Effects are the *only* mechanism — the engine knows no business
semantics.

## KPI

`KPIDefinition`: safe arithmetic `expression` over simulation state accessors:
`stock(u), produced(u), consumed(u), completed(sed), started(sed), events(kind),
utilization(resource), t()`. KPIs power goals, objectives, forecasts, plans.

## Scenario

`Scenario` fully parameterises one simulated future: horizon, start time,
calendar, seed, `initial_stock_overrides`, `event_rate_multipliers`,
`event_stream_overrides`, `injected_events` (t, kind, payload),
`sed_overrides` (duration scale, priority, concurrency), `resource_capacity_overrides`,
`parameter_overrides`, `control_actions` (timed effects = management control u(t)),
`objective` (KPI + direction + optional target), `constraints`, `monte_carlo_runs`,
`base_scenario_id` (what-if derivation), `version`. Scenarios are compared pairwise
or n-way in `dtb.analysis.comparison`.

## State

`dtb.simulation.state.SimulationState` (runtime, plain Python for speed):
model time, utility stocks, SED instance pools (waiting / executing / completed /
cancelled), resource capacity + allocations + temporary capacity effects,
counters (produced/consumed per utility, started/completed per SED, events per
kind), production/consumption ledger, event log. The platform separately keeps
**current** (imported actuals), **historical** (series), **plan** (series),
**forecast** (Monte-Carlo output) and **scenario states** (per-simulation).

## The 8 views (архитектурное описание, виды 1–8)

| View | Content | Where |
|---|---|---|
| 1 Goals | goal tree = SED hierarchy = responsibility centres | `goals` + IMPLEMENTS/RESPONSIBLE_FOR relations |
| 2 СЭД→Results | causal model SED→utilities | `seds` + graph PRODUCES/CONSUMES/TRIGGERS |
| 3 Results dynamics | fact/plan/forecast/history of goal attainment | `actuals`, `plans`, Monte-Carlo forecast — **the twin's main output** |
| 4 Organization | org units, штатное расписание, employees | `org_units`, `employees` |
| 5 Equipment | оборудование, технологические места | `equipment` |
| 6 Materials/Products | products, components, materials, energy | `utility_types`, `resources` |
| 7 Operational state | fact/plan/forecast of atomic ERP data | `initial_state`, `series`, ERP import |
| 8 Cross-links | links 1–2 ↔ 4–6 ↔ ERP (аналитики управленческого плана счетов) | `erp_mappings` + knowledge graph |

Views 1–3 are the macro (DTB-specific) model; 4–7 are largely ERP imports; 8 ties
macro to micro. The whole model lets goal attainment (view 3) be computed from
atomic data (view 7).
