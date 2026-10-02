"""Distinguish server-extracted assertions from measured and caller-declared claims.

The migration changes only the provenance CHECK. Claim occurrences and their
immutable fields retain the safeguards installed by revision 055.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "058"
down_revision = "057"
branch_labels = None
depends_on = None

_CONSTRAINT = "knowledge_claims_provenance_valid"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "knowledge_claims", type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        "knowledge_claims",
        "provenance IN ('measured', 'declared', 'extracted')",
    )


def downgrade() -> None:
    extracted = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM knowledge_claims WHERE provenance = 'extracted'"))
        .scalar_one()
    )
    if extracted:
        raise RuntimeError(
            "knowledge_claims_provenance_downgrade_refused: "
            f"{extracted} extracted claim(s) would violate the 057 CHECK"
        )
    op.drop_constraint(_CONSTRAINT, "knowledge_claims", type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        "knowledge_claims",
        "provenance IN ('measured', 'declared')",
    )
