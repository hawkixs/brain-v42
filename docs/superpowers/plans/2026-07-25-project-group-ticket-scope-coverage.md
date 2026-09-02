# Project Group Ticket Scope Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cover `ProjectGroupTicketService` and hide any ticket with no participant in the group, even if the supplied actor belongs to the group.

**Architecture:** Keep `TicketService` as the owner of business rules. Strengthen only the group fence: lock the union of the useful registry rows, then separately prove that a participant and, where applicable, the actor belong to the group. The tests use async SQLite with the fence's real SQL queries and a minimal double for the canonical service.

**Tech Stack:** Python 3.12+, pytest 9.1.1, pytest-asyncio 1.4.0, SQLAlchemy 2 async, aiosqlite.

## Global Constraints

- Follow RED → GREEN → REFACTOR and keep proof of the expected failure.
- Modify only `ProjectGroupTicketService._lock_participants_scope` in production code.
- Preserve the registry lock during the call to the canonical service.
- Hide out-of-group tickets with `TicketNotFoundError`.
- Do not run any live PostgreSQL mutation, deployment, merge, or push.

---

### Task 1: Test and harden the project-group ticket fence

**Files:**
- Create: `tests/unit/services/test_project_group_ticket_service.py`
- Modify: `src/brain_v42/services/project_group_ticket_service.py`
- Modify: `docs/plans/2026-07-11-sol-ultra-audit-roadmap-plan.md`

**Interfaces:**
- Consumes: `ProjectGroupTicketService`, `TicketCreate`, `TicketService`, `project_contexts`, and `tickets`.
- Produces: unchanged public methods `create`, `reply`, `transition`, and `get_with_thread`; stronger hiding for tickets whose participants are all outside the configured group.

- [x] **Step 1: Write the failing regression test and behavioral coverage**

Create an async SQLite fixture with minimal `project_contexts` and `tickets` tables. Add this regression first:

```python
async def test_reply_hides_outside_ticket_from_an_in_group_actor(ticket_scope_store) -> None:
    ticket_id = await _seed_ticket(
        ticket_scope_store,
        from_project="outside-a",
        to_project="outside-b",
    )
    service, _ = _service(ticket_scope_store)

    with pytest.raises(TicketNotFoundError):
        await service.reply(ticket_id, "red:operator", "must stay hidden")
```

Also cover allowed base/subpartition access, non-recursive colonized bases, outside actors, missing
tickets, create delegation, transition delegation, and read masking. Each test must assert one
caller-visible result or exception.

- [x] **Step 2: Run RED**

Run:

```bash
uv run --offline --extra dev python -m pytest \
  tests/unit/services/test_project_group_ticket_service.py -q
```

Expected: the regression fails because `reply` delegates the outside ticket instead of raising `TicketNotFoundError`; neighboring behavior tests pass.

- [x] **Step 3: Implement the minimal fence correction**

In `_lock_participants_scope`, lock registry rows matching the actor or either participant. Derive these two predicates from the locked rows:

```python
participant_in_scope = in_scope(from_project) or in_scope(to_project)
actor_in_scope = actor_project is None or in_scope(actor_project)
if not participant_in_scope or not actor_in_scope:
    return False
```

Keep the exact participant-row lock and the surrounding transaction unchanged.

- [x] **Step 4: Run GREEN and the coverage gate**

Run:

```bash
uv run --offline --extra dev python -m pytest \
  tests/unit/services/test_project_group_ticket_service.py \
  --cov=brain_v42 \
  --cov-report=json:/tmp/brain-v42-c552-project-group-coverage.json -q
jq -e \
  '.files["src/brain_v42/services/project_group_ticket_service.py"].summary.percent_statements_covered >= 70' \
  /tmp/brain-v42-c552-project-group-coverage.json
```

Expected: all 10 tests pass and `jq` confirms at least 70% statement coverage for the module. The
package-level coverage target avoids a Python 3.14/pytest-cov duplicate-import failure seen when
the dotted submodule itself is passed to `--cov`. The review regression for a colonized group base
must first fail with `DID NOT RAISE NotAllowedError`, then pass after aligning the Python fence with
the SQL non-recursive rule.

- [x] **Step 5: Update tracking and verify the repository**

Record the first `5619c851…` sub-lot in the Sol Ultra roadmap and ticket thread. Run targeted tests, the full unit suite, `ruff check`, `ruff format --check`, `mypy src/`, `git diff --check`, and `gitnexus_detect_changes()`.

- [x] **Step 6: Commit atomically**

Stage only the service, its tests, and the two plan documents. Commit with:

```bash
git commit -m "🐛 fix(tickets): hide out-of-group tickets from scoped actors"
```

Keep the detached worktree intact; do not merge or push.

## Self-review

- Spec coverage: the regression, allowed paths, masking, coverage gate, tracking, and verification all map to explicit steps.
- Placeholder scan: every step names its concrete input, action, and expected result.
- Type consistency: the plan preserves every public signature and names the single modified production method exactly.
