# Self-ticket lifecycle — design

**Date**: 2026-08-03
**Status**: validated, ready for an implementation plan
**Prior art**: `3ecb4a91` (exclusion of self-tickets from the `en_attente` group)

## 1. Problem

`brain-v42` carries 59 non-terminal tickets, of which **46 are self-tickets** (`from_project == to_project`):

| Status | Self | Cross | Total |
|---|---|---|---|
| `open` | 23 | 6 | 29 |
| `resolved` | 11 | 5 | 16 |
| `in_progress` | 12 | 1 | 13 |
| `wontfix` | 0 | 1 | 1 |

The state machine was designed for **two-party inter-project coordination**:
an executor resolves, a requester confirms. Actual usage is 78% single-party.

### 1.1 The role check is inoperative

`src/brain_v42/services/ticket_service.py:125`:

```python
expected = ticket.to_project if role == "executor" else ticket.from_project
if author != expected:
    raise NotAllowedError(...)
```

When `from_project == to_project`, both branches return the same value. The check therefore
always passes, whatever role the transition requires. It constrains nothing while claiming to
constrain — the worst of both worlds, because a reader of the code believes in a guarantee that
does not exist.

### 1.2 The confirmation loop is ceremony

`TERMINAL_STATUSES = {CLOSED, ACKED}`. A `resolved` ticket still counts as open work and
requires a `confirm` from the requester to reach `closed`.

On a self-ticket, that `confirm` is the same agent confirming its own resolution: zero
information added, one more mandatory transition. This is where tickets get stuck. An agent
resolves, considers the task done, and the system still counts it as outstanding.

### 1.3 The symptom was already patched, not the cause

`3ecb4a91` (2026-08-01) excluded self-tickets from the `en_attente` group of the briefing
query, because "waiting on the other party" makes no sense when you are both parties. The
patch addressed the display; the state machine did not move, and self-tickets stayed in the
`_CONFIRMABLE` group.

### 1.4 Verification of the 11 stuck tickets

The 11 self-tickets in `resolved` were all resolved on 2026-08-02, in a single batch.
Cross-referenced with commits from that period, **10 have an identifiable delivery**:

| Ticket | Commit |
|---|---|
| `brain_workflow_guide` (prototype and design) | `8c5a24a2`, `3ce98056`, `d27e2a73` |
| MCP ToolError baseline | `9c8aedbf` |
| 7 ToolError integration tests | `79068186` |
| `embedding_supervisor` aiohttp pin | `320bb328` |
| Hermetic catalog gate | `2277efce` and follow-ups |
| CI 4300 zero-norm | `3686805f` |
| Fake systemd dry-run | `5f88e86b` |
| `render_parent` / `render_dir` | `ac7f501e` |
| Session output schema trim | `4721c74e`, `1cea2f4c` |

The eleventh — "Separate live admission: prove backfill and two Dream canaries" — is about
production proof, not code. It is a legitimate case of "done but not yet verified", and it
alone justifies keeping the `resolved` state as an explicit option rather than removing it.

## 2. Scoping decisions

**`resolve` closes a self-ticket directly**, and an explicit action allows stopping at
`resolved`. The common case becomes free, the useful case stays expressible.

**No `wontfix_pending`.** A `wontfix` is a decision, not a delivery: there is nothing left to
verify afterward.

**The 11 existing ones are not closed by this delivery.** They are listed and verified
(§1.4); their fate is a separate operator decision from the code.

## 3. Blast radius

`impact({target: "allowed_actions", direction: "upstream"})` returns **HIGH**: 6 symbols,
3 processes, 2 modules, `direct: 3`.

| Caller | Usage |
|---|---|
| `mcp/tools/ticket_tools.py` → `brain_ticket_get` | exposes the legal actions to the agent |
| `mcp/tools/ticket_tools.py` → `brain_ticket_transition` | builds the error message |
| `codex_gateway/ticket_routes.py` → `transition_ticket` | **409** response body |

All three have the ticket at the call site, so `from_project == to_project` is computable
everywhere with no extra plumbing.

The third is an **external HTTP surface**, consumed by `red-codex`. The response shape does not
change — `allowed_actions` stays a list of strings — but its values may now include
`resolve_pending` on a self-ticket.

## 4. Design

### 4.1 A second table, without a role

`SELF_TRANSITIONS` is consulted when `from_project == to_project`. It maps
`(kind, status, action) -> new_status`, **with no `Role` field**.

This is what fixes §1.1: on a self-ticket there is only one party, so the role check is
skipped explicitly instead of being executed to always pass.

| State | Action | → | Note |
|---|---|---|---|
| `open` | `start` | `in_progress` | unchanged |
| `open`, `in_progress` | `resolve` | **`closed`** | the default |
| `open`, `in_progress` | `resolve_pending` | `resolved` | "done, still to verify" |
| `open`, `in_progress` | `wontfix` | **`closed`** | |
| `resolved` | `confirm` | `closed` | exit from `resolve_pending`, and closes the 11 existing ones |
| `resolved` | `reopen` | `open` | |
| `open`, `in_progress`, `resolved`, `wontfix` | `cancel` | `closed` | |
| `wontfix` | `confirm`, `reopen` | `closed`, `open` | inert today (0 rows) |

The last two entries are kept deliberately: no self-ticket is in `wontfix` today, but a ticket
could land there between this delivery and its rollout. Without them, that state would become
a dead end.

**`SELF_TRANSITIONS` must be complete, `fyi` included.** The table is consulted *instead of*
`TRANSITIONS` as soon as `from_project == to_project`: if it only held the `request` entries, a
self-ticket `fyi` would find no rule in it and become untransitionable. It therefore reproduces
`open -> acked` and `open -> closed` (cancel) identically, without the `Role` field. `fyi`'s
behavior does not change — it never had a confirmation loop — but it must be represented.

A test must lock in this completeness: for every reachable `(kind, status)`, the self table
exposes at least one action as long as the status is not terminal.

### 4.2 `TicketAction` gains `RESOLVE_PENDING`

A distinct action rather than a boolean parameter, because `allowed_actions` makes it
**discoverable**. An agent reading a self-ticket `open` will see
`["cancel", "resolve", "resolve_pending", "start", "wontfix"]` and understand both intents
without external documentation.

A boolean parameter would be invisible in that list: the agent would have had to read it
elsewhere. That is precisely the transmission mode that fails in practice.

`resolve_pending` is absent from the inter-project table and therefore stays **illegal** there:
a cross-project `resolve` already leads to `resolved` pending the requester, the variant would
be redundant.

### 4.3 `allowed_actions` becomes case-aware

```python
def allowed_actions(kind, status, *, self_ticket: bool = False) -> list[str]
```

The `False` default preserves current behavior: a caller that passes nothing gets the
inter-project table. Each call site opts in explicitly, which makes the change
backward-compatible and makes visible, at review time, which consumers were handled.

The `IllegalTransitionError` message must pass the same flag, or it would propose actions
inapplicable to the ticket in question.

### 4.4 What does not change

No migration: no schema change.

Inter-project tickets keep their two-party protocol identically, role check included. This is
the mandatory regression test.

The `_CONFIRMABLE` group in the briefing keeps the self-tickets, and that membership becomes
**correct**: after this change, a self-ticket in `resolved` got there through an explicit
`resolve_pending`, so it genuinely awaits verification.

## 5. Tests

**Self-tickets**

1. `resolve` from `open` → `closed`
2. `resolve` from `in_progress` → `closed`
3. `resolve_pending` from `open` → `resolved`
4. `wontfix` from `open` → `closed`
5. `confirm` from `resolved` → `closed` — the 11 existing ones stay closable
6. `reopen` from `resolved` → `open`
7. No `NotAllowedError` possible regardless of the declared author

**Inter-project, regression**

8. `resolve` → `resolved` unchanged
9. `resolve_pending` → illegal, with the allowed actions in the message
10. The role check still rejects the wrong author

**Discoverability**

11. `allowed_actions` returns two different lists depending on `self_ticket`

**Table completeness**

12. For every non-terminal `(kind, status)` — `fyi` included — `SELF_TRANSITIONS` exposes at
    least one action. Without this test, a `fyi` self-ticket would become untransitionable at
    the first missing entry.

## 6. Out of scope

- **The fate of the 11 `resolved` self-tickets** — verified in §1.4, separate operator decision.
- **Feature statuses**: three diverging sets (`VALID_FEATURE_STATUSES` at 7,
  `CREATABLE_FEATURE_STATUSES` at 6, `StatusEngine.STATUS_ORDER` at 6 and ordered), plus a
  `legacy` cited in a comment and absent everywhere else.
- **The manual-update / signals asymmetry**: `feature_service.py:264` only checks ownership, so
  `deployed -> planned` passes manually while signals are strictly monotonic.
- **The double archiving**: `status="archived"` and `archived=True` represent the same state.

These last three points form the next project.

## 7. Success criteria

After delivery, `resolve` on a self-ticket closes it in one call, `resolve_pending` remains
available and visible in `allowed_actions`, and inter-project tickets behave exactly as before,
role check included.
