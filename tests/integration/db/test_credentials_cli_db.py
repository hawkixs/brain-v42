"""Replay CLI gestures over the real repository inside rolled-back transactions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
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
    assert item.keys() == {"id", "client_id", "families", "transition", "expires_at", "revoked_at"}
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
