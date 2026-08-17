"""Add a crash-safe recovery interlock for the Neo4j projection.

Revision ID: 035
Revises: 034
"""

from __future__ import annotations

from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE graph_projection_leases
            ADD COLUMN recovery_id UUID,
            ADD COLUMN recovery_phase VARCHAR(16) NOT NULL DEFAULT 'idle',
            ADD COLUMN last_completed_recovery_id UUID,
            ADD CONSTRAINT graph_projection_leases_recovery_state_valid
                CHECK (
                    (
                        recovery_id IS NULL
                        AND recovery_phase = 'idle'
                    )
                    OR (
                        recovery_id IS NOT NULL
                        AND recovery_id IS DISTINCT FROM last_completed_recovery_id
                        AND owner IS NOT NULL
                        AND leased_until IS NOT NULL
                        AND (
                            (
                                recovery_phase = 'prepared'
                                AND neo4j_armed_generation IS NULL
                            )
                            OR (
                                recovery_phase = 'neo_ready'
                                AND neo4j_armed_generation IS NOT NULL
                                AND neo4j_armed_generation = generation
                            )
                        )
                    )
                )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE graph_projection_leases
            DROP CONSTRAINT IF EXISTS graph_projection_leases_recovery_state_valid,
            DROP COLUMN IF EXISTS last_completed_recovery_id,
            DROP COLUMN IF EXISTS recovery_phase,
            DROP COLUMN IF EXISTS recovery_id
        """
    )
