"""Align learnings.source_type CHECK constraint with Pydantic SourceType.

Old DB constraint: experience, documentation, code_review, bug, external
Old Pydantic:      experience, documentation, article, video, book, conversation
New (unified):     experience, documentation, code_review, bug, external,
                   article, video, book, conversation, research

Revision ID: 003
Revises: 002
Create Date: 2026-03-08
"""

from __future__ import annotations

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None

UNIFIED_TYPES = (
    "'experience','documentation','code_review','bug','external',"
    "'article','video','book','conversation','research'"
)

OLD_TYPES = "'experience','documentation','code_review','bug','external'"


def upgrade() -> None:
    op.execute("ALTER TABLE learnings DROP CONSTRAINT IF EXISTS learnings_source_type_check")
    op.execute(
        f"ALTER TABLE learnings ADD CONSTRAINT learnings_source_type_check "
        f"CHECK (source_type IN ({UNIFIED_TYPES}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE learnings DROP CONSTRAINT IF EXISTS learnings_source_type_check")
    op.execute(
        f"ALTER TABLE learnings ADD CONSTRAINT learnings_source_type_check "
        f"CHECK (source_type IN ({OLD_TYPES}))"
    )
