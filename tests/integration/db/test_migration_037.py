"""Real PostgreSQL proofs for the session lifecycle v4 migration."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).parents[3]
_DB_URL = os.environ.get("BRAIN_V42_TEST_DB_URL", "")


def _run_alembic(args: list[str], *, succeeds: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        env={**os.environ, "POSTGRES_URL": _DB_URL},
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if succeeds and result.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{result.stderr}\n{result.stdout}")
    if not succeeds:
        assert result.returncode != 0, f"alembic {' '.join(args)} unexpectedly succeeded"
    return result


def _sql(
    statement: str,
    params: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    async def execute() -> list[dict[str, Any]]:
        engine = create_async_engine(_DB_URL)
        try:
            async with engine.begin() as connection:
                result = await connection.execute(sa.text(statement), params or {})
                if not result.returns_rows:
                    return []
                return [dict(row) for row in result.mappings()]
        finally:
            await engine.dispose()

    return asyncio.run(execute())


def _version() -> str:
    return str(_sql("SELECT version_num FROM alembic_version")[0]["version_num"])


def _has_column(column_name: str) -> bool:
    return bool(
        _sql(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'brain_sessions'
                  AND column_name = :column_name
            ) AS present
            """,
            {"column_name": column_name},
        )[0]["present"]
    )


def _artifact_table_exists() -> bool:
    return _sql("SELECT to_regclass('public.brain_session_artifacts') IS NOT NULL AS present")[0][
        "present"
    ]


def _seed_project(project_key: str) -> None:
    _sql(
        """
        INSERT INTO project_contexts (
            project_key, name, description, current_focus
        ) VALUES (
            :project_key, :project_key, 'migration 037 integration', 'legacy focus'
        )
        """,
        {"project_key": project_key},
    )


def _seed_v3_ended_session(
    project_key: str,
    client_key: str,
    knowledge_id: UUID,
) -> UUID:
    return _sql(
        """
        INSERT INTO brain_sessions (
            project_key, client_key, status, started_focus,
            started_focus_revision, summary, next_focus,
            captured_knowledge_ids, ended_at, updated_at
        ) VALUES (
            :project_key, :client_key, 'ended', 'legacy focus',
            0, 'legacy summary', 'legacy next focus',
            ARRAY[CAST(:knowledge_id AS uuid)],
            TIMESTAMPTZ '2026-07-22 10:05:00+00',
            TIMESTAMPTZ '2026-07-22 10:00:00+00'
        )
        RETURNING id
        """,
        {
            "project_key": project_key,
            "client_key": client_key,
            "knowledge_id": str(knowledge_id),
        },
    )[0]["id"]


def _cleanup_project(project_key: str) -> None:
    _sql("DELETE FROM brain_sessions WHERE project_key = :key", {"key": project_key})
    _sql("DELETE FROM project_contexts WHERE project_key = :key", {"key": project_key})


def _assert_failed_migration_is_atomic(expected_revision: str) -> None:
    assert _version() == expected_revision
    assert _artifact_table_exists() is (expected_revision == "037")
    assert _has_column("last_heartbeat_at") is (expected_revision == "037")


def test_migration_037_round_trip_backfill_and_fail_closed_guards(
    record_migration_downgrade: Callable[..., None],
) -> None:
    """Exercise upgrade atomicity, backfill, safe rollback, and lossy rollback fences."""
    ambiguous_project = f"integ-migration-037-ambiguous-{uuid4().hex[:8]}"
    valid_project = f"integ-migration-037-valid-{uuid4().hex[:8]}"
    artifact_project = f"integ-migration-037-artifact-{uuid4().hex[:8]}"
    conflict_project = f"integ-migration-037-conflict-{uuid4().hex[:8]}"

    record_migration_downgrade("036")
    try:
        _run_alembic(["upgrade", "head"])
        _run_alembic(["-x", "allow_project_context_trigger_downgrade=yes", "downgrade", "036"])

        duplicate_knowledge_id = uuid4()
        _seed_project(ambiguous_project)
        _seed_v3_ended_session(ambiguous_project, "legacy-a", duplicate_knowledge_id)
        _seed_v3_ended_session(ambiguous_project, "legacy-b", duplicate_knowledge_id)

        ambiguous = _run_alembic(["upgrade", "037"], succeeds=False)
        assert "artifact provenance is ambiguous" in ambiguous.stderr
        _assert_failed_migration_is_atomic("036")
        _cleanup_project(ambiguous_project)

        legacy_knowledge_id = uuid4()
        _seed_project(valid_project)
        legacy_session_id = _seed_v3_ended_session(
            valid_project,
            "legacy-valid",
            legacy_knowledge_id,
        )

        _run_alembic(["upgrade", "037"])
        assert _version() == "037"
        migrated = _sql(
            """
            SELECT
                session.focus_outcome,
                session.focus_at_end,
                session.end_expected_focus_revision,
                session.focus_revision_at_end,
                session.last_heartbeat_at = session.updated_at AS heartbeat_backfilled,
                artifact.knowledge_type,
                artifact.captured_at = session.ended_at AS captured_at_backfilled
            FROM brain_sessions AS session
            JOIN brain_session_artifacts AS artifact
              ON artifact.session_id = session.id
            WHERE session.id = :session_id
              AND artifact.knowledge_id = :knowledge_id
            """,
            {
                "session_id": legacy_session_id,
                "knowledge_id": legacy_knowledge_id,
            },
        )[0]
        assert migrated == {
            "focus_outcome": "applied",
            "focus_at_end": "legacy next focus",
            "end_expected_focus_revision": None,
            "focus_revision_at_end": None,
            "heartbeat_backfilled": True,
            "knowledge_type": "legacy",
            "captured_at_backfilled": True,
        }

        _run_alembic(["downgrade", "036"])
        assert _version() == "036"
        assert not _artifact_table_exists()
        assert not _has_column("last_heartbeat_at")
        preserved = _sql(
            """
            SELECT status, summary, next_focus,
                   captured_knowledge_ids = ARRAY[CAST(:knowledge_id AS uuid)] AS capture_preserved
            FROM brain_sessions
            WHERE id = :session_id
            """,
            {
                "session_id": legacy_session_id,
                "knowledge_id": legacy_knowledge_id,
            },
        )[0]
        assert preserved == {
            "status": "ended",
            "summary": "legacy summary",
            "next_focus": "legacy next focus",
            "capture_preserved": True,
        }

        _run_alembic(["upgrade", "037"])
        _cleanup_project(valid_project)

        _seed_project(artifact_project)
        open_session_id = _sql(
            """
            INSERT INTO brain_sessions (
                project_key, client_key, status, started_focus_revision
            ) VALUES (
                :project_key, 'open-artifact', 'open', 0
            )
            RETURNING id
            """,
            {"project_key": artifact_project},
        )[0]["id"]
        _sql(
            """
            INSERT INTO brain_session_artifacts (
                knowledge_id, session_id, knowledge_type
            ) VALUES (
                :knowledge_id, :session_id, 'learning'
            )
            """,
            {"knowledge_id": uuid4(), "session_id": open_session_id},
        )

        unsnapshotted = _run_alembic(["downgrade", "036"], succeeds=False)
        assert "unsnapshotted artifacts" in unsnapshotted.stderr
        _assert_failed_migration_is_atomic("037")
        _cleanup_project(artifact_project)

        _seed_project(conflict_project)
        _sql(
            """
            INSERT INTO brain_sessions (
                project_key, client_key, status, started_focus_revision,
                summary, next_focus, nothing_to_capture_reason,
                end_expected_focus_revision, focus_outcome,
                focus_at_end, focus_revision_at_end, ended_at
            ) VALUES (
                :project_key, 'conflicted-focus', 'ended', 0,
                'conflict summary', 'proposed focus', 'no durable artifact',
                0, 'conflict', 'shared focus', 1, NOW()
            )
            """,
            {"project_key": conflict_project},
        )

        conflicted = _run_alembic(["downgrade", "036"], succeeds=False)
        assert "conflicted focus outcomes" in conflicted.stderr
        _assert_failed_migration_is_atomic("037")
        _cleanup_project(conflict_project)
    finally:
        for project_key in (
            ambiguous_project,
            valid_project,
            artifact_project,
            conflict_project,
        ):
            _cleanup_project(project_key)
        _run_alembic(["upgrade", "head"])
