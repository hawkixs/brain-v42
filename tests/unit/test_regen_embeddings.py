"""CLI contract tests for the bounded embedding regeneration batch size."""

from __future__ import annotations

import sys

import pytest
from scripts import regen_embeddings


@pytest.mark.parametrize("batch_size", ["1", "100"])
def test_parse_args_accepts_batch_size_boundaries(monkeypatch, batch_size):
    monkeypatch.setattr(
        sys,
        "argv",
        ["regen_embeddings.py", "--batch-size", batch_size],
    )
    assert regen_embeddings.parse_args().batch_size == int(batch_size)


@pytest.mark.parametrize("batch_size", ["0", "101"])
def test_parse_args_rejects_batch_size_outside_service_contract(monkeypatch, batch_size):
    monkeypatch.setattr(
        sys,
        "argv",
        ["regen_embeddings.py", "--batch-size", batch_size],
    )
    with pytest.raises(SystemExit) as exc_info:
        regen_embeddings.parse_args()
    assert exc_info.value.code == 2


def test_features_are_reindexable() -> None:
    """A provider switch that leaves `features` behind reopens a closed wound.

    cluster_guard deduplicates roadmap features semantically against stored
    feature vectors, with COSINE_LINK = 0.70. Qodo and codestral-embed put
    vectors in unrelated bases, so stale feature embeddings score near zero
    against any new signal: nothing would ever link, and every CREATING_SIGNAL
    (plan, mr_opened, push, …) would mint a fresh feature instead. That is the
    pseudo-feature flood whose tap has been shut since 2026-08-03.

    Measured 2026-09-21: 920 embedded rows, reachable by no reindex tool.
    """
    assert "features" in regen_embeddings.ENTITY_TYPES
    assert "features" in regen_embeddings.TEXT_FIELDS


def test_features_embed_their_description_alone() -> None:
    """`description` is the only self-consistent definition available.

    Measured on 60 random rows, 2026-09-21: the stored vector matches
    embed(description) on just 3% of them (median similarity 0.81), and
    editing does not explain it -- never-edited rows sit at 0.808. The column
    was never `embed(description)`: `_create_feature` stores the caller's
    embedding, computed from the originating artifact's text, while
    `description` holds a much shorter title. That source text is recorded
    nowhere on the row, so no faithful re-embed exists.

    `description` is what `cluster_guard._absorb` already re-embeds, and the
    only text reproducible from the row alone -- which is what makes the
    column verifiable by check_embedding_model_drift.py from now on.
    """
    assert regen_embeddings.TEXT_FIELDS["features"] == ["description"]


# ── the reindex may not invent its own text ────────────────────────────


def test_every_type_composes_the_canonical_text_and_nothing_else():
    """A reindex that composes its own text writes a second vector population.

    `regen_embeddings.py` carried a parallel field table, and it had already
    drifted: snippets were composed as `title + intention` while the live write
    path, `embedding_text_from_row` and `check_embedding_model_drift.py` all use
    `intention` alone. Reindexing would have redefined 195 rows in silence, and
    the drift check would have reported self-inflicted outliers on them.

    Delegating removes the possibility rather than fixing the instance.
    """
    from scripts.regen_embeddings import CANONICAL_ENTITY_TYPE, ENTITY_TYPES, compose_text

    from brain_v42.services.embedding_text import embedding_text_from_row

    row = {
        "title": "T",
        "description": "D",
        "reasoning": "R",
        "topic": "TO",
        "insight": "I",
        "intention": "IN",
        "trigger": "TR",
        "context": "C",
        "decision": "DE",
    }

    assert set(CANONICAL_ENTITY_TYPE) == set(ENTITY_TYPES), (
        "every reindexed table needs a canonical entity type, or it composes its own text"
    )
    for table in ENTITY_TYPES:
        assert compose_text(table, row) == embedding_text_from_row(
            CANONICAL_ENTITY_TYPE[table], row
        ), f"{table} composes a different text than the write path"


def test_the_selected_columns_are_exactly_what_the_canonical_text_reads():
    """Selecting a column the composer ignores is how the two drifted apart.

    `snippets` selected `title` and no longer uses it. A column list wider than
    the recipe is an invitation to reintroduce it.
    """
    from scripts.regen_embeddings import TEXT_FIELDS

    assert TEXT_FIELDS["snippets"] == ["intention"]
