"""The v16 contract on disposable databases: same verdict as v15, plus its two fixes.

* Base and twin pass against a fresh 058 head and a real restore of it, beyond
  the drift v15 already pins (the disabled 050 trigger, the base's
  extension-version pin) — the v15 module's own assertions, reused.
* The checks come back in byte order on a database whose collation is a
  locale, where v15 returns them in locale order (decision 7d2f7fe8).
* The one historical orphan (decision a301034b) is tolerated by its exact ledger
  row, while its knowledge is absent everywhere and ``brain_entities`` tombstones
  it as ``deleted`` (decisions ee26407b, a00bdf68). Every near miss of that row,
  and every OTHER deleted capture however plausible its tombstone, stays an
  ``artifact_project_mismatches`` failure.

Seeding writes rows directly under ``session_replication_role = replica``: the
contract measures the state a restore carries, not the writers that produced it,
and this database is disposable.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.db.test_recovery_contract_v15 import (
    _assert_no_unexplained,
    _database_url_or_skip,
    _kinds,
    fresh_head_db_url,  # noqa: F401 — module fixture reused as is
    restored_head_db_url,  # noqa: F401 — module fixture reused as is
)
from tests.integration.disposable_db import (
    alembic_upgrade_head,
    asyncpg_dsn,
    drop_database,
    replay_attestation,
    run_sql,
    swap_database,
)

pytestmark = pytest.mark.integration

RECOVERY = Path(__file__).parents[3] / "ops" / "recovery"
V15_SQL = RECOVERY / "brain-v42-v15.sql"
V16_SQL = RECOVERY / "brain-v42-v16.sql"
V16_JSON = RECOVERY / "brain-v42-v16.json"
V16_PGRESTORE = RECOVERY / "brain-v42-v16-pgrestore.sql"

PROJECT = "dr-v16-project"
OTHER_PROJECT = "dr-v16-other"
RUNTIME_CHECK = "brain_runtime_032_036_037"


@pytest.mark.asyncio
async def test_recovery_contract_v16_matches_fresh_and_restored_head(
    fresh_head_db_url: str,  # noqa: F811
    restored_head_db_url: str,  # noqa: F811
) -> None:
    kinds = _kinds(json.loads(V16_JSON.read_text(encoding="utf-8")))

    base_failures = await replay_attestation(fresh_head_db_url, V16_SQL)
    _assert_no_unexplained(base_failures, kinds, exempt_extension=True, target="base")

    twin_failures = await replay_attestation(restored_head_db_url, V16_PGRESTORE)
    _assert_no_unexplained(twin_failures, kinds, exempt_extension=False, target="-pgrestore")
    assert "extension_versions" not in twin_failures


@pytest.fixture(scope="module")
def locale_head_db_url() -> Iterator[str]:
    """A 058 head whose default collation is a locale, as production's is."""
    admin_url = _database_url_or_skip()
    name = f"brain_v16_locale_{uuid.uuid4().hex[:12]}"
    try:
        run_sql(
            asyncpg_dsn(admin_url),
            [
                f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'UTF8' "
                "LC_COLLATE 'en_US.utf8' LC_CTYPE 'en_US.utf8'"
            ],
        )
    except Exception as exc:  # a server without that locale cannot run this proof
        pytest.skip(f"en_US.utf8 is not available on this server: {exc}")
    url = swap_database(admin_url, name)
    try:
        alembic_upgrade_head(url)
        yield url
    finally:
        drop_database(admin_url, name)


async def _receipt_ids(url: str, asset: Path) -> list[str]:
    connection = await asyncpg.connect(asyncpg_dsn(url))
    try:
        async with connection.transaction(readonly=True):
            raw = await connection.fetchval(asset.read_text(encoding="utf-8"))
    finally:
        await connection.close()
    return [check["id"] for check in json.loads(raw)["checks"]]


@pytest.mark.asyncio
async def test_checks_come_back_in_byte_order_under_a_locale_collation(
    locale_head_db_url: str,
) -> None:
    v15_ids = await _receipt_ids(locale_head_db_url, V15_SQL)
    v16_ids = await _receipt_ids(locale_head_db_url, V16_SQL)

    byte_order = sorted(v16_ids, key=lambda value: value.encode("utf-8"))
    assert v16_ids == byte_order
    # The proof is only worth something where v15 actually goes wrong.
    assert v15_ids != sorted(v15_ids, key=lambda value: value.encode("utf-8")), (
        "v15 is already in byte order on this database: the locale does not "
        "exercise the defect, so this test proves nothing"
    )
    assert sorted(v15_ids) == sorted(v16_ids)


async def _execute(url: str, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
    connection = await asyncpg.connect(asyncpg_dsn(url))
    try:
        async with connection.transaction():
            await connection.execute("SET LOCAL session_replication_role = replica")
            for statement, arguments in statements:
                await connection.execute(statement, *arguments)
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def seeded_head_db_url(fresh_head_db_url: str) -> AsyncIterator[str]:  # noqa: F811
    """Two projects and one open session; every test's rows are removed after it."""
    session_id = uuid.uuid4()
    statements: list[tuple[str, tuple[Any, ...]]] = []
    for key in (PROJECT, OTHER_PROJECT):
        statements.append(("INSERT INTO projects (project_key) VALUES ($1)", (key,)))
        statements.append(
            (
                "INSERT INTO project_contexts (project_key, name, description) "
                "VALUES ($1, $1, 'dr v16 fixture')",
                (key,),
            )
        )
    statements.append(
        (
            "INSERT INTO brain_sessions (id, project_key, client_key, started_focus_revision, "
            "started_at, last_heartbeat_at) "
            "VALUES ($1, $2, 'dr-v16', 0, now() - interval '1 hour', now())",
            (session_id, PROJECT),
        )
    )
    await _execute(fresh_head_db_url, statements)
    _SESSION[fresh_head_db_url] = session_id
    try:
        yield fresh_head_db_url
    finally:
        await _execute(
            fresh_head_db_url,
            [
                ("DELETE FROM brain_session_artifacts", ()),
                ("DELETE FROM brain_entities WHERE entity_key LIKE 'dr-v16:%'", ()),
                ("DELETE FROM decisions WHERE title = 'dr v16 fixture'", ()),
                ("DELETE FROM brain_sessions", ()),
                ("DELETE FROM project_contexts WHERE project_key LIKE 'dr-v16-%'", ()),
                ("DELETE FROM projects WHERE project_key LIKE 'dr-v16-%'", ()),
            ],
        )


_SESSION: dict[str, uuid.UUID] = {}


def _artifact(url: str, knowledge_id: uuid.UUID, knowledge_type: str) -> tuple[str, tuple]:
    return (
        "INSERT INTO brain_session_artifacts (knowledge_id, session_id, knowledge_type, "
        "captured_at) VALUES ($1, $2, $3, now() - interval '10 minutes')",
        (knowledge_id, _SESSION[url], knowledge_type),
    )


def _tombstone(
    knowledge_id: uuid.UUID,
    entity_type: str,
    *,
    lifecycle: str = "deleted",
    project: str = PROJECT,
    deleted_minutes_ago: int = 0,
    created_minutes_ago: int = 30,
) -> tuple[str, tuple]:
    """A graph entity for the knowledge; `deleted_at` is set only for a deleted one.

    The session starts an hour ago and artifacts are captured ten minutes ago, so
    the default tombstone (created thirty minutes ago, deleted now) records
    knowledge that existed at capture and was deleted after it.
    """
    return (
        "INSERT INTO brain_entities (entity_type, entity_key, source_uuid, project_key, "
        "scope_kind, lifecycle, created_at, deleted_at) VALUES ($1, $2, $3, $4, 'project', "
        "$5::varchar, now() - make_interval(mins => $7), "
        "CASE WHEN $5::varchar = 'deleted' THEN now() - make_interval(mins => $6) END)",
        (
            entity_type,
            f"dr-v16:{knowledge_id}",
            knowledge_id,
            project,
            lifecycle,
            deleted_minutes_ago,
            created_minutes_ago,
        ),
    )


def _decision(knowledge_id: uuid.UUID, project: str) -> tuple[str, tuple]:
    return (
        "INSERT INTO decisions (id, title, description, reasoning, project_key, created_at) "
        "VALUES ($1, 'dr v16 fixture', 'x', 'x', $2, now() - interval '30 minutes')",
        (knowledge_id, project),
    )


async def _artifact_mismatches(url: str, asset: Path) -> int:
    failures = await replay_attestation(url, asset)
    runtime = failures.get(RUNTIME_CHECK)
    assert runtime is not None, "the pinned 050 trigger drift vanished: re-measure"
    return int(runtime["observed"]["artifact_project_mismatches"])


#: The one tolerated row (decision a00bdf68), exactly as the production ledger holds it.
ORPHAN_ID = uuid.UUID("a301034b-079a-4961-b613-5256a017a519")
ORPHAN_SESSION = uuid.UUID("2923d3c2-ef29-417d-ad52-a31d9c08bfe8")
ORPHAN_CAPTURED = datetime(2026, 9, 26, 20, 21, 39, 223113, tzinfo=UTC)


def _orphan_session() -> tuple[str, tuple]:
    return (
        "INSERT INTO brain_sessions (id, project_key, client_key, started_focus_revision, "
        "started_at, last_heartbeat_at) VALUES ($1, $2, 'dr-v16-orphan', 0, $3, $3)",
        (ORPHAN_SESSION, PROJECT, ORPHAN_CAPTURED - timedelta(hours=2)),
    )


def _orphan_artifact(
    *,
    session_id: uuid.UUID = ORPHAN_SESSION,
    knowledge_type: str = "decision",
    captured_at: datetime = ORPHAN_CAPTURED,
) -> tuple[str, tuple]:
    return (
        "INSERT INTO brain_session_artifacts (knowledge_id, session_id, knowledge_type, "
        "captured_at) VALUES ($1, $2, $3, $4)",
        (ORPHAN_ID, session_id, knowledge_type, captured_at),
    )


def _orphan_tombstone(*, entity_type: str = "decision", lifecycle: str = "deleted") -> tuple:
    return _tombstone(ORPHAN_ID, entity_type, lifecycle=lifecycle)


async def _orphan_mismatches(url: str, rows: list[tuple[str, tuple]]) -> tuple[int, int, int]:
    await _execute(url, [_orphan_session(), *rows])
    return (
        await _artifact_mismatches(url, V15_SQL),
        await _artifact_mismatches(url, V16_SQL),
        await _artifact_mismatches(url, V16_PGRESTORE),
    )


@pytest.mark.asyncio
async def test_the_named_historical_orphan_is_tolerated(seeded_head_db_url: str) -> None:
    """v15 is the drill's failure; v16 names this exact ledger row and tolerates it."""
    counts = await _orphan_mismatches(seeded_head_db_url, [_orphan_artifact(), _orphan_tombstone()])
    assert counts == (1, 0, 0)


def _named_rows_with(**changes: Any) -> list[tuple[str, tuple]]:
    artifact = {k: v for k, v in changes.items() if k in {"knowledge_type", "captured_at"}}
    tombstone = {k: v for k, v in changes.items() if k in {"entity_type", "lifecycle"}}
    rows = [_orphan_artifact(**artifact)]
    if not changes.get("no_tombstone"):
        rows.append(_orphan_tombstone(**tombstone))
    if changes.get("knowledge_present"):
        rows.append(_decision(ORPHAN_ID, OTHER_PROJECT))
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"captured_at": ORPHAN_CAPTURED + timedelta(microseconds=1)}, id="instant"),
        pytest.param({"knowledge_type": "learning", "entity_type": "learning"}, id="type"),
        pytest.param({"lifecycle": "active"}, id="tombstone-active"),
        pytest.param({"lifecycle": "archived"}, id="tombstone-archived"),
        pytest.param({"entity_type": "learning"}, id="tombstone-type"),
        pytest.param({"no_tombstone": True}, id="no-tombstone"),
        pytest.param({"knowledge_present": True}, id="knowledge-present"),
    ],
)
async def test_every_near_miss_of_the_named_orphan_stays_a_mismatch(
    seeded_head_db_url: str, changes: dict[str, Any]
) -> None:
    v15, v16, twin = await _orphan_mismatches(seeded_head_db_url, _named_rows_with(**changes))
    assert (v16, twin) == (1, 1)


@pytest.mark.asyncio
async def test_the_named_orphan_under_another_session_stays_a_mismatch(
    seeded_head_db_url: str,
) -> None:
    other_session = _SESSION[seeded_head_db_url]
    v15, v16, twin = await _orphan_mismatches(
        seeded_head_db_url,
        [_orphan_artifact(session_id=other_session), _orphan_tombstone()],
    )
    # The 2026-09-26 capture also precedes this session's start, a lifecycle
    # violation counted in the same total: v16 must tolerate nothing v15 counts.
    assert v15 >= 1
    assert (v16, twin) == (v15, v15)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "knowledge_type,entity_type",
    [
        ("decision", "decision"),
        ("learning", "learning"),
        ("legacy", "decision"),
        ("indexed_plan", "plan"),
    ],
)
async def test_any_other_deleted_capture_stays_a_mismatch(
    seeded_head_db_url: str, knowledge_type: str, entity_type: str
) -> None:
    """No general rule: a tombstone that looks right is not enough (decision a00bdf68)."""
    knowledge_id = uuid.uuid4()
    await _execute(
        seeded_head_db_url,
        [
            _artifact(seeded_head_db_url, knowledge_id, knowledge_type),
            _tombstone(knowledge_id, entity_type),
        ],
    )
    assert await _artifact_mismatches(seeded_head_db_url, V16_SQL) == 1
    assert await _artifact_mismatches(seeded_head_db_url, V16_PGRESTORE) == 1


@pytest.mark.asyncio
async def test_a_live_captured_artifact_is_still_matched(seeded_head_db_url: str) -> None:
    """The exception does not disturb the ordinary case: present source, no mismatch."""
    knowledge_id = uuid.uuid4()
    await _execute(
        seeded_head_db_url,
        [_decision(knowledge_id, PROJECT), _artifact(seeded_head_db_url, knowledge_id, "decision")],
    )
    assert await _artifact_mismatches(seeded_head_db_url, V16_SQL) == 0
