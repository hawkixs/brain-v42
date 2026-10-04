"""PostgreSQL contract for observer-only ticket release measurements."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import delivery_artifact_bindings
from brain_v42.repositories.pg_release_derivation import OBSERVER_IDENTITY
from brain_v42.repositories.pg_ticket import PgTicketRepo
from tests.integration.db.test_delivery_requester_acceptance import _workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

L = "a" * 40
STALE = "9" * 40
NOW = datetime(2026, 10, 3, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def _release_the_observer_queue(session_factory):
    """An observed, active binding is a derivation candidate for the shared observer queue.

    The observer tests of this disposable database count provider requests: the
    merges given to the bindings below must not outlive the test that made them.
    """
    yield
    async with session_factory.begin() as session:
        await session.execute(
            delivery_artifact_bindings.update()
            .where(delivery_artifact_bindings.c.integration_sha.in_([L, STALE]))
            .values(integration_sha=None)
        )


async def _merged_at(session_factory, ticket_id, sha: str = L) -> None:
    """Give the ticket's binding the merge the observer rows below measure."""
    async with session_factory.begin() as session:
        await session.execute(
            delivery_artifact_bindings.update()
            .where(delivery_artifact_bindings.c.ticket_id == ticket_id)
            .values(integration_sha=sha)
        )


async def test_release_state_reads_only_observer_issued_attestations(session_factory):
    ticket, _binding, service = await _workflow(session_factory)
    await _merged_at(session_factory, ticket.id)
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
            actor_project="executor",
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
    await _merged_at(session_factory, ticket.id)
    message = await PgTicketRepo(session_factory).set_target_release(
        ticket.id, author_project=executor, expected=None, new=release, message="plan"
    )
    assert message is not None
    return ticket, service


async def _released(
    service,
    ticket_id,
    tag: str,
    identity: str = OBSERVER_IDENTITY,
    *,
    key: str = "",
    actor_project: str = "executor",
) -> None:
    await service.attest(
        ticket_id,
        actor_project=actor_project,
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
    await _released(foreign_svc, foreign.id, "v7.2.0", actor_project=other)
    await _released(planned_svc, planned.id, "v7.2.0", identity="caller-declaration")

    lot = await PgTicketRepo(session_factory).shipped_by_release(executor, "7.2.0")

    assert lot.tag_known is False
    assert lot.not_shipped == ()
    assert lot.shipped_elsewhere == ()


async def test_malformed_observer_rows_are_skipped_not_raised(session_factory):
    ticket, _binding, service = await _workflow(session_factory)
    await _merged_at(session_factory, ticket.id)
    await _released(service, ticket.id, "latest")
    await service.attest(
        ticket.id,
        actor_project="executor",
        caller_identity=OBSERVER_IDENTITY,
        kind="deployed",
        payload={"repository_id": 1, "package_version": "0.6.3", "integration_sha": L},
        idempotency_key=f"deployed-no-sha-{ticket.id}",
        emitted_at=NOW,
        contract_revision=1,
    )
    await _released(service, ticket.id, "v0.6.4")

    assert await PgTicketRepo(session_factory).release_state(ticket.id) == (("v0.6.4",), ())


async def test_rows_of_a_merge_the_ticket_no_longer_stands_on_are_not_shown(session_factory):
    ticket, service = await _planned(session_factory, "executor", "7.3.0")
    await _released(service, ticket.id, "v7.3.0")
    await service.attest(
        ticket.id,
        actor_project="executor",
        caller_identity=OBSERVER_IDENTITY,
        kind="deployed",
        payload={
            "repository_id": 1,
            "live_release_sha": L,
            "package_version": "7.3.0",
            "integration_sha": L,
        },
        idempotency_key=f"deployed-stale-{ticket.id}",
        emitted_at=NOW,
        contract_revision=1,
    )
    repo = PgTicketRepo(session_factory)
    assert await repo.release_state(ticket.id) == (("v7.3.0",), (L,))

    await _merged_at(session_factory, ticket.id, sha=STALE)
    assert await repo.release_state(ticket.id) == ((), ())
    lot = await repo.shipped_by_release("executor", "7.3.0")
    assert (lot.tag_known, lot.not_shipped) == (True, (ticket.id,))

    await _merged_at(session_factory, ticket.id)
    async with session_factory.begin() as session:
        await session.execute(
            delivery_artifact_bindings.update()
            .where(delivery_artifact_bindings.c.ticket_id == ticket.id)
            .values(active=False)
        )
    assert await repo.release_state(ticket.id) == ((), ())


async def test_observer_rows_issued_by_the_requester_project_are_not_shown(session_factory):
    ticket, _binding, service = await _workflow(session_factory)
    await _merged_at(session_factory, ticket.id)
    await _released(service, ticket.id, "v0.6.8", actor_project="requester")
    await _released(service, ticket.id, "v0.6.9", actor_project="executor")
    for project, key in (("requester", "deployed-requester"), ("executor", "deployed-executor")):
        await service.attest(
            ticket.id,
            actor_project=project,
            caller_identity=OBSERVER_IDENTITY,
            kind="deployed",
            payload={
                "repository_id": 1,
                "live_release_sha": L if project == "executor" else STALE,
                "package_version": "0.6.9",
                "integration_sha": L,
            },
            idempotency_key=f"{key}-{ticket.id}",
            emitted_at=NOW,
            contract_revision=1,
        )

    # The observer writes as the executor project; the label alone proves nothing.
    assert await PgTicketRepo(session_factory).release_state(ticket.id) == (("v0.6.9",), (L,))


async def _second_deliverable(session_factory, ticket_id, sha: str) -> None:
    """A second active binding on the ticket, merged at `sha`."""
    b = delivery_artifact_bindings
    async with session_factory.begin() as session:
        first = (
            (await session.execute(sa.select(b).where(b.c.ticket_id == ticket_id).limit(1)))
            .mappings()
            .one()
        )
        await session.execute(
            b.insert().values(
                {
                    **first,
                    "id": uuid4(),
                    "deliverable_key": "documentation",
                    "pr_number": first["pr_number"] + 1,
                    "integration_sha": sha,
                }
            )
        )


async def _deployed(service, ticket_id, live_sha: str, integration_sha: str) -> None:
    await service.attest(
        ticket_id,
        actor_project="executor",
        caller_identity=OBSERVER_IDENTITY,
        kind="deployed",
        payload={
            "repository_id": 1,
            "live_release_sha": live_sha,
            "package_version": "0.6.9",
            "integration_sha": integration_sha,
        },
        idempotency_key=f"deployed-{integration_sha[:4]}-{live_sha[:4]}-{ticket_id}",
        emitted_at=NOW,
        contract_revision=1,
    )


async def test_deliverables_are_counted_against_the_release_that_is_live(session_factory):
    ticket, _binding, service = await _workflow(session_factory)
    await _merged_at(session_factory, ticket.id)
    repo = PgTicketRepo(session_factory)
    assert await repo.deployed_deliverables(ticket.id, L) == (0, 1)

    await _deployed(service, ticket.id, L, L)
    assert await repo.deployed_deliverables(ticket.id, L) == (1, 1)

    await _second_deliverable(session_factory, ticket.id, STALE)
    assert await repo.deployed_deliverables(ticket.id, L) == (1, 2)
    assert await repo.deployed_deliverables(ticket.id, STALE) == (0, 2)

    await _deployed(service, ticket.id, L, STALE)
    assert await repo.deployed_deliverables(ticket.id, L) == (2, 2)


async def test_lot_tag_is_not_known_from_a_row_the_requester_project_issued(session_factory):
    executor, requester = "executor", "requester"
    planned, planned_svc = await _planned(session_factory, executor, "7.4.0")
    await _released(planned_svc, planned.id, "v7.4.0", actor_project=requester)

    lot = await PgTicketRepo(session_factory).shipped_by_release(executor, "7.4.0")

    assert lot.tag_known is False
    assert lot.not_shipped == ()
