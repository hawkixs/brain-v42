"""Record verified original authors without backfilling historical rows."""

import sqlalchemy as sa
from alembic import context, op

revision = "069"
down_revision = "068"
branch_labels = None
depends_on = None

TABLES = ("decisions", "learnings", "snippets", "runbooks", "adrs", "focus_slots")
_FORMAT = "author_client_id IS NULL OR author_client_id ~ '^[a-z0-9][a-z0-9.-]{0,63}$'"
_OPT_IN = "allow_author_client_id_downgrade"


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("author_client_id", sa.Text(), nullable=True))
        op.create_check_constraint(f"{table}_author_client_id_format", table, _FORMAT)


def downgrade() -> None:
    # Check every table before dropping anything, so a refusal preserves all authors.
    authored = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT "
                + " + ".join(
                    f"(SELECT count(*) FROM {table} WHERE author_client_id IS NOT NULL)"
                    for table in TABLES
                )
            )
        )
        .scalar_one()
    )
    opted_in = (context.get_x_argument(as_dictionary=True) or {}).get(_OPT_IN) == "yes"
    if authored and not opted_in:
        raise RuntimeError(
            f"{authored} row(s) carry author attribution; dropping it loses their authors. "
            f"Re-run with -x {_OPT_IN}=yes to accept that."
        )
    for table in reversed(TABLES):
        op.drop_constraint(f"{table}_author_client_id_format", table, type_="check")
        op.drop_column(table, "author_client_id")
