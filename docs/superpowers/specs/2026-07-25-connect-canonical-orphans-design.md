# CONNECT Canonical Orphans and Honest Status Design

**Date:** 2026-07-25
**Status:** Proposed for implementation
**Scope:** Dream CONNECT domain classification

## Problem

CONNECT reads classification orphans from Neo4j but writes domain relations through the canonical PostgreSQL graph ledger. Neo4j retains merged entities to preserve lineage. Six merged Learnings therefore satisfy the Neo4j orphan query even though `brain_entities.lifecycle = 'archived'`. The ledger rejects each relation because both endpoints must be active.

The phase then prints `STEP_B ... errors=6`, but Dream records CONNECT as `done`. The orchestrator trusts the agent process exit code and does not validate the two-line CONNECT report.

## Goals

- Select classification candidates from the canonical PostgreSQL ledger when durable graph writes are enabled.
- Return only active knowledge entities with neither an active `RELATED_TO` relation nor an active outgoing `BELONGS_TO_DOMAIN` relation.
- Apply `project_key` and `limit` before returning candidates, without Neo4j limit starvation.
- Mark CONNECT non-OK when either summary reports errors or the report violates its exact two-line contract.
- Preserve the legacy Neo4j read path when the graph ledger is disabled.

## Non-goals

- Remove archived lineage nodes from Neo4j.
- Change graph projection or rebuild Neo4j.
- Change `brain_assign_domain` outcomes. GitNexus reports CRITICAL upstream impact for `DurableGraphService.link_entity_to_domain`; excluding inactive candidates fixes the production path without widening that contract.
- Change other Dream phase report contracts.

## Considered approaches

### 1. Filter candidates after the existing Neo4j query

Join returned UUIDs to `brain_entities` and discard inactive rows in the MCP tool. This is small but incorrect: Neo4j applies `LIMIT` before PostgreSQL filtering, so enough archived rows can hide active candidates indefinitely.

### 2. Project lifecycle into Neo4j

Add `lifecycle` to every projected node and filter Cypher on `active`. This keeps the existing query shape but requires projection changes and a recovery or rebuild to populate existing nodes.

### 3. Query canonical orphans from PostgreSQL

Add a ledger read and expose it through `DurableGraphService`. This makes the write authority the read authority, applies lifecycle and limit in one query, and leaves the legacy service unchanged. This is the selected approach.

For phase status, a CONNECT-specific validator follows the existing PROMOTE and REORG post-validator pattern. A global rule that rejects every failed MCP call could change unrelated phases and would still miss tools that return an error outcome as a successful MCP response.

## Design

### Canonical candidate selection

`PgGraphLedgerRepo.list_active_classification_orphans(limit, project_key)` will query `brain_entities` with these predicates:

- `lifecycle = 'active'`;
- `entity_type IN ('decision', 'learning', 'snippet', 'runbook', 'adr')`;
- `source_uuid IS NOT NULL`;
- `project_key = :project_key` when scoped;
- no active `RELATED_TO` relation where the entity is either endpoint;
- no active outgoing `BELONGS_TO_DOMAIN` relation.

The query will order by `created_at, id` before applying `LIMIT`, producing deterministic batches. It will return `source_uuid` and `entity_type`.

`DurableGraphService.find_orphans_for_classification()` will call this ledger method when enabled and map entity types to the existing Neo4j-compatible labels (`Decision`, `Learning`, `Snippet`, `Runbook`, `ADR`). When disabled, it will delegate unchanged to `GraphService`. The MCP tool will keep its current hydration and JSON contract.

### Honest CONNECT status

A focused `scripts/dream/connect_validate.py` module will parse the final report. A valid report contains exactly one `STEP_A` line and one `STEP_B` line in the documented field order. The validator will fail when:

- a line is absent, duplicated, or malformed;
- any numeric count is negative;
- `STEP_A.errors > 0` or `STEP_B.errors > 0`;
- freshness falls outside `0.00..1.00`.

On failure, the validator will mark the latest CONNECT `dream_runs` row for the run date as `partial`, store a bounded error message, and exit non-zero. `scripts/dream.sh` will translate that exit code to `phase_rc=1`, so the nightly summary cannot remain `8/8 phases OK`. A valid zero-error report exits zero.

## Error handling

The ledger query is read-only and propagates database failures, causing the MCP call and phase to fail closed. The validator fails closed on malformed reports. It will report a missing `dream_runs` row instead of silently accepting a phase it cannot mark partial.

## Tests

- Repository integration tests will prove that active orphans are returned, archived or merged sources are excluded before `LIMIT`, active relations exclude both RELATED_TO orientations, domain membership excludes its source, and project scoping holds.
- Durable service unit tests will prove canonical delegation, label mapping, and legacy fallback.
- CONNECT validator unit tests will cover a valid report, each non-zero error bucket, malformed or duplicate lines, invalid freshness, and partial-row persistence.
- Dream shell contract coverage and `bash -n scripts/dream.sh` will verify validator wiring.
- Existing graph ledger, Dream runner, and MCP orphan-list tests will run as regression suites.

## Success criteria

Given the six merged Dream-failure Learnings observed on 2026-07-25, canonical listing returns none of them. Active unclassified entities remain eligible. A CONNECT report with `errors=6` produces a non-zero phase result and a `partial` database row; a valid zero-error report remains `done`.
