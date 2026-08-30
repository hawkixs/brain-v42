# Reliable pagination of `brain_ticket_list`

## Context

`brain_ticket_list(project_key="brain-v42")` announces 19 tickets to process, but `_format_groups` renders only 10. The `_LIST_CAP` constant also cuts the "À confirmer" and "En attente" categories without notice. The MCP contract exposes no parameter to reach the following rows. Upstream, `PgTicketRepo.list_grouped` sorts only by ascending creation date, which keeps the oldest tickets on the first page.

Ticket fe1c8c33 approves this fix and forbids any direct mutation of sibling tickets. The operational inventory therefore remains read-only evidence intended for the orchestrator.

## Contract adopted

`brain_ticket_list` accepts two parameters compatible with the existing call:

```python
async def brain_ticket_list(
    project_key: str,
    limit: int = 10,
    offset: int = 0,
) -> str:
```

The server bounds `limit` between 1 and 100 and clamps a negative `offset` to 0. `limit` and `offset` apply separately to each of the three categories. The headers keep the total count of tickets in the category.

Each paginated category states the exact number of tickets omitted from the page. When a next page exists, the notice gives the full call with the next `offset`. A repeated call with these offsets makes every ticket reachable.

## Order

The repository orders each category by:

1. `updated_at DESC`;
2. `created_at DESC`;
3. `id ASC` to stabilize ties.

This ordering surfaces new or recently active tickets, instead of reserving the first page for the oldest ones. The MCP description documents this order and asks to walk the flagged pages to review the full backlog, including its deadlines.

## Compatibility and limits

- The historical call with only `project_key` still renders at most 10 rows per category.
- The categories and their labels remain "À traiter", "À confirmer" and "En attente de l'autre côté".
- No database schema, status, or ticket is modified.
- The fix does not parse free-form dates present in titles or bodies. Visibility relies on activity ordering, the exact notice, and access to all pages.
- `brain_session_start` keeps its compact overview. It benefits from the new order via the repository and continues to point to `brain_ticket_list` for full pagination.

## Evidence

The MCP tests cover the default notice, access to a later page, the before/after count, category preservation, and the contract's description. A repository test compiles the three queries and verifies the deterministic order. The targeted suite is run RED then GREEN, followed by the unit suite, Ruff, formatting, and mypy.

The inventory of partial parents, outgoing tickets, and childless tickets uses only read-only Brain tools. It records the observed state and a proposed decision for the orchestrator, without any reply, transition, resolution, or closure.
