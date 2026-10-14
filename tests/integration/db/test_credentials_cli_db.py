"""Replay CLI gestures over the real repository inside rolled-back transactions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain_v42.repositories.pg_client_credentials import PgClientCredentialRepo
from tests.integration.repositories.test_pg_client_credentials import _link, _make_session

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def cli_database(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncSession, async_sessionmaker[AsyncSession]]]:
    from brain_v42.scripts import credentials_cli

    async with engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        monkeypatch.setattr(credentials_cli, "get_session_factory", lambda: factory)
        monkeypatch.setattr(
            credentials_cli,
            "get_settings",
            lambda: SimpleNamespace(elevatable_client_ids={"workstation-claude"}),
        )

        async def dispose() -> None:
            pass

        monkeypatch.setattr(credentials_cli, "dispose_engine", dispose)
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            try:
                yield session, factory
            finally:
                await transaction.rollback()


async def invoke(argv: list[str]) -> int:
    from brain_v42.scripts.credentials_cli import parse_args, run_from_args

    return await run_from_args(parse_args(argv))


async def test_issue_list_and_revoke_keep_only_digest_and_atomic_audits(
    cli_database: tuple[AsyncSession, async_sessionmaker[AsyncSession]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    session, factory = cli_database
    assert (
        await invoke(
            [
                "issue",
                "--client-id",
                "red-rail",
                "--families",
                "read,delivery",
                "--reason",
                "provision",
                "--expires",
                "1h",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    token = output.out.strip()
    assert token.startswith("bk1_") and output.out == token + "\n"
    assert token not in output.err
    digest = hashlib.sha256(token.encode()).digest()
    repo = PgClientCredentialRepo(factory)
    now = datetime.now(UTC)
    assert await repo.disposition_by_digest(digest, now) == ("red-rail", "active")
    rows = await repo.list_rows()
    issued = next(row for row in rows if row.token_sha256 == digest)
    audits = (
        await session.execute(
            sa.text(
                "SELECT event, payload FROM brain_credential_audit "
                "WHERE payload->>'credential_id' = :id ORDER BY id"
            ),
            {"id": str(issued.id)},
        )
    ).all()
    assert [row.event for row in audits] == ["credentials.issued"]
    assert audits[0].payload["reason"] == "provision"
    assert token not in json.dumps([row.payload for row in audits])
    assert digest.hex() not in json.dumps([row.payload for row in audits])

    assert await invoke(["revoke", str(issued.id), "--reason", "rotated"]) == 0
    capsys.readouterr()
    assert await repo.disposition_by_digest(digest, now) == ("red-rail", "revoked")
    before = await session.scalar(sa.text("SELECT count(*) FROM brain_credential_audit"))
    assert await invoke(["list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    item = next(row for row in listed if row["id"] == str(issued.id))
    assert item.keys() == {
        "id",
        "client_id",
        "families",
        "allowed_projects",
        "transition",
        "expires_at",
        "revoked_at",
    }
    assert item["revoked_at"] is not None
    assert await session.scalar(sa.text("SELECT count(*) FROM brain_credential_audit")) == before
    revoked = await session.scalar(
        sa.text(
            "SELECT payload FROM brain_credential_audit WHERE event = 'credentials.revoked' "
            "AND payload->>'credential_id' = :id"
        ),
        {"id": str(issued.id)},
    )
    assert revoked["reason"] == "rotated"


async def test_elevate_freezes_confirmed_allowlist_and_unelevate_audits_the_grant(
    cli_database: tuple[AsyncSession, async_sessionmaker[AsyncSession]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    session, factory = cli_database
    operator = await _make_session(session)
    allowed_connection = "cli-allowed-connection-private"
    excluded_connection = "cli-excluded-connection-private"
    await _link(session, operator, allowed_connection, client_id="workstation-claude")
    await _link(session, operator, excluded_connection, client_id="red-rail")
    assert (
        await invoke(["elevate", str(operator), "--ttl", "1h", "--reason", "maintenance", "--yes"])
        == 0
    )
    output = capsys.readouterr()
    assert allowed_connection not in output.out + output.err
    assert excluded_connection not in output.out + output.err
    elevation_id = UUID(output.out.split()[0])
    repo = PgClientCredentialRepo(factory)
    now = datetime.now(UTC)
    grants = await repo.active_elevations(now)
    granted = next(row for row in grants if row.id == elevation_id)
    assert granted.connection_ids == [allowed_connection]
    assert granted.connection_client_ids == ["workstation-claude"]
    assert granted.excluded_client_ids == ["red-rail"]
    assert granted.excluded_connection_count == 1
    assert granted.via == "cli" and granted.requested_by_client_id is None
    await _link(session, operator, "cli-after-grant-private", client_id="workstation-claude")
    assert not await repo.has_active_elevation("cli-after-grant-private", "workstation-claude", now)
    before = await session.scalar(sa.text("SELECT count(*) FROM brain_credential_audit"))
    assert await invoke(["elevations", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert str(elevation_id) in {row["id"] for row in listed}
    assert allowed_connection not in json.dumps(listed)
    assert await session.scalar(sa.text("SELECT count(*) FROM brain_credential_audit")) == before
    assert await invoke(["unelevate", str(elevation_id), "--reason", "finished"]) == 0
    assert not await repo.has_active_elevation(allowed_connection, "workstation-claude", now)
    audits = (
        await session.execute(
            sa.text(
                "SELECT event, payload FROM brain_credential_audit "
                "WHERE elevation_id = :id ORDER BY id"
            ),
            {"id": elevation_id},
        )
    ).all()
    assert [row.event for row in audits] == ["credentials.elevated", "credentials.unelevated"]
    assert all(row.payload["reason"] == "maintenance" for row in audits)
    assert audits[1].payload["ending_reason"] == "finished"
    assert all(row.payload["excluded_client_ids"] == ["red-rail"] for row in audits)
    assert all(row.payload["via"] == "cli" and row.payload["client_id"] is None for row in audits)
    assert allowed_connection not in json.dumps([row.payload for row in audits])


async def test_scope_and_clear_store_arrays_and_atomic_audit_trail(
    cli_database: tuple[AsyncSession, async_sessionmaker[AsyncSession]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    session, factory = cli_database
    repo = PgClientCredentialRepo(factory)
    issued = await repo.issue(
        "scope-worker", hashlib.sha256(uuid4().bytes).digest(), ["write"], [], "operator"
    )
    assert getattr(issued, "allowed_projects", "missing") is None
    for options, expected in (
        (["--projects", "brain_v42,red-shrik:agent-a"], ["brain-v42", "red-shrik:agent-a"]),
        (["--clear"], None),
    ):
        assert (
            await invoke(["scope", str(issued.id), *options, "--reason", "restrict projects"]) == 0
        )
        output = capsys.readouterr()
        assert output.out == ""
        stored = await session.scalar(
            sa.text("SELECT allowed_projects FROM brain_client_credentials WHERE id = :id"),
            {"id": issued.id},
        )
        assert stored == expected
    audits = (
        await session.execute(
            sa.text(
                "SELECT event, payload FROM brain_credential_audit WHERE payload->>'credential_id' = :id ORDER BY id"
            ),
            {"id": str(issued.id)},
        )
    ).all()
    assert [row.event for row in audits] == [
        "credentials.issued",
        "credentials.scope_changed",
        "credentials.scope_changed",
    ]
    assert audits[1].payload["previous_allowed_projects"] is None
    assert audits[1].payload["allowed_projects"] == ["brain-v42", "red-shrik:agent-a"]
    assert audits[2].payload["previous_allowed_projects"] == audits[1].payload["allowed_projects"]
    assert audits[2].payload["allowed_projects"] is None
    assert all(
        row.payload["reason"] == "restrict projects" and row.payload["author"] for row in audits[1:]
    )
    from brain_v42.credentials.audit import render_event

    async with repo.transaction() as owned:
        scope_rows = [
            row
            for row in await repo.claim_unemitted_audit(1000, session=owned)
            if row.event == "credentials.scope_changed"
        ]
        assert len(scope_rows) == 2
        assert all(
            render_event(row)["allowed_projects"] == row.payload["allowed_projects"]
            for row in scope_rows
        )
    assert issued.token_sha256.hex() not in json.dumps([row.payload for row in audits])


@pytest.mark.parametrize(
    "projects",
    [
        [],
        [None],
        ["a", None],
        [""],
        ["a", ""],
        ["a,b"],
        ["Bad"],
        ["brain_v42"],
        ["a::b"],
        ["a\n"],
        [["a"]],
    ],
)
async def test_scope_check_refuses_noncanonical_arrays(
    cli_database: tuple[AsyncSession, async_sessionmaker[AsyncSession]], projects: object
) -> None:
    session, factory = cli_database
    issued = await PgClientCredentialRepo(factory).issue(
        "scope-check", hashlib.sha256(uuid4().bytes).digest(), ["write"], [], "operator"
    )
    with pytest.raises(IntegrityError, match="brain_client_credentials_allowed_projects_valid"):
        async with session.begin_nested():
            await session.execute(
                sa.text(
                    "UPDATE brain_client_credentials SET allowed_projects = :projects WHERE id = :id"
                ),
                {"projects": projects, "id": issued.id},
            )


@pytest.mark.parametrize("projects", [["a", "a"], ["brain"], ["a" * 51]])
async def test_repository_refuses_scope_rules_not_expressed_by_the_inline_check(
    cli_database: tuple[AsyncSession, async_sessionmaker[AsyncSession]], projects: list[str]
) -> None:
    from brain_v42.repositories.pg_client_credentials import ClientCredentialError

    session, factory = cli_database
    repo = PgClientCredentialRepo(factory)
    issued = await repo.issue(
        "scope-repo-check", hashlib.sha256(uuid4().bytes).digest(), ["write"], [], "operator"
    )
    with pytest.raises(ClientCredentialError):
        await repo.set_scope(issued.id, projects, author="operator", reason="restrict")
    assert (
        await session.scalar(
            sa.text("SELECT allowed_projects FROM brain_client_credentials WHERE id = :id"),
            {"id": issued.id},
        )
        is None
    )


@pytest.mark.parametrize("projects", [["a" * 50], ["red-shrik:agent-a", "red-shrik:agent-a:child"]])
async def test_scope_check_stores_exact_boundary_keys(
    cli_database: tuple[AsyncSession, async_sessionmaker[AsyncSession]], projects: list[str]
) -> None:
    session, factory = cli_database
    issued = await PgClientCredentialRepo(factory).issue(
        "scope-boundary", hashlib.sha256(uuid4().bytes).digest(), ["write"], [], "operator"
    )
    assert (
        await session.scalar(
            sa.text(
                "UPDATE brain_client_credentials SET allowed_projects = :projects WHERE id = :id RETURNING allowed_projects"
            ),
            {"projects": projects, "id": issued.id},
        )
        == projects
    )


async def test_scope_audit_failure_rolls_back_the_change(
    cli_database: tuple[AsyncSession, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.repositories import pg_client_credentials

    session, factory = cli_database
    repo = PgClientCredentialRepo(factory)
    issued = await repo.issue(
        "scope-rollback", hashlib.sha256(uuid4().bytes).digest(), ["write"], [], "operator"
    )

    async def refuse_audit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(pg_client_credentials, "_write_audit", refuse_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await invoke(["scope", str(issued.id), "--projects", "brain-v42", "--reason", "restrict"])
    assert (
        await session.scalar(
            sa.text("SELECT allowed_projects FROM brain_client_credentials WHERE id = :id"),
            {"id": issued.id},
        )
        is None
    )


async def test_scope_notification_reloads_the_verifier(engine: AsyncEngine) -> None:
    from brain_v42.credentials.listener import CredentialListener
    from brain_v42.credentials.verifier import CredentialVerifier

    factory = async_sessionmaker(engine, expire_on_commit=False)
    repo = PgClientCredentialRepo(factory)
    token = "scope-notify-" + uuid4().hex
    issued = await repo.issue(
        "scope-notify", hashlib.sha256(token.encode()).digest(), ["write"], [], "operator"
    )
    loop = asyncio.get_running_loop()
    core = CredentialVerifier(repo, clock=lambda: datetime.now(UTC), monotonic=loop.time)
    ready, reloaded = asyncio.Event(), asyncio.Event()

    async def connected() -> None:
        assert await core.run_listener_reconnected()
        ready.set()

    async def notified() -> None:
        assert await core.notify()
        reloaded.set()

    listener = CredentialListener(
        os.environ["BRAIN_V42_TEST_DB_URL"],
        "brain_client_credentials",
        on_connect=connected,
        on_notification=notified,
    )
    task = asyncio.create_task(listener.run())
    try:
        await asyncio.wait_for(ready.wait(), 5)
        assert getattr(await core.verify(token), "allowed_projects", "missing") is None
        await repo.set_scope(issued.id, ["red-shrik:agent-a"], author="operator", reason="restrict")
        await asyncio.wait_for(reloaded.wait(), 5)
        assert (await core.verify(token)).allowed_projects == frozenset({"red-shrik:agent-a"})
        reloaded.clear()
        await repo.set_scope(issued.id, None, author="operator", reason="clear")
        await asyncio.wait_for(reloaded.wait(), 5)
        assert (await core.verify(token)).allowed_projects is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("DELETE FROM brain_client_credentials WHERE id = :id"), {"id": issued.id}
            )
            await connection.execute(
                sa.text("DELETE FROM brain_credential_audit WHERE payload->>'credential_id' = :id"),
                {"id": str(issued.id)},
            )
