"""Add 'automated' to learnings.source_type CHECK constraint.

Closes a long-standing Pydantic↔DB drift (since plan 2026-04-05-dream-mode.md
Task 1): `SourceType` Literal in src/brain_v42/models/learning.py includes
'automated' and the Pydantic test in tests/unit/test_source_type_automated.py
passes, but migration 003's UNIFIED_TYPES never included it — so any
brain_learn(source_type='automated') crashed on the DB CHECK constraint.

First real impact observed during Dream Night 4 SYNTH (2026-04-24): the
agent's brain_learn call for a meta-insight failed on CheckViolation, had
to fall back to source_type='research'. Two-symptom transport incident
flagged in learning 5fed6f23.

Revision ID: 020
Revises: 019
Create Date: 2026-04-24
"""

from __future__ import annotations

from alembic import op

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None

UNIFIED_TYPES_V2 = (
    "'experience','documentation','code_review','bug','external',"
    "'article','video','book','conversation','research','automated'"
)

# Matches migration 003's UNIFIED_TYPES — the pre-020 shape.
UNIFIED_TYPES_V1 = (
    "'experience','documentation','code_review','bug','external',"
    "'article','video','book','conversation','research'"
)


def upgrade() -> None:
    op.execute("ALTER TABLE learnings DROP CONSTRAINT IF EXISTS learnings_source_type_check")
    op.execute(
        f"ALTER TABLE learnings ADD CONSTRAINT learnings_source_type_check "
        f"CHECK (source_type IN ({UNIFIED_TYPES_V2}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE learnings DROP CONSTRAINT IF EXISTS learnings_source_type_check")
    op.execute(
        f"ALTER TABLE learnings ADD CONSTRAINT learnings_source_type_check "
        f"CHECK (source_type IN ({UNIFIED_TYPES_V1}))"
    )
