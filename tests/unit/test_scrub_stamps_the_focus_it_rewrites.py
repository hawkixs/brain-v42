"""The scrub rewrites the prose, so its date must move — ticket `5281f0ef`.

`scrub_xml_tool_call_leak --live` strips a leaked tool call out of
`project_contexts.current_focus`. That is a mutation of the PROSE, which is
exactly what migration 040 dates. It reached the column without stamping for as
long as it existed, and `focus_updated_at` then answered "when was this written?"
with a date older than the text it describes.

Asserted on the emitted SQL rather than against a database: what has to hold is
that the statement carries the CONDITIONAL stamp — a scrub that changes nothing
must leave the age alone, and that case is real, since the regex can match a
marker and hand back an identical string.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from brain_v42.db.tables import project_contexts
from brain_v42.scripts.scrub_xml_tool_call_leak import _PROJECT_CONTEXT_COLS, _scrub_table

#: A leak the REAL scrubber strips — measured against
#: `scrub_xml_tool_call_leak`, not invented. A fixture the production regex
#: ignores would exercise nothing and still look like a test; the first
#: version of this file had exactly that, and `modified` came back 0.
_LEAK = 'the real focus\n<function_calls>\n<invoke name="x">\n</invoke>\n</function_calls>\ntail'


def _sql(statement: Any) -> str:
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


def _session_returning_one_leaky_context() -> AsyncMock:
    row = MagicMock()
    row.id = UUID("11111111-1111-1111-1111-111111111111")
    row.current_focus = _LEAK

    select_result = MagicMock()
    select_result.fetchall.return_value = [row]
    update_result = MagicMock()
    update_result.mappings.return_value.one.return_value = {
        "project_key": "brain-v42",
        "focus_revision": 4,
        "current_focus": "the real focus tail",
    }

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[select_result, update_result, MagicMock()])
    return session


@pytest.mark.asyncio
async def test_the_scrub_stamps_the_focus_it_rewrites() -> None:
    session = _session_returning_one_leaky_context()

    inspected, modified = await _scrub_table(
        session, project_contexts, _PROJECT_CONTEXT_COLS, live=True, quiet=True
    )

    assert (inspected, modified) == (1, 1)
    update_sql = _sql(session.execute.await_args_list[1].args[0])
    normalized = update_sql.replace(" =", "=").replace("= ", "=")

    assert "focus_updated_at=CASE WHEN" in normalized, update_sql
    assert "project_contexts.current_focus IS DISTINCT FROM" in update_sql, update_sql
    assert "THEN now()" in update_sql, update_sql
    assert "ELSE project_contexts.focus_updated_at" in update_sql, update_sql


@pytest.mark.asyncio
async def test_the_scrub_still_records_its_audit_row() -> None:
    """The stamp is added BESIDE the history write of 050, never instead of it."""
    session = _session_returning_one_leaky_context()

    await _scrub_table(session, project_contexts, _PROJECT_CONTEXT_COLS, live=True, quiet=True)

    inserted = _sql(session.execute.await_args_list[2].args[0])

    assert "project_focus_history" in inserted, inserted
    assert "maintenance_scrub" in inserted, inserted
