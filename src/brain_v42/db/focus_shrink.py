"""The destructive-shrink rule for a write that REPLACES a base focus or slot body.

The lifecycle end and relay replace the base focus or a bound slot body;
`brain_update_project_focus` replaces the base. Callers keep sending a short hand-off where a durable focus
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


def focus_shrink_refusal(
    current: str | None, proposed: str, *, subject: str, target: str = "base focus"
) -> str | None:
    """Return the refusal message when `proposed` would shrink `current` destructively.

    `subject` names what the caller sent ("the handover", "next_focus") so the
    message reads in the caller's own vocabulary. `target` names the base focus or
    slot body being replaced. Returns None when the write is
    allowed; the caller raises its own error type with this text, and decides
    whether an operator override applies.
    """
    current_length = len(current or "")
    proposed_length = len(proposed.strip())
    if proposed_length * 100 >= current_length * FOCUS_SHRINK_FLOOR_PERCENT:
        return None
    floor = -(-current_length * FOCUS_SHRINK_FLOOR_PERCENT // 100)  # ceil, in characters
    dropped = current_length - proposed_length
    kept_percent = proposed_length * 100 / current_length
    return (
        f"{subject} has {proposed_length} characters against {current_length} in the current "
        f"{target}, under the {FOCUS_SHRINK_FLOOR_PERCENT}% floor of {floor} characters. "
        f"It would drop {dropped} characters and keep {kept_percent:.1f}% of the current text. "
        f"This write REPLACES the whole {target}: carry the durable content over, or use "
        "allow_focus_shrink=true with an active /brain-admin elevation (end or operator relay)"
    )
