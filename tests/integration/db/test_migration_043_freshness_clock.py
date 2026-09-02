"""Migration 043 — dating the freshness STATUS, which `updated_at` cannot do.

Spec `2026-08-08-dream-v2-design.md` §4.3 and §6.2. This is the **purge's hard
precondition**, not a scheduling preference.

The deletion criterion already exists in the repository — `decay_tools.py`,
displayed at SCAN every night — and it is wrong on both its terms:

- `access_count = 0` is the TOTAL counter: an artifact re-read by the dream alone
  leaves the criterion and becomes indefinitely non-purgeable;
- `updated_at < cutoff` RESTARTS at every write of the counter flusher, because
  `trg_<table>_updated` is present. There is therefore today **no honest clock** to
  measure a stay in the archive.

Hence the column. With no backfill: `NULL` means "never measured", never "archived
since forever" — the distinction decides who would be deleted.

041's MECHANISM, NOT 040's, and the spec explains why: `focus_updated_at` is
written in application code because the focus has ONLY ONE writer;
`freshness_status` has four, including the REORG judgement which goes through the
generic `brain_update` tool, which knows nothing of the decay. Stamping in the
application would require doing it in `brain_update` itself, for a column 99 % of
its calls do not touch. So it is a conditional trigger.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration

_DECAY_TABLES = (
    "learnings",
    "decisions",
    "snippets",
    "runbooks",
    "adrs",
    "indexed_plans",
)
_SOURCES = ("merge", "judgment", "score", "revive")


class TestColumnShape:
    @pytest.mark.parametrize("table", _DECAY_TABLES)
    async def test_the_clock_column_exists_and_is_nullable(self, table: str, db_session) -> None:
        row = (
            await db_session.execute(
                sa.text(
                    "SELECT data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = 'freshness_status_updated_at'"
                ),
                {"t": table},
            )
        ).one_or_none()

        assert row is not None, f"{table} n'a pas freshness_status_updated_at"
        data_type, is_nullable, default = row
        assert data_type == "timestamp with time zone"
        # NULL = "never measured". A backfill to now() would suggest the whole
        # corpus has just changed status, and the purge would count 180 days from an
        # invented date.
        assert is_nullable == "YES"
        assert default is None

    @pytest.mark.parametrize("table", _DECAY_TABLES)
    async def test_the_source_column_exists_and_is_constrained(
        self, table: str, db_session
    ) -> None:
        row = (
            await db_session.execute(
                sa.text(
                    "SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = 'freshness_source'"
                ),
                {"t": table},
            )
        ).one_or_none()

        assert row is not None, f"{table} n'a pas freshness_source"
        assert row[0] == "character varying"
        assert row[1] == "YES"

    @pytest.mark.parametrize("table", _DECAY_TABLES)
    async def test_no_backfill_happened(self, table: str, db_session) -> None:
        """No existing row must carry an invented date.

        The migration does not write. A row dated with no status transition since the
        cutover would signal a backfill, hence a lying clock.
        """
        stamped = (
            await db_session.execute(
                sa.text(
                    f"SELECT count(*) FROM {table} WHERE freshness_status_updated_at IS NOT NULL"
                )  # noqa: S608
            )
        ).scalar_one()
        total = (await db_session.execute(sa.text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608

        assert stamped < total or total == 0, (
            f"{table} : toutes les lignes sont datées — un backfill a eu lieu"
        )


class TestTriggerBehaviour:
    async def test_a_status_change_stamps_the_clock(self, db_session) -> None:
        row_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO learnings (topic, insight, project_key, freshness_status) "
                    "VALUES ('043 clock probe', 'probe', 'brain-v42', 'fresh') RETURNING id"
                )
            )
        ).scalar_one()

        await db_session.execute(
            sa.text("UPDATE learnings SET freshness_status = 'stale' WHERE id = :i"),
            {"i": row_id},
        )
        stamped = (
            await db_session.execute(
                sa.text("SELECT freshness_status_updated_at FROM learnings WHERE id = :i"),
                {"i": row_id},
            )
        ).scalar_one()

        assert stamped is not None, "le trigger n'a pas daté la transition de statut"

    async def test_writing_the_same_status_does_not_rejuvenate_the_clock(self, db_session) -> None:
        """Rewriting `archived` on an already-archived entity does not rejuvenate it.

        This is the whole reason for the `WHEN … IS DISTINCT FROM`. Without it, an
        idempotent job that re-sets the same status every night would reset the stay
        counter to zero every day — and nothing would ever be purgeable, silently.

        THE ASSERTION DOES NOT COMPARE TWO TIMESTAMPS, and that is deliberate:
        `CURRENT_TIMESTAMP` is the TRANSACTION-START time in PostgreSQL, so two
        writes in the same transaction produce the same value and a
        `first == second` would pass on nothing, trigger armed or not. We set a
        sentinel dated a year back and check that it SURVIVES.
        """
        row_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO learnings (topic, insight, project_key, freshness_status) "
                    "VALUES ('043 idempotence probe', 'probe', 'brain-v42', 'archived') "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        sentinel = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
        await db_session.execute(
            sa.text("UPDATE learnings SET freshness_status_updated_at = :s WHERE id = :i"),
            {"i": row_id, "s": sentinel},
        )

        await db_session.execute(
            sa.text("UPDATE learnings SET freshness_status = 'archived' WHERE id = :i"),
            {"i": row_id},
        )
        stamped = (
            await db_session.execute(
                sa.text("SELECT freshness_status_updated_at FROM learnings WHERE id = :i"),
                {"i": row_id},
            )
        ).scalar_one()

        assert stamped.year == 2025, (
            "réécrire le même statut a rajeuni l'horloge : le prédicat "
            "`WHEN OLD.freshness_status IS DISTINCT FROM NEW.freshness_status` "
            f"ne filtre pas (valeur observée : {stamped})"
        )

    async def test_a_counter_write_does_not_touch_the_clock(self, db_session) -> None:
        """THE defect the column repairs, proved without comparing dates.

        `updated_at` moves at every write of the counter flusher, so the 180-day
        clock restarted on a mere access. The exact proof that the new column does
        not do that: it is NULL at insertion, and a counter write must not take it
        out of NULL. No timestamp is compared, so nothing can pass on nothing.
        """
        row_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO learnings (topic, insight, project_key, freshness_status) "
                    "VALUES ('043 counter probe', 'probe', 'brain-v42', 'fresh') RETURNING id"
                )
            )
        ).scalar_one()

        await db_session.execute(
            sa.text("UPDATE learnings SET access_count = access_count + 1 WHERE id = :i"),
            {"i": row_id},
        )
        clock, updated = (
            await db_session.execute(
                sa.text(
                    "SELECT freshness_status_updated_at, updated_at FROM learnings WHERE id = :i"
                ),
                {"i": row_id},
            )
        ).one()

        assert clock is None, (
            "une écriture de compteur a daté l'horloge de statut — le trigger "
            "n'est pas restreint à `UPDATE OF freshness_status`"
        )
        # Harness guard: the row was indeed touched, otherwise the assertion above
        # would be green on a write that never happened.
        assert updated is not None

    async def test_a_stale_source_never_survives_a_transition(self, db_session) -> None:
        """A writer that does not declare its source must not inherit the old one.

        Without this reset to NULL, `freshness_source` would lie: it would describe
        the PREVIOUS transition, with the new one's date. A false provenance is worse
        than an absent one — the second is visible, the first is believed.
        """
        row_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO learnings (topic, insight, project_key, freshness_status) "
                    "VALUES ('043 source probe', 'probe', 'brain-v42', 'fresh') RETURNING id"
                )
            )
        ).scalar_one()
        await db_session.execute(
            sa.text(
                "UPDATE learnings SET freshness_status = 'archived', "
                "freshness_source = 'merge' WHERE id = :i"
            ),
            {"i": row_id},
        )
        await db_session.execute(
            sa.text("UPDATE learnings SET freshness_status = 'fresh' WHERE id = :i"),
            {"i": row_id},
        )
        source = (
            await db_session.execute(
                sa.text("SELECT freshness_source FROM learnings WHERE id = :i"), {"i": row_id}
            )
        ).scalar_one()

        assert source is None, (
            f"la source 'merge' a survécu à une transition non déclarée: {source}"
        )

    @pytest.mark.parametrize("source", _SOURCES)
    async def test_every_declared_source_is_accepted(self, source: str, db_session) -> None:
        row_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO learnings (topic, insight, project_key, freshness_status) "
                    "VALUES (:t, 'probe', 'brain-v42', 'fresh') RETURNING id"
                ),
                {"t": f"043 source {source}"},
            )
        ).scalar_one()

        await db_session.execute(
            sa.text(
                "UPDATE learnings SET freshness_status = 'stale', freshness_source = :s "
                "WHERE id = :i"
            ),
            {"i": row_id, "s": source},
        )

        stored = (
            await db_session.execute(
                sa.text("SELECT freshness_source FROM learnings WHERE id = :i"), {"i": row_id}
            )
        ).scalar_one()
        assert stored == source

    async def test_an_unknown_source_is_refused(self, db_session) -> None:
        """The constraint is a vocabulary, not a suggestion."""
        row_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO learnings (topic, insight, project_key, freshness_status) "
                    "VALUES ('043 bad source', 'probe', 'brain-v42', 'fresh') RETURNING id"
                )
            )
        ).scalar_one()

        with pytest.raises(Exception, match="freshness_source|check"):
            await db_session.execute(
                sa.text(
                    "UPDATE learnings SET freshness_status = 'stale', "
                    "freshness_source = 'invented' WHERE id = :i"
                ),
                {"i": row_id},
            )
