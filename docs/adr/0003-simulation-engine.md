# ADR-0003: Event-driven continuous-time engine with switching moments (МП)

Status: accepted · Date: 2026-08-19

## Context
The source spec mandates continuous model time advancing between switching
moments (НП-events and SED completions) — not a fixed-timestep simulation. SEDs
are agents; between two МП nothing about SED execution or the environment changes.

## Decision
`dtb.simulation.engine.SimulationEngine` implements exactly the 6-step МП loop of
the spec: (1) sample next occurrence per NP-event stream, (2) earliest SED
completion, (3) МП = min(+ scheduled triggers/control actions), (4) advance clock
and process all occurrences at that instant in deterministic order, (5) spawn
triggered SEDs into the waiting pool and start those whose inputs/resources are
available (consume at start, produce at completion), (6) re-check continuation of
executing SEDs, suspending those invalidated by events. Determinism: a master seed
with SHA-256-derived substreams per event stream / SED / branch choice, so
identical (model, scenario, initial state, seed) ⇒ bit-identical results.
Pause/cancel are cooperative checkpoints that never affect the random sequence.

## Consequences
+ Matches the domain paper 1:1; unbounded temporal resolution; sparse events cost
  nothing.
+ Deterministic replay is a hard test-suite invariant.
− Simultaneous occurrences need a total ordering — defined as
  (time, source-priority, stable id) and documented in SIMULATION_ENGINE.md.
