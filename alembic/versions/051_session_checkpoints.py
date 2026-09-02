"""The semantic checkpoint: an append-only ledger of judgment, guarded in the database.

`brain_session_checkpoints` (M-C, `SPEC-checkpoint.md` §3, ticket `d04dc588`). A
checkpoint records what a session KNOWS — where the work stands, what blocks it,
what comes next — published together in one call so a reader can tell a complete
snapshot from a partial one.

WHY A TABLE AND NOT A SNAPSHOT ON `brain_sessions`. The ticket's MVP proposed
columns on the session plus a CAS on `expected_checkpoint_revision`. Both were
abandoned, and in opposite directions. A checkpoint note is JUDGMENT: overwriting
it with a snapshot destroys the very history the checkpoint exists to produce. And
the CAS is replaced by `UNIQUE(session_id, seq)` + `ON CONFLICT DO NOTHING`, which
buys back the two P0 properties by the KEY instead — an exact replay is idempotent
by construction, which matters because agent retries are the norm (invariant C6)
and a CAS turns each retry into a conflict to be handled.

APPEND-ONLY IS ENFORCED HERE, NOT UPSTREAM. A `BEFORE UPDATE OR DELETE` trigger
raises. House culture, and the same reason 039 pins a function by SHA256 rather
than by trust: "no code path does it" is a property of today's code, while a
trigger is a property of the data. The guard is total — every UPDATE, every
DELETE, no exception to write — and that totality is bought by the FK's `ON DELETE
RESTRICT`. With `CASCADE`, deleting a session would cascade into this table and
the trigger would have to tell a cascaded DELETE from a direct one, i.e. carry an
exception inside the guard that protects the ledger. `SET NULL` is worse still and
is a documented trap in this repository next to a CHECK.

THE COST, WRITTEN DOWN BECAUSE AN OPERATOR WOULD OTHERWISE MEET IT AT 3AM: a
session that carries checkpoints becomes INDELIBLE. Deleting it requires deleting
them first, and the trigger forbids exactly that. This is what "append-only" means
once it is real rather than declared; it is consistent, and it is not obvious.

NO INDEX BEYOND THE KEY. `uq_brain_session_checkpoints_session_seq` serves the
idempotence key AND the per-session read ordered by `seq`. In particular NO index
is added to `brain_sessions`, whose index list is CLOSED by
`expected_session_indexes` in the two v4 recovery assets — adding one there would
break an attestation this revision has no business touching.

ATTESTATION, RE-VERIFIED RATHER THAN ASSUMED. `SPEC-checkpoint.md` §3.2 asked for
exactly that, and it was worth doing: the spec is right that the terminal CHECK
fingerprint and `expected_session_indexes` do not move — no index on
`brain_sessions`, no CHECK of it touched. It is INCOMPLETE about a third surface:
`table_set` is DERIVED from live `METADATA` in both recovery contracts, so any new
table moves it. Both now exclude this table, as they already exclude every other
post-contract one; a contract describing revision 031 must not claim a table
arriving twenty revisions later.

Nullable/backfill do not apply: the table is new and starts empty. The downgrade is
fail-closed and NAMES the sessions whose judgment it would destroy.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import UUID

revision = "051"
down_revision = "050"
branch_labels = None
depends_on = None

#: NAMED, never generic — the 039 then 048 then 050 template. A generic flag gets
#: copied from one migration to the next until nobody re-reads what it authorises.
_DOWNGRADE_OPT_IN = "allow_checkpoint_downgrade"

_APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION public.brain_session_checkpoints_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION
        'brain_session_checkpoints is append-only: % refused on session % seq %',
        TG_OP, OLD.session_id, OLD.seq;
END;
$function$
"""


def upgrade() -> None:
    op.create_table(
        "brain_session_checkpoints",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("brain_sessions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("progress", sa.Text, nullable=False),
        sa.Column("next_step", sa.Text, nullable=False),
        sa.Column("blocker", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("seq >= 1", name="brain_session_checkpoints_seq_positive"),
        sa.CheckConstraint(
            "btrim(progress) <> ''", name="brain_session_checkpoints_progress_nonempty"
        ),
        sa.CheckConstraint(
            "btrim(next_step) <> ''", name="brain_session_checkpoints_next_step_nonempty"
        ),
        sa.CheckConstraint(
            "blocker IS NULL OR btrim(blocker) <> ''",
            name="brain_session_checkpoints_blocker_nonempty",
        ),
        sa.UniqueConstraint("session_id", "seq", name="uq_brain_session_checkpoints_session_seq"),
    )
    op.execute(_APPEND_ONLY_FUNCTION)
    # BEFORE, so the row is refused rather than written and then complained about,
    # and FOR EACH ROW so the message can name the session and the seq it refused
    # — an operator reading it must know WHICH judgment was protected.
    op.execute(
        """
        CREATE TRIGGER brain_session_checkpoints_append_only
            BEFORE UPDATE OR DELETE ON public.brain_session_checkpoints
            FOR EACH ROW
            EXECUTE FUNCTION public.brain_session_checkpoints_append_only()
        """
    )


def downgrade() -> None:
    # Fail-closed with a named opt-in — 039's mechanism, carried by 048 and 050.
    # Dropping this table destroys judgment that exists nowhere else: unlike a
    # counter or a status, a checkpoint's prose has no second copy to recompute it
    # from. The whole point of the table is that a snapshot could not overwrite it.
    #
    # Count AND NAME, 047-048-050's template: "N rows" without saying whose leaves
    # the operator with nothing to act on. Sessions, not rows — the operator thinks
    # in sessions, and a session's checkpoints are one story.
    arguments = context.get_x_argument(as_dictionary=True)
    opted_in = arguments.get(_DOWNGRADE_OPT_IN) == "yes"

    op.execute(
        f"""
        DO $$
        DECLARE
            recorded bigint;
            sessions text;
        BEGIN
            SELECT count(*), coalesce(string_agg(DISTINCT session_id::text, ', '), '')
              INTO recorded, sessions
            FROM public.brain_session_checkpoints;

            IF {"FALSE" if opted_in else "TRUE"} AND recorded > 0 THEN
                RAISE EXCEPTION
                    'cannot downgrade 051: % checkpoint(s) hold session judgment that '
                    'exists in no other column — dropping this table is the one loss '
                    'the append-only guard was built to prevent. Sessions: %. Export '
                    'them elsewhere, or rerun with -x {_DOWNGRADE_OPT_IN}=yes',
                    recorded, sessions;
            END IF;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS brain_session_checkpoints_append_only
            ON public.brain_session_checkpoints
        """
    )
    op.execute("DROP FUNCTION IF EXISTS public.brain_session_checkpoints_append_only()")
    op.drop_table("brain_session_checkpoints")
