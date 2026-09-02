# Ticket List Pagination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every ticket in the backlog reachable from `brain_ticket_list`, flag every truncation, and replace the ascending-age order with a documented activity order.

**Architecture:** The repository keeps the full selection of the three categories and applies a deterministic order common to all its consumers. The MCP tool then paginates each category in memory, keeps the totals, and produces a navigable notice. This separation avoids a migration and preserves `TicketGroups` as well as the session briefing.

**Tech Stack:** Python 3.12+, FastMCP 3.x, SQLAlchemy 2 async, Pydantic 2, pytest, Ruff, mypy.

## Global Constraints

- Follow RED-GREEN-REFACTOR; no production code before a test that fails for the expected reason.
- Use the worktree's ignored venv on Python 3.12.12 and invoke every tool via `.venv/bin/python -m ...`.
- Run the GitNexus upstream impact before modifying each existing symbol.
- Do not mutate any sibling ticket, do not run any direct SQL query, and do not deploy any change.
- Keep the "À traiter", "À confirmer", and "En attente de l'autre côté" categories.
- Bound `limit` to `[1, 100]`, normalize `offset` to `>= 0`, and keep the defaults `limit=10`, `offset=0`.
- Order by `updated_at DESC`, `created_at DESC`, then `id ASC`.

---

### Task 1: MCP Pagination and Notices

**Files:**
- Modify: `tests/unit/mcp/test_ticket_tools.py`
- Modify: `src/brain_v42/mcp/tools/ticket_tools.py`

**Interfaces:**
- Consumes: the complete `TicketGroups` returned by `TicketService.list_grouped(project_key)`.
- Produces: `brain_ticket_list(project_key: str, limit: int = 10, offset: int = 0) -> str` and `_format_groups(groups, project_key, limit, offset) -> str`.

- [ ] **Step 1: Add the RED test for the default-page notice**

```python
async def test_list_default_page_reports_exact_omission_and_next_call(self):
    tickets = [_ticket(title=f"ticket-{index}") for index in range(12)]
    svc = MagicMock()
    svc.list_grouped = AsyncMock(return_value=TicketGroups(a_traiter=tickets))
    tool = await _tool(_mcp_with(svc), "brain_ticket_list")

    result = await tool.fn(project_key=TO)

    assert "À traiter (12)" in result
    assert "2 omis" in result
    assert "limit=10, offset=10" in result
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run: `.venv/bin/python -m pytest tests/unit/mcp/test_ticket_tools.py::TestListAndGet::test_list_default_page_reports_exact_omission_and_next_call -q`

Expected: FAIL, because the current rendering truncates at 10 with no notice.

- [ ] **Step 3: Add the RED test for reaching later pages**

```python
async def test_list_offset_reaches_later_tickets_in_every_category(self):
    incoming = [_ticket(title=f"in-{index}") for index in range(12)]
    outgoing = [_ticket(title=f"out-{index}") for index in range(12)]
    svc = MagicMock()
    svc.list_grouped = AsyncMock(
        return_value=TicketGroups(a_traiter=incoming, en_attente=outgoing)
    )
    tool = await _tool(_mcp_with(svc), "brain_ticket_list")

    result = await tool.fn(project_key=TO, limit=5, offset=10)

    assert "in-10" in result and "in-11" in result
    assert "out-10" in result and "out-11" in result
    assert "in-9" not in result and "out-9" not in result
    assert result.count("10 avant") == 2
```

- [ ] **Step 4: Run the test and verify the expected failure**

Run: `.venv/bin/python -m pytest tests/unit/mcp/test_ticket_tools.py::TestListAndGet::test_list_offset_reaches_later_tickets_in_every_category -q`

Expected: FAIL with an unexpected `limit` argument, because the MCP contract exposes no pagination.

- [ ] **Step 5: Implement the minimal page and notice**

```python
_LIST_DEFAULT_LIMIT = 10
_LIST_MAX_LIMIT = 100


def _format_group_page(
    lines: list[str],
    *,
    label: str,
    tickets: list[Ticket],
    direction: str,
    project_key: str,
    limit: int,
    offset: int,
) -> None:
    if not tickets:
        return
    page = tickets[offset : offset + limit]
    lines.append(f"\n### {label} ({len(tickets)})")
    lines.extend(_ticket_line(ticket, direction=direction) for ticket in page)
    omitted_before = min(offset, len(tickets))
    omitted_after = max(0, len(tickets) - offset - len(page))
    omitted = len(tickets) - len(page)
    if omitted:
        notice = f"… ({omitted} omis sur cette page; {omitted_before} avant, {omitted_after} après"
        if omitted_after:
            notice += (
                "; suite: brain_ticket_list("
                f"project_key='{project_key}', limit={limit}, offset={offset + limit})"
            )
        lines.append(notice + ")")
```

Bump `brain_ticket_list` to version `1.1`, bound `limit`, normalize `offset`, pass the parameters to `_format_groups`, and document per-category pagination.

- [ ] **Step 6: Run the targeted MCP tests**

Run: `.venv/bin/python -m pytest tests/unit/mcp/test_ticket_tools.py -q`

Expected: PASS.

### Task 2: Deterministic Activity Order

**Files:**
- Modify: `tests/unit/repositories/test_pg_ticket.py`
- Modify: `src/brain_v42/repositories/pg_ticket.py`

**Interfaces:**
- Consumes: the SQLAlchemy `tickets` table with `updated_at`, `created_at`, and `id`.
- Produces: three `TicketGroups` lists in the same recent, stable order.

- [ ] **Step 1: Add the RED test for the three queries**

```python
class TestListGrouped:
    async def test_each_category_orders_recent_activity_first_with_stable_ties(self) -> None:
        empty = MagicMock()
        empty.mappings.return_value.all.return_value = []
        session = _session(empty, empty, empty)
        repo = _repo_with_session(session)

        await repo.list_grouped("brain-v42")

        for call in session.execute.await_args_list:
            sql = str(call.args[0].compile(compile_kwargs={"literal_binds": True}))
            assert "ORDER BY tickets.updated_at DESC, tickets.created_at DESC, tickets.id ASC" in sql
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run: `.venv/bin/python -m pytest tests/unit/repositories/test_pg_ticket.py::TestListGrouped::test_each_category_orders_recent_activity_first_with_stable_ties -q`

Expected: FAIL, because the queries only use `tickets.created_at ASC`.

- [ ] **Step 3: Implement the minimal order**

Replace the existing order with:

```python
.order_by(
    tickets.c.updated_at.desc(),
    tickets.c.created_at.desc(),
    tickets.c.id.asc(),
)
```

- [ ] **Step 4: Run the targeted suites**

Run: `.venv/bin/python -m pytest tests/unit/mcp/test_ticket_tools.py tests/unit/repositories/test_pg_ticket.py tests/unit/services/test_ticket_service.py -q`

Expected: PASS.

### Task 3: Inventory and Controlled Delivery

**Files:**
- Modify only if a defect is found by verification: files already listed in Tasks 1 and 2.

**Interfaces:**
- Consumes: the read-only Brain tools `brain_ticket_list` and `brain_ticket_get`.
- Produces: a final inventory of partial parents, outgoing tickets, and childless tickets; a local commit; `red-reviewer` and `red-tester` verdicts on a common HEAD.

- [ ] **Step 1: Take inventory without mutation**

Read the tickets referenced by ticket fe1c8c33 and record, for each, its status, owner, residual work, or blocker. Do not run `brain_ticket_reply` or `brain_ticket_transition`.

- [ ] **Step 2: Verify the GitNexus scope**

Run: `gitnexus_detect_changes(scope="all", worktree="/home/hawixs/.codex/worktrees/3aeb/brain_v42")`

Expected: only `brain_ticket_list`, `_format_groups`, the new rendering helper, `PgTicketRepo.list_grouped`, and their tests are affected.

- [ ] **Step 3: Run the full checks**

```bash
.venv/bin/python -m pytest tests/unit -q
.venv/bin/python -m ruff check src/ tests/
.venv/bin/python -m ruff format --check src/ tests/
.venv/bin/python -m mypy src/
```

Expected: four commands with exit code 0.

- [ ] **Step 4: Commit the ticket's scope**

```bash
git add docs/superpowers/specs/2026-07-30-ticket-list-pagination-design.md \
  docs/superpowers/plans/2026-07-30-ticket-list-pagination.md \
  src/brain_v42/mcp/tools/ticket_tools.py \
  src/brain_v42/repositories/pg_ticket.py \
  tests/unit/mcp/test_ticket_tools.py \
  tests/unit/repositories/test_pg_ticket.py
git commit -m "fix(tickets): expose complete paginated backlog"
```

- [ ] **Step 5: Get two fresh verdicts on the final HEAD**

Run `red-reviewer` and `red-tester` on the same SHA. Fix every actionable finding using RED-GREEN-REFACTOR, commit, then rerun both roles on the new SHA until two fresh positive verdicts are obtained.
