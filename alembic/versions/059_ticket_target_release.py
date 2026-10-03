"""059 — a ticket can name the release it is planned for.

Nullable, no default, no backfill: NULL means not planned. Shipped releases
remain measured from tags by the delivery observer. Dropping this column would
erase plans, so downgrade requires an explicit operator opt-in.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "059"
down_revision = "058"
branch_labels = None
depends_on = None

_OPT_IN = "allow_target_release_downgrade"


def upgrade() -> None:
    op.add_column("tickets", sa.Column("target_release", sa.Text(), nullable=True))
    op.create_check_constraint(
        "tickets_target_release_valid",
        "tickets",
        "target_release ~ '^[0-9]+\\.[0-9]+\\.[0-9]+$'",
    )
    op.create_index(
        "idx_tickets_to_project_target_release",
        "tickets",
        ["to_project", "target_release"],
        postgresql_where=sa.text("target_release IS NOT NULL"),
    )


def downgrade() -> None:
    planned = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM tickets WHERE target_release IS NOT NULL"))
        .scalar_one()
    )
    opted_in = (context.get_x_argument(as_dictionary=True) or {}).get(_OPT_IN) == "yes"
    if planned and not opted_in:
        raise RuntimeError(
            f"{planned} ticket(s) carry a target_release; dropping the column erases "
            f"every plan with no record that it existed. Re-run with -x {_OPT_IN}=yes "
            "to accept that."
        )
    op.drop_index("idx_tickets_to_project_target_release", table_name="tickets")
    op.drop_constraint("tickets_target_release_valid", "tickets", type_="check")
    op.drop_column("tickets", "target_release")
