# ADR-0001: Modular monolith in Python 3.11

Status: accepted · Date: 2026-08-19

## Context
The platform needs an event-driven continuous-time simulation engine, Monte-Carlo
statistics, optimization, a typed REST API, importers and a knowledge graph. The
spec allows a modular monolith for MVP if module boundaries are kept. The repo was
empty, so the stack is our choice.

## Decision
Python 3.11, pydantic v2 for the domain (runtime-validated strong typing),
FastAPI for the API, SQLAlchemy for persistence, pytest for tests. One installable
package `dtb` with strictly layered subpackages; `dtb.simulation` depends only on
`dtb.domain`/`dtb.common` and is framework-free.

## Consequences
+ Fastest path to a correct mathematical core; first-class numeric/stochastic code.
+ pydantic gives validation + JSON snapshots (versioning/import) for free.
+ FastAPI exposes typed OpenAPI; SSE covers simulation progress.
− Python is slower than JVM/Rust for huge models — mitigated by plain-Python
  runtime state (no pydantic in the hot loop) and a documented engine-extraction
  point; the external-model adapter already allows offloading heavy pieces.
