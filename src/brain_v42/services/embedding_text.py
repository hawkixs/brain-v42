"""Canonical text composition for semantic embeddings."""

from collections.abc import Mapping
from typing import Any, Literal

EmbeddingEntityType = Literal["decision", "learning", "snippet", "runbook", "adr", "feature"]


def decision_embedding_text(title: str, description: str, reasoning: str) -> str:
    return f"{title} {description} {reasoning}"


def learning_embedding_text(topic: str, insight: str) -> str:
    return f"{topic} {insight}"


def snippet_embedding_text(intention: str) -> str:
    return intention


def runbook_embedding_text(title: str, description: str, trigger: str) -> str:
    return f"{title} {description} {trigger}"


def adr_embedding_text(title: str, context: str, decision: str) -> str:
    return f"{title} {context} {decision}"


def feature_embedding_text(description: str) -> str:
    """The text `scripts/regen_embeddings.py` rewrites `features.embedding` from.

    It lives here so the bulk reindex and `check_embedding_model_drift.py` cannot
    compose it differently: a checker that recomposes the text its own way would
    report drift on a column nothing had touched.
    """
    return description


def embedding_text_from_row(
    entity_type: EmbeddingEntityType,
    row: Mapping[str, Any],
) -> str:
    """Build canonical text from one repository backlog row."""
    if entity_type == "decision":
        return decision_embedding_text(row["title"], row["description"], row["reasoning"])
    if entity_type == "learning":
        return learning_embedding_text(row["topic"], row["insight"])
    if entity_type == "snippet":
        return snippet_embedding_text(row["intention"])
    if entity_type == "runbook":
        return runbook_embedding_text(row["title"], row["description"], row["trigger"])
    if entity_type == "adr":
        return adr_embedding_text(row["title"], row["context"], row["decision"])
    if entity_type == "feature":
        return feature_embedding_text(row["description"])
    raise ValueError(f"no canonical embedding text for entity type {entity_type!r}")
