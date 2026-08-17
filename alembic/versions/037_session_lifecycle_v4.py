"""Persist session identity, capture provenance, focus outcomes, and heartbeat.

Revision ID: 037
Revises: 036
"""

from alembic import op

revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


_TERMINAL_STATE_V4 = """
ALTER TABLE brain_sessions
ADD CONSTRAINT brain_sessions_terminal_state_valid CHECK (
    (
        status = 'open'
        AND ended_at IS NULL
        AND summary IS NULL
        AND next_focus IS NULL
        AND cardinality(captured_knowledge_ids) = 0
        AND nothing_to_capture_reason IS NULL
        AND abandonment_reason IS NULL
        AND end_expected_focus_revision IS NULL
        AND focus_outcome IS NULL
        AND focus_at_end IS NULL
        AND focus_revision_at_end IS NULL
    )
    OR (
        status = 'ended'
        AND ended_at IS NOT NULL
        AND summary IS NOT NULL
        AND btrim(summary) <> ''
        AND next_focus IS NOT NULL
        AND btrim(next_focus) <> ''
        AND abandonment_reason IS NULL
        AND focus_outcome IS NOT NULL
        AND (end_expected_focus_revision IS NULL OR end_expected_focus_revision >= 0)
        AND (focus_revision_at_end IS NULL OR focus_revision_at_end >= 0)
        AND (
            (
                end_expected_focus_revision IS NULL
                AND focus_outcome = 'applied'
                AND focus_at_end = next_focus
                AND focus_revision_at_end IS NULL
            )
            OR (
                end_expected_focus_revision IS NOT NULL
                AND focus_revision_at_end IS NOT NULL
                AND (
                    (
                        focus_outcome = 'applied'
                        AND focus_at_end = next_focus
                        AND focus_revision_at_end = end_expected_focus_revision + 1
                    )
                    OR (
                        focus_outcome = 'conflict'
                        AND focus_revision_at_end <> end_expected_focus_revision
                    )
                )
            )
        )
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
        AND end_expected_focus_revision IS NULL
        AND focus_outcome IS NULL
        AND focus_at_end IS NULL
        AND focus_revision_at_end IS NULL
    )
)
"""

_TERMINAL_STATE_V3 = """
ALTER TABLE brain_sessions
ADD CONSTRAINT brain_sessions_terminal_state_valid CHECK (
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
"""


def upgrade() -> None:
    op.execute("ALTER TABLE brain_sessions ADD COLUMN last_heartbeat_at TIMESTAMPTZ")
    op.execute(
        "UPDATE brain_sessions SET last_heartbeat_at = updated_at WHERE last_heartbeat_at IS NULL"
    )
    op.execute(
        "ALTER TABLE brain_sessions "
        "ALTER COLUMN last_heartbeat_at SET DEFAULT NOW(), "
        "ALTER COLUMN last_heartbeat_at SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE brain_sessions "
        "ADD COLUMN end_expected_focus_revision BIGINT, "
        "ADD COLUMN focus_outcome VARCHAR(20), "
        "ADD COLUMN focus_at_end TEXT, "
        "ADD COLUMN focus_revision_at_end BIGINT"
    )
    # A v3 session could only reach ended after a successful focus CAS. The
    # exact requested/result revisions were not persisted, so they stay NULL.
    op.execute(
        "UPDATE brain_sessions SET focus_outcome = 'applied', focus_at_end = next_focus "
        "WHERE status = 'ended'"
    )
    op.execute("ALTER TABLE brain_sessions DROP CONSTRAINT brain_sessions_terminal_state_valid")
    op.execute(
        "ALTER TABLE brain_sessions ADD CONSTRAINT brain_sessions_focus_outcome_valid "
        "CHECK (focus_outcome IS NULL OR focus_outcome IN ('applied', 'conflict'))"
    )
    op.execute(_TERMINAL_STATE_V4)

    op.execute(
        """
        CREATE TABLE brain_session_artifacts (
            knowledge_id UUID PRIMARY KEY,
            session_id UUID NOT NULL
                REFERENCES brain_sessions(id) ON DELETE CASCADE,
            knowledge_type VARCHAR(32) NOT NULL,
            captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT brain_session_artifacts_type_valid CHECK (
                knowledge_type IN (
                    'decision', 'learning', 'snippet', 'runbook',
                    'adr', 'indexed_plan', 'legacy'
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_brain_session_artifacts_session_captured
        ON brain_session_artifacts (session_id, captured_at)
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT captured.knowledge_id
                FROM brain_sessions AS existing_session
                CROSS JOIN LATERAL
                    unnest(existing_session.captured_knowledge_ids) AS captured(knowledge_id)
                GROUP BY captured.knowledge_id
                HAVING COUNT(DISTINCT existing_session.id) > 1
            ) THEN
                RAISE EXCEPTION
                    'brain session artifact provenance is ambiguous before migration 037';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        """
        INSERT INTO brain_session_artifacts (
            knowledge_id, session_id, knowledge_type, captured_at
        )
        SELECT DISTINCT
            captured.knowledge_id,
            existing_session.id,
            'legacy',
            COALESCE(existing_session.ended_at, existing_session.updated_at)
        FROM brain_sessions AS existing_session
        CROSS JOIN LATERAL
            unnest(existing_session.captured_knowledge_ids) AS captured(knowledge_id)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM brain_session_artifacts AS artifact
                JOIN brain_sessions AS existing_session
                    ON existing_session.id = artifact.session_id
                WHERE existing_session.status <> 'ended'
                   OR NOT (
                       artifact.knowledge_id = ANY(
                           existing_session.captured_knowledge_ids
                       )
                   )
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade session lifecycle v4 with unsnapshotted artifacts';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM brain_sessions
                WHERE status = 'ended'
                  AND focus_outcome = 'conflict'
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade session lifecycle v4 with conflicted focus outcomes';
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP TABLE IF EXISTS brain_session_artifacts")
    op.execute("ALTER TABLE brain_sessions DROP CONSTRAINT brain_sessions_terminal_state_valid")
    op.execute(_TERMINAL_STATE_V3)
    op.execute("ALTER TABLE brain_sessions DROP CONSTRAINT brain_sessions_focus_outcome_valid")
    op.execute(
        "ALTER TABLE brain_sessions "
        "DROP COLUMN IF EXISTS focus_revision_at_end, "
        "DROP COLUMN IF EXISTS focus_at_end, "
        "DROP COLUMN IF EXISTS focus_outcome, "
        "DROP COLUMN IF EXISTS end_expected_focus_revision, "
        "DROP COLUMN IF EXISTS last_heartbeat_at"
    )
