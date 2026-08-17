"""Persist terminal EXTRACT attempts per ticket for observable resumption.

Revision ID: 038
Revises: 037
"""

from alembic import op

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ticket_extraction_attempts (
            id BIGSERIAL PRIMARY KEY,
            ticket_id UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
            run_date DATE NOT NULL,
            status VARCHAR(10) NOT NULL,
            duration_s DOUBLE PRECISION NOT NULL,
            error_message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ticket_extraction_attempts_status_valid
                CHECK (status IN ('done', 'failed', 'timeout', 'deferred'))
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_ticket_extraction_attempts_ticket "
        "ON ticket_extraction_attempts (ticket_id, created_at)"
    )
    op.execute(
        "CREATE INDEX idx_ticket_extraction_attempts_date "
        "ON ticket_extraction_attempts (run_date, status)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE ticket_extraction_attempts")
