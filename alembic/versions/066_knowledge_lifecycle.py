"""066 — preserve lifecycle evidence separately from its mutable state cache.

Composite keys prevent evidence from being attached to another entry revision.
History is append-only for brain_app; migration 064's broad default grants are
explicitly revoked before granting the narrower ACLs. Owners retain recovery
rights. CHECKs use the fail-closed NULL rule of 060, and DDL is replayable.

This revision stores evidence only. Assertion-schema validation and run-state
transitions belong to the later pipeline; no decision or replay logic lives here.
"""

from __future__ import annotations

from alembic import op

revision = "066"
down_revision = "065"
branch_labels = None
depends_on = None

_ENTITY_TYPES = "('decisions', 'learnings', 'snippets', 'runbooks', 'adrs')"
_STATES = "('valid', 'contested', 'obsolete', 'refuted', 'unverifiable')"
_PRECISIONS = "('exact', 'first_observed', 'unknown')"
# Frozen rank mapping for ranks_version = 'v1'. A changed mapping needs a migration.
_RANK = (
    "CASE source_kind WHEN 'structure' THEN 7 WHEN 'measured' THEN 6 "
    "WHEN 'config' THEN 5 WHEN 'code' THEN 4 WHEN 'pr' THEN 3 "
    "WHEN 'ticket' THEN 3 WHEN 'decision' THEN 3 WHEN 'propagation' THEN 2 "
    "WHEN 'decay' THEN 1 END"
)

_RUNS = """
CREATE TABLE IF NOT EXISTS public.knowledge_lifecycle_runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    scope JSONB NOT NULL,
    status TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    batch_ids JSONB NOT NULL DEFAULT '[]',
    usage JSONB NOT NULL DEFAULT '{}',
    real_cost NUMERIC,
    counts JSONB NOT NULL DEFAULT '{}',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    invalidated_at TIMESTAMPTZ,
    invalidation_reason TEXT,
    CONSTRAINT knowledge_lifecycle_runs_mode_valid
        CHECK (COALESCE(mode IN ('dry_run', 'apply'), false)),
    CONSTRAINT knowledge_lifecycle_runs_batch_ids_valid
        CHECK (COALESCE(jsonb_typeof(batch_ids) = 'array', false)),
    CONSTRAINT knowledge_lifecycle_runs_invalidation_valid CHECK (COALESCE(
        (invalidated_at IS NULL AND invalidation_reason IS NULL)
        OR (invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL), false))
)
"""

_REQUESTS = """
CREATE TABLE IF NOT EXISTS public.knowledge_lifecycle_requests (
    run_id TEXT NOT NULL REFERENCES public.knowledge_lifecycle_runs (run_id),
    custom_id TEXT NOT NULL,
    manifest_entry JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result_stored_at TIMESTAMPTZ,
    PRIMARY KEY (run_id, custom_id),
    CONSTRAINT knowledge_lifecycle_requests_manifest_valid
        CHECK (COALESCE(jsonb_typeof(manifest_entry) = 'object', false)),
    CONSTRAINT knowledge_lifecycle_requests_status_valid CHECK (COALESCE(
        status IN ('pending', 'succeeded', 'errored', 'expired', 'canceled', 'missing', 'invalid'),
        false))
)
"""

_EXTRACTIONS = f"""
CREATE TABLE IF NOT EXISTS public.knowledge_extractions (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES public.knowledge_lifecycle_runs (run_id),
    entity_type TEXT NOT NULL,
    entity_id UUID NOT NULL,
    entry_digest TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    assertions JSONB NOT NULL,
    CONSTRAINT knowledge_extractions_revision_key
        UNIQUE (entity_type, entity_id, entry_digest, model, prompt_version),
    CONSTRAINT knowledge_extractions_id_entry_key UNIQUE (id, entity_type, entity_id, entry_digest),
    CONSTRAINT knowledge_extractions_entity_type_valid
        CHECK (COALESCE(entity_type IN {_ENTITY_TYPES}, false)),
    CONSTRAINT knowledge_extractions_assertions_valid
        CHECK (COALESCE(jsonb_typeof(assertions) = 'array', false))
)
"""

_EVALUATIONS = f"""
CREATE TABLE IF NOT EXISTS public.knowledge_evaluations (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES public.knowledge_lifecycle_runs (run_id),
    entity_type TEXT NOT NULL,
    entity_id UUID NOT NULL,
    entry_digest TEXT NOT NULL,
    extraction_id BIGINT,
    expected_cache_revision BIGINT NOT NULL,
    rules_version TEXT NOT NULL,
    ranks_version TEXT NOT NULL,
    registry_version TEXT NOT NULL,
    origin_main_sha TEXT NOT NULL,
    observed_day DATE NOT NULL,
    complete BOOLEAN NOT NULL,
    state TEXT NOT NULL,
    reason TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    CONSTRAINT knowledge_evaluations_run_entry_key UNIQUE (run_id, entity_type, entity_id),
    CONSTRAINT knowledge_evaluations_id_entry_key UNIQUE (id, entity_type, entity_id),
    CONSTRAINT knowledge_evaluations_extraction_entry_fkey
        FOREIGN KEY (extraction_id, entity_type, entity_id, entry_digest)
        REFERENCES public.knowledge_extractions (id, entity_type, entity_id, entry_digest),
    CONSTRAINT knowledge_evaluations_entity_type_valid
        CHECK (COALESCE(entity_type IN {_ENTITY_TYPES}, false)),
    CONSTRAINT knowledge_evaluations_state_valid CHECK (COALESCE(state IN {_STATES}, false)),
    CONSTRAINT knowledge_evaluations_revision_valid
        CHECK (COALESCE(expected_cache_revision >= 0, false))
)
"""

_JUSTIFICATIONS = f"""
CREATE TABLE IF NOT EXISTS public.knowledge_justifications (
    id BIGSERIAL PRIMARY KEY,
    evaluation_id BIGINT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id UUID NOT NULL,
    assertion_key TEXT,
    polarity TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_rank INTEGER NOT NULL,
    source_ref TEXT NOT NULL,
    observed_value JSONB NOT NULL,
    valid_from DATE,
    valid_from_precision TEXT NOT NULL,
    CONSTRAINT knowledge_justifications_evaluation_entry_fkey
        FOREIGN KEY (evaluation_id, entity_type, entity_id)
        REFERENCES public.knowledge_evaluations (id, entity_type, entity_id),
    CONSTRAINT knowledge_justifications_observation_key
        UNIQUE NULLS NOT DISTINCT (evaluation_id, assertion_key, source_kind, source_ref),
    CONSTRAINT knowledge_justifications_entity_type_valid
        CHECK (COALESCE(entity_type IN {_ENTITY_TYPES}, false)),
    CONSTRAINT knowledge_justifications_polarity_valid
        CHECK (COALESCE(polarity IN ('supports', 'contradicts', 'ends', 'none'), false)),
    CONSTRAINT knowledge_justifications_rank_valid CHECK (COALESCE(source_rank = {_RANK}, false)),
    CONSTRAINT knowledge_justifications_precision_valid
        CHECK (COALESCE(valid_from_precision IN {_PRECISIONS}, false))
)
"""

_EVENTS = f"""
CREATE TABLE IF NOT EXISTS public.knowledge_lifecycle_events (
    id BIGSERIAL PRIMARY KEY,
    evaluation_id BIGINT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id UUID NOT NULL,
    kind TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    valid_from DATE,
    valid_from_precision TEXT NOT NULL,
    compensates_event_id BIGINT,
    invalidated_run_id TEXT REFERENCES public.knowledge_lifecycle_runs (run_id),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_lifecycle_events_id_entry_key UNIQUE (id, entity_type, entity_id),
    CONSTRAINT knowledge_lifecycle_events_invalidation_entry_key
        UNIQUE (invalidated_run_id, entity_type, entity_id),
    CONSTRAINT knowledge_lifecycle_events_evaluation_entry_fkey
        FOREIGN KEY (evaluation_id, entity_type, entity_id)
        REFERENCES public.knowledge_evaluations (id, entity_type, entity_id),
    CONSTRAINT knowledge_lifecycle_events_compensated_entry_fkey
        FOREIGN KEY (compensates_event_id, entity_type, entity_id)
        REFERENCES public.knowledge_lifecycle_events (id, entity_type, entity_id),
    CONSTRAINT knowledge_lifecycle_events_entity_type_valid
        CHECK (COALESCE(entity_type IN {_ENTITY_TYPES}, false)),
    CONSTRAINT knowledge_lifecycle_events_kind_valid
        CHECK (COALESCE(kind IN ('transition', 'compensation'), false)),
    CONSTRAINT knowledge_lifecycle_events_from_state_valid
        CHECK (COALESCE(from_state IS NULL OR from_state IN {_STATES}, false)),
    CONSTRAINT knowledge_lifecycle_events_to_state_valid
        CHECK (COALESCE(to_state IN {_STATES}, false)),
    CONSTRAINT knowledge_lifecycle_events_precision_valid
        CHECK (COALESCE(valid_from_precision IN {_PRECISIONS}, false)),
    CONSTRAINT knowledge_lifecycle_events_obsolete_date_valid
        CHECK (COALESCE(to_state <> 'obsolete' OR valid_from IS NOT NULL, false)),
    CONSTRAINT knowledge_lifecycle_events_compensation_valid CHECK (COALESCE(
        (kind = 'transition' AND compensates_event_id IS NULL AND invalidated_run_id IS NULL)
        OR (kind = 'compensation' AND compensates_event_id IS NOT NULL
            AND invalidated_run_id IS NOT NULL), false))
)
"""

_STATE = f"""
CREATE TABLE IF NOT EXISTS public.knowledge_lifecycle_state (
    entity_type TEXT NOT NULL,
    entity_id UUID NOT NULL,
    state TEXT NOT NULL,
    reason TEXT NOT NULL,
    since TIMESTAMPTZ NOT NULL,
    entry_content_updated_at TIMESTAMPTZ NOT NULL,
    event_id BIGINT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (entity_type, entity_id),
    CONSTRAINT knowledge_lifecycle_state_event_entry_fkey
        FOREIGN KEY (event_id, entity_type, entity_id)
        REFERENCES public.knowledge_lifecycle_events (id, entity_type, entity_id),
    CONSTRAINT knowledge_lifecycle_state_entity_type_valid
        CHECK (COALESCE(entity_type IN {_ENTITY_TYPES}, false)),
    CONSTRAINT knowledge_lifecycle_state_state_valid CHECK (COALESCE(state IN {_STATES}, false)),
    CONSTRAINT knowledge_lifecycle_state_revision_valid CHECK (COALESCE(revision >= 0, false))
)
"""

_HISTORY = (
    "knowledge_extractions",
    "knowledge_evaluations",
    "knowledge_justifications",
    "knowledge_lifecycle_events",
)
_MUTABLE = (
    "knowledge_lifecycle_runs",
    "knowledge_lifecycle_requests",
    "knowledge_lifecycle_state",
)


def upgrade() -> None:
    for ddl in (_RUNS, _REQUESTS, _EXTRACTIONS, _EVALUATIONS, _JUSTIFICATIONS, _EVENTS, _STATE):
        op.execute(ddl)
    op.execute(
        "CREATE INDEX IF NOT EXISTS knowledge_evaluations_agreement_idx "
        "ON public.knowledge_evaluations (entity_type, entity_id, observed_day, fingerprint)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS knowledge_lifecycle_events_transition_key "
        "ON public.knowledge_lifecycle_events (evaluation_id, kind) WHERE kind = 'transition'"
    )
    for table in (*_HISTORY, *_MUTABLE):
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM brain_app, PUBLIC")
        privileges = "SELECT, INSERT" if table in _HISTORY else "SELECT, INSERT, UPDATE"
        op.execute(f"GRANT {privileges} ON TABLE public.{table} TO brain_app")
    for table in _HISTORY:
        op.execute(f"REVOKE ALL ON SEQUENCE public.{table}_id_seq FROM brain_app, PUBLIC")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE public.{table}_id_seq TO brain_app")


def downgrade() -> None:
    for table in (
        "knowledge_lifecycle_state",
        "knowledge_lifecycle_events",
        "knowledge_justifications",
        "knowledge_evaluations",
        "knowledge_extractions",
        "knowledge_lifecycle_requests",
        "knowledge_lifecycle_runs",
    ):
        op.execute(f"DROP TABLE IF EXISTS public.{table}")
