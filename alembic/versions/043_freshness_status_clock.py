"""043 — date the freshness STATUS, and say where the transition came from.

Spec `docs/superpowers/specs/2026-08-08-dream-v2-design.md` §4.3 et §6.2.
A HARD precondition of the purge, not a scheduling preference.

The repository already has a deletion criterion — `decay_tools.brain_decay_status`,
printed at SCAN every night — and it is wrong on both of its terms:

- `access_count = 0` is the TOTAL counter. An artifact re-read by the dream
  alone leaves the criterion and becomes indefinitely unpurgeable.
- `updated_at < cutoff` RESTARTS on every write of the counter flusher, because
  `trg_<table>_updated` is present on all six tables. There is therefore NO
  honest clock today for measuring a stay in the archive.

NO BACKFILL. `NULL` means "never measured", never "archived forever". The
distinction decides who would be deleted: dating retroactively to `now()` would
suggest the whole corpus had just changed status, and dating to `updated_at`
would copy over precisely the false clock we are replacing.

041'S MECHANISM, NOT 040'S. 040 writes `focus_updated_at` in application code
because the focus has exactly ONE writer. `freshness_status` has several,
including the REORG judgement that goes through the generic `brain_update` tool
— which knows nothing about the decay. Stamping in application code would mean
doing it inside `brain_update` itself, for a column 99% of its calls never
touch. Hence a conditional trigger. That is the point: one of the writers is a
prompt.

THE TWO COLUMNS DO NOT BEHAVE ALIKE, AND THAT IS THIS FILE'S TRAP. An earlier
version of this docstring concluded here that "none of the four writers has to
remember it". That is true of `freshness_status_updated_at`, and FALSE of
`freshness_source`:

- `freshness_status_updated_at` is AUTOMATIC. The trigger sets it on every
  transition; no writer has anything to do.
- `freshness_source` must be REDECLARED ON EVERY WRITE. The trigger CLEARS it
  when it is not (see the body of the function, below) — that is deliberate,
  but it means a writer that does not set it produces a `NULL`, not an
  inherited value.

The corrected sentence is not cosmetic: at the 2026-08-22 census, FIVE
writers out of six redeclared nothing, and the old wording is the most
likely root cause — a writer reading it legitimately concluded it had
nothing to declare. Three were repaired straight away; two stay silent on
purpose, awaiting an operator's signature. **Do not copy these numbers
forward: recount them.**

The count of "four" was wrong too, and its failure mode is instructive: it
takes a MULTI-PATTERN census (kwarg, dict key, raw SQL, and the field on the
`*Update` models) to find six of them in `src/`. The sixth — the generic
`brain_update` tool, the one that JUDGES — is visible to NO grep on the column
name, since it never names it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None

# The six tables tracked by the decay — the same set as
# `decay_tools._DECAY_ENTITY_TABLES` and as 041's `_COUNTER_TABLES`.
_DECAY_TABLES = (
    "learnings",
    "decisions",
    "snippets",
    "runbooks",
    "adrs",
    "indexed_plans",
)

# Closed vocabulary of transitions, §4.3. `merge` = CLEAN merged, `judgment`
# = REORG ruled, `score` = the decay computed, `revive` = a human access
# brought the entity back.
_SOURCES = ("merge", "judgment", "score", "revive")

_CREATE_FUNCTION = """
CREATE FUNCTION public.stamp_freshness_status()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    NEW.freshness_status_updated_at := CURRENT_TIMESTAMP;
    -- Une source décrit UNE transition. La laisser survivre à la suivante
    -- ferait décrire la transition précédente avec la date de la nouvelle :
    -- une provenance fausse, qui se croit, au lieu d'une provenance absente,
    -- qui se voit. Un écrivain qui ne redéclare pas la sienne la perd.
    --
    -- Angle mort assumé, et il vaut mieux ici qu'ailleurs : deux transitions
    -- consécutives DE MÊME SOURCE ne sont pas distinguables d'une source non
    -- redéclarée, donc la seconde retombe à NULL. PostgreSQL n'expose pas la
    -- liste SET d'un UPDATE en plpgsql. La dégradation va vers « non mesuré »,
    -- jamais vers « mesuré faux ».
    IF NEW.freshness_source IS NOT DISTINCT FROM OLD.freshness_source THEN
        NEW.freshness_source := NULL;
    END IF;
    RETURN NEW;
END;
$function$;
"""


def upgrade() -> None:
    for table in _DECAY_TABLES:
        op.add_column(
            table,
            sa.Column("freshness_status_updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.add_column(table, sa.Column("freshness_source", sa.String(16), nullable=True))
        # NULL passes the CHECK by construction in SQL, and that is intended:
        # the column reads "not measured" by default across the whole corpus.
        allowed = ", ".join(f"'{source}'" for source in _SOURCES)
        op.create_check_constraint(
            f"ck_{table}_freshness_source",
            table,
            f"freshness_source IS NULL OR freshness_source IN ({allowed})",
        )

    op.execute(_CREATE_FUNCTION)

    for table in _DECAY_TABLES:
        # `OF freshness_status` narrows the firing, the `WHEN` narrows it
        # further: rewriting the SAME status does not rejuvenate the clock.
        # Without that predicate, an idempotent job re-setting `archived` every
        # night would reset the stay to zero daily, and nothing would ever
        # become purgeable — silently.
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_freshness_stamped
            BEFORE UPDATE OF freshness_status ON public.{table}
            FOR EACH ROW
            WHEN (OLD.freshness_status IS DISTINCT FROM NEW.freshness_status)
            EXECUTE FUNCTION public.stamp_freshness_status();
            """
        )


def downgrade() -> None:
    for table in _DECAY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_freshness_stamped ON public.{table};")
    op.execute("DROP FUNCTION IF EXISTS public.stamp_freshness_status();")
    for table in _DECAY_TABLES:
        op.drop_constraint(f"ck_{table}_freshness_source", table, type_="check")
        op.drop_column(table, "freshness_source")
        op.drop_column(table, "freshness_status_updated_at")
