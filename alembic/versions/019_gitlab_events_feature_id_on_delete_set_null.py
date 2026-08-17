"""Fix: gitlab_events.feature_id FK ON DELETE SET NULL.

Belt + suspenders for the FeatureDedupJob fix that now transfers
gitlab_events onto the canonical feature before deleting the source.
This migration guarantees that any future DELETE on features (manual
DB surgery, dream reorg, audit cleanup, …) cannot trip the
gitlab_events_feature_id_fkey even if the caller forgets to re-parent
events first — the rows survive with feature_id=NULL.

Observed error in prod (2026-04-20 21:14):
    ForeignKeyViolationError: update or delete on table "features"
    violates foreign key constraint "gitlab_events_feature_id_fkey"
    on table "gitlab_events"

Revision ID: 019
Revises: 018
Create Date: 2026-04-21
"""

from __future__ import annotations

from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE gitlab_events DROP CONSTRAINT gitlab_events_feature_id_fkey")
    op.execute(
        "ALTER TABLE gitlab_events ADD CONSTRAINT gitlab_events_feature_id_fkey"
        " FOREIGN KEY (feature_id) REFERENCES features(id)"
        " ON DELETE SET NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE gitlab_events DROP CONSTRAINT gitlab_events_feature_id_fkey")
    op.execute(
        "ALTER TABLE gitlab_events ADD CONSTRAINT gitlab_events_feature_id_fkey"
        " FOREIGN KEY (feature_id) REFERENCES features(id)"
    )
