"""Read contract for red-codex (S2a): codex_brain_entity_v1 view + codex_ro role.

Cross-repo brick (brain decision 5b705ec7). red-codex consumes brain READ-ONLY,
scoped to the 'red' group. This migration materializes the contract entirely at
the DB layer, so three concerns collapse into one additive object set:

  * anti-corruption / schema-decoupling -> a VERSIONED view (the _v1 suffix is
    part of the contract: a breaking change means a NEW view, never a mutation
    of this one, because red-codex's reader + fixtures bind to this shape);
  * red-scoping -> the project_group='red' filter is baked INTO the view,
    replicating get_keys_by_group('red') (base keys registered in
    project_contexts UNION colon-sub-partitions whose base is a red key —
    see repositories/pg_project_context.py:152). Fail-closed: an empty red set
    yields zero rows, never everything;
  * least-privilege -> role codex_ro gets SELECT on the VIEW ONLY. In PG16 a
    plain view runs with the OWNER's (brain) rights, so codex_ro needs no grant
    on the 6 base tables and structurally cannot read them.

ADR normalization (contract requirement): an ADR's superseded_by is an internal
per-project INTEGER `number`, not a uuid. The view resolves it to the successor
ADR's stable uuid via a (project_key, number) self-join so codex only ever sees
UUIDs (or NULL).

Purely ADDITIVE + READ-ONLY: touches no existing table, column, constraint or
data. Rollback is trivial = DROP VIEW + DROP ROLE.

Password: env-injected (CODEX_RO_PASSWORD), defaulting to a local dev value to
match the brain/brain localhost posture and keep tests/local working. For any
networked exposure of :5433, set CODEX_RO_PASSWORD; the consumer DSN lives in
red-codex's own config, never here. The idempotent guard means a pre-existing
codex_ro keeps its current password (rotate out-of-band with ALTER ROLE).

Revision ID: 024
Revises: 023
Create Date: 2026-06-28
"""

from __future__ import annotations

import os
import re

from alembic import op

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None

# Operator-controlled, local by default. Validated before interpolation into DDL
# (CREATE ROLE cannot bind a password as a parameter) to refuse unsafe values.
_CODEX_RO_PASSWORD = os.environ.get("CODEX_RO_PASSWORD", "codex_ro")

# Resolves the 'red' group key set exactly like
# ProjectContextRepo.get_keys_by_group('red'): base keys tagged in
# project_contexts, UNION colon-sub-partitions (e.g. 'red-lab:agent') whose
# prefix is a red base key. Referenced by every branch of the view.
_RED_KEYS_CTE = """
red_base AS (
    SELECT project_key FROM project_contexts WHERE project_group = 'red'
),
red_keys AS (
    SELECT project_key FROM red_base
    UNION
    SELECT k.project_key
    FROM (
        SELECT project_key FROM decisions
        UNION ALL SELECT project_key FROM learnings
        UNION ALL SELECT project_key FROM snippets
        UNION ALL SELECT project_key FROM runbooks
        UNION ALL SELECT project_key FROM adrs
    ) AS k
    WHERE k.project_key IS NOT NULL
      AND split_part(k.project_key, ':', 1) <> k.project_key
      AND split_part(k.project_key, ':', 1) IN (SELECT project_key FROM red_base)
)
"""

_CREATE_VIEW = f"""
CREATE OR REPLACE VIEW codex_brain_entity_v1 AS
WITH {_RED_KEYS_CTE}
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
FROM decisions d
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
FROM learnings l
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
FROM snippets s
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
FROM runbooks r
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
    sup.id,  -- per-project integer `number` resolved to the successor's uuid
    a.merged_into
FROM adrs a
LEFT JOIN adrs sup
    ON sup.project_key = a.project_key
   AND sup.number = a.superseded_by
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
    NULL::uuid,   -- indexed_plans has no superseded_by
    NULL::uuid    -- indexed_plans has no merged_into
FROM indexed_plans p
WHERE p.project_key IN (SELECT project_key FROM red_keys);
"""


def _validate_password(pw: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{4,100}", pw):
        raise ValueError(
            "CODEX_RO_PASSWORD must be 4-100 chars of [A-Za-z0-9_.-]; refusing "
            "to interpolate an unsafe value into a CREATE ROLE statement"
        )
    return pw


def upgrade() -> None:
    # 1. The versioned read contract (additive; CREATE OR REPLACE = idempotent).
    op.execute(_CREATE_VIEW)

    # 2. Role codex_ro — idempotent (roles are cluster-global; no IF NOT EXISTS).
    pw = _validate_password(_CODEX_RO_PASSWORD)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'codex_ro') THEN
                CREATE ROLE codex_ro LOGIN PASSWORD '{pw}';
            END IF;
        END
        $$;
        """
    )

    # 3. Least-privilege: revoke any base-table access, grant the VIEW only.
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM codex_ro")
    op.execute("GRANT USAGE ON SCHEMA public TO codex_ro")
    op.execute("GRANT SELECT ON codex_brain_entity_v1 TO codex_ro")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS codex_brain_entity_v1")
    # DROP OWNED clears grants this role holds in THIS db so DROP ROLE succeeds;
    # guarded because DROP OWNED/DROP ROLE error if the role is absent. Fully
    # clean only if codex_ro was used in this db alone (it is cluster-global).
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'codex_ro') THEN
                EXECUTE 'DROP OWNED BY codex_ro';
                EXECUTE 'DROP ROLE codex_ro';
            END IF;
        END
        $$;
        """
    )
