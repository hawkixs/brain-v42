"""Refuse merge chains at commit while allowing atomic child repointing.

Revision ID: 068
Revises: 067
"""

from alembic import op

revision = "068"
down_revision = "067"
branch_labels = None
depends_on = None

_MERGE_TABLES = ("learnings", "decisions", "snippets", "runbooks", "adrs", "features")


def upgrade() -> None:
    # The guard checks only the rows a transaction touches, so legacy chains
    # would survive it silently and make every later merge touching them fail
    # at commit. The repair (scripts/repair_merge_chains.py) must run first:
    # refuse the upgrade, with the per-table counts, while any chain remains.
    counts = " UNION ALL ".join(
        f"SELECT '{table_name}' AS table_name, count(*) AS chained "
        f"FROM public.{table_name} s JOIN public.{table_name} t ON t.id = s.merged_into "
        "WHERE t.merged_into IS NOT NULL"
        for table_name in _MERGE_TABLES
    )
    op.execute(
        f"""
        DO $$
        DECLARE
            residue text;
        BEGIN
            SELECT string_agg(table_name || '=' || chained, ', ' ORDER BY table_name)
              INTO residue
              FROM ({counts}) AS chains
             WHERE chained > 0;
            IF residue IS NOT NULL THEN
                RAISE EXCEPTION 'merge_chains_present: %; run scripts/repair_merge_chains.py '
                    'before migration 068', residue;
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.refuse_merge_chain()
        RETURNS trigger AS $$
        DECLARE
            current_target UUID;
            target_merge UUID;
            child_id UUID;
        BEGIN
            -- Deferred events may describe an earlier version or a deleted row.
            EXECUTE format(
                'SELECT merged_into FROM %I.%I WHERE id = $1',
                TG_TABLE_SCHEMA, TG_TABLE_NAME
            ) INTO current_target USING NEW.id;
            IF current_target IS NULL THEN
                RETURN NEW;
            END IF;

            -- FOR KEY SHARE from the FK does not conflict with merged_into
            -- updates. FOR SHARE waits for an earlier target update and keeps
            -- later updates waiting until this child commits. Their deferred
            -- child check then sees this pointer in a fresh snapshot.
            EXECUTE format(
                'SELECT merged_into FROM %I.%I WHERE id = $1 FOR SHARE',
                TG_TABLE_SCHEMA, TG_TABLE_NAME
            ) INTO target_merge USING current_target;
            IF target_merge IS NOT NULL THEN
                RAISE EXCEPTION 'merge_chain_refused: % % %',
                    TG_TABLE_NAME, NEW.id, current_target
                    USING ERRCODE = '23514';
            END IF;

            -- The serialisation of the two orders comes from the row lock on
            -- the target above. FOR SHARE here only waits for a child that is
            -- being re-pointed elsewhere, so a concurrent repoint is read after
            -- it commits instead of refusing on a pointer that is going away.
            EXECUTE format(
                'SELECT id FROM %I.%I WHERE merged_into = $1 ORDER BY id LIMIT 1 FOR SHARE',
                TG_TABLE_SCHEMA, TG_TABLE_NAME
            ) INTO child_id USING NEW.id;
            IF child_id IS NOT NULL THEN
                RAISE EXCEPTION 'merge_chain_refused: % % %',
                    TG_TABLE_NAME, child_id, NEW.id
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    # Trigger invocation requires no runtime EXECUTE grant (migration 064).
    op.execute("REVOKE ALL ON FUNCTION public.refuse_merge_chain() FROM PUBLIC")
    for table_name in _MERGE_TABLES:
        op.execute(
            f"CREATE INDEX idx_{table_name}_merged_into ON public.{table_name} "
            "(merged_into) WHERE merged_into IS NOT NULL"
        )
        # Only a row whose pointer becomes non-NULL can start or extend a chain.
        # Without the WHEN clause every plain insert queued a deferred check that
        # runs with the committing role's privileges, so a role without SELECT on
        # the table (codex_ro after SET LOCAL ROLE) failed its commit.
        op.execute(
            f"""
            CREATE CONSTRAINT TRIGGER {table_name}_merge_chain_guard
            AFTER INSERT OR UPDATE OF merged_into ON public.{table_name}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW WHEN (NEW.merged_into IS NOT NULL)
            EXECUTE FUNCTION public.refuse_merge_chain()
            """
        )


def downgrade() -> None:
    for table_name in reversed(_MERGE_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table_name}_merge_chain_guard ON public.{table_name}")
        op.execute(f"DROP INDEX IF EXISTS public.idx_{table_name}_merged_into")
    op.execute("DROP FUNCTION IF EXISTS public.refuse_merge_chain()")
