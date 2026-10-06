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
    DEFAULT_ELEVATABLE_CLIENT_IDS,
    ClientCredentialError,
    CredentialRow,
    ElevationRow,
    ForeignClientAttachError,
    PgClientCredentialRepo,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
#: Every client the fixtures link, so a test about something else is not about the allowlist.
ANY_CLIENT = frozenset({"auto-discord", "workstation-claude", "red-rail"})


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as owned:
            try:
                yield owned
            finally:
                await transaction.rollback()


async def _link(
    session: AsyncSession,
    session_id: UUID,
    *connection_ids: str,
    client_id: str | None = "auto-discord",
) -> None:
    """Link connections as LEGACY rows: written with the ownership trigger bypassed.

    Elevation tests need sessions that mix several clients, which the attribution lock now
    forbids by construction. Rows written before the lock existed are exactly that, so
    this helper writes them with ``session_replication_role = replica`` (superuser, and
    scoped to the test transaction); the lock has its own tests.
    """
    await session.execute(sa.text("SET LOCAL session_replication_role = replica"))
    for connection_id in connection_ids:
        await session.execute(
            sa.text(
                "INSERT INTO brain_session_connections (session_id, connection_id, client_id) "
                "VALUES (:session_id, :connection_id, :client_id)"
            ),
            {"session_id": session_id, "connection_id": connection_id, "client_id": client_id},
        )
    await session.execute(sa.text("SET LOCAL session_replication_role = origin"))


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
    await repo.revoke(revoked.id, "leaked", NOW, author="operator", session=session)

    active = {row.id for row in await repo.active_rows(NOW, session=session)}
    assert live.id in active
    assert expired.id not in active
    assert revoked.id not in active
    listed = await repo.list_rows(session=session)
    assert {live.id, expired.id, revoked.id} <= {row.id for row in listed}
    assert [row.created_at for row in listed] == sorted(row.created_at for row in listed)


async def test_disposition_by_digest_tells_revoked_from_expired_from_unknown(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    live = await _issue(repo, session, client_id="red-rail")
    expiring = await _issue(repo, session, client_id="auto-discord", expires_at=NOW)
    revoked = await _issue(repo, session, client_id="workstation-claude")
    await repo.revoke(revoked.id, "leaked", NOW, author="operator", session=session)
    # Revoked, THEN past its expiry: the revocation is what the operator did, so it wins.
    both = await _issue(repo, session, client_id="red-watcher", expires_at=NOW - timedelta(hours=1))
    await repo.revoke(both.id, "leaked", NOW, author="operator", session=session)

    async def disposition(digest: bytes, now: datetime = NOW) -> tuple[str | None, str]:
        return await repo.disposition_by_digest(digest, now, session=session)

    assert await disposition(live.token_sha256) == ("red-rail", "active")
    assert await disposition(expiring.token_sha256, NOW - timedelta(seconds=1)) == (
        "auto-discord",
        "active",
    )
    assert await disposition(expiring.token_sha256) == ("auto-discord", "expired")
    assert await disposition(revoked.token_sha256) == ("workstation-claude", "revoked")
    assert await disposition(both.token_sha256) == ("red-watcher", "revoked")
    assert await disposition(_digest()) == (None, "unknown")


async def test_revoke_stamps_the_row_and_a_second_revoke_is_refused(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    issued = await _issue(repo, session)
    revoked = await repo.revoke(issued.id, "rotated", NOW, author="operator", session=session)
    assert (revoked.revoked_at, revoked.revoked_reason) == (NOW, "rotated")
    with pytest.raises(ClientCredentialError) as already:
        await repo.revoke(issued.id, "again", NOW, author="operator", session=session)
    assert already.value.code == "already_revoked"
    with pytest.raises(ClientCredentialError) as unknown:
        await repo.revoke(uuid4(), "nobody", NOW, author="operator", session=session)
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
                elevatable_client_ids=ANY_CLIENT,
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
        elevatable_client_ids=ANY_CLIENT,
        session=session,
    )
    assert granted.connection_ids == ["conn-1", "conn-2"]
    assert granted.revoked_at is None
    expired = await repo.grant_elevation(
        operator,
        ["conn-3"],
        NOW + timedelta(minutes=5),
        "operator",
        "short",
        NOW,
        elevatable_client_ids=ANY_CLIENT,
        session=session,
    )

    at_30_minutes = {
        row.id for row in await repo.active_elevations(NOW + timedelta(minutes=30), session=session)
    }
    assert granted.id in at_30_minutes
    assert expired.id not in at_30_minutes

    ended = await repo.end_elevation(
        granted.id, NOW + timedelta(minutes=31), author="operator", session=session
    )
    assert ended.revoked_at == NOW + timedelta(minutes=31)
    assert granted.id not in {
        row.id for row in await repo.active_elevations(NOW + timedelta(minutes=32), session=session)
    }
    with pytest.raises(ClientCredentialError) as unknown:
        await repo.end_elevation(uuid4(), NOW, author="operator", session=session)
    assert unknown.value.code == "unknown_elevation"


async def _grant(
    repo: PgClientCredentialRepo,
    session: AsyncSession,
    session_id: UUID,
    connection_ids: list[str],
    elevatable_client_ids: frozenset[str] = ANY_CLIENT,
) -> ElevationRow:
    return await repo.grant_elevation(
        session_id,
        connection_ids,
        NOW + timedelta(hours=1),
        "operator",
        "maintenance",
        NOW,
        elevatable_client_ids=elevatable_client_ids,
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


async def test_grant_elevation_freezes_only_attributed_connections_as_pairs(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-historical", client_id=None)
    await _link(session, operator, "conn-a", client_id="workstation-claude")
    await _link(session, operator, "conn-b", client_id="red-rail")

    granted = await _grant(repo, session, operator, ["conn-historical", "conn-b", "conn-a"])

    assert granted.connection_ids == ["conn-b", "conn-a"]
    assert granted.connection_client_ids == ["red-rail", "workstation-claude"]


async def test_grant_elevation_without_an_attributed_connection_is_refused(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-historical", client_id=None)
    with pytest.raises(ClientCredentialError) as refused:
        await _grant(repo, session, operator, ["conn-historical"])
    assert refused.value.code == "no_attributed_connection"
    assert await session.scalar(sa.text("SELECT count(*) FROM brain_admin_elevations")) == 0


async def test_grant_elevation_on_a_session_without_connections_is_refused(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    with pytest.raises(ClientCredentialError) as refused:
        await _grant(repo, session, operator, [])
    assert refused.value.code == "no_attributed_connection"
    assert await session.scalar(sa.text("SELECT count(*) FROM brain_admin_elevations")) == 0
    assert await session.scalar(sa.text("SELECT count(*) FROM brain_credential_audit")) == 0


async def test_an_elevation_matches_only_its_frozen_pair(session: AsyncSession) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-a", client_id="workstation-claude")
    await _link(session, operator, "conn-b", client_id="red-rail")
    await _grant(repo, session, operator, ["conn-a", "conn-b"])
    at = NOW + timedelta(minutes=1)

    assert await repo.has_active_elevation("conn-a", "workstation-claude", at, session=session)
    assert await repo.has_active_elevation("conn-b", "red-rail", at, session=session)
    # Same connection under another credential, and the crossed pair: both refused.
    assert not await repo.has_active_elevation("conn-a", "red-rail", at, session=session)
    assert not await repo.has_active_elevation("conn-b", "workstation-claude", at, session=session)
    assert not await repo.has_active_elevation("conn-z", "workstation-claude", at, session=session)


async def test_a_connection_linked_after_the_grant_is_not_elevated(session: AsyncSession) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-a", client_id="workstation-claude")
    await _grant(repo, session, operator, ["conn-a"])
    await _link(session, operator, "conn-late", client_id="workstation-claude")
    at = NOW + timedelta(minutes=1)

    assert not await repo.has_active_elevation(
        "conn-late", "workstation-claude", at, session=session
    )


async def test_the_pair_lookup_follows_expiry_revocation_and_the_session(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-a", client_id="workstation-claude")
    granted = await _grant(repo, session, operator, ["conn-a"])

    async def lookup(at: datetime) -> bool:
        return await repo.has_active_elevation("conn-a", "workstation-claude", at, session=session)

    assert await lookup(NOW + timedelta(minutes=59))
    assert not await lookup(NOW + timedelta(hours=1))
    await repo.end_elevation(
        granted.id, NOW + timedelta(minutes=10), author="operator", session=session
    )
    assert not await lookup(NOW + timedelta(minutes=11))

    other = await _make_session(session)
    await _link(session, other, "conn-a", client_id="workstation-claude")
    await _grant(repo, session, other, ["conn-a"])
    assert await lookup(NOW + timedelta(minutes=11))
    await session.execute(
        sa.text(
            "UPDATE brain_sessions SET status = 'abandoned', ended_at = now(), "
            "abandonment_reason = 'test' WHERE id = :id"
        ),
        {"id": other},
    )
    assert not await lookup(NOW + timedelta(minutes=11))


async def test_parallel_elevations_on_one_session_and_on_two_sessions_are_all_active(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    first = await _make_session(session)
    second = await _make_session(session)
    await _link(session, first, "conn-1", "conn-2")
    await _link(session, second, "conn-3")

    grants = [
        await _grant(repo, session, first, ["conn-1"]),
        await _grant(repo, session, first, ["conn-2"]),
        await _grant(repo, session, second, ["conn-3"]),
    ]

    active = {row.id for row in await repo.active_elevations(NOW, session=session)}
    assert {grant.id for grant in grants} <= active


async def test_a_hook_elevation_records_its_requester_and_a_cli_one_does_not(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-1")
    by_cli = await _grant(repo, session, operator, ["conn-1"])
    by_hook = await repo.grant_elevation(
        operator,
        ["conn-1"],
        NOW + timedelta(hours=1),
        "workstation-elevate",
        "maintenance",
        NOW,
        via="hook",
        requested_by_client_id="workstation-elevate",
        elevatable_client_ids=ANY_CLIENT,
        session=session,
    )
    assert (by_cli.via, by_cli.requested_by_client_id) == ("cli", None)
    assert (by_hook.via, by_hook.requested_by_client_id) == ("hook", "workstation-elevate")
    assert by_hook.expiry_audited_at is None


async def test_grant_elevation_refuses_a_hook_without_requester_and_a_long_reason(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-1")
    window = (operator, ["conn-1"], NOW + timedelta(hours=1), "operator")
    with pytest.raises(ClientCredentialError) as no_requester:
        await repo.grant_elevation(
            *window, "why", NOW, via="hook", elevatable_client_ids=ANY_CLIENT, session=session
        )
    assert no_requester.value.code == "requester_required"
    with pytest.raises(ClientCredentialError) as too_long:
        await repo.grant_elevation(
            *window, "x" * 201, NOW, elevatable_client_ids=ANY_CLIENT, session=session
        )
    assert too_long.value.code == "reason_too_long"
    assert await session.scalar(sa.text("SELECT count(*) FROM brain_admin_elevations")) == 0


async def test_only_the_allowlisted_pairs_are_frozen_and_the_rest_is_recorded_as_excluded(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-historical", client_id=None)
    await _link(session, operator, "conn-a", client_id="workstation-claude")
    await _link(session, operator, "conn-b", client_id="red-rail")
    await _link(session, operator, "conn-c", client_id="red-rail")

    granted = await _grant(
        repo,
        session,
        operator,
        ["conn-historical", "conn-b", "conn-a", "conn-c"],
        DEFAULT_ELEVATABLE_CLIENT_IDS,
    )

    assert (granted.connection_ids, granted.connection_client_ids) == (
        ["conn-a"],
        ["workstation-claude"],
    )
    assert granted.excluded_client_ids == ["red-rail"]
    assert granted.excluded_connection_count == 2
    at = NOW + timedelta(minutes=1)
    assert await repo.has_active_elevation("conn-a", "workstation-claude", at, session=session)
    assert not await repo.has_active_elevation("conn-b", "red-rail", at, session=session)


async def test_a_grant_that_excludes_nothing_records_no_exclusion(session: AsyncSession) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-a", client_id="workstation-claude")
    granted = await _grant(repo, session, operator, ["conn-a"], DEFAULT_ELEVATABLE_CLIENT_IDS)
    assert (granted.excluded_client_ids, granted.excluded_connection_count) == ([], 0)


async def test_a_grant_whose_attributed_connections_are_all_foreign_is_refused(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-historical", client_id=None)
    await _link(session, operator, "conn-b", client_id="red-rail")
    with pytest.raises(ClientCredentialError) as refused:
        await _grant(
            repo, session, operator, ["conn-historical", "conn-b"], DEFAULT_ELEVATABLE_CLIENT_IDS
        )
    assert refused.value.code == "no_elevatable_connection"
    assert await session.scalar(sa.text("SELECT count(*) FROM brain_admin_elevations")) == 0


async def test_a_grant_needs_a_non_empty_allowlist(session: AsyncSession) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-a", client_id="workstation-claude")
    with pytest.raises(ClientCredentialError) as refused:
        await _grant(repo, session, operator, ["conn-a"], frozenset())
    assert refused.value.code == "no_elevatable_clients"


async def test_a_cli_elevation_that_names_a_requester_is_refused(session: AsyncSession) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await _link(session, operator, "conn-1")
    with pytest.raises(ClientCredentialError) as refused:
        await repo.grant_elevation(
            operator,
            ["conn-1"],
            NOW + timedelta(hours=1),
            "operator",
            "maintenance",
            NOW,
            via="cli",
            requested_by_client_id="workstation-elevate",
            elevatable_client_ids=ANY_CLIENT,
            session=session,
        )
    assert refused.value.code == "requester_forbidden"
    assert await session.scalar(sa.text("SELECT count(*) FROM brain_admin_elevations")) == 0


async def _opener(session: AsyncSession, session_id: UUID) -> str | None:
    return await session.scalar(
        sa.text("SELECT opener_client_id FROM brain_sessions WHERE id = :id"), {"id": session_id}
    )


async def test_the_first_allowlisted_client_claims_an_unowned_operator_session(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    owner = await repo.claim_or_check_session_owner(
        operator,
        "workstation-claude",
        elevatable_client_ids=DEFAULT_ELEVATABLE_CLIENT_IDS,
        session=session,
    )
    assert owner == "workstation-claude"
    assert await _opener(session, operator) == "workstation-claude"
    again = await repo.claim_or_check_session_owner(
        operator,
        "workstation-claude",
        elevatable_client_ids=DEFAULT_ELEVATABLE_CLIENT_IDS,
        session=session,
    )
    assert again == "workstation-claude"


async def test_a_foreign_client_cannot_claim_an_unowned_operator_session(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    with pytest.raises(ForeignClientAttachError) as refused:
        await repo.claim_or_check_session_owner(
            operator,
            "red-rail",
            elevatable_client_ids=DEFAULT_ELEVATABLE_CLIENT_IDS,
            session=session,
        )
    assert refused.value.code == "foreign_client_attach"
    assert (refused.value.requesting_client_id, refused.value.owner_client_id) == (
        "red-rail",
        None,
    )
    assert await _opener(session, operator) is None


async def test_a_foreign_client_is_refused_on_a_claimed_session_and_nothing_is_written(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session)
    await repo.claim_or_check_session_owner(
        operator,
        "workstation-claude",
        elevatable_client_ids=DEFAULT_ELEVATABLE_CLIENT_IDS,
        session=session,
    )
    with pytest.raises(ForeignClientAttachError) as refused:
        await repo.claim_or_check_session_owner(
            operator,
            "red-rail",
            elevatable_client_ids=ANY_CLIENT,
            session=session,
        )
    assert (refused.value.requesting_client_id, refused.value.owner_client_id) == (
        "red-rail",
        "workstation-claude",
    )
    assert "conn" not in str(refused.value)
    assert await _opener(session, operator) == "workstation-claude"


async def test_an_agent_trace_is_not_checked_and_never_claimed(session: AsyncSession) -> None:
    repo = PgClientCredentialRepo()
    trace = await _make_session(session, nature="agent")
    owner = await repo.claim_or_check_session_owner(
        trace, "red-rail", elevatable_client_ids=DEFAULT_ELEVATABLE_CLIENT_IDS, session=session
    )
    assert owner == "red-rail"
    assert await _opener(session, trace) is None


async def test_claiming_an_unknown_session_is_refused(session: AsyncSession) -> None:
    repo = PgClientCredentialRepo()
    with pytest.raises(ClientCredentialError) as refused:
        await repo.claim_or_check_session_owner(
            uuid4(),
            "workstation-claude",
            elevatable_client_ids=DEFAULT_ELEVATABLE_CLIENT_IDS,
            session=session,
        )
    assert refused.value.code == "unknown_session"


async def test_an_operator_nature_session_is_locked_like_an_unnamed_one(
    session: AsyncSession,
) -> None:
    repo = PgClientCredentialRepo()
    operator = await _make_session(session, nature="operator")
    with pytest.raises(ForeignClientAttachError):
        await repo.claim_or_check_session_owner(
            operator,
            "red-rail",
            elevatable_client_ids=DEFAULT_ELEVATABLE_CLIENT_IDS,
            session=session,
        )
    assert await _opener(session, operator) is None
    await repo.claim_or_check_session_owner(
        operator,
        "workstation-claude",
        elevatable_client_ids=DEFAULT_ELEVATABLE_CLIENT_IDS,
        session=session,
    )
    assert await _opener(session, operator) == "workstation-claude"
    with pytest.raises(ForeignClientAttachError) as refused:
        await repo.claim_or_check_session_owner(
            operator, "red-rail", elevatable_client_ids=ANY_CLIENT, session=session
        )
    assert refused.value.owner_client_id == "workstation-claude"
