"""Persistent concurrent Brain sessions with explicit terminal states.

Revision ID: 032
Revises: 031
"""

from alembic import op

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE project_contexts ADD COLUMN focus_revision BIGINT NOT NULL DEFAULT 0")
    op.execute(
        """
        CREATE FUNCTION increment_project_focus_revision()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.current_focus IS DISTINCT FROM OLD.current_focus THEN
                NEW.focus_revision := OLD.focus_revision + 1;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_contexts_focus_revision_trigger
        BEFORE UPDATE OF current_focus ON project_contexts
        FOR EACH ROW EXECUTE FUNCTION increment_project_focus_revision()
        """
    )
    op.execute(
        """
        CREATE TABLE brain_sessions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_key VARCHAR(50) NOT NULL,
            client_key VARCHAR(128) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'open',
            started_focus TEXT,
            started_focus_revision BIGINT NOT NULL,
            summary TEXT,
            next_focus TEXT,
            captured_knowledge_ids UUID[] NOT NULL DEFAULT '{}',
            nothing_to_capture_reason TEXT,
            abandonment_reason TEXT,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ended_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_brain_sessions_project_client
                UNIQUE (project_key, client_key),
            CONSTRAINT brain_sessions_project_key_fkey
                FOREIGN KEY (project_key)
                REFERENCES project_contexts(project_key) ON DELETE RESTRICT,
            CONSTRAINT brain_sessions_status_valid
                CHECK (status IN ('open', 'ended', 'abandoned')),
            CONSTRAINT brain_sessions_client_key_nonblank
                CHECK (btrim(client_key) <> ''),
            CONSTRAINT brain_sessions_capture_ids_valid CHECK (
                cardinality(captured_knowledge_ids) <= 100
                AND array_position(captured_knowledge_ids, NULL) IS NULL
            ),
            CONSTRAINT brain_sessions_terminal_state_valid CHECK (
                (
                    status = 'open'
                    AND ended_at IS NULL
                    AND summary IS NULL
                    AND next_focus IS NULL
                    AND cardinality(captured_knowledge_ids) = 0
                    AND nothing_to_capture_reason IS NULL
                    AND abandonment_reason IS NULL
                )
                OR (
                    status = 'ended'
                    AND ended_at IS NOT NULL
                    AND summary IS NOT NULL
                    AND btrim(summary) <> ''
                    AND next_focus IS NOT NULL
                    AND btrim(next_focus) <> ''
                    AND abandonment_reason IS NULL
                    AND (
                        (
                            cardinality(captured_knowledge_ids) > 0
                            AND nothing_to_capture_reason IS NULL
                        )
                        OR (
                            cardinality(captured_knowledge_ids) = 0
                            AND nothing_to_capture_reason IS NOT NULL
                            AND btrim(nothing_to_capture_reason) <> ''
                        )
                    )
                )
                OR (
                    status = 'abandoned'
                    AND ended_at IS NOT NULL
                    AND summary IS NULL
                    AND next_focus IS NULL
                    AND cardinality(captured_knowledge_ids) = 0
                    AND nothing_to_capture_reason IS NULL
                    AND abandonment_reason IS NOT NULL
                    AND btrim(abandonment_reason) <> ''
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_brain_sessions_project_status_started
        ON brain_sessions (project_key, status, started_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS brain_sessions")
    op.execute("DROP TRIGGER IF EXISTS project_contexts_focus_revision_trigger ON project_contexts")
    op.execute("DROP FUNCTION IF EXISTS increment_project_focus_revision()")
    op.execute("ALTER TABLE project_contexts DROP COLUMN IF EXISTS focus_revision")
