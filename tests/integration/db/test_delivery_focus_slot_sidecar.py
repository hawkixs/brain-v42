"""The real block over PostgreSQL conforms to the published contract (D12)."""

import json
from pathlib import Path

import pytest

from brain_v42.repositories.pg_focus_slot import session_slots_block
from tests.integration.db import test_delivery_focus_slots as _slots
from tests.integration.db.test_delivery_focus_slot_bound_end import bound
from tests.unit.metrics.test_session_slots_block import _conforms

# Re-exported so that this module resolves the PRIVATE-head `session_factory`: without it the
# name falls through to the parent conftest's fixture, built on the shared head, and the slot
# rows written here make every later downgrading migration test refuse (migration 060).
session_factory = _slots.session_factory
slot_project = _slots.slot_project

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
CONTRACT = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "session_slots_block.json"


async def test_the_live_block_conforms_and_names_the_bound_session(
    session_factory,
    slot_project,
):
    session_id, _key, slot_id = await bound(session_factory, slot_project)
    async with session_factory() as session:
        block = await session_slots_block(session)
    ours = next(p for p in block["projects"] if p["project"] == slot_project)
    shape = json.loads(CONTRACT.read_text(encoding="utf-8"))["shape"]["projects"][0]
    _conforms(ours, shape)
    assert ours["slots"][0]["slot_id"] == str(slot_id)
    assert ours["slots"][0]["bound_session_id"] == str(session_id)
    assert ours["sessions"][0]["slot_id"] == str(slot_id)
