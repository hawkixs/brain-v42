"""Real PostgreSQL handover coverage for projection fencing migration 034."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.repositories.pg_graph_ledger import PgGraphLedgerRepo

pytestmark = pytest.mark.integration


async def _create_claimable_feature_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    prefix: str,
) -> tuple[str, UUID]:
    suffix = uuid4().hex[:12]
    project_key = f"integ-{prefix}-{suffix}"
    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                """
                INSERT INTO project_contexts (project_key, name, description)
                VALUES (:project_key, :project_key, 'projection fencing integration')
                """
            ),
            {"project_key": project_key},
        )
        feature_id = (
            await session.execute(
                sa.text(
                    """
                    INSERT INTO features (project_key, name, description)
                    VALUES (:project_key, :name, 'fenced event')
                    RETURNING id
                    """
                ),
                {"project_key": project_key, "name": f"fenced-{suffix}"},
            )
        ).scalar_one()
    async with session_factory.begin() as session:
        event_id = (
            await session.execute(
                sa.text(
                    """
                    SELECT outbox.event_id
                    FROM graph_outbox AS outbox
                    JOIN brain_entities AS entity ON entity.id = outbox.entity_id
                    WHERE entity.source_uuid = :feature_id
                    ORDER BY outbox.aggregate_revision DESC
                    LIMIT 1
                    """
                ),
                {"feature_id": feature_id},
            )
        ).scalar_one()
        await session.execute(
            sa.text(
                """
                UPDATE graph_outbox
                SET available_at = '-infinity'::timestamptz
                WHERE event_id = :event_id
                """
            ),
            {"event_id": event_id},
        )
    return project_key, event_id


async def test_unarmed_generation_never_catches_up_silently(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory.begin() as session:
        original = dict(
            (
                await session.execute(
                    sa.text(
                        """
                        SELECT generation, owner, leased_until, neo4j_armed_generation
                        FROM graph_projection_leases
                        WHERE slot = 'neo4j'
                        """
                    )
                )
            )
            .mappings()
            .one()
        )
        await session.execute(
            sa.text(
                """
                UPDATE graph_projection_leases
                SET generation = 700,
                    owner = 'restore-predecessor',
                    leased_until = clock_timestamp() - INTERVAL '1 second',
                    neo4j_armed_generation = NULL
                WHERE slot = 'neo4j'
                """
            )
        )

    try:
        first_repo = PgGraphLedgerRepo(session_factory)
        first = await first_repo.acquire_leadership("restore-worker-a", lease_seconds=30)
        assert first is not None
        assert first.generation == 700
        assert first.armed is False

        async with session_factory.begin() as session:
            await session.execute(
                sa.text(
                    """
                    UPDATE graph_projection_leases
                    SET leased_until = clock_timestamp() - INTERVAL '1 second'
                    WHERE slot = 'neo4j'
                    """
                )
            )

        second_repo = PgGraphLedgerRepo(session_factory)
        second = await second_repo.acquire_leadership("restore-worker-b", lease_seconds=30)
        assert second is not None
        assert second.generation == first.generation
        assert second.armed is False
    finally:
        async with session_factory.begin() as session:
            await session.execute(
                sa.text(
                    """
                    UPDATE graph_projection_leases
                    SET generation = :generation,
                        owner = :owner,
                        leased_until = :leased_until,
                        neo4j_armed_generation = :armed_generation
                    WHERE slot = 'neo4j'
                    """
                ),
                {
                    "generation": original["generation"],
                    "owner": original["owner"],
                    "leased_until": original["leased_until"],
                    "armed_generation": original["neo4j_armed_generation"],
                },
            )


async def test_handover_reclaims_event_and_rejects_predecessor_cas(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    suffix = uuid4().hex[:12]
    project_key = f"integ-fence-{suffix}"
    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                """
                INSERT INTO project_contexts (project_key, name, description)
                VALUES (:project_key, :project_key, 'projection fencing integration')
                """
            ),
            {"project_key": project_key},
        )
        feature_id = (
            await session.execute(
                sa.text(
                    """
                    INSERT INTO features (project_key, name, description)
                    VALUES (:project_key, :name, 'fenced event')
                    RETURNING id
                    """
                ),
                {"project_key": project_key, "name": f"fenced-{suffix}"},
            )
        ).scalar_one()

    async with session_factory.begin() as session:
        event_id = (
            await session.execute(
                sa.text(
                    """
                    SELECT outbox.event_id
                    FROM graph_outbox AS outbox
                    JOIN brain_entities AS entity ON entity.id = outbox.entity_id
                    WHERE entity.source_uuid = :feature_id
                    ORDER BY outbox.aggregate_revision DESC
                    LIMIT 1
                    """
                ),
                {"feature_id": feature_id},
            )
        ).scalar_one()
        await session.execute(
            sa.text(
                """
                UPDATE graph_outbox
                SET available_at = '-infinity'::timestamptz
                WHERE event_id = :event_id
                """
            ),
            {"event_id": event_id},
        )

    first_repo = PgGraphLedgerRepo(session_factory)
    first_leader = await first_repo.acquire_leadership("projector-a", lease_seconds=30)
    assert first_leader is not None
    assert await first_repo.arm_leadership(first_leader) is True
    first_claim = (await first_repo.claim_pending(first_leader, limit=1, lease_seconds=60))[0]
    assert first_claim.event.event_id == event_id

    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                """
                UPDATE graph_projection_leases
                SET leased_until = clock_timestamp() - INTERVAL '1 second'
                WHERE slot = 'neo4j'
                """
            )
        )

    second_repo = PgGraphLedgerRepo(session_factory)
    second_leader = await second_repo.acquire_leadership("projector-b", lease_seconds=30)
    assert second_leader is not None
    assert second_leader.generation == first_leader.generation + 1
    assert await second_repo.arm_leadership(second_leader) is True
    second_claim = (await second_repo.claim_pending(second_leader, limit=1, lease_seconds=60))[0]

    assert second_claim.event.event_id == event_id
    assert second_claim.claim_version == first_claim.claim_version + 1
    assert second_claim.lease_generation == second_leader.generation

    assert await first_repo.mark_delivered(first_claim) is False
    assert await first_repo.mark_failed(first_claim, "neo4j_error", max_attempts=10) is False

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.text(
                        """
                    SELECT lease_owner, lease_generation, claim_version, attempt_count
                    FROM graph_outbox
                    WHERE event_id = :event_id
                    """
                    ),
                    {"event_id": event_id},
                )
            )
            .mappings()
            .one()
        )
    assert row == {
        "lease_owner": second_claim.owner_id,
        "lease_generation": second_claim.lease_generation,
        "claim_version": second_claim.claim_version,
        "attempt_count": 0,
    }
    assert await second_repo.mark_delivered(second_claim) is True
    assert await second_repo.release_leadership(second_leader) is True


async def test_lowered_max_attempts_normalizes_existing_event_to_terminal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _project_key, event_id = await _create_claimable_feature_event(
        session_factory,
        prefix="lowered-max-attempts",
    )
    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                """
                UPDATE graph_projection_leases
                SET leased_until = clock_timestamp() - INTERVAL '1 second'
                WHERE slot = 'neo4j'
                """
            )
        )
        await session.execute(
            sa.text(
                """
                UPDATE graph_outbox
                SET attempt_count = 5,
                    last_error_code = 'projection_failed',
                    available_at = '-infinity'::timestamptz,
                    lease_owner = NULL,
                    lease_generation = NULL,
                    leased_until = NULL
                WHERE event_id = :event_id
                """
            ),
            {"event_id": event_id},
        )

    repo = PgGraphLedgerRepo(session_factory)
    leader = await repo.acquire_leadership("lowered-max-attempts", lease_seconds=30)
    assert leader is not None
    assert await repo.arm_leadership(leader) is True

    claims = await repo.claim_pending(
        leader,
        limit=10,
        lease_seconds=30,
        max_attempts=3,
    )

    assert all(claim.event.event_id != event_id for claim in claims)
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.text(
                        """
                        SELECT last_error_code,
                               available_at = 'infinity'::timestamptz AS is_infinite,
                               lease_owner,
                               lease_generation,
                               leased_until
                        FROM graph_outbox
                        WHERE event_id = :event_id
                        """
                    ),
                    {"event_id": event_id},
                )
            )
            .mappings()
            .one()
        )
    assert row == {
        "last_error_code": "max_attempts",
        "is_infinite": True,
        "lease_owner": None,
        "lease_generation": None,
        "leased_until": None,
    }
    assert await repo.release_leadership(leader) is True


async def test_stale_renewal_rolls_back_leader_extension(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _project_key, event_id = await _create_claimable_feature_event(
        session_factory,
        prefix="renew-rollback",
    )
    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                """
                UPDATE graph_projection_leases
                SET leased_until = clock_timestamp() - INTERVAL '1 second'
                WHERE slot = 'neo4j'
                """
            )
        )

    repo = PgGraphLedgerRepo(session_factory)
    leader = await repo.acquire_leadership("renew-rollback", lease_seconds=60)
    assert leader is not None
    assert await repo.arm_leadership(leader) is True
    claim = (await repo.claim_pending(leader, limit=1, lease_seconds=60))[0]
    assert claim.event.event_id == event_id

    async with session_factory.begin() as session:
        before = (
            await session.execute(
                sa.text("SELECT leased_until FROM graph_projection_leases WHERE slot = 'neo4j'")
            )
        ).scalar_one()
        await session.execute(
            sa.text(
                """
                UPDATE graph_outbox
                SET claim_version = claim_version + 1
                WHERE event_id = :event_id
                """
            ),
            {"event_id": event_id},
        )

    assert await repo.renew_claim(claim, lease_seconds=300) is None

    async with session_factory() as session:
        after = (
            await session.execute(
                sa.text("SELECT leased_until FROM graph_projection_leases WHERE slot = 'neo4j'")
            )
        ).scalar_one()
    assert after == before
    assert await repo.release_leadership(leader) is True
    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                """
                UPDATE graph_outbox
                SET delivered_at = clock_timestamp(),
                    lease_owner = NULL,
                    lease_generation = NULL,
                    leased_until = NULL
                WHERE event_id = :event_id
                """
            ),
            {"event_id": event_id},
        )


async def test_delivery_locks_leader_before_waiting_on_the_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _project_key, event_id = await _create_claimable_feature_event(
        session_factory,
        prefix="ack-lock-order",
    )
    async with session_factory.begin() as session:
        await session.execute(
            sa.text(
                """
                UPDATE graph_projection_leases
                SET leased_until = clock_timestamp() - INTERVAL '1 second'
                WHERE slot = 'neo4j'
                """
            )
        )

    repo = PgGraphLedgerRepo(session_factory)
    leader = await repo.acquire_leadership("ack-lock-order", lease_seconds=60)
    assert leader is not None
    assert await repo.arm_leadership(leader) is True
    claim = (await repo.claim_pending(leader, limit=1, lease_seconds=60))[0]
    assert claim.event.event_id == event_id

    blocker = session_factory()
    await blocker.begin()
    await blocker.execute(
        sa.text("SELECT id FROM graph_outbox WHERE event_id = :event_id FOR UPDATE"),
        {"event_id": event_id},
    )
    acknowledgement = asyncio.create_task(repo.mark_delivered(claim))
    leader_locked = False
    try:
        for _ in range(100):
            await asyncio.sleep(0.02)
            async with session_factory() as probe:
                try:
                    await probe.execute(
                        sa.text(
                            """
                            SELECT slot
                            FROM graph_projection_leases
                            WHERE slot = 'neo4j'
                            FOR UPDATE NOWAIT
                            """
                        )
                    )
                except Exception as exc:  # asyncpg wraps PostgreSQL lock_not_available
                    await probe.rollback()
                    if "could not obtain lock" in str(exc).lower():
                        leader_locked = True
                        break
                    raise
                else:
                    await probe.rollback()
        assert leader_locked is True
    finally:
        await blocker.rollback()
        await blocker.close()

    assert await asyncio.wait_for(acknowledgement, timeout=3.0) is True
    assert await repo.release_leadership(leader) is True
