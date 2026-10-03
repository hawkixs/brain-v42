"""Release derivation through the real provider adapter and fenced PG writes."""

import json
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain_v42.db.tables import delivery_artifact_bindings, delivery_workflows
from brain_v42.facts.model import ReleaseIdentity
from brain_v42.repositories.delivery_ticket_guard import DeliveryTransitionMutation
from brain_v42.repositories.pg_delivery_attestations import PgDeliveryAttestationsRepo
from brain_v42.repositories.pg_release_derivation import OBSERVER_IDENTITY, PgReleaseDerivationRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from tests.integration.db.delivery_observer_cases import M2, RID, ROOT, M, ObserverCase
from tests.integration.db.delivery_observer_cases import (
    observer_queue_isolation as observer_queue_isolation,
)
from tests.integration.db.test_delivery_receipt_publication import _contract

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.usefixtures("observer_queue_isolation"),
]

T1, T2, T3 = "1" * 40, "2" * 40, "3" * 40
L = "a" * 40
L2 = "e" * 40


async def test_earliest_containing_tag_is_recorded_once_and_restart_is_idle(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    case = ObserverCase(engine, session_factory)
    ticket, _, _ = await case.create()
    case.tags = [
        ("v0.6.4", T3, "2026-10-09T10:00:00Z", True),
        ("v0.6.2", T1, "2026-10-01T10:00:00Z", False),
        ("v0.6.3", T2, "2026-10-04T10:00:00Z", True),
    ]
    async with case.runtime() as runtime:
        assert await runtime.owner.acquire()
        await runtime.run_once()
        await runtime.run_once()
    rows = await case.attestations(ticket.id, kind="released")
    assert [r["payload"] for r in rows] == [
        {"repository_id": RID, "tag": "v0.6.3", "tag_sha": T2, "integration_sha": M}
    ]
    assert rows[0]["idempotency_key"] == f"released:{ticket.id}:implementation:v0.6.3"
    assert rows[0]["issuer_identity"] == "brain-v42-delivery-observer"
    assert rows[0]["emitted_at"] == datetime(2026, 10, 4, 10, tzinfo=UTC)
    assert [path for _, path in case.requests if "/compare/" in path] == [
        f"{ROOT}/compare/{T1}...{M}",
        f"{ROOT}/compare/{T2}...{M}",
    ]
    before = len(case.requests)
    async with case.runtime() as runtime:
        await runtime.run_once()
    assert len(await case.attestations(ticket.id, kind="released")) == 1
    assert not any("/compare/" in path for _, path in case.requests[before:])


async def test_no_containing_tag_records_nothing_and_memoizes_the_negative_compare(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    case = ObserverCase(engine, session_factory)
    ticket, _, _ = await case.create()
    case.tags = [("v0.6.2", T1, "2026-10-01T10:00:00Z", False)]
    async with case.runtime() as runtime:
        assert await runtime.owner.acquire()
        await runtime.run_once()
        before = len(case.requests)
        await runtime.run_once()
    assert await case.attestations(ticket.id, kind="released") == []
    assert any("/compare/" in path for _, path in case.requests[:before])
    # A negative result for immutable SHAs now saves the next cycle's budget.
    assert not any("/compare/" in path for _, path in case.requests[before:])
    assert sum(path == f"{ROOT}/git/commits/{T1}" for _, path in case.requests) == 1


async def test_tags_older_than_the_merge_are_not_compared(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    case = ObserverCase(engine, session_factory)
    ticket, _, _ = await case.create()
    case.tags = [("v0.6.2", T1, "2026-09-30T10:00:00Z", True)]
    async with case.runtime() as runtime:
        assert await runtime.owner.acquire()
        await runtime.run_once()
        await runtime.run_once()
    assert await case.attestations(ticket.id, kind="released") == []
    assert any(path == f"{ROOT}/tags" for _, path in case.requests)
    assert not any("/compare/" in path for _, path in case.requests)


async def test_compare_budget_is_capped_per_pass(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    case = ObserverCase(engine, session_factory)
    tickets = [(await case.create(number=number))[0] for number in range(42, 57)]
    case.tags = [("v0.6.3", T2, "2026-10-04T10:00:00Z", True)]
    async with case.runtime() as runtime:
        assert await runtime.owner.acquire()
        await runtime.run_once()
        compares = [path for _, path in case.requests if "/compare/" in path]
        assert len(compares) == 10
        assert all(path == f"{ROOT}/compare/{T2}...{M}" for path in compares)
        assert (
            sum([len(await case.attestations(ticket.id, kind="released")) for ticket in tickets])
            == 10
        )
        assert sum(path == f"{ROOT}/tags" for _, path in case.requests) == 1
        before = len(case.requests)
        await runtime.run_once()
    assert sum("/compare/" in path for _, path in case.requests[before:]) == 5
    for ticket in tickets:
        rows = await case.attestations(ticket.id, kind="released")
        assert [row["payload"]["tag"] for row in rows] == ["v0.6.3"]


async def test_provider_error_on_compare_records_nothing(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    case = ObserverCase(engine, session_factory)
    ticket, _, _ = await case.create()
    case.tags = [("v0.6.3", T2, "2026-10-04T10:00:00Z", True)]
    case.compare_status = 500
    async with case.runtime() as runtime:
        assert await runtime.owner.acquire()
        result = await runtime.run_once()
        assert (result.collected, result.failed, result.exit_code) == (1, 0, 0)
        before = len(case.requests)
        result = await runtime.run_once()
    assert (result.collected, result.failed, result.exit_code) == (0, 0, 0)
    assert await case.attestations(ticket.id, kind="released") == []
    assert any("/compare/" in path for _, path in case.requests[:before])
    assert any("/compare/" in path for _, path in case.requests[before:])


async def test_deployed_is_recorded_when_the_live_release_contains_the_merge(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    case = ObserverCase(engine, session_factory)
    ticket, _, _ = await case.create()
    case.contained[L] = True
    started = datetime.now(UTC)
    async with case.runtime(release_identity=lambda: ReleaseIdentity(L, "0.6.3")) as runtime:
        assert await runtime.owner.acquire()
        await runtime.run_once()
        rows = await case.attestations(ticket.id, kind="deployed")
        assert [row["payload"] for row in rows] == [
            {
                "repository_id": RID,
                "live_release_sha": L,
                "package_version": "0.6.3",
                "integration_sha": M,
            }
        ]
        assert rows[0]["idempotency_key"] == f"deployed:{ticket.id}:implementation:{L}"
        assert rows[0]["issuer_identity"] == "brain-v42-delivery-observer"
        assert started <= rows[0]["emitted_at"] <= datetime.now(UTC)
        before = len(case.requests)
        await runtime.run_once()
    assert len(await case.attestations(ticket.id, kind="deployed")) == 1
    assert [path for _, path in case.requests[:before] if "/compare/" in path] == [
        f"{ROOT}/compare/{L}...{M}"
    ]
    assert not any("/compare/" in path for _, path in case.requests[before:])
    async with case.runtime(release_identity=lambda: ReleaseIdentity(L, "0.6.3")) as runtime:
        await runtime.run_once()
    assert len(await case.attestations(ticket.id, kind="deployed")) == 1
    assert not any("/compare/" in path for _, path in case.requests[before:])


async def test_not_contained_or_no_identity_records_nothing(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    case = ObserverCase(engine, session_factory)
    ticket, _, _ = await case.create()
    case.contained[L] = False
    async with case.runtime(release_identity=lambda: ReleaseIdentity(L, "0.6.3")) as runtime:
        await runtime.run_once()
    assert await case.attestations(ticket.id, kind="deployed") == []
    assert [path for _, path in case.requests if "/compare/" in path] == [
        f"{ROOT}/compare/{L}...{M}"
    ]
    before = len(case.requests)
    case.contained[L] = True
    async with case.runtime(release_identity=lambda: None) as runtime:
        await runtime.run_once()
    assert await case.attestations(ticket.id, kind="deployed") == []
    assert not any("/compare/" in path for _, path in case.requests[before:])


async def test_other_repositories_never_get_deployed(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    case = ObserverCase(engine, session_factory, repository_id=RID + 10)
    ticket, _, _ = await case.create()
    case.contained[L] = True
    async with case.runtime(release_identity=lambda: ReleaseIdentity(L, "0.6.3")) as runtime:
        await runtime.run_once()
    assert await case.attestations(ticket.id, kind="deployed") == []
    assert not any("/compare/" in path for _, path in case.requests)
    async with session_factory() as session:
        assert (
            await PgReleaseDerivationRepo().undeployed(
                session, repository_id=RID + 10, live_release_sha=L, limit=10
            )
            == []
        )


async def test_deployed_shares_the_release_compare_budget(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    case = ObserverCase(engine, session_factory)
    tickets = [(await case.create(number=number))[0] for number in range(42, 48)]
    case.tags = [("v0.6.3", T2, "2026-10-04T10:00:00Z", True)]
    case.contained[L] = True
    async with case.runtime(release_identity=lambda: ReleaseIdentity(L, "0.6.3")) as runtime:
        assert await runtime.owner.acquire()
        await runtime.run_once()
        compares = [path for _, path in case.requests if "/compare/" in path]
        assert compares == [f"{ROOT}/compare/{T2}...{M}"] * 6 + [f"{ROOT}/compare/{L}...{M}"] * 4
        assert sum([len(await case.attestations(t.id, kind="released")) for t in tickets]) == 6
        assert sum([len(await case.attestations(t.id, kind="deployed")) for t in tickets]) == 4
        before = len(case.requests)
        await runtime.run_once()
    assert [path for _, path in case.requests[before:] if "/compare/" in path] == [
        f"{ROOT}/compare/{L}...{M}"
    ] * 2


@pytest.mark.parametrize("kind", ["released", "deployed"])
@pytest.mark.parametrize("exact_key", [False, True])
async def test_foreign_attestations_do_not_hide_observer_candidates(
    engine, session_factory, kind, exact_key, capsys
):
    case = ObserverCase(engine, session_factory)
    ticket, binding, _ = await case.create()
    case.tags = (
        [
            ("v0.6.3", T2, "2026-10-04T10:00:00Z", True),
            ("v0.6.4", T3, "2026-10-09T10:00:00Z", True),
        ]
        if kind == "released"
        else []
    )
    case.contained[L] = True
    suffix = "v0.6.3" if kind == "released" else L
    key = f"{kind}:{ticket.id}:implementation:{suffix}"
    async with session_factory.begin() as session:
        # Seed an observed merge without running the derivation before the foreign row exists.
        await session.execute(
            delivery_artifact_bindings.update()
            .where(delivery_artifact_bindings.c.id == binding.id)
            .values(integration_sha=M, due_at=None)
        )
        await PgDeliveryAttestationsRepo().attest(
            session,
            ticket.id,
            actor_project="brain-v42",
            caller_identity="declared-by-participant",
            kind=kind,
            payload={"declared": True},
            idempotency_key=key if exact_key else key + ":foreign",
            emitted_at=datetime(2026, 10, 4, 10, tzinfo=UTC),
            contract_revision=1,
        )
    async with case.runtime(release_identity=lambda: ReleaseIdentity(L, "0.6.3")) as runtime:
        assert await runtime.owner.acquire()
        await runtime.run_once()
        before = len(case.requests)
        await runtime.run_once()
    rows = await case.attestations(ticket.id, kind=kind)
    measured = [row for row in rows if row["issuer_identity"] == OBSERVER_IDENTITY]
    assert len(measured) == (0 if exact_key else 1)
    assert sum("/compare/" in path for _, path in case.requests[:before]) == (
        2 if kind == "released" else 1
    )
    assert not any("/compare/" in path for _, path in case.requests[before:])
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [line["error_code"] for line in lines] == (
        ["idempotency_key_reused"] if exact_key else []
    )


@pytest.mark.parametrize("kind", ["released", "deployed"])
async def test_two_active_contract_revisions_only_measure_the_current_binding(
    engine, session_factory, kind, capsys
):
    case = ObserverCase(engine, session_factory)
    ticket, first, contract = await case.create()
    await case.service.set_contract(
        ticket.id,
        actor_project="brain-v42",
        expected_revision=1,
        idempotency_key=f"amend-release-{ticket.id}",
        contract=_contract(checks=contract.deliverables[0].required_checks),
    )
    async with session_factory() as session:
        version = await session.scalar(
            sa.select(delivery_workflows.c.row_version).where(
                delivery_workflows.c.ticket_id == ticket.id
            )
        )
    second = await case.service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=RID,
        pr_number=43,
        expected_revision=2,
        expected_workflow_version=version,
        idempotency_key=f"bind-release-revision-2-{ticket.id}",
    )
    async with session_factory.begin() as session:
        await session.execute(
            delivery_artifact_bindings.update()
            .where(delivery_artifact_bindings.c.id.in_([first.id, second.id]))
            .values(active=True, integration_sha=M, due_at=None)
        )
        await session.execute(
            delivery_artifact_bindings.update()
            .where(delivery_artifact_bindings.c.id == first.id)
            .values(last_success_at=datetime(2026, 10, 1, tzinfo=UTC))
        )
    case.tags = [("v0.6.3", T2, "2026-10-04T10:00:00Z", True)] if kind == "released" else []
    case.contained[L] = True
    async with case.runtime(release_identity=lambda: ReleaseIdentity(L, "0.6.3")) as runtime:
        await runtime.run_once()
    rows = await case.attestations(ticket.id, kind=kind)
    assert [row["contract_revision"] for row in rows] == [2]
    assert capsys.readouterr().err == ""


async def _reopen_with_new_merge(case, session_factory, ticket, sha: str) -> None:
    """Replay the guard's reopen, then bind the new attempt to a PR merged at ``sha``."""
    async with session_factory.begin() as session:
        await DeliveryTransitionMutation("active", reopen=True).apply(session, ticket.id)
        version = await session.scalar(
            sa.select(delivery_workflows.c.row_version).where(
                delivery_workflows.c.ticket_id == ticket.id
            )
        )
    binding = await case.service.bind_pr(
        ticket.id,
        actor_project="brain-v42",
        deliverable_key="implementation",
        repository_id=RID,
        pr_number=43,
        expected_revision=1,
        expected_workflow_version=version,
        idempotency_key=f"observer-rebind-{ticket.id}",
    )
    async with session_factory.begin() as session:
        await session.execute(
            delivery_artifact_bindings.update()
            .where(delivery_artifact_bindings.c.id == binding.id)
            .values(integration_sha=sha, due_at=None)
        )


async def test_reopen_hides_the_old_release_and_derives_the_new_merge_for_a_later_tag(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    case = ObserverCase(engine, session_factory)
    ticket, _, _ = await case.create()
    case.tag_dates[M2] = "2026-10-06T10:00:00Z"
    case.tags = [("v0.6.3", T2, "2026-10-04T10:00:00Z", True)]
    view = PgTicketRepo(session_factory)
    async with case.runtime() as runtime:
        assert await runtime.owner.acquire()
        await runtime.run_once()
        assert await view.release_state(ticket.id) == (("v0.6.3",), ())
        await _reopen_with_new_merge(case, session_factory, ticket, M2)
        # The old tag row measured M, not the merge the reopened ticket now stands on.
        assert await view.release_state(ticket.id) == ((), ())
        await runtime.run_once()
        assert await view.release_state(ticket.id) == ((), ())
        case.tags.append(("v0.6.4", T3, "2026-10-09T10:00:00Z", True))
        await runtime.run_once()
    rows = await case.attestations(ticket.id, kind="released")
    assert [(r["payload"]["tag"], r["payload"]["integration_sha"]) for r in rows] == [
        ("v0.6.3", M),
        ("v0.6.4", M2),
    ]
    assert await view.release_state(ticket.id) == (("v0.6.4",), ())


async def test_reopen_does_not_inherit_the_deployment_of_the_old_merge(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    case = ObserverCase(engine, session_factory)
    ticket, _, _ = await case.create()
    case.contained[L] = True
    case.contained_pairs[(L, M2)] = False
    case.contained[L2] = True
    view = PgTicketRepo(session_factory)
    async with case.runtime(release_identity=lambda: ReleaseIdentity(L, "0.6.3")) as runtime:
        assert await runtime.owner.acquire()
        await runtime.run_once()
        assert await view.release_state(ticket.id) == ((), (L,))
        await _reopen_with_new_merge(case, session_factory, ticket, M2)
        assert await view.release_state(ticket.id) == ((), ())
        await runtime.run_once()
        assert await view.release_state(ticket.id) == ((), ())
    assert f"{ROOT}/compare/{L}...{M2}" in [path for _, path in case.requests]
    async with case.runtime(release_identity=lambda: ReleaseIdentity(L2, "0.6.4")) as runtime:
        await runtime.run_once()
    rows = await case.attestations(ticket.id, kind="deployed")
    assert [(r["payload"]["live_release_sha"], r["payload"]["integration_sha"]) for r in rows] == [
        (L, M),
        (L2, M2),
    ]
    assert await view.release_state(ticket.id) == ((), (L2,))


@pytest.mark.parametrize("kind", ["released", "deployed"])
async def test_reopen_into_the_same_tag_or_live_release_reuses_the_key_and_claims_nothing(
    engine, session_factory, kind, capsys
):
    case = ObserverCase(engine, session_factory)
    ticket, _, _ = await case.create()
    case.tag_dates[M2] = "2026-10-03T00:00:00Z"
    case.tags = [("v0.6.3", T2, "2026-10-04T10:00:00Z", True)] if kind == "released" else []
    case.contained[L] = True
    identity = ReleaseIdentity(L, "0.6.3") if kind == "deployed" else None
    view = PgTicketRepo(session_factory)
    async with case.runtime(release_identity=lambda: identity) as runtime:
        assert await runtime.owner.acquire()
        await runtime.run_once()
        assert len(await case.attestations(ticket.id, kind=kind)) == 1
        await _reopen_with_new_merge(case, session_factory, ticket, M2)
        await runtime.run_once()
        before = len(case.requests)
        await runtime.run_once()
    # The key names the tag or live release, not the merge: no second row, no claim for M2.
    assert [
        r["payload"]["integration_sha"] for r in await case.attestations(ticket.id, kind=kind)
    ] == [M]
    assert await view.release_state(ticket.id) == ((), ())
    # The blocked key stops the retry and says why exactly once.
    assert not any("/compare/" in path for _, path in case.requests[before:])
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [line["error_code"] for line in lines] == ["idempotency_key_reused"]
