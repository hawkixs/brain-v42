# Modular monolith with a proven DAG — layer contract

**Date**: 2026-07-30
**Status**: proposed
**Context**: theory/architecture discussion — "the project is getting big, should we move
to microservices to give a session per service to less-loaded agents?"

## Problem

The concern raised is real and documented: context is the bottleneck of code
agents. Research on *context rot* shows a precision degradation on the
18 frontier models tested as soon as the input grows, with an effective context far
below the announced limit.

The envisioned remedy — splitting `brain_v42` into microservices to get one agent
session per service — rests on a false implication:

> deployment boundary ⇒ context boundary

These are two independent axes. Context isolation is obtained via subagent, scoping
and retrieval, at zero cost. The microservice solves an organizational problem (independent
deployment, independent teams, independent scaling) at the price of versioned contracts,
distributed migrations and the loss of atomic changes.

The repo's measurement confirms it, and worsens the diagnosis.

## Measured finding

Dependency graph between top-level modules of `src/brain_v42`, built by
AST under Python 3.12:

```
_root, models              leaves
db          -> _root
repositories-> db, models
services    -> db, models, repositories, mcp*, metrics*
automation  -> _root, db, services
metrics     -> _root, db, services, automation
mcp         -> _root, db, models, repositories, services, metrics
```

One strongly connected component: `automation ↔ mcp ↔ metrics ↔ services`, i.e.
27k of the package's 39k lines.

The edges marked `*` closed it and came from **three import sites**:

| Site | Target | Symbols |
|---|---|---|
| `services/brain_service.py` | `mcp.dream_project_authorization` | `DreamProjectAuthorizationError`, `get_dream_project_scope` |
| `services/dream_run_service.py` | `metrics.collector_nightly` | `KILLSWITCHES_PATH`, `parse_killswitches` |
| `services/feature_service.py` | `mcp.tools.parsing` | `normalize_uuid_prefix`, `parse_uuid`, `resolve_entity_id` |

All three had the same nature: a low-level primitive (authorization policy, configuration
parsing, UUID parsing) parked in a high-level module. None
is an intentional architectural coupling.

Removing these three edges makes the graph fully acyclic — verified by test.

**Consequence for the original question**: splitting today would have turned this
cycle into four services calling each other in a loop over HTTP, i.e. a *distributed monolith*.
A cycle is a design flaw; no deployment topology fixes it.

## Decision

Stay a modular monolith and make acyclicity **proven in CI** rather
than hoped for. The targeted property is not "being split" but "staying splittable":
as long as the graph is a DAG, extracting a service remains mechanical. It is an
option preserved at zero cost, not an architecture chosen in advance.

Uncertainty is embraced: the concern is anticipated, not experienced. Under uncertainty, you don't
choose the architecture, you keep the choice available.

## Design

### Component 1 — `scripts/check_module_layering.py`

Follows the convention of existing preflights (`check_mcp_http_port.py`,
`check_container_image_pins.py`): dedicated error, pure `validate_*` functions,
`main() -> int` returning `2`, no new dependency (stdlib `ast`).

- `build_module_graph(package_root)` — resolves `brain_v42.X` to a real sub-package or
  to `_root` if `X` is a file module; handles relative imports of any level;
  ignores self-imports and third parties.
- `find_cycles(graph)` — Tarjan with an explicit stack, returns the SCCs of size > 1.
- `validate_module_layering(package_root)` — fails on the slightest SCC of size greater
  than one: no baseline or exception is admitted.

### Component 2 — `tests/unit/test_module_layering.py`

The tests on synthetic packages (`tmp_path`) cover package/module/relative resolution,
fail-closed on unreadable source, cycle detection and return code `2`. The test against the
real package directly requires a graph with no SCC. The checker does not claim to compute a
minimal set of edges: it only verifies the useful invariant, the DAG.

### What is not done here

The GREEN phase moves the primitives to the lower layers, with shims that
preserve the public identities. The shared symbols (`parse_uuid`,
`resolve_entity_id`) require a prior GitNexus impact analysis.

## Proposed follow-up

1. **GREEN done** — the killswitches and UUID are at the root, the Dream scope is under
   `services`, and the old paths are identity shims.
2. The contract is "zero cycles": baseline, heuristic and `xfail` were removed after
   the DAG's proof.
3. `check_module_layering.py` is wired into `test:unit`, before pytest.
4. Out of scope for this contract but in the same diagnosis: `CLAUDE.md` scoped by
   module, and splitting the four files over 1000 lines (`db/tables.py` 1510,
   `repositories/pg_graph_ledger.py` 1488, `services/brain_graph_projection.py` 1400,
   `services/graph_service.py` 947) — today's real agent friction.

## Exit criteria toward a real service

Extract a module only if one of these becomes true:

- divergent runtime constraint (the real reason for the `embedding` extraction);
- security boundary (the real reason for `codex_gateway`);
- genuinely divergent deployment cadence or criticality;
- demonstrated need for independent scaling.

"The agent has too much context" is not a criterion. A change that systematically touches
three or more modules signals bad boundaries, not a need for
services.
