from uuid import uuid4

from sqlalchemy.dialects import postgresql

from brain_v42.db import focus_slots
from brain_v42.db.focus_slots import AnchorState, anchor_states_statement, slot_satisfied
from brain_v42.repositories.pg_release_derivation import OBSERVER_IDENTITY


def _state(kind: str, done: bool) -> AnchorState:
    return AnchorState(kind=kind, ref=kind, completing_row_id=uuid4() if done else None)


def test_the_observer_identity_has_one_value() -> None:
    assert focus_slots.OBSERVER_IDENTITY == OBSERVER_IDENTITY


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
