"""§5.5 — `end` says what its focus write changed, in the only shape that fits.

The plan grafts a `focus_diff` onto the `end` result: "characters added/removed
versus the CAS's base focus — visibility before any guard". The hard shrink guard
the same proposal carried is NOT here; an arbitrary percentage threshold was
disqualified by two judges and remains open question #7.

WHY IT IS A STRING AND NOT AN OBJECT — measured on 2026-09-02, not preferred.

The eight lifecycle tools had 79 bytes of output-schema margin
(`19_674 - 9_500 - 10_095`). Measured against the real `BrainSessionEndResult`,
with a control that cancels the `create_model` title bias:

    focus_diff: dict[str, int | bool] | None   +168   refused
    focus_diff: str | None = None               +95   refused
    focus_diff: str = ""                        +65   fits

Dropping the `| None` branch is what buys the 30 bytes. The empty string is
therefore load-bearing: it is the "nothing to report" value, and it is why this
field needs no null.

In situ the field costs **44**, not 65 — the derived schemas share their `$defs`.
That is the number written into `OUTPUT_SCHEMA_TOTAL`, because it is the one the
budget actually spends. 35 bytes are left after it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.db.focus_history import render_focus_diff
from brain_v42.models.brain_session import BrainSessionEndResult

# ── the renderer, pure ───────────────────────────────────────────────────────


def test_a_growing_focus_reports_what_it_gained() -> None:
    assert render_focus_diff("abc", "abcdef") == "+3/-0 chars"


def test_a_shrinking_focus_reports_what_it_lost() -> None:
    assert render_focus_diff("abcdef", "abc") == "+0/-3 chars"


def test_a_copy_forward_says_unchanged_rather_than_plus_zero_minus_zero() -> None:
    """The NORMAL regime of a close: the CAS spends the token without the text moving.

    "+0/-0 chars" is arithmetically true and operationally useless — it reads as
    a measurement failure. The word is what tells a reader the close carried the
    previous prose forward on purpose.
    """
    assert render_focus_diff("carried", "carried") == "unchanged"


def test_an_erasure_is_measured_against_what_it_destroyed() -> None:
    """A focus overwritten to NULL is the move this whole batch exists for."""
    assert render_focus_diff("prose worth keeping", None) == "+0/-19 chars"


def test_a_first_focus_is_not_reported_as_growth_from_nothing() -> None:
    """`None → text` is a birth, not an edit. Saying "+N/-0" invents a predecessor."""
    assert render_focus_diff(None, "the first prose") == "first focus"


# ── the field on the result ──────────────────────────────────────────────────


def _result(**overrides: Any) -> BrainSessionEndResult:
    session = MagicMock()
    fields: dict[str, Any] = {
        "session": session,
        "replayed": False,
        "remaining_open_session_count": 0,
        "unattributed_in_window": 0,
        "current_focus": "after",
        "current_focus_revision": 4,
        "focus_outcome": "applied",
        "focus_at_end": "after",
        "focus_revision_at_end": 4,
    }
    fields.update(overrides)
    return BrainSessionEndResult.model_construct(**fields)


def test_the_field_defaults_to_empty_so_it_needs_no_null_branch() -> None:
    """The 30 bytes that made the graft fit at all — pinned where they are spent."""
    assert BrainSessionEndResult.model_fields["focus_diff"].default == ""
    assert _result().focus_diff == ""


@pytest.mark.asyncio
async def test_a_conflict_reports_no_diff_because_no_focus_was_written() -> None:
    """The half that is easy to get wrong.

    On `conflict` the returned focus IS the stored one, so before == after and a
    naive renderer would print "unchanged" — announcing a copy-forward for a
    write that never happened. Empty is the honest value.
    """
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    session = AsyncMock()
    repo = PgBrainSessionRepo()
    row, outcome = await repo._apply_focus_if_current(
        session,
        {"current_focus": "stored", "focus_revision": 9},
        "brain-v42",
        "proposed",
        7,
    )

    assert outcome.value == "conflict"
    session.execute.assert_not_awaited()
    assert row["current_focus"] == "stored", "a conflict returns the STORED focus untouched"
