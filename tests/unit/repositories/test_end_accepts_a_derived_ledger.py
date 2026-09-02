"""ONE scene, three rails: closing a session whose ledger the SERVER filled.

The scene is the reflex of a user we do not want to punish: they close their
session saying "I produced nothing durable", while derivation has already
attributed artifacts on their behalf. Before this batch, all three rails refused
it — repository, Pydantic model, then the database CHECK — and the session
became **impossible to close**. A flag that makes a session impossible to close
cannot be armed; that is what made the whole previous batch dead code.

The XOR measured "did the client DECLARE". Derivation removes the only failure
mode it caught (produced-but-not-declared) and would now feed its signal from the
server side. **A control is hollow as soon as the controlled object can influence
its own signal.** So we are not removing a guard: we are removing a receipt the
server would issue to itself.

The replacement gate lives in `test_end_gate_is_judgement_only.py`.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.unit.repositories.test_pg_brain_session import (
    _make_session,
    _session_row,
    _terminal_router,
)


@pytest.mark.asyncio
async def test_end_accepts_a_derived_ledger_alongside_an_explicit_reason() -> None:
    """Rail 1 — the repository. The ledger is full WITHOUT the client asking."""
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    opened = _session_row()
    derived = uuid4()
    ended = _session_row(
        session_id=opened["id"],
        status="ended",
        summary="reviewed design",
        next_focus="implement tools",
        captured_knowledge_ids=[derived],
        nothing_to_capture_reason="no durable new knowledge",
    )
    _, _statements, _, factory = _make_session(
        _terminal_router(
            opened,
            updated_row=ended,
            focus_row={"current_focus": "implement tools", "focus_revision": 8},
            current_focus_row={"current_focus": "old focus", "focus_revision": 7},
            artifact_rows=[{"knowledge_id": derived, "session_id": opened["id"]}],
            # The derived artifact is in the project and in the window: that is
            # `absorb_tracer_ledger`'s invariant, which accepts ONLY what an
            # explicit capture would have accepted. `end` revalidates it, and it
            # passes.
            valid_capture_ids=[derived],
        )
    )

    result = await PgBrainSessionRepo(factory).end(
        opened["id"],
        "client-a",
        "reviewed design",
        "implement tools",
        7,
        "no durable new knowledge",
    )

    assert result.session.captured_knowledge_ids == [derived]
    assert result.session.nothing_to_capture_reason == "no durable new knowledge"


def test_the_model_accepts_an_ended_session_carrying_both() -> None:
    """Rail 2 — the PYDANTIC rail, the one the original brief forgot.

    It would have failed the batch AFTER the migration: the database would have
    accepted the row, and the model would have refused to read it back. A
    persisted session its own model cannot load is worse than a refusal at write
    time.
    """
    from brain_v42.models.brain_session import BrainSession

    now = _session_row(status="ended", summary="s", next_focus="n")
    payload = dict(now)
    payload["captured_knowledge_ids"] = [uuid4()]
    payload["attributed_knowledge_ids"] = payload["captured_knowledge_ids"]
    payload["nothing_to_capture_reason"] = "no durable new knowledge"

    session = BrainSession.model_validate(payload)

    assert session.nothing_to_capture_reason == "no durable new knowledge"
    assert len(session.captured_knowledge_ids) == 1
