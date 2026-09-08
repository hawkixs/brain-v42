"""brain_v42.db — SQLAlchemy async database layer.

Re-exports engine, session factory, table definitions and metadata.
Also re-exports Neo4j driver factory helpers (optional graph index).
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from brain_v42.db.engine import dispose_engine, get_engine, get_session_factory
from brain_v42.db.tables import (
    METADATA,
    adrs,
    brain_entities,
    brain_session_artifacts,
    brain_sessions,
    decisions,
    entity_relations,
    graph_outbox,
    graph_projection_leases,
    learnings,
    project_aliases,
    project_contexts,
    projects,
    runbooks,
    snippets,
)

if TYPE_CHECKING:
    from brain_v42.db.neo4j import close_neo4j_driver, create_neo4j_driver, neo4j_healthcheck


def __getattr__(name: str) -> Any:
    """Keep graph helpers compatible without importing Neo4j for PostgreSQL users."""
    if name in {"close_neo4j_driver", "create_neo4j_driver", "neo4j_healthcheck"}:
        value = getattr(import_module("brain_v42.db.neo4j"), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "get_engine",
    "get_session_factory",
    "dispose_engine",
    "METADATA",
    "projects",
    "project_aliases",
    "brain_entities",
    "entity_relations",
    "graph_outbox",
    "graph_projection_leases",
    "decisions",
    "learnings",
    "snippets",
    "runbooks",
    "adrs",
    "brain_sessions",
    "brain_session_artifacts",
    "project_contexts",
    "create_neo4j_driver",
    "close_neo4j_driver",
    "neo4j_healthcheck",
]
