# ADR-0002: PostgreSQL-compatible relational storage; graph and time-series behind interfaces

Status: accepted · Date: 2026-08-19

## Context
The spec separates transactional, graph and time-series storage but explicitly
allows starting on PostgreSQL(+suitable graph representation) if a dedicated
graph/TS DB adds MVP complexity — as long as the architecture doesn't block a
later move.

## Decision
- Transactional: SQLAlchemy; SQLite by default (one-command local run),
  PostgreSQL via `DTB_DATABASE_URL` — no SQLite-specific SQL is used.
- Model snapshots, scenarios and simulation results are stored as validated JSON
  documents (pydantic dumps) keyed by version — this *is* the versioning format.
- Graph: typed nodes/relations in relational rows + in-memory index, accessed only
  through `dtb.graph.store.GraphStore`; a Neo4j implementation can be dropped in.
- Time-series: a dedicated `series` table (kind = actual|plan|forecast) behind
  `dtb.persistence.series`; swappable for Timescale/ClickHouse.

## Consequences
+ Single database to run locally; clean migration seams.
− Graph queries are in-process BFS, fine for MVP model sizes (10³–10⁴ nodes);
  large deployments should adopt the graph-DB backend.
