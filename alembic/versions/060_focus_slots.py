"""060 — focus slots, their history, session binding and relay; defect 16314b31.

ADR #34 (`ccae2270`), spec 2026-10-03. Each project keeps ONE base focus in
`project_contexts`; the topics in flight move to `focus_slots`, each anchored to
a ticket, a lot or a PR (`focus_slot_anchors`, written once, never changed),
each guarded by its OWN revision. `focus_slot_history` records every slot
revision, and a deferred constraint trigger refuses a COMMIT that moved a slot
revision without its row. It is created ENABLED, unlike 050's: no pre-060 code
writes `focus_slots`, so there is no restart window to protect.

`brain_sessions` gains `slot_id` (the slot a session is bound to) and
`relayed_from_session_id` (the session a relay ended). A trace (`nature =
'agent'`) can carry neither; a relay successor is always bound; one open session
per slot and one relay per session are database facts.

16314b31. 046's `closed_inactive` branch read `nature = 'agent'` inside an OR:
for `nature IS NULL` that is NULL, not false, and a CHECK accepts NULL, so an
operator row could be closed by inactivity. The branch now reads `nature IS NOT
NULL AND nature = 'agent'`. The text is RE-READ from 047, never retyped (045's
template), and the replacement is asserted to hit exactly once. A precount
refuses the upgrade while any row already violates the corrected CHECK.

NULL rule: every new CHECK is `COALESCE(<predicate>, false)`.

No text moves: every `current_focus` stays its project's base. Replayable by
hand (048's rule): `IF NOT EXISTS`, `CREATE OR REPLACE`, `DROP ... IF EXISTS`.
The precount is SQL, not Python: `alembic upgrade --sql` renders the chain
offline, where reading rows in `upgrade()` crashes (050's note).

The downgrade drops every slot, its history and every binding at once. It
refuses while any exists, naming the counts, unless the operator names the
decision: `-x allow_focus_slots_downgrade=yes`. It restores 047's CHECK exactly.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import context, op

revision = "060"
down_revision = "059"
branch_labels = None
depends_on = None

_OPT_IN = "allow_focus_slots_downgrade"
_LOOSE = "AND nature = 'agent'"
_STRICT = "AND nature IS NOT NULL\n        AND nature = 'agent'"


def _terminal_state_047() -> str:
    """Re-read the terminal constraint FROM 047, never retype it here."""
    source = Path(__file__).with_name("047_end_without_the_capture_receipt.py")
    spec = importlib.util.spec_from_file_location("_migration_047_sessions", source)
    if spec is None or spec.loader is None:  # pragma: no cover - frozen path
        raise RuntimeError(f"047 is unreadable from 060: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module._TERMINAL_STATE_V6)


_TERMINAL_STATE_059 = _terminal_state_047()
if _TERMINAL_STATE_059.count(_LOOSE) != 1:  # pragma: no cover - import guard
    raise RuntimeError(
        "047's closed_inactive branch changed shape: 060 would reinstall an unchanged "
        "constraint while believing it fixed 16314b31"
    )
_TERMINAL_STATE_060 = _TERMINAL_STATE_059.replace(_LOOSE, _STRICT)
_DROP_TERMINAL = (
    "ALTER TABLE brain_sessions DROP CONSTRAINT IF EXISTS brain_sessions_terminal_state_valid"
)

_PRECOUNT = """
DO $$
DECLARE
    offending bigint;
BEGIN
    SELECT count(*) INTO offending
    FROM public.brain_sessions
    WHERE status = 'closed_inactive' AND nature IS NULL;
    IF offending > 0 THEN
        RAISE EXCEPTION
            'cannot upgrade to 060: % closed_inactive session(s) have nature IS NULL, '
            'which the corrected CHECK refuses (ticket 16314b31). Resolve them first.',
            offending;
    END IF;
END;
$$
"""

_FOCUS_SLOTS = """
CREATE TABLE IF NOT EXISTS public.focus_slots (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    project_key VARCHAR(50) NOT NULL,
    title VARCHAR(120) NOT NULL,
    body TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 0,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    body_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    close_reason VARCHAR(48),
    close_note TEXT,
    CONSTRAINT focus_slots_pkey PRIMARY KEY (id),
    CONSTRAINT focus_slots_project_key_fkey FOREIGN KEY (project_key)
        REFERENCES public.project_contexts (project_key) ON DELETE RESTRICT,
    CONSTRAINT focus_slots_title_nonblank CHECK (COALESCE(btrim(title) <> '', false)),
    CONSTRAINT focus_slots_body_valid
        CHECK (COALESCE(btrim(body) <> '' AND char_length(body) <= 4000, false)),
    CONSTRAINT focus_slots_revision_valid CHECK (COALESCE(revision >= 0, false)),
    CONSTRAINT focus_slots_close_valid CHECK (COALESCE(
        (closed_at IS NULL AND close_reason IS NULL AND close_note IS NULL)
        OR (closed_at IS NOT NULL AND close_reason = 'explicit'
            AND close_note IS NOT NULL AND btrim(close_note) <> '')
        OR (closed_at IS NOT NULL AND close_reason ~ '^receipt:[0-9a-f-]{36}$'
            AND close_note IS NULL),
        false))
)
"""

_FOCUS_SLOT_ANCHORS = r"""
CREATE TABLE IF NOT EXISTS public.focus_slot_anchors (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    slot_id UUID NOT NULL,
    kind VARCHAR(8) NOT NULL,
    ticket_id UUID,
    target_release TEXT,
    repository_id BIGINT,
    pr_number INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT focus_slot_anchors_pkey PRIMARY KEY (id),
    CONSTRAINT focus_slot_anchors_slot_id_fkey FOREIGN KEY (slot_id)
        REFERENCES public.focus_slots (id) ON DELETE RESTRICT,
    CONSTRAINT focus_slot_anchors_ticket_id_fkey FOREIGN KEY (ticket_id)
        REFERENCES public.tickets (id) ON DELETE RESTRICT,
    CONSTRAINT focus_slot_anchors_shape CHECK (COALESCE(
        (kind = 'ticket' AND ticket_id IS NOT NULL AND target_release IS NULL
            AND repository_id IS NULL AND pr_number IS NULL)
        OR (kind = 'lot' AND target_release IS NOT NULL
            AND target_release ~ '^[0-9]+\.[0-9]+\.[0-9]+$'
            AND ticket_id IS NULL AND repository_id IS NULL AND pr_number IS NULL)
        OR (kind = 'pr' AND repository_id IS NOT NULL AND pr_number IS NOT NULL
            AND pr_number >= 1 AND ticket_id IS NULL AND target_release IS NULL),
        false)),
    CONSTRAINT uq_focus_slot_anchors UNIQUE NULLS NOT DISTINCT
        (slot_id, kind, ticket_id, target_release, repository_id, pr_number)
)
"""

_FOCUS_SLOT_HISTORY = """
CREATE TABLE IF NOT EXISTS public.focus_slot_history (
    slot_id UUID NOT NULL,
    revision BIGINT NOT NULL,
    body TEXT NOT NULL,
    source VARCHAR(16) NOT NULL,
    session_id UUID,
    actor VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT focus_slot_history_pkey PRIMARY KEY (slot_id, revision),
    CONSTRAINT focus_slot_history_slot_id_fkey FOREIGN KEY (slot_id)
        REFERENCES public.focus_slots (id) ON DELETE RESTRICT,
    CONSTRAINT focus_slot_history_session_id_fkey FOREIGN KEY (session_id)
        REFERENCES public.brain_sessions (id) ON DELETE RESTRICT,
    CONSTRAINT focus_slot_history_source_valid CHECK (COALESCE(
        source IN ('slot_open', 'session_end', 'session_relay', 'slot_close'), false)),
    CONSTRAINT focus_slot_history_session_valid CHECK (COALESCE(
        (source IN ('session_end', 'session_relay') AND session_id IS NOT NULL)
        OR (source IN ('slot_open', 'slot_close') AND session_id IS NULL),
        false))
)
"""

_HISTORY_APPEND_ONLY = """
CREATE OR REPLACE FUNCTION public.focus_slot_history_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION
        'focus_slot_history is append-only: % refused on slot % revision %',
        TG_OP, OLD.slot_id, OLD.revision;
END;
$function$
"""

_ANCHORS_APPEND_ONLY = """
CREATE OR REPLACE FUNCTION public.focus_slot_anchors_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION
        'focus_slot_anchors are written once by brain_slot_open: % refused on anchor %',
        TG_OP, OLD.id;
END;
$function$
"""

_HISTORY_REQUIRED = """
CREATE OR REPLACE FUNCTION public.require_focus_slot_history()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM public.focus_slot_history
        WHERE slot_id = NEW.id
          AND revision = NEW.revision
    ) THEN
        RAISE EXCEPTION
            'focus_slot_history_row_missing: slot % moved to revision % without a '
            'history row. The write path is brain_v42.db.focus_slots.record_slot_history.',
            NEW.id, NEW.revision;
    END IF;
    RETURN NULL;
END;
$function$
"""

_SESSION_COLUMNS = """
ALTER TABLE public.brain_sessions
    ADD COLUMN IF NOT EXISTS slot_id UUID,
    ADD COLUMN IF NOT EXISTS relayed_from_session_id UUID
"""

_SESSION_CONSTRAINTS = (
    (
        "brain_sessions_slot_id_fkey",
        "FOREIGN KEY (slot_id) REFERENCES public.focus_slots (id) ON DELETE RESTRICT",
    ),
    (
        "brain_sessions_relayed_from_session_id_fkey",
        "FOREIGN KEY (relayed_from_session_id) REFERENCES public.brain_sessions (id) "
        "ON DELETE RESTRICT",
    ),
    (
        "brain_sessions_slot_operator_only",
        "CHECK (COALESCE((slot_id IS NULL AND relayed_from_session_id IS NULL) "
        "OR nature IS DISTINCT FROM 'agent', false))",
    ),
    (
        "brain_sessions_relay_requires_slot",
        "CHECK (COALESCE(relayed_from_session_id IS NULL OR slot_id IS NOT NULL, false))",
    ),
)


def upgrade() -> None:
    op.execute(_PRECOUNT)
    op.execute(_DROP_TERMINAL)
    op.execute(_TERMINAL_STATE_060)

    op.execute(_FOCUS_SLOTS)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_focus_slots_open_title "
        "ON public.focus_slots (project_key, title) WHERE closed_at IS NULL"
    )
    op.execute(_FOCUS_SLOT_ANCHORS)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_focus_slot_anchors_ticket "
        "ON public.focus_slot_anchors (ticket_id) WHERE kind = 'ticket'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_focus_slot_anchors_lot "
        "ON public.focus_slot_anchors (target_release) WHERE kind = 'lot'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_focus_slot_anchors_pr "
        "ON public.focus_slot_anchors (repository_id, pr_number) WHERE kind = 'pr'"
    )
    op.execute(_FOCUS_SLOT_HISTORY)

    op.execute(_HISTORY_APPEND_ONLY)
    op.execute(
        "DROP TRIGGER IF EXISTS focus_slot_history_append_only_trigger ON public.focus_slot_history"
    )
    op.execute(
        "CREATE TRIGGER focus_slot_history_append_only_trigger "
        "BEFORE UPDATE OR DELETE ON public.focus_slot_history "
        "FOR EACH ROW EXECUTE FUNCTION public.focus_slot_history_append_only()"
    )
    op.execute(_ANCHORS_APPEND_ONLY)
    op.execute(
        "DROP TRIGGER IF EXISTS focus_slot_anchors_append_only_trigger ON public.focus_slot_anchors"
    )
    op.execute(
        "CREATE TRIGGER focus_slot_anchors_append_only_trigger "
        "BEFORE UPDATE OR DELETE ON public.focus_slot_anchors "
        "FOR EACH ROW EXECUTE FUNCTION public.focus_slot_anchors_append_only()"
    )
    op.execute(_HISTORY_REQUIRED)
    op.execute("DROP TRIGGER IF EXISTS focus_slots_history_required ON public.focus_slots")
    # ENABLED at birth: no pre-060 writer exists, so no restart window to protect.
    op.execute(
        "CREATE CONSTRAINT TRIGGER focus_slots_history_required "
        "AFTER INSERT OR UPDATE OF revision ON public.focus_slots "
        "DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION public.require_focus_slot_history()"
    )

    op.execute(_SESSION_COLUMNS)
    for name, definition in _SESSION_CONSTRAINTS:
        op.execute(f"ALTER TABLE public.brain_sessions DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE public.brain_sessions ADD CONSTRAINT {name} {definition}")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_brain_sessions_open_slot "
        "ON public.brain_sessions (slot_id) WHERE status = 'open' AND slot_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_brain_sessions_relayed_from "
        "ON public.brain_sessions (relayed_from_session_id) "
        "WHERE relayed_from_session_id IS NOT NULL"
    )


def downgrade() -> None:
    opted_in = (context.get_x_argument(as_dictionary=True) or {}).get(_OPT_IN) == "yes"
    op.execute(
        f"""
        DO $$
        DECLARE
            slots bigint;
            history bigint;
            bound bigint;
        BEGIN
            SELECT count(*) INTO slots FROM public.focus_slots;
            SELECT count(*) INTO history FROM public.focus_slot_history;
            SELECT count(*) INTO bound FROM public.brain_sessions WHERE slot_id IS NOT NULL;
            IF {"FALSE" if opted_in else "TRUE"} AND (slots > 0 OR history > 0 OR bound > 0) THEN
                RAISE EXCEPTION
                    'cannot downgrade 060: % focus slot(s), % slot history row(s) and % '
                    'slot-bound session(s) would be destroyed with no record that they '
                    'existed. Rerun with -x {_OPT_IN}=yes to accept that.',
                    slots, history, bound;
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP INDEX IF EXISTS public.uq_brain_sessions_relayed_from")
    op.execute("DROP INDEX IF EXISTS public.uq_brain_sessions_open_slot")
    for name, _ in reversed(_SESSION_CONSTRAINTS):
        op.execute(f"ALTER TABLE public.brain_sessions DROP CONSTRAINT IF EXISTS {name}")
    op.execute(
        "ALTER TABLE public.brain_sessions DROP COLUMN IF EXISTS relayed_from_session_id, "
        "DROP COLUMN IF EXISTS slot_id"
    )
    op.execute("DROP TABLE IF EXISTS public.focus_slot_history")
    op.execute("DROP TABLE IF EXISTS public.focus_slot_anchors")
    op.execute("DROP TABLE IF EXISTS public.focus_slots")
    op.execute("DROP FUNCTION IF EXISTS public.require_focus_slot_history()")
    op.execute("DROP FUNCTION IF EXISTS public.focus_slot_anchors_append_only()")
    op.execute("DROP FUNCTION IF EXISTS public.focus_slot_history_append_only()")
    op.execute(_DROP_TERMINAL)
    op.execute(_TERMINAL_STATE_059)
