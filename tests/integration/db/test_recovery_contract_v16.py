"""The v16 contract on disposable databases: same verdict as v15, plus its two fixes.

* Base and twin pass against a fresh 058 head and a real restore of it, beyond
  the drift v15 already pins (the disabled 050 trigger, the base's
  extension-version pin) — the v15 module's own assertions, reused.
* The checks come back in byte order on a database whose collation is a
  locale, where v15 returns them in locale order (decision 7d2f7fe8).
* A captured artifact whose knowledge is gone is tolerated ONLY when the
  knowledge is absent from every table and ``brain_entities`` tombstones it as
  ``deleted`` with the matching type, in the session's project (ee26407b). Every
  near miss stays an ``artifact_project_mismatches`` failure.

Seeding writes rows directly under ``session_replication_role = replica``: the
contract measures the state a restore carries, not the writers that produced it,
and this database is disposable.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
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
async def test_an_artifact_whose_knowledge_was_deleted_is_tolerated(
    seeded_head_db_url: str, knowledge_type: str, entity_type: str
) -> None:
    knowledge_id = uuid.uuid4()
    await _execute(
        seeded_head_db_url,
        [
            _artifact(seeded_head_db_url, knowledge_id, knowledge_type),
            _tombstone(knowledge_id, entity_type),
        ],
    )
    # v15 is the drill's failure; v16 tolerates exactly this case.
    assert await _artifact_mismatches(seeded_head_db_url, V15_SQL) == 1
    assert await _artifact_mismatches(seeded_head_db_url, V16_SQL) == 0
    assert await _artifact_mismatches(seeded_head_db_url, V16_PGRESTORE) == 0


def _no_tombstone(knowledge_id: uuid.UUID) -> list[tuple[str, tuple]]:
    return []


def _active_entity(knowledge_id: uuid.UUID) -> list[tuple[str, tuple]]:
    return [_tombstone(knowledge_id, "decision", lifecycle="active")]


def _archived_entity(knowledge_id: uuid.UUID) -> list[tuple[str, tuple]]:
    return [_tombstone(knowledge_id, "decision", lifecycle="archived")]


def _wrong_type(knowledge_id: uuid.UUID) -> list[tuple[str, tuple]]:
    return [_tombstone(knowledge_id, "learning")]


def _other_project(knowledge_id: uuid.UUID) -> list[tuple[str, tuple]]:
    return [_tombstone(knowledge_id, "decision", project=OTHER_PROJECT)]


def _still_exists_elsewhere(knowledge_id: uuid.UUID) -> list[tuple[str, tuple]]:
    return [_tombstone(knowledge_id, "decision"), _decision(knowledge_id, OTHER_PROJECT)]


def _deleted_before_capture(knowledge_id: uuid.UUID) -> list[tuple[str, tuple]]:
    """Knowledge deleted before the capture was never there to capture."""
    return [_tombstone(knowledge_id, "decision", deleted_minutes_ago=20)]


def _created_after_capture(knowledge_id: uuid.UUID) -> list[tuple[str, tuple]]:
    """Knowledge created after the capture was not there to capture either."""
    return [_tombstone(knowledge_id, "decision", created_minutes_ago=5)]


def _created_before_the_session(knowledge_id: uuid.UUID) -> list[tuple[str, tuple]]:
    """A session captures only what it created: v15's own source window."""
    return [_tombstone(knowledge_id, "decision", created_minutes_ago=120)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "near_miss",
    [
        _no_tombstone,
        _active_entity,
        _archived_entity,
        _wrong_type,
        _other_project,
        _still_exists_elsewhere,
        _deleted_before_capture,
        _created_after_capture,
        _created_before_the_session,
    ],
)
async def test_every_near_miss_stays_a_mismatch(seeded_head_db_url: str, near_miss: Any) -> None:
    knowledge_id = uuid.uuid4()
    await _execute(
        seeded_head_db_url,
        [_artifact(seeded_head_db_url, knowledge_id, "decision"), *near_miss(knowledge_id)],
    )
    assert await _artifact_mismatches(seeded_head_db_url, V16_SQL) == 1
    assert await _artifact_mismatches(seeded_head_db_url, V16_PGRESTORE) == 1


@pytest.mark.asyncio
async def test_a_live_captured_artifact_is_still_matched(seeded_head_db_url: str) -> None:
    """The tolerance does not disturb the ordinary case: present source, no mismatch."""
    knowledge_id = uuid.uuid4()
    await _execute(
        seeded_head_db_url,
        [_decision(knowledge_id, PROJECT), _artifact(seeded_head_db_url, knowledge_id, "decision")],
    )
    assert await _artifact_mismatches(seeded_head_db_url, V16_SQL) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "knowledge_type,entity_type",
    [("legacy", "legacy"), ("indexed_plan", "indexed_plan"), ("decision", "plan")],
)
async def test_a_tombstone_of_a_non_knowledge_or_mismatched_type_stays_a_mismatch(
    seeded_head_db_url: str, knowledge_type: str, entity_type: str
) -> None:
    """`legacy` and `indexed_plan` are artifact labels, never graph entity types."""
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
