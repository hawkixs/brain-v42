"""Give dream_runs the dimension the dream believed it covered.

Revision ID: 042
Revises: 041

The dream has always been single-project — `dream.sh:70` carries
`PROJECT_KEY="${1:-brain-v42}"`, no loop, one systemd unit — and `dream_runs`
has NO project column at all. The schema itself therefore cannot express the
dimension we believed was covered: eight phases over four months, 124 nights,
all indistinguishable. This is the column that must exist before the loop can
open.

NULLABLE, AND THAT IS A MEASURED CONSEQUENCE, NOT CAUTION. Corrected on
2026-08-09: there are SIX INSERT sites, not five, and NONE of them surfaces its
failure. Three swallow it inside their own function, as their docstrings say —
`scripts/ticket_extract.py` and `scripts/roadmap_curate.py`
("Best-effort — never raises"), `src/brain_v42/maintenance/session_sweep.py`
("the trace must never kill the phase"). Two raise in Python but are swallowed
by the orchestrator: the site shared by both parsers
(`dream.sh`, `WARN … (non-fatal)`) and `scripts/dream/_promote_helpers.py`
(`WARN promote — empty-pool dream_runs row NOT recorded`). The sixth,
`scripts/dream/cross_project_resonance.py`, is dead — no caller, so never
executed, so never swallowed either. A NOT NULL column would turn a schema
error into a printed warning on every one of them that runs: the night would
lose its trace in silence, which is exactly the defect we are trying to
remove.

NO DEFAULT EITHER. A `server_default='brain-v42'` would label every night of
every project with a single project's key the day the loop opens — the class of
bug this column exists to make visible.

NO BACKFILL: the 864 prior rows stay NULL, and NULL reads as "written before
042". That is the doctrine of revisions 040 and 041, and it makes the round
trip safe — an unmigrated reader and a migrated writer coexist, which lets the
migration be applied in production BEFORE the merge that introduces its
readers.

THE `'*'` SENTINEL marks a global phase. Corrected on 2026-08-09, when the
writers shipped: it is set by FOUR of them, not three — `extract`, `roadmap`,
`sweep`, plus `cross_project_resonance`, which is dead (no caller, no row) and
receives it so as not to be the only inconsistent one the day someone wires it
back in.

The first writing added that those three were, "and this is no accident",
exactly the three best-effort sites above. That was true by accident and false
in substance: all SIX INSERT sites lose their trace in silence — three swallow
it in their own function, three are swallowed by the orchestrator. The nullable
argument therefore rests on six, not three, and comes out stronger for it. An
elegance that rests on a head-count is an elegance to re-check.

THE INDEX IS ADDED, it does not replace. `idx_dream_runs_date(run_date DESC)`
serves the ten reads measured today, none of which filters by project; dropping
it would break their plan for no gain.

Downgrade: a plain `drop_column`, like 041, with no fail-closed guard. The
loss is real and accepted — a `dream_runs` without a project key is exactly the
prior state, which held for 124 nights, and the data rebuilds itself in one
night of telemetry. A guard would make the way back harder than the way in,
which is the wrong direction for telemetry.
"""

import sqlalchemy as sa
from alembic import op

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None

_INDEX = "idx_dream_runs_date_project"


def upgrade() -> None:
    op.add_column(
        "dream_runs",
        sa.Column("project_key", sa.String(64), nullable=True),
    )
    op.create_index(
        _INDEX,
        "dream_runs",
        [sa.text("run_date DESC"), "project_key"],
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="dream_runs")
    op.drop_column("dream_runs", "project_key")
