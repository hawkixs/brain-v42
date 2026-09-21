"""Canonical embedding text shared by request and backfill paths."""

from brain_v42.services.embedding_text import (
    adr_embedding_text,
    decision_embedding_text,
    embedding_text_from_row,
    feature_embedding_text,
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


def test_a_feature_embeds_its_description_and_nothing_else() -> None:
    """`features.embedding` was redefined as embed(description) by the bulk reindex.

    The column never was that: `_create_feature` stored the caller's embedding,
    computed from the originating artifact's text, which no column records. The
    reindex chose `description` because it is the only text reproducible from the
    row alone -- and reproducible is exactly what a drift check needs. Composing
    it anywhere but here would let the checker and the reindex drift apart while
    both stayed green.
    """
    assert feature_embedding_text("description") == "description"


def test_every_entity_type_dispatches_to_its_own_composer() -> None:
    """A fallback `return` cannot be trusted to be right for a type added later.

    The dispatch used to end on an unguarded adr branch, so an unknown type
    silently produced adr text. A feature row carries no `title`, so the bug
    would surface as a KeyError rather than as a wrong vector -- but only by
    luck, and luck is not a guard.
    """
    assert (
        embedding_text_from_row("decision", {"title": "t", "description": "d", "reasoning": "r"})
        == "t d r"
    )
    assert embedding_text_from_row("learning", {"topic": "t", "insight": "i"}) == "t i"
    assert embedding_text_from_row("snippet", {"intention": "i"}) == "i"
    assert (
        embedding_text_from_row("runbook", {"title": "t", "description": "d", "trigger": "g"})
        == "t d g"
    )
    assert (
        embedding_text_from_row("adr", {"title": "t", "context": "c", "decision": "d"}) == "t c d"
    )
    assert embedding_text_from_row("feature", {"description": "d"}) == "d"
