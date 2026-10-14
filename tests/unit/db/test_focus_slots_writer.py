from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.db import focus_slots
from brain_v42.db.focus_slots import AnchorState, anchor_states_statement, slot_satisfied
from brain_v42.repositories.pg_release_derivation import OBSERVER_IDENTITY


def _state(kind: str, done: bool) -> AnchorState:
    return AnchorState(kind=kind, ref=kind, completing_row_id=uuid4() if done else None)


def test_the_observer_identity_has_one_value() -> None:
    assert focus_slots.OBSERVER_IDENTITY == OBSERVER_IDENTITY


@pytest.mark.parametrize("shared", [False, True], ids=["exclusive", "shared"])
async def test_the_project_fence_uses_its_own_two_key_advisory_lock_class(shared: bool) -> None:
    session = AsyncMock(spec=AsyncSession)
    lock = (
        focus_slots.lock_anchor_project_shared
        if shared
        else focus_slots.lock_anchor_project_exclusive
    )
    await lock(session, project_key="brain-v42")

    session.execute.assert_awaited_once()
    statement = session.execute.await_args.args[0]
    sql = str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    assert f"{function}({0x534C4F54}, hashtext('brain-v42'))" in sql
    assert focus_slots.FOCUS_SLOT_FENCE_CLASS == 0x534C4F54


def test_without_a_lot_every_ticket_and_pr_anchor_must_be_integrated() -> None:
    assert slot_satisfied([_state("ticket", True), _state("pr", True)])
    assert not slot_satisfied([_state("ticket", True), _state("pr", False)])


def test_a_lot_anchor_decides_a_mixed_slot_q2() -> None:
    assert slot_satisfied([_state("lot", True), _state("ticket", False)])
    assert not slot_satisfied([_state("lot", False), _state("ticket", True)])
    assert not slot_satisfied([_state("lot", True), _state("lot", False)])


def test_a_slot_without_anchor_states_is_never_satisfied() -> None:
    assert not slot_satisfied([])


def test_the_predicate_reads_integration_receipts_and_observer_releases_only() -> None:
    compiled = anchor_states_statement([uuid4()]).compile(dialect=postgresql.dialect())
    values = {str(value) for value in compiled.params.values()}
    assert {"integration", "released", OBSERVER_IDENTITY} <= values
    assert "fulfilled" not in values
    assert "delivery_digest" not in str(compiled)
