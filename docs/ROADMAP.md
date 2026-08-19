# DTofBO — Roadmap

## Delivered in this codebase (MVP vertical slice + core phases)

- **Phase 0 — Discovery**: source docs analysed; ARCHITECTURE.md, DOMAIN_MODEL.md,
  ADRs 0001–0005, this roadmap.
- **Phase 1 — Domain core**: Enterprise, Goal, СЭД, Utility, Resource,
  Organization, Equipment, ERP objects, Event kinds, KPI, Scenario, State.
- **Phase 2 — Graph**: causal model + knowledge graph, projection from the domain
  model, neighborhood/path/impact/dependency queries, custom nodes/relations.
- **Phase 3 — Simulation Engine**: event-driven continuous-time engine (МП loop),
  SED agents with waiting/executing pools, Poisson/scheduled/external streams,
  Mode A + Mode B adapters, deterministic seeding, provenance.
- **Phase 4 — API**: REST (FastAPI) for enterprises/model entities/scenarios/
  simulations/graph/forecasts/import/optimization/audit + SSE progress.
- **Phase 5 — UI**: self-contained SPA — dashboard, goal tree, СЭД/utility graph,
  organization, resources/equipment, events, scenario builder + what-if,
  simulation monitor (SSE), timeline, forecast, comparison, knowledge-graph
  explorer, import center, validation report, versions, audit.
- **Phase 6 — ERP import**: CSV/Excel/JSON importers (8-view workbook layout),
  mock ERP connector + normalization, generic REST connector, connector interface.
- **Phase 7 — Forecast / Analytics**: actual/plan/forecast series, deviation
  analysis, bottleneck analysis.
- **Phase 8 — Monte Carlo**: multi-run stochastic simulation, expected values,
  variance, quantiles, confidence intervals, goal-achievement probability.
- **Phase 9 — Optimization**: objective-function & decision-variable interfaces,
  constraints, grid search over scenario space, optimizer plugin registry.
- **Phase 10 — AI (foundation)**: rule-based assistant over the structured model
  (explain, search, dependency & deviation analysis, missing-link suggestions,
  model questions), `LLMProvider` interface + change-proposal workflow
  (validate → preview → diff → confirm). Live LLM wiring is intentionally a
  pluggable TODO — no fake calls.

## Next iterations (planned, not faked)

1. **Optimization depth**: dynamic programming over МП steps (state/decision
   tabulation per the source spec §(4)–(6)), stochastic & evolutionary
   optimizers via the plugin interface, RL hook.
2. **Graph/TS storage**: optional Neo4j backend for `GraphStore`;
   Timescale/ClickHouse backend for series.
3. **Real ERP adapters**: 1С, Галактика connectors on the existing interface;
   scheduled Actualizer agents.
4. **Engine service extraction**: job queue (e.g. Redis/NATS) + worker pool;
   horizontal Monte-Carlo fan-out.
5. **AuthN/Z**: OIDC, full RBAC matrix, per-tenant encryption at rest.
6. **AI**: LLM provider implementations (Claude API), NL→graph query, report
   generation grounded in simulation provenance.
7. **UI**: drag-and-drop editing of the causal graph (create/move/link nodes is
   present; richer inline property editors and undo/redo pending), collaborative
   editing.
8. **Calendars**: working calendars/shifts affecting durations and streams.
9. **Locations**: multi-location utility stocks and transfer SEDs as first-class
   UI concepts (engine already keys ledger records by location metadata).
10. **Continuous validation**: CI pipeline with mutation tests on the engine.
