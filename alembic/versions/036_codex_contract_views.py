"""Add the nine Codex v2 read-contract views.

The views expose the Brain Explorer management families without embeddings.
Ticket and roadmap families are scoped to the ``red`` project group at the
database boundary. Dream and consolidation audit views are global by contract.

Revision ID: 036
Revises: 035
"""

from __future__ import annotations

from alembic import op

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None

# Resolve the red key set with the same fail-closed semantics as migration 024:
# explicit red base keys plus colon-sub-partitions whose base is red. Candidate
# child keys come from the scoped contract families introduced before 036.
_RED_KEYS_CTE = """
red_base AS (
    SELECT project_key
    FROM project_contexts
    WHERE project_group = 'red'
),
red_keys AS (
    SELECT project_key FROM red_base
    UNION
    SELECT candidate.project_key
    FROM (
        SELECT from_project AS project_key FROM tickets
        UNION ALL SELECT to_project FROM tickets
        UNION ALL SELECT project_key FROM features
    ) AS candidate
    WHERE candidate.project_key IS NOT NULL
      AND split_part(candidate.project_key, ':', 1) <> candidate.project_key
      AND split_part(candidate.project_key, ':', 1) IN (
          SELECT project_key FROM red_base
      )
)
"""

_CREATE_TICKET_VIEW = f"""
CREATE OR REPLACE VIEW codex_ticket_v1 WITH (security_barrier = true) AS
WITH {_RED_KEYS_CTE}
SELECT
    t.id,
    t.kind,
    t.title,
    t.body,
    t.from_project,
    t.to_project,
    t.status,
    t.extraction_status,
    t.resolved_at,
    t.closed_at,
    t.created_at,
    t.updated_at
FROM tickets AS t
WHERE t.from_project IN (SELECT project_key FROM red_keys)
   OR t.to_project IN (SELECT project_key FROM red_keys)
"""

_CREATE_TICKET_MESSAGE_VIEW = f"""
CREATE OR REPLACE VIEW codex_ticket_message_v1 WITH (security_barrier = true) AS
WITH {_RED_KEYS_CTE}
SELECT
    m.id,
    m.ticket_id,
    m.author_project,
    m.body,
    m.status_to,
    m.created_at
FROM ticket_messages AS m
JOIN tickets AS t ON t.id = m.ticket_id
WHERE t.from_project IN (SELECT project_key FROM red_keys)
   OR t.to_project IN (SELECT project_key FROM red_keys)
"""

_CREATE_FEATURE_VIEW = f"""
CREATE OR REPLACE VIEW codex_feature_v1 WITH (security_barrier = true) AS
WITH {_RED_KEYS_CTE}
SELECT
    f.id,
    f.project_key,
    f.name,
    f.description,
    f.status,
    f.status_updated_at,
    f.pinned,
    f.merged_into,
    f.created_at,
    f.updated_at
FROM features AS f
WHERE f.project_key IN (SELECT project_key FROM red_keys)
"""

_CREATE_FEATURE_ARTIFACT_VIEW = f"""
CREATE OR REPLACE VIEW codex_feature_artifact_v1 WITH (security_barrier = true) AS
WITH {_RED_KEYS_CTE}
SELECT
    a.feature_id,
    a.artifact_type,
    a.artifact_id,
    a.similarity_score,
    a.created_at
FROM feature_artifacts AS a
JOIN features AS f ON f.id = a.feature_id
WHERE f.project_key IN (SELECT project_key FROM red_keys)
"""

_CREATE_DREAM_RUN_VIEW = """
CREATE OR REPLACE VIEW codex_dream_run_v1 AS
SELECT
    r.id,
    r.run_date,
    r.phase,
    r.model,
    r.status,
    r.phase_dry_run,
    r.duration_s,
    r.cost_usd,
    r.input_tokens,
    r.output_tokens,
    r.api_calls,
    r.tool_calls,
    r.error_message,
    r.created_at
FROM dream_runs AS r
"""

_CREATE_DREAM_PROMOTION_VIEW = """
CREATE OR REPLACE VIEW codex_dream_promotion_v1 AS
SELECT
    p.id,
    p.dream_run_id,
    p.source_learning_id,
    p.target_type,
    p.target_adr_id,
    p.target_runbook_id,
    p.cosine_observed,
    p.skipped_reason,
    p.created_at
FROM dream_promotions AS p
"""

_CREATE_TICKET_EXTRACTION_PROPOSAL_VIEW = f"""
CREATE OR REPLACE VIEW codex_ticket_extraction_proposal_v1 WITH (security_barrier = true) AS
WITH {_RED_KEYS_CTE}
SELECT
    p.id,
    p.ticket_id,
    p.target_type,
    p.target_project,
    p.payload,
    p.rationale,
    p.status,
    p.applied_entity_id,
    p.created_at,
    p.applied_at
FROM ticket_extraction_proposals AS p
JOIN tickets AS t ON t.id = p.ticket_id
WHERE t.from_project IN (SELECT project_key FROM red_keys)
   OR t.to_project IN (SELECT project_key FROM red_keys)
"""

_CREATE_ROADMAP_CURATION_PROPOSAL_VIEW = f"""
CREATE OR REPLACE VIEW codex_roadmap_curation_proposal_v1 WITH (security_barrier = true) AS
WITH {_RED_KEYS_CTE}
SELECT
    p.id,
    p.op,
    p.feature_id,
    p.payload,
    p.rationale,
    p.status,
    p.apply_log,
    p.created_at,
    p.applied_at
FROM roadmap_curation_proposals AS p
JOIN features AS f ON f.id = p.feature_id
WHERE f.project_key IN (SELECT project_key FROM red_keys)
"""

_CREATE_CONSOLIDATION_LOG_VIEW = """
CREATE OR REPLACE VIEW codex_consolidation_log_v1 AS
SELECT
    c.id,
    c.source_id,
    c.target_id,
    c.entity_type,
    c.similarity,
    c.action,
    c.created_at
FROM consolidation_log AS c
"""

# Migration 024 omitted ``indexed_plans`` from its red-key discovery CTE even
# though the view exposes plans. Re-state the versioned contract with the
# corrected key discovery and CREATE OR REPLACE so its OID and dependants stay
# intact. Historical migration 024 remains immutable.
_BRAIN_RED_KEYS_CTE = """
red_base AS (
    SELECT project_key FROM project_contexts WHERE project_group = 'red'
),
red_keys AS (
    SELECT project_key FROM red_base
    UNION
    SELECT candidate.project_key
    FROM (
        SELECT project_key FROM decisions
        UNION ALL SELECT project_key FROM learnings
        UNION ALL SELECT project_key FROM snippets
        UNION ALL SELECT project_key FROM runbooks
        UNION ALL SELECT project_key FROM adrs
        UNION ALL SELECT project_key FROM indexed_plans
    ) AS candidate
    WHERE candidate.project_key IS NOT NULL
      AND split_part(candidate.project_key, ':', 1) <> candidate.project_key
      AND split_part(candidate.project_key, ':', 1) IN (
          SELECT project_key FROM red_base
      )
)
"""

_CREATE_BRAIN_ENTITY_VIEW = f"""
CREATE OR REPLACE VIEW codex_brain_entity_v1 WITH (security_barrier = true) AS
WITH {_BRAIN_RED_KEYS_CTE}
SELECT
    d.id,
    'decision'::text AS type,
    d.title::text AS title,
    d.status::text AS status,
    d.freshness_status::text AS freshness_status,
    concat_ws(
        E'\\n\\n',
        d.description,
        '## Raisonnement' || E'\\n' || d.reasoning,
        CASE WHEN cardinality(d.alternatives) > 0
             THEN '## Alternatives' || E'\\n- ' || array_to_string(d.alternatives, E'\\n- ')
        END,
        CASE WHEN d.consequences IS NOT NULL AND d.consequences <> ''
             THEN '## Conséquences' || E'\\n' || d.consequences
        END
    ) AS content,
    d.project_key::text AS project_key,
    d.updated_at,
    d.superseded_by,
    d.merged_into
FROM decisions AS d
WHERE d.project_key IN (SELECT project_key FROM red_keys)

UNION ALL
SELECT
    l.id,
    'learning'::text,
    l.topic::text,
    NULL::text,
    l.freshness_status::text,
    concat_ws(
        E'\\n\\n',
        l.insight,
        CASE WHEN l.source IS NOT NULL AND l.source <> ''
             THEN 'Source: ' || l.source
        END
    ),
    l.project_key::text,
    l.updated_at,
    NULL::uuid,
    l.merged_into
FROM learnings AS l
WHERE l.project_key IN (SELECT project_key FROM red_keys)

UNION ALL
SELECT
    s.id,
    'snippet'::text,
    s.title::text,
    NULL::text,
    s.freshness_status::text,
    concat_ws(
        E'\\n\\n',
        s.intention,
        '```' || s.language || E'\\n' || s.code || E'\\n```',
        CASE WHEN s.usage_example IS NOT NULL AND s.usage_example <> ''
             THEN '## Usage' || E'\\n' || s.usage_example
        END,
        CASE WHEN s.gotchas IS NOT NULL AND s.gotchas <> ''
             THEN '## Gotchas' || E'\\n' || s.gotchas
        END
    ),
    s.project_key::text,
    s.updated_at,
    NULL::uuid,
    s.merged_into
FROM snippets AS s
WHERE s.project_key IN (SELECT project_key FROM red_keys)

UNION ALL
SELECT
    r.id,
    'runbook'::text,
    r.title::text,
    NULL::text,
    r.freshness_status::text,
    concat_ws(
        E'\\n\\n',
        r.description,
        'Trigger: ' || r.trigger,
        CASE WHEN cardinality(r.prerequisites) > 0
             THEN '## Prérequis' || E'\\n- ' || array_to_string(r.prerequisites, E'\\n- ')
        END,
        '## Steps' || E'\\n' || r.steps::text,
        CASE WHEN r.rollback_steps::text NOT IN ('[]', 'null')
             THEN '## Rollback' || E'\\n' || r.rollback_steps::text
        END
    ),
    r.project_key::text,
    r.updated_at,
    NULL::uuid,
    r.merged_into
FROM runbooks AS r
WHERE r.project_key IN (SELECT project_key FROM red_keys)

UNION ALL
SELECT
    a.id,
    'adr'::text,
    a.title::text,
    a.status::text,
    a.freshness_status::text,
    concat_ws(
        E'\\n\\n',
        '## Contexte' || E'\\n' || a.context,
        '## Décision' || E'\\n' || a.decision,
        '## Conséquences' || E'\\n' || a.consequences,
        CASE WHEN a.alternatives_considered::text NOT IN ('[]', 'null')
             THEN '## Alternatives' || E'\\n' || a.alternatives_considered::text
        END
    ),
    a.project_key::text,
    a.updated_at,
    successor.id,
    a.merged_into
FROM adrs AS a
LEFT JOIN adrs AS successor
    ON successor.project_key = a.project_key
   AND successor.number = a.superseded_by
WHERE a.project_key IN (SELECT project_key FROM red_keys)

UNION ALL
SELECT
    p.id,
    'plan'::text,
    p.title::text,
    p.status::text,
    p.freshness_status::text,
    p.content,
    p.project_key::text,
    p.updated_at,
    NULL::uuid,
    NULL::uuid
FROM indexed_plans AS p
WHERE p.project_key IN (SELECT project_key FROM red_keys)
"""

_RESTORE_BRAIN_ENTITY_VIEW_024 = _CREATE_BRAIN_ENTITY_VIEW.replace(
    "        UNION ALL SELECT project_key FROM indexed_plans\n",
    "",
).replace(
    "CREATE OR REPLACE VIEW codex_brain_entity_v1 WITH (security_barrier = true) AS",
    "CREATE OR REPLACE VIEW codex_brain_entity_v1 AS",
)

_CREATE_FEATURE_ARTIFACT_FENCE_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_live_feature_artifact_target()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    target_status text;
    target_merged_into uuid;
BEGIN
    SELECT status, merged_into
      INTO target_status, target_merged_into
      FROM features
     WHERE id = NEW.feature_id
     FOR SHARE;

    IF NOT FOUND THEN
        RETURN NEW;
    END IF;
    IF target_status = 'archived' OR target_merged_into IS NOT NULL THEN
        RAISE EXCEPTION 'cannot link an artifact to non-live feature %', NEW.feature_id
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$$
"""

_CREATE_FEATURE_ARTIFACT_FENCE_TRIGGER = """
CREATE TRIGGER trg_feature_artifact_live_target
BEFORE INSERT OR UPDATE OF feature_id ON feature_artifacts
FOR EACH ROW
EXECUTE FUNCTION enforce_live_feature_artifact_target()
"""

_CREATE_TICKET_PARTICIPANT_FENCE_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_immutable_ticket_participants()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.from_project IS DISTINCT FROM OLD.from_project
       OR NEW.to_project IS DISTINCT FROM OLD.to_project THEN
        RAISE EXCEPTION 'ticket participants are immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$$
"""

_CREATE_TICKET_PARTICIPANT_FENCE_TRIGGER = """
CREATE TRIGGER trg_ticket_participants_immutable
BEFORE UPDATE OF from_project, to_project ON tickets
FOR EACH ROW
EXECUTE FUNCTION enforce_immutable_ticket_participants()
"""


def upgrade() -> None:
    op.execute(_CREATE_BRAIN_ENTITY_VIEW)
    op.execute(_CREATE_TICKET_VIEW)
    op.execute(_CREATE_TICKET_MESSAGE_VIEW)
    op.execute(_CREATE_FEATURE_VIEW)
    op.execute(_CREATE_FEATURE_ARTIFACT_VIEW)
    op.execute(_CREATE_DREAM_RUN_VIEW)
    op.execute(_CREATE_DREAM_PROMOTION_VIEW)
    op.execute(_CREATE_TICKET_EXTRACTION_PROPOSAL_VIEW)
    op.execute(_CREATE_ROADMAP_CURATION_PROPOSAL_VIEW)
    op.execute(_CREATE_CONSOLIDATION_LOG_VIEW)
    op.execute(_CREATE_FEATURE_ARTIFACT_FENCE_FUNCTION)
    op.execute(_CREATE_FEATURE_ARTIFACT_FENCE_TRIGGER)
    op.execute(_CREATE_TICKET_PARTICIPANT_FENCE_FUNCTION)
    op.execute(_CREATE_TICKET_PARTICIPANT_FENCE_TRIGGER)

    # Re-assert least privilege after all views exist. REVOKE also clears the
    # migration-024 view grant, so grant the complete Codex contract back.
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM codex_ro")
    op.execute("GRANT USAGE ON SCHEMA public TO codex_ro")
    op.execute("GRANT SELECT ON codex_brain_entity_v1 TO codex_ro")
    op.execute("GRANT SELECT ON codex_ticket_v1 TO codex_ro")
    op.execute("GRANT SELECT ON codex_ticket_message_v1 TO codex_ro")
    op.execute("GRANT SELECT ON codex_feature_v1 TO codex_ro")
    op.execute("GRANT SELECT ON codex_feature_artifact_v1 TO codex_ro")
    op.execute("GRANT SELECT ON codex_dream_run_v1 TO codex_ro")
    op.execute("GRANT SELECT ON codex_dream_promotion_v1 TO codex_ro")
    op.execute("GRANT SELECT ON codex_ticket_extraction_proposal_v1 TO codex_ro")
    op.execute("GRANT SELECT ON codex_roadmap_curation_proposal_v1 TO codex_ro")
    op.execute("GRANT SELECT ON codex_consolidation_log_v1 TO codex_ro")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_ticket_participants_immutable ON tickets")
    op.execute("DROP FUNCTION IF EXISTS enforce_immutable_ticket_participants()")
    op.execute("DROP TRIGGER IF EXISTS trg_feature_artifact_live_target ON feature_artifacts")
    op.execute("DROP FUNCTION IF EXISTS enforce_live_feature_artifact_target()")
    op.execute("DROP VIEW IF EXISTS codex_consolidation_log_v1")
    op.execute("DROP VIEW IF EXISTS codex_roadmap_curation_proposal_v1")
    op.execute("DROP VIEW IF EXISTS codex_ticket_extraction_proposal_v1")
    op.execute("DROP VIEW IF EXISTS codex_dream_promotion_v1")
    op.execute("DROP VIEW IF EXISTS codex_dream_run_v1")
    op.execute("DROP VIEW IF EXISTS codex_feature_artifact_v1")
    op.execute("DROP VIEW IF EXISTS codex_feature_v1")
    op.execute("DROP VIEW IF EXISTS codex_ticket_message_v1")
    op.execute("DROP VIEW IF EXISTS codex_ticket_v1")
    op.execute(_RESTORE_BRAIN_ENTITY_VIEW_024)
    op.execute("ALTER VIEW codex_brain_entity_v1 RESET (security_barrier)")
    op.execute("GRANT SELECT ON codex_brain_entity_v1 TO codex_ro")
