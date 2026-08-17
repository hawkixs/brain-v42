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
