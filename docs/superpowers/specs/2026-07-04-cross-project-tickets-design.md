# Design — Cross-project tickets (inter-session coordination)

**Date**: 2026-07-04
**Status**: validated (brainstorming with the user, options settled)
**Context**: Claude Code sessions from different projects (e.g. red-shrik → red-data)
need to pass requests to each other ("modify X for me") and heads-up
("I changed contract Y"). Today this is hijacked via `brain_learn`:
no recipient, no state, no reply thread, and it pollutes durable
memory with transient content.

## 1. Concept: a 2nd family, not a 7th knowledge type

The brain has a **memory** family (decision, learning, snippet, runbook, adr,
plan): embeddings, search, decay, domains, graph. Tickets are an orthogonal
**coordination** family: **addressed, transient, stateful**.

Hard consequences (non-negotiable in the implementation):
- Tickets are **outside** `brain_search`, outside embeddings, outside decay,
  outside domain classification, outside Neo4j sync.
- The only bridge to memory is extraction (§6): it's the extracted
  learning that is searchable, never the ticket.

## 2. Data model — 2 PG tables (Alembic migration)

### `tickets`

| Field | Type | Note |
|---|---|---|
| `id` | UUID PK | |
| `kind` | `request` \| `fyi` | determines the lifecycle |
| `title` | str ≤ 200 | |
| `body` | text | the initial request |
| `from_project` | canonical project_key | sender |
| `to_project` | canonical project_key | recipient |
| `status` | enum, see §3 | |
| `extraction_status` | `null` \| `pending` \| `proposed` \| `skipped` \| `done` | driven by the dream job; `pending` set on entering a terminal state |
| `created_at` / `updated_at` | timestamps | |
| `resolved_at` / `closed_at` | nullable timestamps | |

**Project validation**: `from_project` and `to_project` are canonicalized
(existing mixin `ProjectKeyCanonicalMixin`) **and validated against the registry
of existing projects** — refused if the project is unknown. Lesson from the
`brain_v42`/`brain-v42` drift: no phantom project creation from a typo.

### `ticket_messages`

| Field | Type | Note |
|---|---|---|
| `id` | UUID PK | |
| `ticket_id` | FK → tickets, ON DELETE CASCADE | |
| `author_project` | canonical project_key | which side wrote it |
| `body` | text | |
| `status_to` | nullable enum | if the message accompanies a transition — the thread tells its own story |
| `created_at` | timestamp | |

**Index**: `(to_project, status)`, `(from_project, status)` on `tickets`;
`(ticket_id, created_at)` on `ticket_messages`.

## 3. State machine

```
request : open ──start──▶ in_progress ──resolve/wontfix──▶ resolved | wontfix ──confirm──▶ closed
          (resolve/wontfix also legal from open)             └───reopen (requester)───▶ open
fyi     : open ──ack──▶ acked
```

| Actor | Allowed actions |
|---|---|
| Executor (`to_project`) | `start` (→ in_progress), `resolve`, `wontfix`, `ack` (fyi) |
| Requester (`from_project`) | `confirm` (→ closed), `reopen`, `cancel` (→ closed, at any time) |

- The loop is complete: `resolved` **and** `wontfix` wait for the
  requester's confirmation (they see it in their briefing; they can
  contest a wontfix via reply + reopen).
- Messages can be posted at any time regardless of state — the
  status constrains the *transitions*, not the discussion.
- Terminal states: `closed`, `acked` → set `extraction_status='pending'`.

## 4. MCP surface — 5 tools

| Tool | Role |
|---|---|
| `brain_ticket_create(from_project, to_project, kind, title, body)` | open |
| `brain_ticket_reply(ticket_id, author_project, body)` | post to the thread |
| `brain_ticket_transition(ticket_id, author_project, action, message?)` | `start` / `resolve` / `wontfix` / `confirm` / `reopen` / `ack` / `cancel` — **validates who has the right** based on `author_project` vs from/to, and the legality of the transition based on the current state and the kind |
| `brain_ticket_list(project_key)` | grouped by action: `a_traiter` (I'm to_project, open/in_progress) / `a_confirmer` (I'm from_project, resolved/wontfix) / `en_attente` (the other side must act) |
| `brain_ticket_get(ticket_id)` | ticket + full thread |

A single polymorphic transition tool rather than 7 micro-tools — the brain
surface is already at ~30 tools.

## 5. Briefing integration (`brain_session_start`)

New top section (it's actionable), same graceful-degrade as
the other sections (briefing spec §9):

```
### Tickets (2 to handle · 1 to confirm)
⬅️ #a1b2 [request] from red-shrik: "expose /api/signals as ndjson" (open, 2d)
⬅️ #c3d4 [fyi] from red-data: "response format changed" — to ack
➡️ #e5f6 to red-data: resolved — check and confirm
```

Cap at ~5 entries + pointer to `brain_ticket_list`.

## 6. Dream job `ticket_extract` — proposer-only

- New nightly job + **dedicated killswitch `EXTRACT`**, starts in **dry**,
  same soak trajectory as REORG.
- Scans tickets with `extraction_status='pending'`, sends the full thread to
  the LLM (NVIDIA API, deepseek strict JSON **without tools** — pattern validated by
  the domain backfill), which proposes **0..n** entities: learning or decision, with
  `target_project` chosen among {from_project, to_project} + rationale.
  0 proposals → `skipped`.
- Before any persistence, a deduplication gate computes the canonical
  embedding text of each draft and searches, within the same project, for the
  best match among active learnings and decisions, then among drafts
  already kept in the run. A cosine `>= 0.85` drops the draft, even across
  types; archived, merged or superseded entities do not block a
  new extraction.
- The corpus search stays exact and project-scoped during the soak: an
  ANN query could miss a duplicate. Its cost must be measured before WET.
- The gate is **fail-closed**: an active row with a missing embedding or one that is not
  comparable (norm `<= 1e-6`), an invalid/unavailable new vector, or
  a corpus read failure → no proposal from the run is persisted, no
  WET is applied, tickets stay `pending` and the Dream phase is
  `fail`.
- Persistence locks and revalidates the `pending` ticket. Two runners that
  scanned the same ticket therefore cannot create two batches of proposals.
- Proposals are stored in the `ticket_extraction_proposals` table
  (PROMOTE pattern): human review → apply.
- In wet mode (post-soak): direct creation with `source_type="automated"`,
  `source="ticket:<id>"`, confidence **fixed at `medium`** (lesson `6dfb9064`:
  LLM confidence calibration is flat, we don't propagate it).
- The extracted learning/decision enters normal memory (embeddings,
  graph, search) via the existing services.
- The killswitch stays in DRY after the gate ships. The move to WET still requires
  a few soak nights, calibrating the `0.85` threshold, a human
  measurement of the duplicate rate and a check on the cost of exact search.
- `--apply-ids` remains an operator override for proposals already reviewed and
  does not replay the automatic gate.

### 6.1 Extraction opt-out — `extraction='skipped'`

Some tickets must not feed durable memory: high-frequency operational
tickets (e.g. the `red-lab-factory` daemon), automated jobs,
transient signals — noise, not knowledge.

- **At creation**: `brain_ticket_create(..., extraction='skipped')` sets
  `extraction_status='skipped'` immediately; no value is generated at
  the terminal transition.
- **Terminal transition**: the side-effect that sets `extraction_status='pending'`
  on entering `closed` / `acked` is inhibited if `extraction_status` is
  already `'skipped'` — the flag is **preserved**, not overwritten.
- **Extract job**: `fetch_pending_threads` filters `WHERE extraction_status='pending'`;
  `'skipped'` tickets are invisible to the nightly LLM scan, end to end.
- **No migration**: the `'skipped'` value has been allowed by the CHECK of
  migration 028 from the start.

## 7. Non-goals v1 (explicit YAGNI)

No: priorities, multi-recipient broadcast (fan-out = create N
tickets), purging terminal tickets, Watchk integration, Neo4j sync of
tickets, inclusion in `brain_search`, notion of user/assignee
(the "identity" is the project_key, single-user).

## 8. Tests (TDD mandatory)

- **Unit**: full transition matrix (who × action × state × kind —
  legal and illegal), project registry validation, MCP tools (5),
  briefing section (rendering + cap + degrade), extract job with mocked LLM
  (0 proposals, n proposals, invalid JSON).
- **Integration**: full PG round-trip create → reply → resolve →
  confirm → extraction_status.
- Coverage ≥ 60% (existing CI gate).
