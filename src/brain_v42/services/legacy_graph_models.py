"""Typed, content-light records used by the legacy graph cutover."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, overload
from uuid import UUID

from brain_v42.models.project_key import canonicalize_project_key

_LEGACY_PROJECT_KEY_ALIASES = {
    "brain": "brain-v42",
    "brain_v42": "brain-v42",
    "auto_discord": "auto-discord",
    "datalake_v2": "datalake-v2",
    "edr_hawkixs": "edr-hawkixs",
    "hk_anime_list": "hk-anime-list",
    "hk_infofeed": "hk-infofeed",
    "mrc_rag": "mrc-rag",
    "poc_lyriks_v2": "poc-lyriks-v2",
    "purple_team_lab": "purple-team-lab",
    "red (écosystème parent)": "red",
    "red-alerts (notifications Discord)": "red-alerts",
    "red_daemon": "red-daemon",
    "red_data": "red-data",
    "red-lab (agent runtime)": "red-lab",
    "red_llm": "red-llm",
    "red_ml": "red-ml",
    "red-monitor (source des métriques)": "red-monitor",
    "red-orchestrator (workflow engine)": "red-orchestrator",
    "red_story": "red-story",
    "red_tsdb": "red-tsdb",
    "red_writer": "red-writer",
}

LEGACY_LABEL_TYPES: tuple[tuple[str, str], ...] = (
    ("Project", "project"),
    ("Domain", "domain"),
    ("Decision", "decision"),
    ("Learning", "learning"),
    ("Snippet", "snippet"),
    ("Runbook", "runbook"),
    ("ADR", "adr"),
    ("Feature", "feature"),
    ("Plan", "plan"),
)
CANONICAL_RELATION_TYPES = frozenset(
    {
        "SUPERSEDES",
        "MOTIVATED_BY",
        "IMPLEMENTS",
        "DOCUMENTS",
        "USES",
        "RELATED_TO",
        "CONTAINS",
        "DEPENDS_ON",
        "BELONGS_TO",
        "MERGED_INTO",
        "BELONGS_TO_DOMAIN",
    }
)


@dataclass(frozen=True, slots=True)
class LegacyGraphNode:
    entity_type: str
    entity_key: str
    source_uuid: UUID | None
    project_key: str | None
    scope_kind: str
    display_label: str | None


@dataclass(frozen=True, slots=True)
class LegacyGraphRelation:
    source_type: str
    source_key: str
    target_type: str
    target_key: str
    relation_type: str
    properties: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LegacyGraphSnapshot:
    nodes: tuple[LegacyGraphNode, ...] = ()
    relations: tuple[LegacyGraphRelation, ...] = ()
    status: str = "ok"
    truncated_nodes: bool = False
    truncated_relations: bool = False
    skipped_nodes: int = 0
    skipped_relations: int = 0


@dataclass(frozen=True, slots=True)
class StoredEntity:
    id: UUID
    entity_type: str
    entity_key: str
    revision: int
    lifecycle: str


@overload
def canonicalize_legacy_project_key(value: str) -> str: ...


@overload
def canonicalize_legacy_project_key(value: None) -> None: ...


def canonicalize_legacy_project_key(value: str | None) -> str | None:
    """Normalize every alias known to migration 033 without widening MCP writes."""
    if value is None:
        return None
    raw = value.strip()
    return _LEGACY_PROJECT_KEY_ALIASES.get(
        raw,
        canonicalize_project_key(raw, strict=False),
    )


def canonicalize_legacy_node(node: LegacyGraphNode) -> LegacyGraphNode:
    """Normalize every project-bearing field before persistence or reporting."""

    project_key = (
        canonicalize_legacy_project_key(node.project_key) if node.project_key is not None else None
    )
    entity_key = (
        canonicalize_legacy_project_key(node.entity_key)
        if node.entity_type == "project"
        else node.entity_key
    )
    if node.entity_type == "project":
        project_key = entity_key
    return LegacyGraphNode(
        entity_type=node.entity_type,
        entity_key=entity_key,
        source_uuid=node.source_uuid,
        project_key=project_key,
        scope_kind=node.scope_kind,
        display_label=node.display_label,
    )


def canonicalize_legacy_relation(relation: LegacyGraphRelation) -> LegacyGraphRelation:
    """Normalize Project endpoints carried by a legacy graph relation."""

    source_key = (
        canonicalize_legacy_project_key(relation.source_key)
        if relation.source_type == "project"
        else relation.source_key
    )
    target_key = (
        canonicalize_legacy_project_key(relation.target_key)
        if relation.target_type == "project"
        else relation.target_key
    )
    return LegacyGraphRelation(
        source_type=relation.source_type,
        source_key=source_key,
        target_type=relation.target_type,
        target_key=target_key,
        relation_type=relation.relation_type,
        properties=relation.properties,
    )


def canonicalize_legacy_snapshot(snapshot: LegacyGraphSnapshot) -> LegacyGraphSnapshot:
    """Normalize aliases even when callers construct a snapshot directly."""

    return LegacyGraphSnapshot(
        nodes=tuple(canonicalize_legacy_node(node) for node in snapshot.nodes),
        relations=tuple(canonicalize_legacy_relation(relation) for relation in snapshot.relations),
        status=snapshot.status,
        truncated_nodes=snapshot.truncated_nodes,
        truncated_relations=snapshot.truncated_relations,
        skipped_nodes=snapshot.skipped_nodes,
        skipped_relations=snapshot.skipped_relations,
    )
