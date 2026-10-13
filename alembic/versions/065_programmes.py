"""065 — programme progress: programmes and their gates (programmes/v1).

Tickets 8a063256 (the publisher red-cockpit asked for) and 24c1f172 (the freeze
red-monitor asked for); the contract is `docs/contracts/programmes_v1.md`. A
programme is a multi-session effort the operator follows in the cockpit; a gate
is one declared step of it, owned by one project.

The service validates every write; the CHECKs below repeat the contract's
limits so that a write that skipped the service still cannot store a shape the
consumers were promised never to see. Who wrote a row and when are server
facts: `updated_by` is the caller's project, `updated_at` and `last_change_at`
are transaction times.

`last_change_*` is the programme's latest gate state TRANSITION: all five set
or all five NULL, and `from` differs from `to`.

No trigger, no sequence, no function: the recovery contract v23 adds exactly
two tables, their constraints, four indexes and three foreign keys.

NULL rule (060): every CHECK is `COALESCE(<predicate>, false)`. Replayable by
hand: `IF NOT EXISTS`, `DROP ... IF EXISTS`. The downgrade drops both tables
and every programme in them; programmes are declared, not measured, so they
are re-declared after a rollback rather than migrated back.
"""

from __future__ import annotations

from alembic import op

revision = "065"
down_revision = "064"
branch_labels = None
depends_on = None

_STATES_PROGRAMME = "('active', 'paused', 'done')"
_STATES_GATE = "('todo', 'in_progress', 'blocked', 'done')"

_PROGRAMMES = f"""
CREATE TABLE IF NOT EXISTS public.programmes (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    coordinator_project VARCHAR(50) NOT NULL,
    state VARCHAR(16) NOT NULL,
    position INTEGER NOT NULL,
    last_change_gate TEXT,
    last_change_from VARCHAR(16),
    last_change_to VARCHAR(16),
    last_change_at TIMESTAMPTZ,
    last_change_by VARCHAR(50),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by VARCHAR(50) NOT NULL,
    CONSTRAINT programmes_pkey PRIMARY KEY (id),
    CONSTRAINT programmes_key_key UNIQUE (key),
    CONSTRAINT programmes_coordinator_project_fkey FOREIGN KEY (coordinator_project)
        REFERENCES public.project_contexts (project_key) ON DELETE RESTRICT,
    CONSTRAINT programmes_key_valid
        CHECK (COALESCE(key ~ '^[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}$', false)),
    CONSTRAINT programmes_title_valid
        CHECK (COALESCE(btrim(title) <> '' AND char_length(title) <= 120, false)),
    CONSTRAINT programmes_state_valid CHECK (COALESCE(state IN {_STATES_PROGRAMME}, false)),
    CONSTRAINT programmes_position_valid CHECK (COALESCE(position >= 0, false)),
    CONSTRAINT programmes_last_change_valid CHECK (COALESCE(
        (last_change_gate IS NULL AND last_change_from IS NULL AND last_change_to IS NULL
            AND last_change_at IS NULL AND last_change_by IS NULL)
        OR (last_change_gate IS NOT NULL AND last_change_at IS NOT NULL
            AND last_change_by IS NOT NULL
            AND last_change_from IN {_STATES_GATE} AND last_change_to IN {_STATES_GATE}
            AND last_change_from <> last_change_to),
        false))
)
"""

_GATES = f"""
CREATE TABLE IF NOT EXISTS public.programme_gates (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    programme_id UUID NOT NULL,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    owner_project VARCHAR(50) NOT NULL,
    state VARCHAR(16) NOT NULL,
    depends_on TEXT[] NOT NULL DEFAULT '{{}}',
    weight INTEGER NOT NULL DEFAULT 1,
    waiting_ask TEXT,
    waiting_since TIMESTAMPTZ,
    progress_done INTEGER,
    progress_total INTEGER,
    progress_unit TEXT,
    note TEXT,
    position INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by VARCHAR(50) NOT NULL,
    CONSTRAINT programme_gates_pkey PRIMARY KEY (id),
    CONSTRAINT programme_gates_programme_id_key_key UNIQUE (programme_id, key),
    CONSTRAINT programme_gates_programme_id_fkey FOREIGN KEY (programme_id)
        REFERENCES public.programmes (id) ON DELETE CASCADE,
    CONSTRAINT programme_gates_owner_project_fkey FOREIGN KEY (owner_project)
        REFERENCES public.project_contexts (project_key) ON DELETE RESTRICT,
    CONSTRAINT programme_gates_key_valid
        CHECK (COALESCE(key ~ '^[A-Za-z0-9][A-Za-z0-9._-]{{0,31}}$', false)),
    CONSTRAINT programme_gates_title_valid
        CHECK (COALESCE(btrim(title) <> '' AND char_length(title) <= 120, false)),
    CONSTRAINT programme_gates_state_valid CHECK (COALESCE(state IN {_STATES_GATE}, false)),
    CONSTRAINT programme_gates_depends_on_valid CHECK (COALESCE(
        cardinality(depends_on) <= 12 AND array_position(depends_on, NULL) IS NULL
            AND array_to_string(depends_on, ',')
                ~ '^([A-Za-z0-9][A-Za-z0-9._-]{{0,31}}(,|$))*$'
            AND NOT (key = ANY (depends_on)),
        false)),
    CONSTRAINT programme_gates_weight_valid CHECK (COALESCE(weight BETWEEN 1 AND 100, false)),
    CONSTRAINT programme_gates_waiting_valid CHECK (COALESCE(
        (waiting_ask IS NULL AND waiting_since IS NULL)
        OR (btrim(waiting_ask) <> '' AND char_length(waiting_ask) <= 200
            AND waiting_since IS NOT NULL),
        false)),
    CONSTRAINT programme_gates_progress_valid CHECK (COALESCE(
        (progress_done IS NULL AND progress_total IS NULL AND progress_unit IS NULL)
        OR (progress_total BETWEEN 1 AND 100000 AND progress_done BETWEEN 0 AND progress_total
            AND btrim(progress_unit) <> '' AND char_length(progress_unit) <= 24),
        false)),
    CONSTRAINT programme_gates_note_valid
        CHECK (COALESCE(note IS NULL OR (btrim(note) <> '' AND char_length(note) <= 200), false)),
    CONSTRAINT programme_gates_position_valid CHECK (COALESCE(position >= 0, false))
)
"""


def upgrade() -> None:
    op.execute(_PROGRAMMES)
    op.execute(_GATES)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.programme_gates")
    op.execute("DROP TABLE IF EXISTS public.programmes")
