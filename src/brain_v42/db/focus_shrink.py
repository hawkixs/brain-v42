"""The one destructive-shrink rule for a write that REPLACES the project base focus.

Four writers replace the base focus whole: `brain_session_relay` onto the base,
`brain_session_end` of an unbound session, `brain_update_project_focus`, and the
slot-less upsert. Callers keep sending a short hand-off where a durable focus
stood, and the CAS does not notice: the revision is current, the write is legal,
the content is gone (red-rail lost 65% of its focus in one call, ticket
`91faa1a8`). The relay shipped the guard first; this module is that rule lifted
out, so that every guarded writer measures the same way and speaks the same
words.

The measure is CHARACTERS of the stripped proposal against CHARACTERS of the
stored text, as the relay always did. A refusal is strict: exactly 70% passes.
An empty or absent current focus has nothing to destroy and is never refused.
"""

from __future__ import annotations

from typing import Final

#: A replacement shorter than this share of the current base is a destructive shrink.
FOCUS_SHRINK_FLOOR_PERCENT: Final = 70


def focus_shrink_refusal(current: str | None, proposed: str, *, subject: str) -> str | None:
    """Return the refusal message when `proposed` would shrink `current` destructively.

    `subject` names what the caller sent ("the handover", "next_focus") so the
    message reads in the caller's own vocabulary. Returns None when the write is
    allowed; the caller raises its own error type with this text, and decides
    whether an operator override applies.
    """
    current_length = len(current or "")
    proposed_length = len(proposed.strip())
    if proposed_length * 100 >= current_length * FOCUS_SHRINK_FLOOR_PERCENT:
        return None
    floor = -(-current_length * FOCUS_SHRINK_FLOOR_PERCENT // 100)  # ceil, in characters
    return (
        f"{subject} has {proposed_length} characters against {current_length} in the current "
        f"base focus, under the {FOCUS_SHRINK_FLOOR_PERCENT}% floor of {floor} characters. "
        "This write REPLACES the whole base focus: carry the durable content over, or have "
        "the operator pass allow_focus_shrink=true"
    )
