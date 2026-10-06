# Projects, focus, slots, and tickets

This page describes the project model implemented by the repository. PostgreSQL
stores project context and knowledge; measured counts and deployment state belong
to the facts registry, not copied into project prose.

## Project keys and aliases

The canonical key rule is implemented in `src/brain_v42/models/project_key.py`.
Keys match `^[a-z0-9]+([:-][a-z0-9]+)*$`: lowercase letters and digits separated
by single hyphens or colons. A colon is a naming convention, not a parent-child
relationship. Project comparisons are exact except where an explicit project-group
scope is used.

The exact legacy aliases `brain` and `brain_v42` canonicalize to `brain-v42`.
Writes use strict canonicalization and reject malformed keys; tolerant reads may
pass a malformed key through and return no matches. `None` denotes unscoped,
global knowledge where the tool supports it. The registry and database constraints
also validate project keys. Project context keys are immutable after creation;
renaming requires a migration.

## Project records and focus

`project_contexts` holds operational project context: description, conventions,
related projects, group scope, roadmap metadata, focus, and its revision. The
`projects` registry tracks claimed, unclaimed, and archived keys; it is not the
operational project record. Archived contexts remain stored with their knowledge.

Focus is shared by a project and guarded by a monotonically increasing revision.
`brain_update_project_focus` applies its focus, blockers, feature status changes,
and pins as one validated batch under an expected-revision check. A conflict or
invalid batch writes nothing. A successful batch advances the revision. The
optional CLAUDE.md update happens after the database transaction.

Ending an unbound operator session can replace the project's base focus using
compare-and-swap. The new focus must be at least 70% of the current focus length;
shorter replacements fail with `base_focus_shrink` before writing. The explicit
operator override is `allow_focus_shrink=true`. Bound sessions write their slot's
focus and do not use this base-focus guard.

## Focus slots and session relay

ADR #34 slots let concurrent work tied to a ticket keep its own focus. Slot
anchors identify the owning tickets and are written once; slot history records
successive focus revisions. A session can bind to a slot, and its end updates that
slot under the slot revision. Receipts and observed releases can close slots when
their requirements are satisfied. A closed slot cannot be resumed as active work;
the briefing reports that state and guides the operator to relay or open another
slot.

`brain_session_relay` atomically captures and closes an open operator session,
then starts its successor under a new `client_key`. A bound session relays on its
slot; an unbound session relays onto the project base focus, using the applicable
revision and shrink guard. Relay retries are idempotent. The relay is an operator
action; it does not apply to server-owned HTTP agent traces.

## Tickets and release planning

Tickets have an owning project and may target another project. Project-group
operations use the explicit group-scope predicate; colon-prefixed keys are not
implicitly included. `target_release` is optional and accepts a bare `major.minor.patch`
version. `NULL` means no release is planned. Ticket listing can filter by target
release and summarizes planned, shipped, and differently shipped tickets from
delivery observations.

## Facts and judgments

Project focus, blockers, descriptions, conventions, and roadmap choices are
operator judgments. Revisions, timestamps, registry state, delivery observations,
and measured facts are system-maintained. Use `brain_fact_get` or
`brain_fact_list` for registered measurements rather than copying a value into a
focus field.
