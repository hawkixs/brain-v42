"""Cross-project tickets: the tickets and ticket_messages tables.

Coordination family (spec 2026-07-04) — no embedding, no
search_vector, no decay. See
docs/superpowers/specs/2026-07-04-cross-project-tickets-design.md.

Revision ID: 028
Revises: 027
"""

from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tickets (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            kind VARCHAR(10) NOT NULL,
            title VARCHAR(200) NOT NULL,
            body TEXT NOT NULL,
            from_project VARCHAR(50) NOT NULL,
            to_project VARCHAR(50) NOT NULL,
            status VARCHAR(15) NOT NULL DEFAULT 'open',
            extraction_status VARCHAR(10),
            resolved_at TIMESTAMPTZ,
            closed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT tickets_kind_valid CHECK (kind IN ('request', 'fyi')),
            CONSTRAINT tickets_status_valid CHECK (
                status IN ('open', 'in_progress', 'resolved', 'wontfix', 'closed', 'acked')
            ),
            CONSTRAINT tickets_extraction_status_valid CHECK (
                extraction_status IS NULL
                OR extraction_status IN ('pending', 'proposed', 'skipped', 'done')
            )
        )
        """
    )
    op.execute("CREATE INDEX idx_tickets_to_project_status ON tickets (to_project, status)")
    op.execute("CREATE INDEX idx_tickets_from_project_status ON tickets (from_project, status)")
    op.execute(
        "CREATE INDEX idx_tickets_extraction_pending ON tickets (extraction_status)"
        " WHERE extraction_status = 'pending'"
    )
    op.execute(
        """
        CREATE TABLE ticket_messages (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_id UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
            author_project VARCHAR(50) NOT NULL,
            body TEXT NOT NULL,
            status_to VARCHAR(15),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX idx_ticket_messages_ticket ON ticket_messages (ticket_id, created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ticket_messages;")
    op.execute("DROP TABLE IF EXISTS tickets;")
