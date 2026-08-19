# ADR-0004: External models via URI through a plugin adapter layer

Status: accepted · Date: 2026-08-19

## Context
The spec requires Mode B: a SED's execution — or an event stream — may be computed
by an arbitrarily detailed external model (CAD/CAE/ML/Python service) addressed by
URI. Executing arbitrary code inside the backend is forbidden by the security
requirements.

## Decision
`dtb.simulation.adapters` defines `ExternalModelAdapter` (two operations:
`execute_sed` and `next_event`) and an `AdapterRegistry`. Shipped adapters:
- `http` — POST JSON contract to an `http(s)://` URI with timeouts and an
  optional host allowlist (`DTB_EXTERNAL_MODEL_HOSTS`); never follows non-HTTP
  schemes.
- `callable` — in-process plugins registered explicitly in code (used by tests
  and future bundled models). The API never registers callables from user input.

The engine consumes only the adapter interface; the core is language-agnostic
about what runs behind the URI.

## Consequences
+ Plugin-based, safe (no code execution from model data), language-neutral.
− An unreachable external model fails the simulation run with a clear error —
  by design, no silent fallback that would fake results.
