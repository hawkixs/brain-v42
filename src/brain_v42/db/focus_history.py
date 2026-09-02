"""The one way a focus write leaves its trace — used by all seven writers.

Seven sites persist `project_contexts.current_focus`. Spreading the audit insert
over all of them is how an audit rots: the eighth writer copies five lines,
drops one, and the trail acquires a hole nobody can see from the data left
behind. `focus_stamp` already exists for exactly this reason, one column over.

THREE PROPERTIES, AND EACH ONE IS A DECISION

**It adds no bump.** Migration 032 assigns `NEW.focus_revision := OLD + 1` in a
BEFORE trigger whenever the text changes, and two sites — `brain_session_end`'s
CAS and `RoadmapService.update_project_focus` — set the revision explicitly
because the 037 CHECK requires it. Neither regime tolerates a third writer of
that column. So the revision is READ, never computed: callers pass what the
write returned.

**It reads the revision AFTER the write.** A value computed before is a guess
about what the trigger will do. `RETURNING focus_revision` is the only source
that holds under both regimes — trigger-assigned and explicitly set.

**It is fail-closed.** No `try/except`, deliberately, and this is where it
differs from the `dream_runs` writers next door, which are best-effort and say
so. An audit that can stay silent proves nothing: if the row cannot be written,
the focus write it describes must not commit either. Being in the caller's
transaction is what makes that automatic.

WHAT IT DOES NOT COVER, SAID RATHER THAN DISCOVERED

The deferred constraint trigger migration 050 installs sees UPDATEs only, so the
two INSERT paths (`create`, and `get_or_create`'s INSERT branch) are carried by
this function ALONE — no database guard stands behind them. That is route (b) of
the plan, chosen explicitly in 050's docstring. It makes the per-writer canary on
those two paths the only proof there is, which is why they have one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from brain_v42.db.tables import project_focus_history

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: Six sources for seven writers, mirroring 050's CHECK exactly. `focus_tool`
#: covers the roadmap CAS and the repository's `update_focus` — both are "a tool
#: asked for this focus"; `context_upsert` covers `create` and `get_or_create`,
#: which are the same write seen at birth and at conflict.
FocusHistorySource = Literal[
    "session_end",
    "focus_tool",
    "context_upsert",
    "generic_update",
    "maintenance_scrub",
]

#: How much of an actor identity the column holds. 050 sized it at 64, corrected
#: down from the proposals' 128 (R4). Truncating here rather than letting
#: Postgres raise keeps an over-long agent header from failing a focus write —
#: the identity is context for a human, never a key.
_ACTOR_CHARS = 64


async def record_focus_history(
    session: AsyncSession,
    *,
    project_key: str,
    focus_revision: int,
    focus: str | None,
    source: FocusHistorySource,
    actor: str | None = None,
) -> None:
    """Append one audit row for a focus revision that has just been persisted.

    Call it INSIDE the caller's transaction, AFTER the write, with the revision
    that write returned. `focus` is the value now stored — `None` included, since
    an erased focus is the overwrite worth recording most.

    `ON CONFLICT DO NOTHING` on `(project_key, focus_revision)`: replaying a
    persisted `brain_session_end` must not add a second row for a revision that
    already has one. It is idempotence, not tolerance — two DIFFERENT texts at
    one revision cannot happen, the revision being what the write returned.
    """
    statement = pg_insert(project_focus_history).values(
        project_key=project_key,
        focus_revision=focus_revision,
        focus=focus,
        actor=actor[:_ACTOR_CHARS] if actor else None,
        source=source,
    )
    await session.execute(
        statement.on_conflict_do_nothing(index_elements=["project_key", "focus_revision"])
    )


def focus_diff(before: str | None, after: str | None) -> dict[str, int | bool]:
    """Characters added and removed between two focus texts, plus "nothing moved".

    Visibility before any guard (graft C). The hard shrink threshold the same
    proposal carried is NOT here: an arbitrary percentage was disqualified by two
    judges and stays open question #7.

    `unchanged` is not `added == removed == 0` restated. A session close that
    re-posts the previous prose verbatim is the NORMAL regime — the CAS sets
    `expected + 1` without comparing the text — so the trail fills with content
    duplicates at the rate of session closes. Marking them beats filtering them:
    a filtered row is a row somebody has to go looking for.
    """
    before_text = before or ""
    after_text = after or ""
    grew = max(len(after_text) - len(before_text), 0)
    shrank = max(len(before_text) - len(after_text), 0)
    return {
        "added": grew,
        "removed": shrank,
        "unchanged": before_text == after_text,
    }


def focus_history_select(project_key: str, *, limit: int, offset: int) -> sa.Select:
    """Newest revision first — the order the primary key already serves."""
    return (
        sa.select(
            project_focus_history.c.focus_revision,
            project_focus_history.c.focus,
            project_focus_history.c.actor,
            project_focus_history.c.source,
            project_focus_history.c.created_at,
        )
        .where(project_focus_history.c.project_key == project_key)
        .order_by(project_focus_history.c.focus_revision.desc())
        .limit(limit)
        .offset(offset)
    )
