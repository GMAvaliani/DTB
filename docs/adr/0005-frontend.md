# ADR-0005: Self-contained SPA served by the API (no build step)

Status: accepted · Date: 2026-08-19

## Context
The platform must run locally with one command; the UI must include several graph
visualisations (goal tree, СЭД/utility causal graph, knowledge graph), a
simulation monitor and forecast charts. UI is explicitly the *last* architectural
priority — it must not compensate for the core.

## Decision
A dependency-free vanilla-JS SPA (`frontend/`) served statically by FastAPI:
hash routing, `fetch` + SSE, custom SVG renderers (layered DAG layout for the
causal graph, force layout for the knowledge graph, tree layouts, line/band
charts). No npm toolchain, no CDN dependencies — works offline.

## Consequences
+ `pip install -e . && dtb serve` is the whole story; zero frontend supply chain.
+ All domain logic stays server-side (engineering rule 3).
− A future rich editor may justify a React/TS app; the API is already the full
  contract, so the SPA is replaceable without backend changes.
