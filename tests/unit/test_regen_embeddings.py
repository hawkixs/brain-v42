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
