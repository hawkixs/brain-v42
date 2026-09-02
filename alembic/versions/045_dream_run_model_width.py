"""045 — `dream_runs.model` stops refusing the models it is configured with.

Ticket `bcb5e6d8`. The column is born `varchar(30)` in 013. Model names, for
their part, have grown: the provider now prefixes the publisher and suffixes
the snapshot date. Measured on 2026-08-16 against the real inventory:

    nvidia/nemotron-3-super-120b-a12b       33 chars  WET fallback, ALREADY SET
    deepseek-ai/deepseek-v4-flash-0731      34 chars  live primary candidate
    nvidia/nemotron-3.5-lightning-30b-a3b   37 chars  candidate dropped at canary

What is lost is not the `model` column, it is the WHOLE ROW:
`StringDataRightTruncation` surfaces inside a best-effort `except Exception`
that prints `! warning: could not record dream_run` and carries on. A night
that really ran would leave no trace — the failure path that erases the proof
of success.

The defect has not struck yet because ROADMAP runs in DRY, whose chain is the
dead primary then `meta/llama-3.1-8b-instruct` (26 chars). It is armed for the
day of the WET cutover.

120 and not "the longest + margin": a round number absorbs the next name
without asking for another migration. No backfill, no data touched — widening
a `varchar` rewrites the catalogue, not the rows.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None

_TARGET_WIDTH = 120
_PREVIOUS_WIDTH = 30


def _dream_run_view_sql() -> str:
    """Re-read the view definition FROM 036, never retype it here.

    Measured in production: `ALTER TABLE dream_runs ALTER COLUMN model` is
    refused by Postgres for as long as `codex_dream_run_v1` projects that column
    (`cannot alter type of a column used by a view or rule`). The view must
    therefore fall and come back — and "come back" means IDENTICAL. Copying its
    SELECT would make this revision a second source of truth for a contract that
    `test_codex_contract_views_036.py` guards elsewhere: the drift would surface
    only on read, on the codex side.
    """
    source = Path(__file__).with_name("036_codex_contract_views.py")
    spec = importlib.util.spec_from_file_location("_migration_036_views", source)
    if spec is None or spec.loader is None:  # pragma: no cover — frozen path
        raise RuntimeError(f"036 illisible depuis la 045 : {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module._CREATE_DREAM_RUN_VIEW)


_DREAM_RUN_VIEW_SQL = _dream_run_view_sql()

# Re-install the grant after recreation: a DROP VIEW takes its GRANTs with it,
# and `codex_ro` would lose its read silently — the view would exist, empty of
# permissions, which reads as a client-side failure rather than as a migration
# oversight here. 036 installs exactly this line (l.461).
_GRANT_SQL = "GRANT SELECT ON codex_dream_run_v1 TO codex_ro"


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS codex_dream_run_v1")
    op.alter_column(
        "dream_runs",
        "model",
        existing_type=sa.String(length=_PREVIOUS_WIDTH),
        type_=sa.String(length=_TARGET_WIDTH),
        existing_nullable=True,
    )
    op.execute(_DREAM_RUN_VIEW_SQL)
    op.execute(_GRANT_SQL)


def downgrade() -> None:
    """Fail-closed: narrowing must never truncate a measurement already written.

    Postgres would refuse on its own, but with a bare driver error. Saying it
    here names the cause and the offending row, instead of leaving the operator
    to guess in front of a `value too long for type character varying(30)`.
    """
    offenders = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT model FROM dream_runs "
                "WHERE char_length(model) > :width ORDER BY char_length(model) DESC LIMIT 5"
            ),
            {"width": _PREVIOUS_WIDTH},
        )
        .scalars()
        .all()
    )
    if offenders:
        raise RuntimeError(
            f"downgrade 045 refusé : {len(offenders)} modèle(s) dépassent "
            f"{_PREVIOUS_WIDTH} car. et seraient tronqués — {', '.join(offenders)}. "
            "Réécrire ou supprimer ces lignes avant de rétrécir la colonne."
        )

    op.execute("DROP VIEW IF EXISTS codex_dream_run_v1")
    op.alter_column(
        "dream_runs",
        "model",
        existing_type=sa.String(length=_TARGET_WIDTH),
        type_=sa.String(length=_PREVIOUS_WIDTH),
        existing_nullable=True,
    )
    op.execute(_DREAM_RUN_VIEW_SQL)
    op.execute(_GRANT_SQL)
