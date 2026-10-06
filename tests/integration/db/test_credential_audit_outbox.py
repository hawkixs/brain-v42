"""The credential audit outbox (migration 063), against a real PostgreSQL.

Every gesture writes its audit row in its own transaction, so the two commit together or
not at all. The drain primitives are at-least-once: a row claimed and not marked is
claimed again.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from brain_v42.repositories.pg_client_credentials import (
    ClientCredentialError,
    ElevationRow,
    PgClientCredentialRepo,
)
from tests.integration.repositories.test_pg_client_credentials import _link, _make_session

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
ALLOWED = frozenset({"workstation-claude"})
ELEVATED_KEYS = {
    "elevation_id",
    "session_id",
    "session_label",
    "expires_at",
    "ttl_seconds",
    "reason",
    "via",
    "client_id",
    "connection_count",
    "excluded_client_ids",
    "excluded_connection_count",
}


class _Rollback(Exception):
    pass


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as owned:
            try:
                # Rows other tests committed are not this test's business: drained inside
                # the transaction that is rolled back.
                await owned.execute(sa.text("UPDATE brain_credential_audit SET emitted_at = now()"))
                yield owned
            finally:
                await transaction.rollback()


async def _audit_rows(session: AsyncSession) -> Sequence[Any]:
    return (
        await session.execute(
            sa.text(
                "SELECT id, event, elevation_id, payload FROM brain_credential_audit ORDER BY id"
            )
        )
    ).all()


async def _grant(
    session: AsyncSession,
    session_id: UUID,
    connection_ids: list[str],
    *,
    via: str = "hook",
    requested_by: str | None = "workstation-elevate",
    ttl: timedelta = timedelta(hours=1),
    reason: str = "maintenance",
) -> ElevationRow:
    return await PgClientCredentialRepo().grant_elevation(
        session_id,
        connection_ids,
        NOW + ttl,
        "operator",
        reason,
        NOW,
        elevatable_client_ids=ALLOWED,
        via=via,  # type: ignore[arg-type]
        requested_by_client_id=requested_by,
        session=session,
    )


async def _client_key(session: AsyncSession, session_id: UUID) -> str:
    key = await session.scalar(
        sa.text("SELECT client_key FROM brain_sessions WHERE id = :id"), {"id": session_id}
    )
    assert isinstance(key, str)
    return key


async def test_a_grant_writes_one_elevated_row_with_the_frozen_fields(
    session: AsyncSession,
) -> None:
    operator = await _make_session(session)
    await _link(session, operator, "conn-b", "conn-a", client_id="workstation-claude")
    await _link(session, operator, "conn-x", "conn-y", client_id="red-rail")
    await _link(session, operator, "conn-z", client_id="auto-discord")

    granted = await _grant(
        session,
        operator,
        ["conn-b", "conn-a", "conn-x", "conn-y", "conn-z"],
        ttl=timedelta(minutes=90),
        reason="rotate the registry",
    )

    (row,) = await _audit_rows(session)
    assert row.event == "credentials.elevated"
    assert row.elevation_id == granted.id
    assert row.payload == {
        "elevation_id": str(granted.id),
        "session_id": str(operator),
        "session_label": await _client_key(session, operator),
        "expires_at": "2026-10-06T13:30:00+00:00",
        "ttl_seconds": 5400,
        "reason": "rotate the registry",
        "via": "hook",
        "client_id": "workstation-elevate",
        "connection_count": 2,
        "excluded_client_ids": ["auto-discord", "red-rail"],
        "excluded_connection_count": 3,
    }
    assert set(row.payload) == ELEVATED_KEYS


async def test_a_cli_grant_carries_a_null_client_id_and_no_exclusion(
    session: AsyncSession,
) -> None:
    operator = await _make_session(session)
    await _link(session, operator, "conn-a", client_id="workstation-claude")

    await _grant(session, operator, ["conn-a"], via="cli", requested_by=None)

    (row,) = await _audit_rows(session)
    assert row.payload["via"] == "cli"
    assert row.payload["client_id"] is None
    assert row.payload["excluded_client_ids"] == []
    assert row.payload["excluded_connection_count"] == 0


async def test_a_rolled_back_grant_writes_no_audit_row(session: AsyncSession) -> None:
    operator = await _make_session(session)
    await _link(session, operator, "conn-a", client_id="workstation-claude")

    with pytest.raises(_Rollback):
        async with session.begin_nested():
            await _grant(session, operator, ["conn-a"])
            assert len(await _audit_rows(session)) == 1
            raise _Rollback

    assert await _audit_rows(session) == []
    assert await session.scalar(sa.text("SELECT count(*) FROM brain_admin_elevations")) == 0


async def test_a_refused_grant_writes_no_audit_row(session: AsyncSession) -> None:
    operator = await _make_session(session)
    await _link(session, operator, "conn-a", client_id="red-rail")

    with pytest.raises(ClientCredentialError) as refused:
        await _grant(session, operator, ["conn-a"])

    assert refused.value.code == "no_elevatable_connection"
    assert await _audit_rows(session) == []


async def test_ending_an_elevation_writes_one_unelevated_row_with_the_grants_fields(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-a", client_id="workstation-claude")
    await _link(session, operator, "conn-z", client_id="red-rail")
    granted = await _grant(session, operator, ["conn-a", "conn-z"])

    await repo.end_elevation(granted.id, NOW + timedelta(minutes=5), session=session)

    elevated, unelevated = await _audit_rows(session)
    assert unelevated.event == "credentials.unelevated"
    assert unelevated.elevation_id == granted.id
    # The ending gesture is the CLI's: it names no credential, whatever the grant did.
    assert unelevated.payload == {**elevated.payload, "via": "cli", "client_id": None}
    with pytest.raises(ClientCredentialError):
        await repo.end_elevation(granted.id, NOW + timedelta(minutes=6), session=session)
    assert len(await _audit_rows(session)) == 2


async def test_the_expiry_sweep_audits_each_expired_elevation_once(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    first = await _make_session(session)
    second = await _make_session(session)
    await _link(session, first, "conn-1", client_id="workstation-claude")
    await _link(session, second, "conn-2", client_id="workstation-claude")
    short_a = await _grant(session, first, ["conn-1"], ttl=timedelta(minutes=10))
    short_b = await _grant(
        session, second, ["conn-2"], ttl=timedelta(minutes=20), via="cli", requested_by=None
    )
    still_running = await _grant(session, first, ["conn-1"], ttl=timedelta(hours=3))
    ended = await _grant(session, second, ["conn-2"], ttl=timedelta(minutes=10))
    await repo.end_elevation(ended.id, NOW + timedelta(minutes=1), session=session)
    before = len(await _audit_rows(session))

    assert await repo.audit_expired_elevations(NOW + timedelta(minutes=5), session=session) == 0
    assert await repo.audit_expired_elevations(NOW + timedelta(minutes=30), session=session) == 2
    assert await repo.audit_expired_elevations(NOW + timedelta(minutes=30), session=session) == 0
    assert await repo.audit_expired_elevations(NOW + timedelta(minutes=45), session=session) == 0

    rows = await _audit_rows(session)
    expired = [row for row in rows if row.event == "credentials.elevation_expired"]
    assert sorted(row.elevation_id for row in expired) == sorted([short_a.id, short_b.id])
    assert len(rows) == before + 2
    by_elevation = {row.elevation_id: row.payload for row in expired}
    assert set(by_elevation[short_a.id]) == ELEVATED_KEYS
    assert (by_elevation[short_a.id]["via"], by_elevation[short_a.id]["client_id"]) == (
        "hook",
        "workstation-elevate",
    )
    assert (by_elevation[short_b.id]["via"], by_elevation[short_b.id]["client_id"]) == ("cli", None)
    audited = {
        row.id
        for row in (
            await session.execute(
                sa.text("SELECT id FROM brain_admin_elevations WHERE expiry_audited_at IS NOT NULL")
            )
        ).all()
    }
    assert audited == {short_a.id, short_b.id}
    assert still_running.id not in audited


async def test_claimed_rows_are_oldest_first_and_claimed_again_until_marked(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-a", client_id="workstation-claude")
    granted = await _grant(session, operator, ["conn-a"])
    await repo.end_elevation(granted.id, NOW, session=session)
    written = [row.id for row in await _audit_rows(session)]
    assert len(written) == 2

    with pytest.raises(_Rollback):  # a drainer that dies after emitting, before marking
        async with session.begin_nested():
            claimed = await repo.claim_unemitted_audit(10, session=session)
            assert [row.id for row in claimed] == written
            raise _Rollback

    again = await repo.claim_unemitted_audit(1, session=session)
    assert [row.id for row in again] == written[:1]
    assert again[0].event == "credentials.elevated"
    assert again[0].emitted_at is None

    assert await repo.mark_audit_emitted([again[0].id], NOW, session=session) == 1
    assert (
        await repo.mark_audit_emitted([again[0].id], NOW + timedelta(hours=1), session=session) == 0
    )
    assert await repo.mark_audit_emitted([], NOW, session=session) == 0
    remaining = await repo.claim_unemitted_audit(10, session=session)
    assert [row.id for row in remaining] == written[1:]
    stamp = await session.scalar(
        sa.text("SELECT emitted_at FROM brain_credential_audit WHERE id = :id"),
        {"id": again[0].id},
    )
    assert stamp == NOW


async def test_a_claimed_row_is_skipped_by_a_concurrent_drainer(engine: AsyncEngine) -> None:
    repo = PgClientCredentialRepo()
    ids: list[int] = []
    async with engine.begin() as connection:
        for index in range(2):
            row_id = await connection.scalar(
                sa.text(
                    "INSERT INTO brain_credential_audit (event, payload) "
                    "VALUES ('credentials.issued', CAST(:payload AS jsonb)) RETURNING id"
                ),
                {"payload": json.dumps({"probe": index})},
            )
            assert isinstance(row_id, int)
            ids.append(row_id)
    try:
        async with AsyncSession(engine) as first, AsyncSession(engine) as second:
            async with first.begin(), second.begin():
                held = {row.id for row in await repo.claim_unemitted_audit(1000, session=first)}
                other = {row.id for row in await repo.claim_unemitted_audit(1000, session=second)}
        assert set(ids) <= held
        assert held.isdisjoint(other)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("DELETE FROM brain_credential_audit WHERE id = ANY(:ids)"), {"ids": ids}
            )


async def test_issue_and_revoke_write_their_events_in_their_transaction(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    digest = uuid4().bytes * 2

    issued = await repo.issue(
        "auto-discord", digest, ["read", "write"], [], "operator", session=session
    )
    await repo.revoke(issued.id, "rotated", NOW, session=session)
    with pytest.raises(ClientCredentialError):
        await repo.revoke(issued.id, "again", NOW, session=session)
    with pytest.raises(ClientCredentialError):
        await repo.issue("auto-discord", uuid4().bytes * 2, ["admin"], [], "op", session=session)

    created, revoked = await _audit_rows(session)
    assert (created.event, created.elevation_id) == ("credentials.issued", None)
    assert created.payload == {
        "credential_id": str(issued.id),
        "client_id": "auto-discord",
        "families": ["read", "write"],
        "author": "operator",
    }
    assert revoked.event == "credentials.revoked"
    assert revoked.payload == {
        "credential_id": str(issued.id),
        "client_id": "auto-discord",
        "families": ["read", "write"],
        "reason": "rotated",
    }


async def test_a_rolled_back_issue_writes_no_audit_row(session: AsyncSession) -> None:
    repo = PgClientCredentialRepo()

    with pytest.raises(_Rollback):
        async with session.begin_nested():
            await repo.issue(
                "auto-discord", uuid4().bytes * 2, ["read"], [], "operator", session=session
            )
            raise _Rollback

    assert await _audit_rows(session) == []


async def test_no_audit_row_holds_a_digest_or_a_connection_id(session: AsyncSession) -> None:
    repo = PgClientCredentialRepo()
    digest = uuid4().bytes * 2
    connection_id = f"conn-{uuid4().hex}"
    issued = await repo.issue(
        "workstation-claude", digest, ["read"], [], "operator", session=session
    )
    await repo.revoke(issued.id, "rotated", NOW, session=session)
    operator = await _make_session(session)
    await _link(session, operator, connection_id, client_id="workstation-claude")
    granted = await _grant(session, operator, [connection_id], ttl=timedelta(minutes=1))
    await repo.end_elevation(granted.id, NOW, session=session)
    expiring = await _grant(session, operator, [connection_id], ttl=timedelta(minutes=1))
    await repo.audit_expired_elevations(NOW + timedelta(hours=1), session=session)

    rows = await _audit_rows(session)
    assert [row.event for row in rows] == [
        "credentials.issued",
        "credentials.revoked",
        "credentials.elevated",
        "credentials.unelevated",
        "credentials.elevated",
        "credentials.elevation_expired",
    ]
    dump = json.dumps([row.payload for row in rows])
    assert connection_id not in dump
    assert connection_id.removeprefix("conn-")[:12] not in dump
    assert digest.hex() not in dump
    assert digest.hex()[:16] not in dump
    assert str(expiring.id) in dump
