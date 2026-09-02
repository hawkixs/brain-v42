"""Session identity, nature, and the terminal state that closes without a ritual.

Revision ID: 046
Revises: 045

M-A + M-G in ONE head (ADR §0ter.1, signed 2026-08-20). Two objects that the
corridor rule would otherwise force into two production rendezvous — with a
window in between where ``agent`` sessions exist and have no reachable terminal
state. The procedural argument, applied here, produces the risk it exists to
reduce.

FIVE COLUMNS, NONE BACKFILLED. ``NULL`` means "before this migration" — the
doctrine of 040 and 041, kept deliberately: a backfill would invent facts about
sessions nobody observed. ``nature IS NULL`` therefore stays under the existing
7-day sweep, not under the new inactivity rule (ratified resolution, ADR §0ter.4).

THE PARTIAL UNIQUE INDEX IS NOT AN OPTIMISATION. ``uq_brain_sessions_connection``
carries ``WHERE status = 'open'``. A full unique index — on the model of
``uq_brain_sessions_project_client`` — would burn a connection FOR ITS WHOLE LIFE
on the first auto-close, and destroy the property that being cut costs a split,
not a loss. ``closed_inactive`` exists precisely to take rows out of
``status = 'open'`` in bulk, every night.

BOTH CHECKS MOVE, and the order matters. ``brain_sessions_status_valid`` (032)
rejects an unknown status BEFORE ``brain_sessions_terminal_state_valid`` (037)
ever sees it. Widening only the terminal check would produce a fourth state that
no row can ever hold. Measured 2026-08-20: ``status`` is ``varchar(20)`` and
``closed_inactive`` is 15 characters — it fits, so there is no ``ALTER TYPE`` and
no view/GRANT dance (zero views depend on ``brain_sessions``, verified through
``view_column_usage`` and ``pg_depend``/``pg_rewrite``).
"""

from alembic import context, op

revision = "046"
down_revision = "045"
branch_labels = None
depends_on = None

#: 039's template: a downgrade that destroys human JUDGEMENT demands a named
#: opt-in, not a generic flag. `intent` is declared by a human and cannot be
#: re-derived; losing it silently is the failure this repository refuses.
_DOWNGRADE_OPT_IN = "allow_session_intent_downgrade"


# The fourth terminal branch. ``captured_knowledge_ids`` carries NO constraint —
# that is the entire point of the migration. ``abandoned`` forces it to zero, so
# an agent session that did its work and attributed artifacts would declare, in
# its terminal snapshot, that it captured nothing (ADR §0.4, route (1) rejected).
#
# ``nothing_to_capture_reason`` is FORBIDDEN here, and that is not symmetry for
# its own sake: on the ``ended`` branch it is the fail-closed counterpart of an
# empty ledger — the operator must SAY why nothing was captured. That is a
# judgement. A server filling it in for an agent session would manufacture
# judgement, which is objection C9, the one that killed route (2).
#
# ``nature = 'agent'`` is IN the check (signature S4, 2026-08-21): the guarantee
# that a claimed session is never closed by inactivity stops being an application
# promise and becomes a database constraint.
_TERMINAL_STATE_V5_BRANCH = """
    OR (
        status = 'closed_inactive'
        AND nature = 'agent'
        AND ended_at IS NOT NULL
        AND summary IS NULL
        AND next_focus IS NULL
        AND nothing_to_capture_reason IS NULL
        AND abandonment_reason IS NULL
        AND end_expected_focus_revision IS NULL
        AND focus_outcome IS NULL
        AND focus_at_end IS NULL
        AND focus_revision_at_end IS NULL
    )
"""

_TERMINAL_STATE_V5 = f"""
ALTER TABLE brain_sessions
ADD CONSTRAINT brain_sessions_terminal_state_valid CHECK (
    (
        status = 'open'
        AND ended_at IS NULL
        AND summary IS NULL
        AND next_focus IS NULL
        AND cardinality(captured_knowledge_ids) = 0
        AND nothing_to_capture_reason IS NULL
        AND abandonment_reason IS NULL
        AND end_expected_focus_revision IS NULL
        AND focus_outcome IS NULL
        AND focus_at_end IS NULL
        AND focus_revision_at_end IS NULL
    )
    OR (
        status = 'ended'
        AND ended_at IS NOT NULL
        AND summary IS NOT NULL
        AND btrim(summary) <> ''
        AND next_focus IS NOT NULL
        AND btrim(next_focus) <> ''
        AND abandonment_reason IS NULL
        AND focus_outcome IS NOT NULL
        AND (end_expected_focus_revision IS NULL OR end_expected_focus_revision >= 0)
        AND (focus_revision_at_end IS NULL OR focus_revision_at_end >= 0)
        AND (
            (
                end_expected_focus_revision IS NULL
                AND focus_outcome = 'applied'
                AND focus_at_end = next_focus
                AND focus_revision_at_end IS NULL
            )
            OR (
                end_expected_focus_revision IS NOT NULL
                AND focus_revision_at_end IS NOT NULL
                AND (
                    (
                        focus_outcome = 'applied'
                        AND focus_at_end = next_focus
                        AND focus_revision_at_end = end_expected_focus_revision + 1
                    )
                    OR (
                        focus_outcome = 'conflict'
                        AND focus_revision_at_end <> end_expected_focus_revision
                    )
                )
            )
        )
        AND (
            (
                cardinality(captured_knowledge_ids) > 0
                AND nothing_to_capture_reason IS NULL
            )
            OR (
                cardinality(captured_knowledge_ids) = 0
                AND nothing_to_capture_reason IS NOT NULL
                AND btrim(nothing_to_capture_reason) <> ''
            )
        )
    )
    OR (
        status = 'abandoned'
        AND ended_at IS NOT NULL
        AND summary IS NULL
        AND next_focus IS NULL
        AND cardinality(captured_knowledge_ids) = 0
        AND nothing_to_capture_reason IS NULL
        AND abandonment_reason IS NOT NULL
        AND btrim(abandonment_reason) <> ''
        AND end_expected_focus_revision IS NULL
        AND focus_outcome IS NULL
        AND focus_at_end IS NULL
        AND focus_revision_at_end IS NULL
    ){_TERMINAL_STATE_V5_BRANCH})
"""

# The 037 shape, restored verbatim on downgrade.
_TERMINAL_STATE_V4 = _TERMINAL_STATE_V5.replace(_TERMINAL_STATE_V5_BRANCH, "")


def upgrade() -> None:
    # Five columns, all nullable, none backfilled. `started_by_actor` is sized on
    # `provenance.MAX_ACTOR_LENGTH = 64`, the same width as `access_log.actor`:
    # the actor is truncated there, so a wider column here would store a value
    # the rest of the system cannot produce.
    op.execute(
        """
        ALTER TABLE brain_sessions
            ADD COLUMN started_by_actor VARCHAR(64),
            ADD COLUMN last_observed_at TIMESTAMPTZ,
            ADD COLUMN intent VARCHAR(500),
            ADD COLUMN nature VARCHAR(16),
            ADD COLUMN connection_id VARCHAR(64)
        """
    )

    # `nature` has NO database default (signature S3). A default would silently
    # give every pre-046 row a nature it never had, which is exactly the
    # retroactive rule change that resolution (d) refuses.
    op.execute(
        """
        ALTER TABLE brain_sessions
        ADD CONSTRAINT brain_sessions_nature_valid
        CHECK (nature IS NULL OR nature IN ('agent', 'operator'))
        """
    )

    # PARTIAL. See the module docstring — this is the one line of this migration
    # that must never be "simplified" into a full unique index.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_brain_sessions_connection
        ON brain_sessions (project_key, connection_id)
        WHERE status = 'open'
        """
    )

    # 032 first: it rejects an unknown status before the terminal check sees it.
    op.execute("ALTER TABLE brain_sessions DROP CONSTRAINT brain_sessions_status_valid")
    op.execute(
        """
        ALTER TABLE brain_sessions
        ADD CONSTRAINT brain_sessions_status_valid
        CHECK (status IN ('open', 'ended', 'abandoned', 'closed_inactive'))
        """
    )

    op.execute("ALTER TABLE brain_sessions DROP CONSTRAINT brain_sessions_terminal_state_valid")
    op.execute(_TERMINAL_STATE_V5)


def downgrade() -> None:
    # `intent` is human judgement and is not derivable again. Losing it needs a
    # NAMED opt-in, never a generic flag — template 039. Checked in Python rather
    # than in SQL so the refusal names the opt-in that lifts it.
    arguments = context.get_x_argument(as_dictionary=True)
    intent_opt_in = arguments.get(_DOWNGRADE_OPT_IN) == "yes"

    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM brain_sessions WHERE status = 'closed_inactive') THEN
                RAISE EXCEPTION
                    'cannot downgrade 046 with closed_inactive sessions: they have no '
                    'terminal state in the v4 machine and would be lost. There is no '
                    'opt-in for this one — resolve or delete those rows first';
            END IF;

            IF {"FALSE" if intent_opt_in else "TRUE"} AND EXISTS (
                SELECT 1 FROM brain_sessions
                WHERE intent IS NOT NULL AND btrim(intent) <> ''
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 046 with declared session intent: it is human '
                    'judgement and is not derivable again. Purge canaries first, or '
                    'rerun with -x {_DOWNGRADE_OPT_IN}=yes';
            END IF;
        END;
        $$
        """
    )

    op.execute("ALTER TABLE brain_sessions DROP CONSTRAINT brain_sessions_terminal_state_valid")
    op.execute(_TERMINAL_STATE_V4)
    op.execute("ALTER TABLE brain_sessions DROP CONSTRAINT brain_sessions_status_valid")
    op.execute(
        """
        ALTER TABLE brain_sessions
        ADD CONSTRAINT brain_sessions_status_valid
        CHECK (status IN ('open', 'ended', 'abandoned'))
        """
    )
    op.execute("DROP INDEX IF EXISTS uq_brain_sessions_connection")
    op.execute("ALTER TABLE brain_sessions DROP CONSTRAINT brain_sessions_nature_valid")
    op.execute(
        """
        ALTER TABLE brain_sessions
            DROP COLUMN IF EXISTS connection_id,
            DROP COLUMN IF EXISTS nature,
            DROP COLUMN IF EXISTS intent,
            DROP COLUMN IF EXISTS last_observed_at,
            DROP COLUMN IF EXISTS started_by_actor
        """
    )
