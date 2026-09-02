"""Migration 042 — dream_runs can finally say which project a night ran for.

The defect this column repairs is measured: `dream_runs` covers eight phases over
four months and no row says which project it belongs to, so all that telemetry is
indistinguishable. The schema itself could not express the dimension we believed
covered. (No row or night count is pinned here: the table gains six to nine every
morning at 06:00, and a test freezing such a figure reddens by itself the next
day.)

What these tests fix, and what the `add_column` alone would not say:
  1. The column is NULLABLE, and that is a consequence — none of the SIX writers
     surfaces its failure (spec §15.3). NOT NULL would turn a schema error into a
     warning printed on all the ones that run.
  2. No backfill: the earlier rows stay NULL, permanently.
  3. The global phases' `'*'` sentinel is storable.
  4. The composite index exists, in the right order — it is what makes per-project
     telemetry queryable without scanning the table.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration

_INDEX = "idx_dream_runs_date_project"


class TestColumnShape:
    async def test_column_exists_and_is_nullable(self, db_session) -> None:
        row = (
            await db_session.execute(
                sa.text(
                    "SELECT data_type, is_nullable, character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'dream_runs' AND column_name = 'project_key'"
                )
            )
        ).one_or_none()
        assert row is not None, "la colonne project_key est absente de dream_runs"
        data_type, is_nullable, max_length = row
        assert data_type == "character varying"
        assert max_length == 64
        # Nullable is not caution: see spec §14.3. A NOT NULL would have the error
        # swallowed by ticket_extract, roadmap_curate and session_sweep.
        assert is_nullable == "YES"

    async def test_has_no_default(self, db_session) -> None:
        """A 'brain-v42' default would label every night with the wrong project."""
        default = (
            await db_session.execute(
                sa.text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'dream_runs' AND column_name = 'project_key'"
                )
            )
        ).scalar_one()
        assert default is None


class TestNoBackfill:
    async def test_a_row_inserted_without_the_key_stays_null(self, db_session) -> None:
        """NULL means "written before 042", and nothing else."""
        run_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO dream_runs (run_date, phase, status) "
                    "VALUES (:d, 'scan', 'done') RETURNING id"
                ),
                {"d": dt.date(2026, 1, 1)},
            )
        ).scalar_one()
        value = (
            await db_session.execute(
                sa.text("SELECT project_key FROM dream_runs WHERE id = :id"), {"id": run_id}
            )
        ).scalar_one()
        assert value is None


class TestSentinelAndRealKeys:
    async def test_global_phase_sentinel_is_storable(self, db_session) -> None:
        """`'*'` is written by the three global phases, and by them alone."""
        run_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO dream_runs (run_date, phase, status, project_key) "
                    "VALUES (:d, 'extract', 'done', '*') RETURNING id"
                ),
                {"d": dt.date(2026, 1, 2)},
            )
        ).scalar_one()
        value = (
            await db_session.execute(
                sa.text("SELECT project_key FROM dream_runs WHERE id = :id"), {"id": run_id}
            )
        ).scalar_one()
        assert value == "*"

    async def test_a_long_project_key_fits(self, db_session) -> None:
        """64 characters, not 50: composite keys like `red-lab:orchestrator` already
        exist and nothing guarantees they will not get longer."""
        key = "a" * 64
        run_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO dream_runs (run_date, phase, status, project_key) "
                    "VALUES (:d, 'scan', 'done', :k) RETURNING id"
                ),
                {"d": dt.date(2026, 1, 3), "k": key},
            )
        ).scalar_one()
        value = (
            await db_session.execute(
                sa.text("SELECT project_key FROM dream_runs WHERE id = :id"), {"id": run_id}
            )
        ).scalar_one()
        assert value == key


class TestIndex:
    async def test_composite_index_exists(self, db_session) -> None:
        definition = (
            await db_session.execute(
                sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
                {"n": _INDEX},
            )
        ).scalar_one_or_none()
        assert definition is not None, f"index {_INDEX} absent"
        assert "run_date DESC" in definition
        assert "project_key" in definition

    async def test_the_pre_042_date_index_is_left_alone(self, db_session) -> None:
        """The new index is added, it does not replace.

        `idx_dream_runs_date(run_date DESC)` serves the reads that ignore the project
        — the readers surveyed in §15.6 are all in that case today. Removing it would
        break their plan for zero gain.
        """
        found = (
            await db_session.execute(
                sa.text("SELECT indexname FROM pg_indexes WHERE indexname = 'idx_dream_runs_date'")
            )
        ).scalar_one_or_none()
        assert found == "idx_dream_runs_date"
