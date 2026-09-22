"""Canonical text composition for semantic embeddings.

Two families live here. `EmbeddingEntityType` covers the six tables the
backfill and `regen_embeddings.py` rewrite; their text is always recomposable
from the row. `ReproducibleEntityType` adds the three vector tables nothing
rewrites -- `indexed_plans`, `indexed_plan_chunks`, `gitlab_events` -- and its
composer can answer None, because one of them genuinely cannot say what was
embedded from it.

They are shared with the write paths on purpose. A checker that recomposes
text its own way reports drift on a column nothing has touched.
"""

from collections.abc import Mapping
from typing import Any, Literal

from brain_v42.services.plan_chunker import preamble_from_body

EmbeddingEntityType = Literal["decision", "learning", "snippet", "runbook", "adr", "feature"]

VectorOnlyEntityType = Literal["plan", "plan_chunk", "gitlab_event"]

ReproducibleEntityType = EmbeddingEntityType | VectorOnlyEntityType

REPRODUCIBLE_ENTITY_TYPES: tuple[ReproducibleEntityType, ...] = (
    "decision",
    "learning",
    "snippet",
    "runbook",
    "adr",
    "feature",
    "plan",
    "plan_chunk",
    "gitlab_event",
)
"""Every table carrying an `embedding` column. Listing them in one place is
what stops a table from being forgotten by simply being absent."""

PLAN_EMBED_INPUT_MAX_CHARS = 15000
"""Ceiling on a single plan embedding input. The GPU service returns 500 past
roughly 6000 words; 15000 characters is about 2500 and leaves headroom. Shared
rather than re-declared, because a checker that cuts elsewhere reads every long
plan as drift."""

GITLAB_EVENT_EMBED_MAX_CHARS = 2000
"""What `gitlab_ingestor` embeds."""

GITLAB_EVENT_TITLE_MAX_CHARS = 500
"""What `gitlab_ingestor` STORES. The gap between these two numbers is why
more than half of `gitlab_events` cannot be verified: the column is a shorter
truncation than the vector's input, so the row does not contain what was
embedded."""


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


def truncate_plan_embed_input(text: str) -> str:
    """Cut a plan embedding input at the shared ceiling, and nowhere else."""
    if len(text) <= PLAN_EMBED_INPUT_MAX_CHARS:
        return text
    return text[:PLAN_EMBED_INPUT_MAX_CHARS]


def indexed_plan_embedding_text(title: str, summary: str | None, preamble: str) -> str:
    """The parent vector's input: title, then the summary, else the preamble.

    `if summary:` and not `if summary is not None:` -- an empty summary falls
    through to the preamble, which is what the write path does. 188 of the 208
    rows have no summary at all, so the preamble branch is the common case.
    """
    if summary:
        return f"{title}\n\n{summary}"
    if preamble:
        return f"{title}\n\n{preamble}"
    return title


def indexed_plan_chunk_embedding_text(content: str) -> str:
    """A chunk embeds its own content. The column IS the embedded text."""
    return content


def gitlab_event_embedding_text(title: str) -> str | None:
    """None when the stored title cannot be the text that was embedded.

    The ingestor embeds `text[:2000]` and stores `text[:500]`. A row sitting
    exactly at the storage ceiling was truncated -- or is exactly that long,
    which is indistinguishable -- so it is refused rather than guessed.
    Composing from a truncation would report drift on a vector nobody touched.
    """
    if len(title) >= GITLAB_EVENT_TITLE_MAX_CHARS:
        return None
    return title


def reproducible_embedding_text(
    entity_type: ReproducibleEntityType,
    row: Mapping[str, Any],
) -> str | None:
    """Recompose what was embedded from one row, or say it cannot be done.

    One entry point for all nine vector tables. None means the row does not
    contain the text its vector was built from; the caller must count it as
    unverified, never as a match and never as drift.
    """
    if entity_type == "plan":
        return truncate_plan_embed_input(
            indexed_plan_embedding_text(
                row["title"],
                row.get("summary"),
                preamble_from_body(row.get("content") or ""),
            )
        )
    if entity_type == "plan_chunk":
        return truncate_plan_embed_input(indexed_plan_chunk_embedding_text(row["content"]))
    if entity_type == "gitlab_event":
        return gitlab_event_embedding_text(row["title"] or "")
    return embedding_text_from_row(entity_type, row)
