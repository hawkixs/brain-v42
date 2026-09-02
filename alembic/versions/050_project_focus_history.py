"""The memory of focus: an append-only audit trail, and the guard that fills it.

Revision ID: 050
Revises: 049

M-D of the projects/sessions overhaul (C5 of the corridor), the head 049
RESERVED for it — "M-D stays ISOLATED and takes the next head". It closes B6 for
recoverability: `brain_set_project_context` rewrites `current_focus` with no CAS,
**including to NULL when the argument is omitted**, and until now nothing kept the
previous prose. The channel is not silent — 032 bumps the revision and 040 dates
the change — it is UNRECOVERABLE. That, and only that, is what this revision
fixes. Production shows no victim yet: the ten NULL focuses out of fifty-nine
contexts are all at `focus_revision = 0` with `focus_updated_at IS NULL`, i.e.
never written rather than erased.

WHAT LANDS IN THE DATABASE

1. ``project_focus_history`` — `project_key` with NO foreign key (the knowledge
   tables' doctrine: dropping a context must not take the audit trail down with
   it), `focus_revision`, and **`focus` NULLABLE**. An erased focus IS the
   destructive overwrite the trail exists to record; and a `NOT NULL` would abort
   this very `upgrade` at seed time on the ten NULL focuses measured in
   production — a defect invisible in CI, whose database is empty when the seed
   runs.

   PK ``(project_key, focus_revision)``: the generalized CAS's monotonicity makes
   it the natural key, and it is the ONLY index this table needs — the reading
   tool pages one project by descending revision, which the PK serves. A second
   index on `created_at` would be a scan nobody performs.

2. An **append-only trigger** — UPDATE and DELETE refused. The audit trail becomes
   a constraint rather than a convention.

3. A **deferred constraint trigger** on `project_contexts`, and it is the real
   deliverable: at COMMIT, an `UPDATE OF current_focus` must have left its history
   row for the new revision. It catches any writer that bypasses the shared
   application path.

   **The `OF current_focus` clause is mandatory.** Without it the trigger would
   fire on EVERY update of `project_contexts` — including the plan-index repair's
   two, which set `plan_scan_paths` and `updated_at` and nothing else
   (`plan_index_repair_store.py:349-356` and `:619-626`) — and would abort a
   repair that has no business writing a focus. That review is the one R1.4
   demands; it is written in full in the pin block of `plan_index_repair_store`,
   not here.

   **It ships DISABLED** (R1.3). Between this `upgrade` and the MCP restart the
   live process still runs pre-050 code, which writes no history row: an armed
   trigger would abort at COMMIT every `brain_session_end` carrying
   `focus_outcome=applied`, fail-closed, session left open, with no practicable
   killswitch. Enabling it is a NAMED operator gesture, after the restart:

       ALTER TABLE project_contexts ENABLE TRIGGER project_contexts_focus_history_required;

   That switch is not free. While it is off, the `ops/recovery` attestation is RED
   by construction: its CTE requires `tgenabled = 'O'`. Reusing the switch in an
   emergency is legitimate; doing so without dating it is not.

4. A **seed**, one row per context including the NULL focuses,
   `source='migration_seed'`. It anchors the trail and keeps the enum from being
   orphaned. It is ONE `INSERT … SELECT`, not the Python planner §5.2 proposed —
   see the note at the statement: the offline-render gate forbids reading rows in
   `upgrade()`, and removing the Python is a stronger answer to §5.2's question
   than unit-testing it would have been.

THE INSERT SCOPE, SETTLED HERE AND NOT DISCOVERED IN PRODUCTION (§5.2)

The constraint trigger cannot see INSERTs, and `pg_project_context.create` and
`get_or_create`'s INSERT branch write a focus at row birth with
`focus_revision = 0`. The plan offered three routes and warned that **not
choosing is choosing (b) without saying so**. Route **(b) is chosen, and said**:
the hard database guard starts at revision 1; revision 0 of a context created
after 050 is carried by the shared application path ALONE, and by the per-writer
canary that covers both INSERT paths.

Why not (a) — `AFTER INSERT OR UPDATE OF current_focus`. PostgreSQL does not
accept that clause: a column list is legal on `UPDATE` only, so route (a) means a
SECOND constraint trigger. That adds a fourteenth entry to a list of thirteen
runtime triggers that `ops/recovery` treats as CLOSED, widening the very
attestation collision this revision already carries — and it would write a
history row for every project ever created, NULL focus included, filling the
trail with non-events. Why not (c) — forbidding `create`/`get_or_create` from
writing a focus at birth changes the behaviour of existing clients, which is a
question, not a migration.

WHAT THIS REVISION DELIBERATELY DOES NOT DO

No revision-guard trigger. An earlier draft wanted one refusing an UPDATE that
moves `current_focus` without `focus_revision = old + 1`. That is VERBATIM what
032 already does, by assigning the value instead of refusing it — and the draft
would have coexisted badly: PostgreSQL fires BEFORE ROW triggers in ALPHABETICAL
order of their name, so `…_focus_history_guard` sorts before
`project_contexts_focus_revision_trigger`, would read a not-yet-incremented
`NEW.focus_revision` and would reject every focus write from the four writers
that update without setting the revision themselves. Named after, it is dead
code. Either way, a loss.

And Q13 is NOT shipped: whether an omitted `current_focus` should stop erasing the
focus in `brain_set_project_context` is an induced behaviour change with a free
veto standing against it, and no production victim to force the hand.
"""

from __future__ import annotations

from alembic import context, op

revision = "050"
down_revision = "049"
branch_labels = None
depends_on = None

#: NAMED, never generic — the 039 then 048 template. A generic flag gets copied
#: from one migration to the next until nobody re-reads what it authorises.
_DOWNGRADE_OPT_IN = "allow_focus_history_downgrade"

#: An exact mirror of the CHECK installed below AND of `tables.py`. Six sources
#: for seven writers: `focus_tool` covers both the roadmap CAS and the
#: repository's `update_focus`, `context_upsert` covers `create` and
#: `get_or_create`.
_SOURCES = (
    "session_end",
    "focus_tool",
    "context_upsert",
    "generic_update",
    "maintenance_scrub",
    "migration_seed",
)

_SEED_SOURCE = "migration_seed"

_APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION public.project_focus_history_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION
        'project_focus_history is append-only: % refused on project % revision %',
        TG_OP, OLD.project_key, OLD.focus_revision;
END;
$function$
"""

#: Reads the history for the revision the row now carries. `RETURN NULL` because
#: an AFTER trigger's return value is ignored: the only effect that matters is
#: the exception, raised at COMMIT because the trigger is deferred.
_REQUIRED_FUNCTION = """
CREATE OR REPLACE FUNCTION public.require_project_focus_history()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM public.project_focus_history
        WHERE project_key = NEW.project_key
          AND focus_revision = NEW.focus_revision
    ) THEN
        RAISE EXCEPTION
            'focus_history_row_missing: project % moved its focus to revision % '
            'without an audit row. The write path is '
            'brain_v42.db.focus_history.record_focus_history.',
            NEW.project_key, NEW.focus_revision;
    END IF;
    RETURN NULL;
END;
$function$
"""


def upgrade() -> None:
    # Replayable by hand, 048's rule: the cutover order is "apply, verify, THEN
    # restart", so someone will replay these statements one by one.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.project_focus_history (
            project_key VARCHAR(50) NOT NULL,
            focus_revision BIGINT NOT NULL,
            focus TEXT,
            actor VARCHAR(64),
            source VARCHAR(20) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT project_focus_history_pkey
                PRIMARY KEY (project_key, focus_revision),
            CONSTRAINT project_focus_history_source_valid
                CHECK (source IN (
        """
        + ", ".join(f"'{source}'" for source in _SOURCES)
        + """
                ))
        )
        """
    )

    op.execute(_APPEND_ONLY_FUNCTION)
    op.execute(
        """
        DROP TRIGGER IF EXISTS project_focus_history_append_only_trigger
            ON public.project_focus_history
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_focus_history_append_only_trigger
        BEFORE UPDATE OR DELETE ON public.project_focus_history
        FOR EACH ROW
        EXECUTE FUNCTION public.project_focus_history_append_only()
        """
    )

    # The seed runs BEFORE the constraint trigger is created, so ordering never
    # depends on the trigger being disabled — one less thing to get right.
    # ONE statement, and no Python between the two tables. The plan proposed a
    # pure planner in Python so the NULL-focus case could be unit-tested; this
    # repository forbids the shape that planner needs. `alembic upgrade --sql`
    # renders the whole chain offline, with no database
    # (`test_test_database_renders_all_migrations_without_secret`), and offline
    # `op.get_bind().execute(...)` returns None — reading rows in `upgrade()`
    # crashes the render. An `INSERT … SELECT` answers the plan's question more
    # strongly than a tested planner would: there is no Python left that could
    # mishandle a NULL, so the case cannot be got wrong rather than being got
    # right under test.
    #
    # `ON CONFLICT DO NOTHING` makes a hand replay idempotent, which the cutover
    # order ("apply, verify, THEN restart") makes likely.
    op.execute(
        f"""
        INSERT INTO public.project_focus_history
            (project_key, focus_revision, focus, actor, source)
        SELECT project_key, focus_revision, current_focus, NULL, '{_SEED_SOURCE}'
        FROM public.project_contexts
        ON CONFLICT (project_key, focus_revision) DO NOTHING
        """
    )

    op.execute(_REQUIRED_FUNCTION)
    op.execute(
        """
        DROP TRIGGER IF EXISTS project_contexts_focus_history_required
            ON public.project_contexts
        """
    )
    # `OF current_focus` is what keeps the plan-index repair's two UPDATEs
    # (plan_scan_paths + updated_at, never a focus) out of reach. DEFERRABLE
    # INITIALLY DEFERRED so the check lands at COMMIT, after the shared write
    # path has inserted its row in the same transaction.
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER project_contexts_focus_history_required
        AFTER UPDATE OF current_focus ON public.project_contexts
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION public.require_project_focus_history()
        """
    )
    # DISABLED at birth (R1.3), and this line is the whole cutover contract.
    op.execute(
        """
        ALTER TABLE public.project_contexts
            DISABLE TRIGGER project_contexts_focus_history_required
        """
    )


def downgrade() -> None:
    # Fail-closed OUTSIDE THE SEED, with a named opt-in — 039's mechanism, not a
    # destructive purge. An earlier draft claimed "an Alembic downgrade has no
    # confirmation parameter" and concluded the audit trail had to be destroyed
    # to allow a rollback. That is false, and 039 in this very tree implements
    # the opposite.
    #
    # Count AND NAME, 047-048's template: "N rows" without saying which ones
    # leaves the operator with no move to make.
    arguments = context.get_x_argument(as_dictionary=True)
    opted_in = arguments.get(_DOWNGRADE_OPT_IN) == "yes"

    op.execute(
        f"""
        DO $$
        DECLARE
            audited bigint;
            projects text;
        BEGIN
            SELECT count(*), coalesce(string_agg(DISTINCT project_key, ', '), '')
              INTO audited, projects
            FROM public.project_focus_history
            WHERE source <> '{_SEED_SOURCE}';

            IF {"FALSE" if opted_in else "TRUE"} AND audited > 0 THEN
                RAISE EXCEPTION
                    'cannot downgrade 050: % focus revision(s) were recorded AFTER the '
                    'seed and dropping this table destroys the only copy of the prose '
                    'they replaced — the recovery this revision exists to make possible. '
                    'Projects: %. Export them elsewhere, or rerun with '
                    '-x {_DOWNGRADE_OPT_IN}=yes',
                    audited, projects;
            END IF;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS project_contexts_focus_history_required
            ON public.project_contexts
        """
    )
    op.execute("DROP FUNCTION IF EXISTS public.require_project_focus_history()")
    op.execute(
        """
        DROP TRIGGER IF EXISTS project_focus_history_append_only_trigger
            ON public.project_focus_history
        """
    )
    op.execute("DROP TABLE IF EXISTS public.project_focus_history")
    op.execute("DROP FUNCTION IF EXISTS public.project_focus_history_append_only()")
