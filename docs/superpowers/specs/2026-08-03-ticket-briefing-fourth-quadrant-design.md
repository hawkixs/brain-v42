# Fourth quadrant of ticket grouping — design

**Date**: 2026-08-03
**Status**: validated, ready for implementation plan
**Prior art**: `3ecb4a91` (exclusion of self-tickets from `en_attente`)

## 1. Problem

`PgTicketRepo.list_grouped` (`src/brain_v42/repositories/pg_ticket.py:143`) splits a
project's tickets into three groups. Crossed against the two real dimensions — our role
on the ticket, and its progress — they cover only three of the four cases:

| | `to_project` = us (executor) | `from_project` = us (requester) |
|---|---|---|
| `open`, `in_progress` | `a_traiter` | `en_attente` |
| `resolved`, `wontfix` | **no group** | `a_confirmer` |

The missing quadrant is "**we delivered, the requester has not yet confirmed**". These
tickets are neither terminal nor visible: `TERMINAL_STATUSES` only contains `closed` and
`acked`, so they count as open work, but no query surfaces them to the project that
delivered them.

### 1.1 Six tickets involved, measured on 2026-08-03

| Invisible to | Awaiting confirmation from | Status | Created |
|---|---|---|---|
| red-shrik | red-story | resolved | 2026-07-05 |
| red-writer | red-story | resolved | 2026-07-12 |
| claude-dev-pc | red-codex | resolved | 2026-07-21 |
| claude-dev-pc | red-gift | resolved | 2026-07-23 |
| brain-v42 | red-writer | wontfix | 2026-07-24 |
| red-monitor | red | resolved | 2026-07-31 |

The oldest has been sitting for a month. The defect is structural to the ReD ecosystem, not
specific to brain-v42.

### 1.2 No legal action exists in this quadrant

`confirm`, `reopen` and `cancel` are all reserved for the `requester` by the `TRANSITIONS`
table. The executor who delivered can therefore transition nothing: the only move available
is a `brain_ticket_reply`, which is not a transition and stays permitted regardless of state.

This is what justifies not listing these tickets in the briefing, whose contract is
"what you can act on" — `en_attente` is already excluded for the same reason
(`session_tools.py:38-56` only renders `a_traiter` and `a_confirmer`).

## 2. Design

### 2.1 The query

Exact mirror of `en_attente`, with `to_project` and `_CONFIRMABLE`:

```python
awaiting_requester_confirmation = (
    await session.execute(
        _q(tickets.c.to_project, _CONFIRMABLE).where(
            tickets.c.from_project != tickets.c.to_project
        )
    )
).mappings().all()
```

**Excluding self-tickets is mandatory, not cosmetic.** Without it, a self-ticket in
`resolved` would surface in `a_confirmer` (`from_project` = us) *and* in the new group
(`to_project` = us), so it would be counted twice. This is the reasoning of `3ecb4a91`
applied one step further.

### 2.2 Naming

`TicketGroups` gains the field `awaiting_requester_confirmation`.

The name reuses the role vocabulary already defined in `models/ticket.py`
(`Role = Literal["executor", "requester"]`) and removes the ambiguity with `a_confirmer`,
which denotes the confirmation **we owe**, not the one **we're waiting on**.

The model becomes linguistically mixed (`a_traiter`, `a_confirmer`, `en_attente`,
`awaiting_requester_confirmation`). Aligning the other three is a rename touching
`session_tools.py` and `ticket_tools.py`: **out of scope**, mentioned in §4.

### 2.3 Briefing — counter only, no list

`session_tools.py:41` currently renders:

```
### Tickets (N à traiter · M à confirmer)
```

It becomes, only when the new group is non-empty:

```
### Tickets (N à traiter · M à confirmer · P livrés à valider)
```

No ticket from the new group is listed: the briefing keeps its actionability contract.
The label stays in French, since the briefing is written in French end to end.

**The early-return guard must be widened.** `session_tools.py:39` returns early when
`a_traiter` and `a_confirmer` are both empty. Without adding the new group to it, the exact
case this project fixes — nothing actionable but deliveries stuck — would remain invisible.
This is the situation of `red-shrik`, whose ticket has been sitting since July 5th.

### 2.4 `brain_ticket_list` — full section

`ticket_tools.py:101` includes the new group in the total, and a fourth section is rendered
by the same helper as the other three.

The label must carry both useful pieces of information: the ball is with the requester, and
we have no legal transition — the only move is a `brain_ticket_reply` nudge.

## 3. Tests

1. A cross-project ticket `resolved` with `to_project` = us lands in
   `awaiting_requester_confirmation`
2. Same for `wontfix`
3. **A self-ticket `resolved` stays in `a_confirmer` and does NOT appear in the new
   group** — locks down the no-double-counting
4. `a_traiter`, `a_confirmer` and `en_attente` are unchanged (non-regression)
5. The briefing shows the third counter when the group is non-empty, and omits it otherwise
6. The briefing renders the Tickets section even when `a_traiter` and `a_confirmer` are
   empty but the new group is not
7. `brain_ticket_list`'s total includes the new group

## 4. Out of scope

- **`resolved_at` stays `NULL` on a `wontfix`.** `ticket_service.py:135` only timestamps it
  on `RESOLVED`, so a `wontfix`'s age falls back to `created_at`. Still useful
  information, a distinct defect in another function; fixing it here would mix two
  changes.
- **Renaming the three French fields** to English (§2.2).
- **Resolving the six tickets listed in §1.1.** This project makes them visible; handling
  them is an operator decision, and five of them belong to other projects.

## 5. Success criterion

After delivery, `brain_ticket_list` on brain-v42 shows `732aa639` — the ticket opened by
red-writer that brain-v42 marked `wontfix` on July 24th, and that red-writer never
confirmed — and the briefing carries its count without listing it. The three existing
groups are identical, and no self-ticket appears twice.
