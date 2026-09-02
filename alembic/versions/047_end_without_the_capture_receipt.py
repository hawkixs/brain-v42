"""Drop the closing XOR: `end` stops measuring the client's diligence.

Revision ID: 047
Revises: 046

THIS IS NOT A WEAKENING, and the nuance decides everything. The XOR
"non-empty ledger XOR `nothing_to_capture_reason`" measured a single thing:
"did the client DECLARE what it produced". Derived capture
(`brain_session_derived_capture_enabled`) removes the failure mode it caught —
produced-but-undeclared — and would from now on feed its signal FROM THE
SERVER.

**A check is hollow as soon as the thing checked can influence its signal.**
Keeping it would therefore keep nothing: it would be a receipt the server
issues to itself. Worse, it would make UNCLOSABLE any session whose ledger the
server filled — the user closes saying "nothing durable", the server has
already attributed on their behalf, and the database refuses. A flag that makes
a session unclosable cannot be armed.

WHAT REMAINS is what the server cannot manufacture in the user's place:
non-blank `summary` and `next_focus`, and a reason that says something IF one is
given. Proven rather than asserted by
`tests/unit/repositories/test_end_gate_is_judgement_only.py`: `summary` has only
two sites in all of `src/` — the tool that relays the human text and the
repository that persists it — and the `closed_inactive` branch of CHECK 046
FORBIDS a sweep from writing one.

THE CHECK'S TEXT IS RE-READ FROM 046, never retyped — 045's template. A second
source of truth for this constraint would diverge only in production, and only
the day someone attempted a row the other version refuses. The replacement is
ASSERTED: if 046 moved, this revision would fail at import instead of
re-installing an unchanged constraint.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import op

revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None


#: The removed block, exactly as written in 046. It serves both as the cut
#: marker AND as a witness: if it vanishes from 046, this revision must fail.
_CAPTURE_RECEIPT = """
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
        )"""

#: What replaces it: giving a reason stays an act, and so does giving none.
_JUDGEMENT_ONLY = """
        AND (
            nothing_to_capture_reason IS NULL
            OR btrim(nothing_to_capture_reason) <> ''
        )"""


def _terminal_state_046() -> str:
    """Re-read the terminal constraint FROM 046, never retype it here."""
    source = Path(__file__).with_name("046_session_identity_and_nature.py")
    spec = importlib.util.spec_from_file_location("_migration_046_sessions", source)
    if spec is None or spec.loader is None:  # pragma: no cover — frozen path
        raise RuntimeError(f"046 illisible depuis la 047 : {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module._TERMINAL_STATE_V5)


_TERMINAL_STATE_V6_SOURCE = _terminal_state_046()

if _CAPTURE_RECEIPT not in _TERMINAL_STATE_V6_SOURCE:  # pragma: no cover — import guard
    raise RuntimeError(
        "le bloc XOR de la 046 a changé de forme : la 047 reposerait une "
        "contrainte inchangée en croyant l'avoir relâchée"
    )

#: v6 = v5 without the receipt. `captured_knowledge_ids` no longer carries ANY
#: constraint on the `ended` branch, exactly as on `closed_inactive`.
_TERMINAL_STATE_V6 = _TERMINAL_STATE_V6_SOURCE.replace(_CAPTURE_RECEIPT, _JUDGEMENT_ONLY)

#: What a downgrade would restore — and therefore what it would destroy.
_TERMINAL_STATE_V5 = _TERMINAL_STATE_V6_SOURCE

_DROP = "ALTER TABLE brain_sessions DROP CONSTRAINT brain_sessions_terminal_state_valid"

#: Fail-closed, 037's template. Two shapes become legal with 047 and illegal
#: without it: "ledger AND reason" (the derived case, the one that motivates
#: this revision) and "neither ledger nor reason" (the honest closing of a
#: session that produced nothing). A silent downgrade would not corrupt them —
#: the database would refuse the constraint — but it would fail halfway, with a
#: constraint message, without naming what is at stake. This one names it.
_REFUSE_LOSSY_DOWNGRADE = """
DO $$
DECLARE
    offending bigint;
BEGIN
    SELECT count(*) INTO offending
    FROM brain_sessions
    WHERE status = 'ended'
      AND (
          (cardinality(captured_knowledge_ids) > 0 AND nothing_to_capture_reason IS NOT NULL)
          OR (cardinality(captured_knowledge_ids) = 0 AND nothing_to_capture_reason IS NULL)
      );

    IF offending > 0 THEN
        RAISE EXCEPTION
            'cannot downgrade 047: % ended session(s) hold a capture outcome the '
            'restored XOR forbids (derived ledger with a reason, or neither). '
            'Reconcile them before downgrading — they are user-visible closures.',
            offending;
    END IF;
END;
$$;
"""


def upgrade() -> None:
    op.execute(_DROP)
    op.execute(_TERMINAL_STATE_V6)


def downgrade() -> None:
    op.execute(_REFUSE_LOSSY_DOWNGRADE)
    op.execute(_DROP)
    op.execute(_TERMINAL_STATE_V5)
