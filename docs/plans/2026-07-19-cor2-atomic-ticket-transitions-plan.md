---
title: "COR2 — Atomic ticket transitions"
status: completed
completed_at: "2026-07-19"
deployed_at: "2026-07-19"
summary: "Make ticket state changes conflict-safe and commit the transition message in the same PostgreSQL transaction."
tags:
  - sol-ultra
  - cor2
  - tickets
  - atomicity
  - tdd
---

# COR2 — Atomic ticket transitions

## Goal

Replace last-write-wins ticket transitions with one compare-and-swap transaction that changes
status and records the optional transition message atomically.

## Starting evidence

- `TicketService.transition` reads the current ticket before the repository opens its update
  transaction.
- `PgTicketRepo.apply_transition` updates by ticket ID only.
- The optional message is inserted in a later transaction, so message failure can leave a
  committed state change and two concurrent valid transitions can both succeed.

## Acceptance criteria

1. The repository updates with `WHERE id = expected_id AND status = expected_status` and
   returns no ticket on a lost compare-and-swap race.
2. Status update and optional transition message execute inside one session transaction.
3. A deterministic domain conflict tells callers to reload and retry; MCP renders it without
   leaking SQL or internal details.
4. Ordinary message replies remain independent and keep their current behavior.
5. Exactly one of two transitions racing from the same initial status succeeds, and its
   message matches the winning status. The test synchronizes both services after they read
   the same initial status and before either compare-and-swap executes.
6. Injected message-insert failure rolls back the status change and leaves no partial message.
7. Public MCP schemas and the database schema remain unchanged.
8. PostgreSQL concurrency and rollback proofs require an isolated test DSN and skip safely
   when it is unavailable; no production or fallback DSN is used.
9. The compare-and-swap predicate is only ticket ID plus expected status, so a concurrent
   ordinary reply does not cause a false conflict. Existing `resolved_at`, `closed_at`, and
   `extraction_status` semantics, including the `SKIPPED` opt-out, remain covered.

## TDD sequence

1. Add non-skippable service tests for one repository call carrying expected status and
   message, no separate `add_message`, `None` becoming `TicketTransitionConflictError`, a
   bounded MCP error, and a conflict retry that does not replay the original action.
2. Add non-skippable repository tests for `WHERE id AND status`, one transaction, UPDATE
   before INSERT, no INSERT on a CAS miss, and unchanged ordinary replies.
3. Add an isolated PostgreSQL rollback test that raises during the real message INSERT after
   the real UPDATE, then verifies from a new session that the original status and zero
   messages remain.
4. Add an isolated PostgreSQL concurrency test with a one-shot barrier after both services
   read `open` and before either CAS. Race two distinct legal actions (`resolve` and `cancel`)
   under a timeout; assert one success, one conflict, one message, and winner fields/body
   matching the final ticket.
5. Implement the minimal repository/service/error changes and preserve normal replies.
6. Run targeted, integration, unit, lint, format, and type gates.

## Boundaries

No migration, distributed lock, generic unit-of-work abstraction, ticket workflow redesign,
or public tool argument is part of COR2.

## Delivery gate

An unavailable isolated PostgreSQL DSN leaves COR2 `active` with code complete and runtime
proof pending; it cannot be marked `done` from unit tests alone. Completion additionally
requires documented rollback/retry behavior, secret-safe errors, `gitnexus_detect_changes()`
before commit, a final whole-diff `SHIP` review, and a Brain update carrying the real evidence.
The gate was satisfied on 2026-07-19; production rollout evidence is recorded below.

## Delivery evidence

- Local commit `50b38bc` on `codex/sol-ultra-cor123`.
- Real PostgreSQL proofs cover rollback after INSERT failure and a deterministic race where
  exactly one transition and its matching message win.
- CAS uses only ID plus expected status; ordinary replies retain their independent INSERT
  and `updated_at` activity bump.
- Independent review approved at 99%; database schemas and MCP parameters remain unchanged.
- GitLab MR `!66` merged the work into `main` at `75c6b05`; MR pipeline `4145` and main
  pipeline `4146`, including the registry build, both passed.
- A live production race from the same initial ticket state produced exactly one winner,
  one `TicketTransitionConflictError`, and one message matching the committed status. The
  deployed MCP read the winner, and the ticket fixture was then removed.
