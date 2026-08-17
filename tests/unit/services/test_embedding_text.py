"""Canonical embedding text shared by request and backfill paths."""

from brain_v42.services.embedding_text import (
    adr_embedding_text,
    decision_embedding_text,
    learning_embedding_text,
    runbook_embedding_text,
    snippet_embedding_text,
)


def test_canonical_embedding_texts() -> None:
    assert decision_embedding_text("title", "description", "reasoning") == (
        "title description reasoning"
    )
    assert learning_embedding_text("topic", "insight") == "topic insight"
    assert snippet_embedding_text("intention") == "intention"
    assert runbook_embedding_text("title", "description", "trigger") == (
        "title description trigger"
    )
    assert adr_embedding_text("title", "context", "decision") == "title context decision"
