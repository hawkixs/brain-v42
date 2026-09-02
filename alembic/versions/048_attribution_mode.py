"""Say BY WHICH KEY an artifact was attributed, and make the guess undoable.

Revision ID: 048
Revises: 047

Derived absorption gains a second tier: when the tracer of the current
connection carries nothing — because that transport is dead, which happens ~26
times a day, killed by the 900 s idle timeout — the user's session may take
over the ledger of ANOTHER tracer of the project, provided it is the only
non-`agent` session covering the instant of creation.

The first tier is a PROOF: same connection, exact pairing. The second is a
DEDUCTION: nobody else could have produced it. Both write the same `session_id`
column, and without this revision nothing tells them apart afterwards — not for
an audit, not for a human wanting to undo a bad attribution. **A rate cannot be
undone, a list can.**

NULLABLE AND WITHOUT A DEFAULT, the doctrine of 040-041-042-046: `NULL` means
"written before 048". No backfill, and that is a reasoned refusal — setting
`'explicit'` everywhere would lie about the rows `derive_capture` had already
deposited, which were never anyone's explicit capture.

THE INDEX IS PARTIAL on the derived mode alone: undoing a guess must be a
query, not a scan. The other three modes are not searched in bulk.
"""

from __future__ import annotations

from alembic import context, op

revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None

#: The opt-in that lifts the downgrade refusal. NAMED, never generic — the
#: template of 039 then 046: a generic flag gets copied from one migration to
#: the next without anyone re-reading what it authorises.
_DOWNGRADE_OPT_IN = "allow_attribution_mode_downgrade"

#: An exact mirror of the CHECK installed below AND of `tables.py`. The four
#: modes: `explicit` (a human named the UUID), `derived_deposit` (the server
#: deposited into a tracer), `derived_connection` (the exact tier) and
#: `derived_window` (the tier deduced by temporal exclusivity).
_MODES = ("explicit", "derived_deposit", "derived_connection", "derived_window")

#: Postgres has no ``ADD CONSTRAINT IF NOT EXISTS``: so we drop first. The
#: template of 047, which already does DROP then ADD on this constraint family.
_DROP_CHECK = (
    "ALTER TABLE brain_session_artifacts "
    "DROP CONSTRAINT IF EXISTS brain_session_artifacts_attribution_mode_valid"
)

_ADD_CHECK = (
    "ALTER TABLE brain_session_artifacts "
    "ADD CONSTRAINT brain_session_artifacts_attribution_mode_valid "
    "CHECK (attribution_mode IS NULL OR attribution_mode IN ("
    + ", ".join(f"'{mode}'" for mode in _MODES)
    + "))"
)


def upgrade() -> None:
    # GENUINELY REPLAYABLE, and that is a settled choice rather than a cosmetic
    # one. The previous version carried `ADD COLUMN IF NOT EXISTS` — thereby
    # promising to be replayable — next to an `ADD CONSTRAINT` that was not.
    # Alembic rolls back the whole revision on failure, so nothing broke by that
    # path; what broke was the promise, for anyone replaying these statements BY
    # HAND. Given the cutover order — apply 048 and VERIFY it before any restart
    # — someone will replay them by hand.
    op.execute(
        "ALTER TABLE brain_session_artifacts ADD COLUMN IF NOT EXISTS attribution_mode VARCHAR(24)"
    )
    op.execute(_DROP_CHECK)
    op.execute(_ADD_CHECK)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_brain_session_artifacts_derived_window "
        "ON brain_session_artifacts (session_id) "
        "WHERE attribution_mode = 'derived_window'"
    )


def downgrade() -> None:
    # This downgrade destroys NO ledger row: the attributions remain, the
    # artifacts keep their session. It destroys the one thing that tells a PROOF
    # from a DEDUCTION — and that is precisely what must be named, because the
    # loss is invisible in the data left behind.
    #
    # Count AND NAME, 047's template: a message saying "N rows" without saying
    # which ones leaves the operator with no possible move.
    arguments = context.get_x_argument(as_dictionary=True)
    opted_in = arguments.get(_DOWNGRADE_OPT_IN) == "yes"

    op.execute(
        f"""
        DO $$
        DECLARE
            guessed bigint;
            names text;
            targets text;
        BEGIN
            SELECT count(*),
                   coalesce(string_agg(DISTINCT knowledge_id::text, ', '), ''),
                   coalesce(string_agg(DISTINCT session_id::text, ', '), '')
              INTO guessed, names, targets
            FROM brain_session_artifacts
            WHERE attribution_mode = 'derived_window';

            IF {"FALSE" if opted_in else "TRUE"} AND guessed > 0 THEN
                RAISE EXCEPTION
                    'cannot downgrade 048: % attribution(s) were DEDUCED by temporal '
                    'exclusivity, not proven by a connection. Dropping the column makes '
                    'them indistinguishable from a human explicit capture, and undoing a '
                    'wrong one becomes impossible. Artifacts: %. Target sessions: %. '
                    'Record them elsewhere, or rerun with -x {_DOWNGRADE_OPT_IN}=yes',
                    guessed, names, targets;
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_brain_session_artifacts_derived_window")
    op.execute(_DROP_CHECK)
    op.execute("ALTER TABLE brain_session_artifacts DROP COLUMN IF EXISTS attribution_mode")
