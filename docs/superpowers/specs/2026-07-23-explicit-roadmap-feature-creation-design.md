# Explicit creation of roadmap features

**Date**: 2026-07-23

**Status**: decision implemented

**Related Brain decision**: `1e9b1929`

**Partially supersedes**: `2026-07-04-roadmap-curation-design.md` §2.1 and §11,
only on ClusterGuard's exclusivity as the writer of `features`.

## Context

The roadmap was fed exclusively by ClusterGuard from an already captured
signal. This model remains suited to emergent features, but does not let an
agent or an operator declare an intent to work before an artifact is
created. Fabricating a fake signal to obtain this line would blur
provenance and make the result dependent on an implicit semantic decision.

## Decision

Add `brain_feature_create` as a second, deliberate write path:

- ClusterGuard remains the writer of signals and keeps its semantic
  deduplication;
- `brain_feature_create` creates exactly the requested feature, without invoking
  ClusterGuard;
- creation requires an existing `project_context`, validated fields and a
  usable embedding;
- an exact name duplicate, after trimming and case normalization within the same
  project, is rejected;
- any controlled error is fail-closed and persists no feature.

The explicit feature is `pinned` by default to stay visible in the roadmap.
The initial status can be any of the live statuses, but never `archived`.

## Concurrency and scope of uniqueness

The service locks the project's `project_contexts` row, revalidates the project and
the name in the same transaction, then inserts. Two concurrent **explicit**
creations of the same name therefore yield one success and one conflict.

This guarantee is not global: ClusterGuard does not take this lock, and
the table carries no unique constraint on `(project_key, lower(trim(name)))`.
A race between an explicit creation and a ClusterGuard signal can still
produce two rows. The documentation and the tool's responses must
therefore not promise cross-writer uniqueness.

A functional SQL constraint is not added in this lot: it would first require an
audit and remediation of historical duplicates, and it could reject
semantically distinct features sharing a short title. A lock protocol shared
across both writers remains a stability improvement to evaluate separately.

## Alternatives discarded

1. **Continue without explicit creation**: does not cover work planned before an
   artifact and pushes toward falsifying a signal.
2. **Route the request through ClusterGuard**: the result could be a
   link or a merge when the caller requests a deterministic creation.
3. **Add SQL uniqueness immediately**: risky migration without an inventory
   of duplicates and no product contract on identical titles.

## Verification

- payload and MCP schema validation;
- failure before embedding for a missing project or an existing exact duplicate;
- failure before write for an unavailable or invalid embedding;
- PostgreSQL test of two concurrent explicit creations;
- full unit and integration suites, Ruff and mypy before delivery.
