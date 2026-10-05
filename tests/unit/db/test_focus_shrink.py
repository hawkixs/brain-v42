"""The one destructive-shrink rule shared by every writer of the project base focus."""

from __future__ import annotations

import pytest

from brain_v42.db.focus_shrink import focus_shrink_refusal


def _refusal(current: int, proposed: int, *, text: str = "x") -> str | None:
    return focus_shrink_refusal(text * current, text * proposed, subject="the next focus")


@pytest.mark.parametrize(
    ("current", "proposed", "refused"),
    [
        (100, 70, False),  # exactly 70%: the guard refuses strictly below
        (100, 69, True),
        (100, 100, False),
        (100, 400, False),
        (11, 8, False),  # 70% of 11 is 7.7: the smallest accepted length is 8
        (11, 7, True),
        (0, 1, False),  # no current focus: nothing to destroy
        (0, 0, False),
    ],
)
def test_the_floor_is_seventy_percent_of_the_current_base(
    current: int, proposed: int, refused: bool
) -> None:
    assert (_refusal(current, proposed) is not None) is refused


def test_a_null_current_focus_has_nothing_to_destroy() -> None:
    assert focus_shrink_refusal(None, "short", subject="the next focus") is None


def test_the_proposed_text_is_measured_stripped() -> None:
    padded = "  " + "y" * 69 + "  "
    assert focus_shrink_refusal("x" * 100, padded, subject="the next focus") is not None


def test_the_refusal_names_both_sizes_the_floor_and_the_replacement() -> None:
    message = _refusal(100, 40)
    assert message is not None
    assert "40 characters" in message and "100" in message
    assert "70" in message  # the floor, in characters
    assert "REPLACES" in message and "allow_focus_shrink" in message
    assert message.startswith("the next focus has ")
