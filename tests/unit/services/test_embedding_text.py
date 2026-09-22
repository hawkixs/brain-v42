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


# ── the three vector tables the sample could not reach ──────────────────
#
# `indexed_plans`, `indexed_plan_chunks` and `gitlab_events` carry 2239 of the
# corpus's embedded rows and no composer could recompose their text, so the
# drift check announced them as a blind spot on every run. A provider switch
# could therefore leave a quarter of the corpus on the old model and still
# report MATCH.
#
# These composers close it, and they are shared with the write path on purpose:
# a checker that recomposes text its own way reports drift on a column nothing
# touched.


class TestIndexedPlanEmbeddingText:
    def test_the_summary_wins_when_there_is_one(self) -> None:
        from brain_v42.services.embedding_text import indexed_plan_embedding_text

        assert (
            indexed_plan_embedding_text("Title", "A summary", "Some preamble")
            == "Title\n\nA summary"
        )

    def test_the_preamble_is_used_when_there_is_no_summary(self) -> None:
        from brain_v42.services.embedding_text import indexed_plan_embedding_text

        assert (
            indexed_plan_embedding_text("Title", None, "Some preamble") == "Title\n\nSome preamble"
        )

    def test_the_title_stands_alone_when_there_is_neither(self) -> None:
        """188 of 208 rows have no summary, so this is the common case, not the edge."""
        from brain_v42.services.embedding_text import indexed_plan_embedding_text

        assert indexed_plan_embedding_text("Title", None, "") == "Title"

    def test_an_empty_summary_falls_through_to_the_preamble(self) -> None:
        """`if summary:` and `if summary is not None:` differ here, and the write
        path uses the first. Pinning it is what keeps the two identical."""
        from brain_v42.services.embedding_text import indexed_plan_embedding_text

        assert indexed_plan_embedding_text("Title", "", "Preamble") == "Title\n\nPreamble"


class TestIndexedPlanChunkEmbeddingText:
    def test_a_chunk_embeds_its_content_verbatim(self) -> None:
        """1792 rows, every one reproducible: the column IS the embedded text."""
        from brain_v42.services.embedding_text import indexed_plan_chunk_embedding_text

        assert indexed_plan_chunk_embedding_text("## Section\n\nBody.") == "## Section\n\nBody."


class TestGitlabEventEmbeddingText:
    def test_a_short_event_reproduces_its_text(self) -> None:
        from brain_v42.services.embedding_text import gitlab_event_embedding_text

        assert gitlab_event_embedding_text("merge request: add a thing") == (
            "merge request: add a thing"
        )

    def test_an_event_at_the_storage_ceiling_cannot_reproduce_anything(self) -> None:
        """The ingestor embeds `text[:2000]` and stores `text[:500]`.

        127 of 239 rows sit at 500 characters, so for more than half the table
        the stored column is not what was embedded. Returning None is the only
        honest answer: composing from a truncation would report drift on a
        vector nobody touched.
        """
        from brain_v42.services.embedding_text import (
            GITLAB_EVENT_TITLE_MAX_CHARS,
            gitlab_event_embedding_text,
        )

        assert gitlab_event_embedding_text("x" * GITLAB_EVENT_TITLE_MAX_CHARS) is None

    def test_one_character_below_the_ceiling_is_still_trusted(self) -> None:
        from brain_v42.services.embedding_text import (
            GITLAB_EVENT_TITLE_MAX_CHARS,
            gitlab_event_embedding_text,
        )

        text = "x" * (GITLAB_EVENT_TITLE_MAX_CHARS - 1)
        assert gitlab_event_embedding_text(text) == text


class TestReproducibleEmbeddingText:
    def test_it_answers_for_all_nine_vector_tables(self) -> None:
        """One entry point, so a table cannot be forgotten by being absent."""
        from brain_v42.services.embedding_text import REPRODUCIBLE_ENTITY_TYPES

        assert set(REPRODUCIBLE_ENTITY_TYPES) == {
            "decision",
            "learning",
            "snippet",
            "runbook",
            "adr",
            "feature",
            "plan",
            "plan_chunk",
            "gitlab_event",
        }

    def test_the_six_original_types_still_route_to_the_same_composer(self) -> None:
        from brain_v42.services.embedding_text import (
            embedding_text_from_row,
            reproducible_embedding_text,
        )

        row = {"topic": "A topic", "insight": "An insight"}

        assert reproducible_embedding_text("learning", row) == embedding_text_from_row(
            "learning", row
        )

    def test_a_truncated_gitlab_row_answers_none_rather_than_a_guess(self) -> None:
        from brain_v42.services.embedding_text import (
            GITLAB_EVENT_TITLE_MAX_CHARS,
            reproducible_embedding_text,
        )

        assert (
            reproducible_embedding_text(
                "gitlab_event", {"title": "x" * GITLAB_EVENT_TITLE_MAX_CHARS}
            )
            is None
        )

    def test_a_plan_row_composes_from_its_stored_columns(self) -> None:
        from brain_v42.services.embedding_text import reproducible_embedding_text

        row = {"title": "Auth Design", "summary": None, "content": "Intro line.\n\n## A\n\nBody."}

        assert reproducible_embedding_text("plan", row) == "Auth Design\n\nIntro line."


class TestPlanEmbedTruncation:
    def test_the_write_path_ceiling_is_shared_not_re_declared(self) -> None:
        """The indexer truncated with a private constant. The checker must cut
        at the same place or every long plan reads as drift."""
        import brain_v42.services.plan_indexer as indexer_module
        from brain_v42.services.embedding_text import PLAN_EMBED_INPUT_MAX_CHARS

        source = indexer_module.__doc__ or ""
        assert PLAN_EMBED_INPUT_MAX_CHARS == 15000
        assert not hasattr(indexer_module, "_EMBED_INPUT_MAX_CHARS"), (
            "the private ceiling must be gone, not shadowed"
        )
        assert source is not None

    def test_truncation_is_a_prefix_and_nothing_else(self) -> None:
        from brain_v42.services.embedding_text import (
            PLAN_EMBED_INPUT_MAX_CHARS,
            truncate_plan_embed_input,
        )

        long_text = "y" * (PLAN_EMBED_INPUT_MAX_CHARS + 10)

        assert truncate_plan_embed_input(long_text) == "y" * PLAN_EMBED_INPUT_MAX_CHARS
        assert truncate_plan_embed_input("short") == "short"
