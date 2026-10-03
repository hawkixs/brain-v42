from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.models.brain_session import BrainSessionInputError
from brain_v42.models.focus_slot import FocusSlotError, SlotAnchor
from brain_v42.services.focus_slot_service import FocusSlotService

LOT = SlotAnchor(kind="lot", target_release="0.6.4")


def _service() -> tuple[FocusSlotService, MagicMock]:
    repo = MagicMock()
    repo.open = AsyncMock(return_value="opened")
    repo.list = AsyncMock(return_value="listed")
    repo.close = AsyncMock(return_value="closed")
    return FocusSlotService(repo), repo


async def test_open_canonicalises_and_trims_before_the_repository() -> None:
    service, repo = _service()
    await service.open("brain_v42", "  relay  ", "  handover  ", [LOT])
    repo.open.assert_awaited_once_with("brain-v42", "relay", "handover", [LOT])


async def test_zero_anchors_is_anchor_required_s6() -> None:
    service, repo = _service()
    with pytest.raises(FocusSlotError, match="^anchor_required: "):
        await service.open("brain-v42", "t", "b", [])
    repo.open.assert_not_awaited()


async def test_eleven_anchors_and_duplicates_are_anchor_invalid() -> None:
    service, repo = _service()
    many = [SlotAnchor(kind="pr", repository_id=1, pr_number=n) for n in range(1, 12)]
    with pytest.raises(FocusSlotError, match="^anchor_invalid: "):
        await service.open("brain-v42", "t", "b", many)
    with pytest.raises(FocusSlotError, match="^anchor_invalid: .*duplicate"):
        await service.open("brain-v42", "t", "b", [LOT, LOT])
    repo.open.assert_not_awaited()


async def test_body_over_4000_characters_is_slot_body_too_long_and_4000_is_accepted() -> None:
    service, repo = _service()
    with pytest.raises(FocusSlotError, match="^slot_body_too_long: "):
        await service.open("brain-v42", "t", "é" * 4001, [LOT])
    repo.open.assert_not_awaited()
    await service.open("brain-v42", "t", "é" * 4000, [LOT])  # 8,000 bytes, 4,000 characters
    repo.open.assert_awaited_once()


@pytest.mark.parametrize(
    ("title", "body"), [("", "b"), ("   ", "b"), ("t", "  "), ("x" * 121, "b")]
)
async def test_blank_or_long_title_and_blank_body_are_input_errors(title: str, body: str) -> None:
    service, repo = _service()
    with pytest.raises(BrainSessionInputError):
        await service.open("brain-v42", title, body, [LOT])
    repo.open.assert_not_awaited()


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (101, 0), (20, -1)])
async def test_list_bounds_are_invalid_limit(limit: int, offset: int) -> None:
    service, repo = _service()
    with pytest.raises(FocusSlotError, match="^invalid_limit: "):
        await service.list("brain-v42", "open", limit=limit, offset=offset)
    repo.list.assert_not_awaited()


async def test_close_requires_a_note_and_a_non_negative_revision() -> None:
    service, repo = _service()
    slot_id = uuid4()
    for revision, note in ((-1, "n"), (0, "  "), (0, "x" * 2001), (True, "n")):
        with pytest.raises(BrainSessionInputError):
            await service.close(slot_id, revision, note)
    await service.close(slot_id, 3, " done ")
    repo.close.assert_awaited_once_with(slot_id, 3, "done")
