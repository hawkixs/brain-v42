"""Client credential registry and admin elevations, against a real PostgreSQL.

Every test runs inside one outer transaction that is rolled back: the registry's rows
are never deleted by the application, so a committed test row would stay forever.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from brain_v42.repositories.pg_client_credentials import (
    ClientCredentialError,
    CredentialRow,
    ElevationRow,
    PgClientCredentialRepo,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as owned:
            try:
                yield owned
            finally:
                await transaction.rollback()


async def _link(session: AsyncSession, session_id: UUID, *connection_ids: str) -> None:
    for connection_id in connection_ids:
        await session.execute(
            sa.text(
                "INSERT INTO brain_session_connections (session_id, connection_id) "
                "VALUES (:session_id, :connection_id)"
            ),
            {"session_id": session_id, "connection_id": connection_id},
        )


async def _make_session(
    session: AsyncSession, *, abandoned: bool = False, nature: str | None = None
) -> UUID:
    key = f"integ-063-{uuid4().hex[:16]}"
    await session.execute(
        sa.text(
            "INSERT INTO project_contexts (project_key, name, description) "
            "VALUES (:key, :key, 'client credentials')"
        ),
        {"key": key},
    )
    session_id = await session.scalar(
        sa.text(
            "INSERT INTO brain_sessions (project_key, client_key, started_focus_revision, "
            "status, nature, connection_id, ended_at, abandonment_reason) "
            "VALUES (:key, :client_key, 0, :status, :nature, :connection_id, "
            "CASE WHEN :abandoned THEN now() END, CASE WHEN :abandoned THEN 'test' END) "
            "RETURNING id"
        ),
        {
            "key": key,
            "client_key": f"integ-{uuid4().hex[:12]}",
            "status": "abandoned" if abandoned else "open",
            "abandoned": abandoned,
            "nature": nature,
            "connection_id": "conn-agent" if nature == "agent" else None,
        },
    )
    assert isinstance(session_id, UUID)
    return session_id


def _digest() -> bytes:
    return uuid4().bytes * 2


async def _issue(
    repo: PgClientCredentialRepo, session: AsyncSession, **kwargs: object
) -> CredentialRow:
    params: dict[str, object] = {
        "client_id": "auto-discord",
        "token_sha256": _digest(),
        "families": ["read"],
        "issuers": [],
        "created_by": "operator",
    }
    params.update(kwargs)
    return await repo.issue(session=session, **params)  # type: ignore[arg-type]


async def test_issue_then_active_rows_returns_the_credential(session: AsyncSession) -> None:
    repo = PgClientCredentialRepo()
    issued = await _issue(repo, session, families=["read", "write"], issuers=["red-lab"])
    assert issued.client_id == "auto-discord"
    assert issued.families == ["read", "write"]
    assert issued.issuers == ["red-lab"]
    assert issued.transition is False
    assert issued.revoked_at is None
    assert issued in await repo.active_rows(NOW + timedelta(days=1), session=session)


async def test_admin_is_refused_before_any_sql(session: AsyncSession) -> None:
    repo = PgClientCredentialRepo()
    with pytest.raises(ValueError, match="admin"):
        await _issue(repo, session, families=["read", "admin"])
    assert await session.scalar(sa.text("SELECT count(*) FROM brain_client_credentials")) == 0


async def test_expired_and_revoked_rows_are_not_active_but_are_listed(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    live = await _issue(repo, session)
    expired = await _issue(repo, session, expires_at=NOW - timedelta(seconds=1))
    revoked = await _issue(repo, session)
    await repo.revoke(revoked.id, "leaked", NOW, session=session)

    active = {row.id for row in await repo.active_rows(NOW, session=session)}
    assert live.id in active
    assert expired.id not in active
    assert revoked.id not in active
    listed = await repo.list_rows(session=session)
    assert {live.id, expired.id, revoked.id} <= {row.id for row in listed}
    assert [row.created_at for row in listed] == sorted(row.created_at for row in listed)


async def test_revoke_stamps_the_row_and_a_second_revoke_is_refused(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    issued = await _issue(repo, session)
    revoked = await repo.revoke(issued.id, "rotated", NOW, session=session)
    assert (revoked.revoked_at, revoked.revoked_reason) == (NOW, "rotated")
    with pytest.raises(ClientCredentialError) as already:
        await repo.revoke(issued.id, "again", NOW, session=session)
    assert already.value.code == "already_revoked"
    with pytest.raises(ClientCredentialError) as unknown:
        await repo.revoke(uuid4(), "nobody", NOW, session=session)
    assert unknown.value.code == "unknown_credential"


async def test_touch_last_used_coalesces_within_one_minute(session: AsyncSession) -> None:
    repo = PgClientCredentialRepo()
    issued = await _issue(repo, session)
    assert await repo.touch_last_used([issued.id], NOW, session=session) == 1
    assert (
        await repo.touch_last_used([issued.id], NOW + timedelta(seconds=59), session=session) == 0
    )
    assert (
        await repo.touch_last_used([issued.id], NOW + timedelta(seconds=61), session=session) == 1
    )
    assert await repo.touch_last_used([], NOW, session=session) == 0


async def test_grant_elevation_refuses_agent_closed_and_unknown_sessions(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    cases = {
        "session_not_operator": await _make_session(session, nature="agent"),
        "session_not_open": await _make_session(session, abandoned=True),
        "unknown_session": uuid4(),
    }
    for code, session_id in cases.items():
        with pytest.raises(ClientCredentialError) as refused:
            await repo.grant_elevation(
                session_id,
                ["conn-1"],
                NOW + timedelta(hours=1),
                "operator",
                "maintenance",
                NOW,
                session=session,
            )
        assert refused.value.code == code
    assert await session.scalar(sa.text("SELECT count(*) FROM brain_admin_elevations")) == 0


async def test_elevation_lifecycle_through_active_and_end(session: AsyncSession) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-1", "conn-2", "conn-3")
    granted = await repo.grant_elevation(
        operator,
        ["conn-1", "conn-2"],
        NOW + timedelta(hours=1),
        "operator",
        "maintenance",
        NOW,
        session=session,
    )
    assert granted.connection_ids == ["conn-1", "conn-2"]
    assert granted.revoked_at is None
    expired = await repo.grant_elevation(
        operator, ["conn-3"], NOW + timedelta(minutes=5), "operator", "short", NOW, session=session
    )

    at_30_minutes = {
        row.id for row in await repo.active_elevations(NOW + timedelta(minutes=30), session=session)
    }
    assert granted.id in at_30_minutes
    assert expired.id not in at_30_minutes

    ended = await repo.end_elevation(granted.id, NOW + timedelta(minutes=31), session=session)
    assert ended.revoked_at == NOW + timedelta(minutes=31)
    assert granted.id not in {
        row.id for row in await repo.active_elevations(NOW + timedelta(minutes=32), session=session)
    }
    with pytest.raises(ClientCredentialError) as unknown:
        await repo.end_elevation(uuid4(), NOW, session=session)
    assert unknown.value.code == "unknown_elevation"


async def _grant(
    repo: PgClientCredentialRepo,
    session: AsyncSession,
    session_id: UUID,
    connection_ids: list[str],
) -> ElevationRow:
    return await repo.grant_elevation(
        session_id,
        connection_ids,
        NOW + timedelta(hours=1),
        "operator",
        "maintenance",
        NOW,
        session=session,
    )


async def test_grant_elevation_refuses_connections_not_linked_to_that_session(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    other = await _make_session(session)
    await _link(session, operator, "conn-linked")
    await _link(session, other, "conn-of-another-session")
    secret = "0123456789abcdef-the-rest-must-never-be-echoed"

    for unlinked in (["conn-linked", secret], ["conn-of-another-session"]):
        with pytest.raises(ClientCredentialError) as refused:
            await _grant(repo, session, operator, unlinked)
        assert refused.value.code == "connection_not_linked"
        assert secret[8:] not in str(refused.value)
        assert "conn-of-another-session" not in str(refused.value)
    assert await session.scalar(sa.text("SELECT count(*) FROM brain_admin_elevations")) == 0


async def test_grant_elevation_deduplicates_the_connection_ids(session: AsyncSession) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-1", "conn-2")
    granted = await _grant(repo, session, operator, ["conn-1", "conn-2", "conn-1"])
    assert granted.connection_ids == ["conn-1", "conn-2"]


async def test_an_elevation_stops_being_active_when_its_session_is_no_longer_open(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-1")
    granted = await _grant(repo, session, operator, ["conn-1"])
    at = NOW + timedelta(minutes=1)
    assert granted in await repo.active_elevations(at, session=session)

    await session.execute(
        sa.text(
            "UPDATE brain_sessions SET status = 'abandoned', ended_at = now(), "
            "abandonment_reason = 'test' WHERE id = :id"
        ),
        {"id": operator},
    )
    assert await repo.active_elevations(at, session=session) == []


async def test_a_credential_row_never_shows_its_digest_in_repr(session: AsyncSession) -> None:
    repo = PgClientCredentialRepo()
    digest = bytes.fromhex("ab" * 32)
    issued = await _issue(repo, session, token_sha256=digest)
    shown = repr(issued)
    assert "token_sha256" not in shown
    assert digest.hex() not in shown
    assert repr(digest) not in shown
