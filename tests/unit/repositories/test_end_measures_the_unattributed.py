"""`end` MEASURES what stayed outside every ledger, instead of demanding it.

The XOR asked the user to DECLARE their diligence. This counter asks nothing: it
says how many project artifacts, created during the session, belong to NO ledger.
It is an observation, not a gate — and that is the difference between informing
and punishing.

The property that makes it non-influenceable, and that a dedicated test pins:
**a session cannot lower it by doing nothing.** Inaction produces zero artifacts,
hence zero orphans, hence zero to display; the counter only goes down by actually
attributing. A counter one improves by staying silent would be exactly the
receipt just removed, under another name.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.unit.repositories.test_pg_brain_session import (
    _make_session,
    _params,
    _session_row,
    _sql,
    _terminal_router,
)


async def _end_with(unattributed: int, **overrides: object) -> tuple[object, list[object]]:
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    opened = _session_row()
    ended = _session_row(
        session_id=opened["id"],
        status="ended",
        summary="reviewed design",
        next_focus="implement tools",
        **overrides,  # type: ignore[arg-type]
    )
    _, statements, _, factory = _make_session(
        _terminal_router(
            opened,
            updated_row=ended,
            focus_row={"current_focus": "implement tools", "focus_revision": 8},
            current_focus_row={"current_focus": "old focus", "focus_revision": 7},
            remaining_open=0,
            unattributed=unattributed,
        )
    )
    result = await PgBrainSessionRepo(factory).end(
        opened["id"], "client-a", "reviewed design", "implement tools", 7, None
    )
    return result, statements


@pytest.mark.asyncio
async def test_end_reports_what_stayed_out_of_every_ledger() -> None:
    result, _statements = await _end_with(3)
    assert result.unattributed_in_window == 3  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_session_that_did_nothing_cannot_lower_the_count() -> None:
    """Inaction produces nothing to count — hence zero, never progress.

    That is what stops the counter becoming a score: it improves only by
    attributing, never by staying silent.
    """
    result, _statements = await _end_with(0)
    assert result.unattributed_in_window == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_the_window_and_the_anti_join_are_both_in_the_query() -> None:
    """Bounds of the count: the project, the window, and "in NO ledger".

    Without the anti-join it would count everything the session produced,
    including what it attributed — the figure would rise when one does well.
    """
    from brain_v42.repositories.pg_brain_session import CAPTURE_TABLES

    _result, statements = await _end_with(2)
    counts = [
        stmt
        for stmt in statements
        if "count(" in _sql(stmt) and "brain_session_artifacts" in _sql(stmt)
    ]
    assert counts, "aucune requête ne compte les artefacts hors ledger"
    query = _sql(counts[-1])
    for table, _knowledge_type in CAPTURE_TABLES:
        assert f"from {table.name}" in query, f"{table.name} hors du comptage"
    assert "not (exists" in query, "l'anti-jointure a disparu du comptage"
    assert "from brain_session_artifacts" in query
    assert "created_at >=" in query and "created_at <=" in query
    values = set(_params(counts[-1]).values())
    assert "brain-v42" in values


@pytest.mark.asyncio
async def test_the_counter_never_blocks_the_close() -> None:
    """A measure is not a gate: it cannot refuse a closure."""
    result, _statements = await _end_with(97, captured_knowledge_ids=[uuid4()])
    assert result.session.status.value == "ended"  # type: ignore[attr-defined]
    assert result.unattributed_in_window == 97  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_an_artifact_parked_in_a_tracer_counts_as_UNattributed() -> None:
    """The receipt LIED in exactly the case being repaired.

    A row parked in an `agent` tracer does have a ledger row, so the anti-join
    counted it as attributed. That is why `unattributed_in_window` already read 0
    on the day the promise was not kept — measured on the closure of 2026-08-24:
    the only derived artifact appeared neither as attributed to the user nor as
    an orphan.

    A refusal by the exclusivity rule must be VISIBLE. A fail-closed batch
    without visibility reads as a broken batch.
    """
    _result, statements = await _end_with(1)
    counts = [
        stmt
        for stmt in statements
        if "count(" in _sql(stmt) and "brain_session_artifacts" in _sql(stmt)
    ]
    query = _sql(counts[-1])
    assert "brain_sessions" in query, (
        "l'anti-jointure ignore la NATURE du propriétaire : une ligne garée "
        "dans une traçante passe encore pour attribuée"
    )
    # `agent` travels as a PARAMETER, never in the compiled text: looking for it
    # in the SQL would turn this test green having seen nothing.
    assert "agent" in set(_params(counts[-1]).values())
