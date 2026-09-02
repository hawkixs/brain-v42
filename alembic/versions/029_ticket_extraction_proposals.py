"""Knowledge-extraction proposals drawn from terminal tickets.

PROMOTE pattern (dream_promotions): a proposer-only audit table, human
review → apply. Spec §6.

Revision ID: 029
Revises: 028
"""

from alembic import op

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ticket_extraction_proposals (
            id BIGSERIAL PRIMARY KEY,
            ticket_id UUID REFERENCES tickets(id) ON DELETE SET NULL,
            target_type VARCHAR(10) NOT NULL,
            target_project VARCHAR(50) NOT NULL,
            payload JSONB NOT NULL,
            rationale TEXT,
            status VARCHAR(10) NOT NULL DEFAULT 'proposed',
            applied_entity_id UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            applied_at TIMESTAMPTZ,

            CONSTRAINT tep_target_type_valid CHECK (target_type IN ('learning', 'decision')),
            CONSTRAINT tep_status_valid CHECK (status IN ('proposed', 'applied', 'rejected'))
        )
        """
    )
    op.execute("CREATE INDEX idx_tep_status ON ticket_extraction_proposals (status)")
    op.execute("CREATE INDEX idx_tep_ticket ON ticket_extraction_proposals (ticket_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ticket_extraction_proposals;")
