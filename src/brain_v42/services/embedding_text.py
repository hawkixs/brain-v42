"""Canonical text composition for semantic embeddings."""

from collections.abc import Mapping
from typing import Any, Literal

EmbeddingEntityType = Literal["decision", "learning", "snippet", "runbook", "adr"]


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
    return adr_embedding_text(row["title"], row["context"], row["decision"])
