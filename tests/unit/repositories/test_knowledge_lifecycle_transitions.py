"""Exhaust the run edges and the UTC maintenance-window boundaries."""

from datetime import time
from itertools import product
from unittest.mock import AsyncMock

import pytest

from brain_v42.repositories.pg_knowledge_lifecycle import (
    RunStatus,
    apply_window_open,
    transition_allowed,
)

CHAIN = (
    "created",
    "manifest_frozen",
    "submitting",
    "submitted",
    "results_stored",
    "evaluated",
    "applying",
    "applied",
)
EDGES = set(zip(CHAIN, CHAIN[1:], strict=False)) | {(status, "failed") for status in CHAIN[:-1]}


@pytest.mark.parametrize(("source", "target"), list(product(RunStatus, repeat=2)))
def test_every_normal_edge(source: RunStatus, target: RunStatus) -> None:
    assert transition_allowed(source, target) == ((source, target) in EDGES)


@pytest.mark.parametrize("source", RunStatus)
def test_invalidation_is_only_from_terminal_runs(source: RunStatus) -> None:
    assert transition_allowed(source, RunStatus.INVALIDATED, invalidation=True) == (
        source in {RunStatus.APPLIED, RunStatus.FAILED}
    )


@pytest.mark.parametrize(
    ("instant", "allowed"),
    [
        (time(0, 49, 59), True),
        (time(0, 50), False),
        (time(3, 59, 59), False),
        (time(4), True),
        (time(23, 59, 59), True),
    ],
)
def test_apply_window_boundaries(instant: time, allowed: bool) -> None:
    assert apply_window_open(instant) is allowed


async def test_read_run_raises_typed_not_found_only_for_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.repositories import pg_knowledge_lifecycle as module

    monkeypatch.setattr(module, "_row", AsyncMock(return_value=None))
    with pytest.raises(module.RunNotFound):
        await module.PgKnowledgeLifecycleRepo().read_run(AsyncMock(), "missing")


async def test_read_run_does_not_translate_internal_keyerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain_v42.repositories import pg_knowledge_lifecycle as module

    failure = KeyError("internal field")
    monkeypatch.setattr(module, "_row", AsyncMock(side_effect=failure))
    with pytest.raises(KeyError) as exc:
        await module.PgKnowledgeLifecycleRepo().read_run(AsyncMock(), "r")
    assert exc.value is failure
