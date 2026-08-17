"""Widen dream_promotions.skipped_reason from VARCHAR(100) to TEXT.

Dream nights 2026-05-02 and 2026-05-03 both crashed PROMOTE on
StringDataRightTruncationError: the LLM-generated reason for
classification_uncertain was ~430 chars, well over the original
VARCHAR(100) cap from migration 016. The crash happened on the very
first candidate of the night (058e528d), short-circuiting the whole
PROMOTE phase and flagging both runs as partial — even though the
agent's classification was correct, the audit row simply could not
be persisted.

The skipped_reason field is an LLM justification, naturally verbose
and unbounded. TEXT is the right type. Dropping the cap also removes
the matching `max_length=100` constraint on the Pydantic model.

Revision ID: 021
Revises: 020
Create Date: 2026-05-03
"""

from __future__ import annotations

from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE dream_promotions ALTER COLUMN skipped_reason TYPE TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE dream_promotions ALTER COLUMN skipped_reason TYPE VARCHAR(100)")
