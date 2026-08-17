"""The one definition of when `project_contexts.focus_updated_at` moves.

Six call sites across three modules write `current_focus`. Spreading a
five-line CASE over all of them is how a rule rots: the seventh writer copies
it and quietly drops the ELSE branch, and the column silently degenerates into
a second row timestamp.

The rule: stamp only when the stored focus text actually changes.

That condition is what keeps a copy-forward visible. `brain_session_end`
rewrites the whole focus blob every time a session closes, and
`RoadmapService.update_project_focus` re-sends the same text to consume the CAS
token on a blockers-only batch. If either refreshed the stamp, the age would
read "0j" forever and answer a question nobody asked — "when was this row
written?" instead of "how old is this prose?".

The comparison is evaluated in SQL, against the row being written, inside the
same statement. Comparing in Python would open a read-then-write window and
would need every caller to have already fetched the current value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from brain_v42.db.tables import project_contexts

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement


def focus_stamp(new_focus: ColumnElement[str] | str | None) -> sa.Case:
    """Return the SET expression for `focus_updated_at`.

    `new_focus` is the value the statement is about to write — a literal, a
    bound parameter, or `excluded.current_focus` in an upsert.

    IS DISTINCT FROM rather than `!=` so a focus moving to or from NULL counts
    as a change; `NULL != 'x'` is NULL, which would silently take the ELSE
    branch and leave the row undated.
    """
    return sa.case(
        (project_contexts.c.current_focus.is_distinct_from(new_focus), sa.func.now()),
        else_=project_contexts.c.focus_updated_at,
    )
