"""Published parameter names and compatibility aliases for MCP tools."""

from collections.abc import Mapping
from types import MappingProxyType

PARAMETER_ALIASES: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "brain_get": MappingProxyType(
            {"id": "entity_id", "type": "entity_type", "item_type": "entity_type"}
        ),
        "brain_list": MappingProxyType({"type": "entity_type"}),
        "brain_log_decision": MappingProxyType(
            {"decision": "decision_made", "description": "context"}
        ),
        "brain_learn": MappingProxyType({"title": "topic", "content": "insight"}),
        "brain_save_snippet": MappingProxyType(
            {"description": "intention", "purpose": "intention"}
        ),
        "brain_fact_get": MappingProxyType({"key": "name"}),
        "brain_ticket_reply": MappingProxyType(
            {"message": "body", "from_project": "author_project", "project_key": "author_project"}
        ),
        "brain_ticket_transition": MappingProxyType(
            {
                "note": "message",
                "comment": "message",
                "actor_project": "author_project",
                "project_key": "author_project",
            }
        ),
        "brain_ticket_plan": MappingProxyType({"project_key": "author_project"}),
        "brain_ticket_create": MappingProxyType(
            {"author_project": "from_project", "target_project": "to_project"}
        ),
    }
)

# Entity references and ownership.
VOCABULARY = frozenset(
    "entity_type entity_id item_type source_id target_id old_decision_id decision_id learning_id runbook_id snippet_id adr_id claim_id ticket_id slot_id proposal_id proposal_ids knowledge_ids related_to fields expected_active_claim_ids".split()
    # Project and actor identity.
    + "project_key author_project from_project to_project actor_project issuer_project project_group repository_id client_key expected_client_key owner_key started_by_actor related_projects".split()
    # Free text and knowledge content.
    + "title topic intention description context decision decision_made insight content reasoning consequences alternatives alternatives_considered source trigger steps prerequisites rollback_steps estimated_duration code language dependencies usage_example gotchas body message note corrects_body name reason summary handover next_focus current_focus blocker blockers next_step progress error_context workflow phase domain_name feature target_release status kind stage detail work work_kind action anchors extraction".split()
    # List filters, pagination, and search.
    + "query types tags rel_types depth max_depth limit offset cursor history_limit history_cursor after_seq include_archived summary_only include_related group_by_type min_score confidence min_size max_members_per_cluster threshold max_links max_chars max_age_seconds full".split()
    # Session, slot, and focus lifecycle revisions.
    + "session_id seq expected_focus_revision expected_slot_revision expected_revision expected_workflow_version expected_assessment_id expected_attempt expected_delivery_digest expected_catalog_revision known_catalog_revision known_guide_version allow_focus_shrink nothing_to_capture_reason initiator briefing".split()
    # Delivery contracts, claims, and attestations.
    + "deliverable_key pr_number claim_token epoch ttl_seconds idempotency_key rationale issuer payload emitted_at contract contract_revision since until".split()
    # Dream and claim controls.
    + "dream_run_id source_learning_id source_type freshness_status freshness_source claims".split()
    # Other published controls.
    + "pinned feature_status unpin gitlab_project_path git_workflow test_strategy current_phase languages frameworks databases plan_scan_paths code_style include_retired new_client_key arguments".split()
)
