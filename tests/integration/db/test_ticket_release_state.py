"""PostgreSQL contract for observer-only ticket release measurements."""

from datetime import UTC, datetime

import pytest

from brain_v42.repositories.pg_release_derivation import OBSERVER_IDENTITY
from brain_v42.repositories.pg_ticket import PgTicketRepo
from tests.integration.db.test_delivery_requester_acceptance import _workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

L = "a" * 40
NOW = datetime(2026, 10, 3, tzinfo=UTC)


async def test_release_state_reads_only_observer_issued_attestations(session_factory):
    ticket, _binding, service = await _workflow(session_factory)
    for kind, payload, key, identity in (
        (
            "released",
            {"repository_id": 1, "tag": "v0.6.3", "tag_sha": L, "integration_sha": L},
            "released-observer",
            "brain-v42-delivery-observer",
        ),
        (
            "deployed",
            {
                "repository_id": 1,
                "live_release_sha": L,
                "package_version": "0.6.3",
                "integration_sha": L,
            },
            "deployed-observer",
            "brain-v42-delivery-observer",
        ),
        (
            "released",
            {"repository_id": 1, "tag": "v9.9.9", "tag_sha": L, "integration_sha": L},
            "released-caller",
            "caller-declaration",
        ),
    ):
        await service.attest(
            ticket.id,
            actor_project="requester",
            caller_identity=identity,
            kind=kind,
            payload=payload,
            idempotency_key=f"{key}-{ticket.id}",
            emitted_at=NOW,
            contract_revision=1,
        )

    state = await PgTicketRepo(session_factory).release_state(ticket.id)

    assert state == (("v0.6.3",), (L,))


async def _planned(session_factory, executor: str, release: str):
    ticket, _binding, service = await _workflow(session_factory, executor=executor)
    message = await PgTicketRepo(session_factory).set_target_release(
        ticket.id, author_project=executor, expected=None, new=release, message="plan"
    )
    assert message is not None
    return ticket, service


async def _released(
    service, ticket_id, tag: str, identity: str = OBSERVER_IDENTITY, *, key: str = ""
) -> None:
    await service.attest(
        ticket_id,
        actor_project="requester",
        caller_identity=identity,
        kind="released",
        payload={"repository_id": 1, "tag": tag, "tag_sha": L, "integration_sha": L},
        idempotency_key=f"released-{tag}-{identity}-{ticket_id}{key}",
        emitted_at=NOW,
        contract_revision=1,
    )


async def test_lot_shipping_compares_the_plan_with_observer_tags_only(session_factory):
    # Releases 7.x are used by no other test of this disposable database.
    executor = "executor"
    shipped, shipped_svc = await _planned(session_factory, executor, "7.1.0")
    missing, _ = await _planned(session_factory, executor, "7.1.0")
    elsewhere, elsewhere_svc = await _planned(session_factory, executor, "7.1.0")
    declared, declared_svc = await _planned(session_factory, executor, "7.1.0")
    foreign, _ = await _planned(session_factory, "brain-v42", "7.1.0")
    await _released(shipped_svc, shipped.id, "v7.1.0")
    await _released(shipped_svc, shipped.id, "v7.1.0", key="-again")
    await _released(shipped_svc, shipped.id, "v7.1.1")
    await _released(elsewhere_svc, elsewhere.id, "v7.1.1")
    await _released(declared_svc, declared.id, "v7.1.0", identity="caller-declaration")

    lot = await PgTicketRepo(session_factory).shipped_by_release(executor, "7.1.0")

    assert lot.tag_known is True
    assert set(lot.not_shipped) == {missing.id, declared.id}
    assert foreign.id not in lot.not_shipped
    assert lot.shipped_elsewhere == ((elsewhere.id, "v7.1.1"),)


async def test_lot_tag_is_known_only_from_this_projects_observer_rows(session_factory):
    executor, other = "executor", "brain-v42"
    planned, planned_svc = await _planned(session_factory, executor, "7.2.0")
    foreign, foreign_svc = await _planned(session_factory, other, "7.2.0")
    await _released(foreign_svc, foreign.id, "v7.2.0")
    await _released(planned_svc, planned.id, "v7.2.0", identity="caller-declaration")

    lot = await PgTicketRepo(session_factory).shipped_by_release(executor, "7.2.0")

    assert lot.tag_known is False
    assert lot.not_shipped == ()
    assert lot.shipped_elsewhere == ()


async def test_malformed_observer_rows_are_skipped_not_raised(session_factory):
    ticket, _binding, service = await _workflow(session_factory)
    await _released(service, ticket.id, "latest")
    await service.attest(
        ticket.id,
        actor_project="requester",
        caller_identity=OBSERVER_IDENTITY,
        kind="deployed",
        payload={"repository_id": 1, "package_version": "0.6.3", "integration_sha": L},
        idempotency_key=f"deployed-no-sha-{ticket.id}",
        emitted_at=NOW,
        contract_revision=1,
    )
    await _released(service, ticket.id, "v0.6.4")

    assert await PgTicketRepo(session_factory).release_state(ticket.id) == (("v0.6.4",), ())
