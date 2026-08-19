# DTB — Architecture

DTB (Digital Twin of Business) is a platform for building, populating, simulating,
analysing and optimising a **digital twin of an enterprise's activity**, following the
domain specification in `Алгоритм_2025_10_16.docx` and `Платформа_2026_06.docx`
(the source of truth for the domain model).

This is **not** an ERP/CRM/BPM/BI/process-mining system. The twin is an *executable
model of activity*: the enterprise is a dynamic system whose state is a vector of
**utility parameters** (полезности), its activity is a hierarchy of **structural
activity elements (SEDs / СЭДы)** acting as agents, the environment acts through
**NP-events**, and model time is continuous, advancing between **switching moments
(МП)**.

## A. Current state of the repository

At project start the repository contained only `README.md` and the two source
specification documents. Everything below was designed and built from scratch.

## B. Target architecture

**Style: modular monolith** (ADR-0001) with strict module boundaries, so that the
Simulation Engine, Knowledge Graph and AI layer can later be extracted into
separate services without rewrites.

```
┌────────────────────────────────────────────────────────────────────┐
│  frontend/            self-contained SPA (no build step)           │
└───────────────▲────────────────────────────────────────────────────┘
                │ REST + SSE
┌───────────────┴────────────────────────────────────────────────────┐
│  dtb.api              FastAPI: REST API, SSE progress, jobs, auth  │
├────────────────────────────────────────────────────────────────────┤
│  dtb.services         application services / job orchestration     │
├──────────┬───────────┬───────────┬───────────┬─────────────────────┤
│dtb.domain│dtb.        │dtb.graph  │dtb.       │dtb.validation       │
│(entities,│simulation  │(knowledge │analysis   │(model validation,   │
│ scenario,│(engine,    │ + causal  │(MC,       │ data quality)       │
│ state)   │ agents,    │ graph)    │ forecast, ├─────────────────────┤
│          │ streams,   │           │ bottle-   │dtb.optimization     │
│          │ adapters)  │           │ necks,    │(objective, grid,    │
│          │            │           │ compare)  │ plugin optimizers)  │
├──────────┴───────────┴───────────┴───────────┴─────────────────────┤
│  dtb.integration      connectors: CSV/Excel/JSON, mock ERP, REST   │
├────────────────────────────────────────────────────────────────────┤
│  dtb.persistence      SQLAlchemy (SQLite→PostgreSQL), versioning,  │
│                       audit trail, time-series storage             │
├────────────────────────────────────────────────────────────────────┤
│  dtb.ai               AI assistant over the structured model       │
└────────────────────────────────────────────────────────────────────┘
```

### Bounded contexts → packages

| Bounded context            | Package              | Notes |
|----------------------------|----------------------|-------|
| Enterprise Model           | `dtb.domain`         | Enterprise, versions, container model |
| Goals                      | `dtb.domain.goals`   | goal hierarchy, target metrics |
| Activities / СЭД           | `dtb.domain.seds`    | SED definitions + lifecycle |
| Utilities                  | `dtb.domain.utilities` | utility types, flows, stocks |
| Resources / Organization / Equipment | `dtb.domain.resources` | org units, employees, equipment, ERP objects |
| Events                     | `dtb.domain.events`  | NP-event kinds, streams, effects |
| Scenario Management        | `dtb.domain.scenario` | scenarios, what-if overrides, control actions |
| Simulation Engine          | `dtb.simulation`     | **independent, framework-free, deterministic** |
| Knowledge Graph            | `dtb.graph`          | typed graph + causal queries |
| Forecasting / Analytics    | `dtb.analysis`       | Monte Carlo, forecast, bottlenecks, comparison |
| Optimization               | `dtb.optimization`   | objective/variables/constraints, grid search, plugins |
| ERP Integration            | `dtb.integration`    | connector interface, CSV/Excel/JSON, mock ERP, REST |
| Audit / Versioning         | `dtb.persistence`    | snapshots, diffs, audit trail |
| Identity / Access          | `dtb.api.security`   | API-key auth, roles, rate limiting |
| AI Assistant               | `dtb.ai`             | rule-based analysis now, LLM adapter interface |

### Rules that hold everywhere

1. `dtb.simulation` imports only `dtb.domain` and `dtb.common`. No FastAPI, no
   SQLAlchemy, no I/O besides the external-model adapter interface. It is testable
   and runnable standalone.
2. `dtb.domain` is pure pydantic definitions — no engine, no persistence.
3. Domain logic never lives in the frontend; the SPA only renders API data.
4. Every simulation result carries **provenance**: model version, scenario id +
   version, initial state hash, parameters, random seed, engine version, timestamp.
5. The engine has no hard-coded business processes: "sales" / "production" are
   demo-model content, never engine concepts.

## C. Domain model (summary — details in DOMAIN_MODEL.md)

```
Enterprise ─ has ─ Goal (hierarchy) ─ implemented by ─ SED (hierarchy, agent)
SED consumes/produces Utility, uses Resource, executed by OrgUnit
EventKind (НП-события: poisson / scheduled / external / injected) ─ triggers ─ SED
Scenario = horizon + initial state + event overrides + control actions + objective
State = utility stocks + SED pools + resource capacities + counters + KPIs
```

## D. Simulation architecture (details in SIMULATION_ENGINE.md)

Event-driven **continuous-time** simulation. Model time jumps between switching
moments (МП): the earliest of (a) next NP-event across all streams, (b) earliest
completion of an executing SED, (c) scheduled trigger / control action. At each МП
the engine processes occurrences, spawns SEDs into the *waiting pool*, moves SEDs
whose input utilities and resources are available into the *executing pool*
(consuming inputs at start, producing outputs at completion), and re-checks
continuation conditions of executing SEDs (suspending those whose resources were
lost to events). Two SED execution modes: **Mode A** (aggregated: inputs → duration
→ outputs, with stochastic durations and outcome branches) and **Mode B** (external
model via URI through a plugin adapter). Reproducibility: one master seed;
per-stream / per-SED substreams derived via SHA-256, so results are bit-identical
for identical (model, scenario, initial state, seed).

## E. Technology decisions (ADRs in docs/adr/)

| Decision | Choice | Why |
|---|---|---|
| Language / typing | Python 3.11 + pydantic v2 + type hints | best fit for simulation, Monte-Carlo, optimisation; strong runtime validation |
| API | FastAPI + SSE | typed schemas, OpenAPI for free, SSE for simulation progress |
| Transactional storage | SQLAlchemy, SQLite by default, PostgreSQL-ready | one-command local run; swap by `DTB_DATABASE_URL` |
| Graph storage | typed graph in relational rows + in-memory index | avoids a graph DB on MVP; interface allows a Neo4j/JanusGraph backend later (ADR-0002) |
| Time-series | dedicated `series` table + JSON result blobs | same migration story |
| Background jobs | thread-based job manager inside API | sufficient for MVP; queue extraction point documented |
| Frontend | self-contained vanilla JS SPA served by the API | zero build step, one-command run; graph rendering via custom SVG |
| External models | HTTP adapter + in-process plugin registry | no arbitrary code execution in the backend (ADR-0004) |

## F. MVP scope (vertical slice)

Enterprise → Goal → СЭД → Utility → Resource → Event → Scenario →
event-driven Simulation → Forecast — fully working end-to-end, plus:
knowledge graph + impact analysis, Monte Carlo, scenario comparison, bottleneck
analysis, grid-search optimization, CSV/Excel/JSON import, mock ERP connector,
model versioning + audit, validation + data quality, demo model, deterministic
replay test, SPA covering the main workflow (steps 1–14 of the UI workflow).

## G. Implementation plan

See ROADMAP.md. Phases 0–9 of the master specification are implemented in this
codebase; Phase 10 (live LLM integration) ships as an interface + rule-based
assistant (no fake LLM calls).

## Security

- Optional API-key authentication with roles (`viewer` / `editor` / `admin`) via
  `DTB_API_KEYS` (`key:role,key2:role2`). Without it the server runs in open dev
  mode and says so at startup.
- Tenant isolation by enterprise id on every route; audit log of all mutations.
- Rate limiting on import and external-call endpoints.
- External model adapters: HTTP(S) only, host allowlist via `DTB_EXTERNAL_MODEL_HOSTS`,
  timeouts; in-process adapters must be registered explicitly in code — the API
  never executes user-supplied code.

## Observability

Structured logging (`dtb.common.logging`), per-simulation metrics in every result:
simulation time, wall-clock time, number of events, active/completed SEDs, queue
sizes, failed activities, seed, model version, scenario version. Job statuses are
exposed via the API; tracing hooks are TODO (documented, not faked).

## Extraction points (post-MVP)

- Simulation Engine → worker service consuming a job queue (interface: `dtb.services.jobs`).
- Knowledge Graph → graph DB behind `dtb.graph.store.GraphStore` interface.
- Time-series → TimescaleDB/ClickHouse behind `dtb.persistence.series`.
