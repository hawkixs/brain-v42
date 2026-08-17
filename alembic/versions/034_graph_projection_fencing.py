"""Add durable PostgreSQL fencing state for the Neo4j projector.

Revision ID: 034
Revises: 033
"""

from __future__ import annotations

from alembic import op

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE graph_projection_leases (
            slot VARCHAR(32) PRIMARY KEY,
            protocol_version INTEGER NOT NULL DEFAULT 2,
            generation BIGINT NOT NULL DEFAULT 0,
            owner VARCHAR(128),
            leased_until TIMESTAMPTZ,
            neo4j_armed_generation BIGINT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT graph_projection_leases_protocol_valid
                CHECK (protocol_version = 2),
            CONSTRAINT graph_projection_leases_armed_generation_valid
                CHECK (
                    neo4j_armed_generation IS NULL
                    OR neo4j_armed_generation = generation
                )
        )
        """
    )
    op.execute(
        """
        ALTER TABLE graph_outbox
            ADD COLUMN lease_generation BIGINT,
            ADD COLUMN claim_version BIGINT NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        UPDATE graph_outbox
        SET lease_owner = NULL,
            leased_until = NULL,
            lease_generation = NULL
        WHERE delivered_at IS NULL
        """
    )
    op.execute(
        """
        INSERT INTO graph_projection_leases (
            slot,
            protocol_version,
            generation,
            neo4j_armed_generation
        ) VALUES ('neo4j', 2, 0, 0)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS graph_projection_leases")
    op.execute(
        """
        ALTER TABLE graph_outbox
            DROP COLUMN IF EXISTS claim_version,
            DROP COLUMN IF EXISTS lease_generation
        """
    )
