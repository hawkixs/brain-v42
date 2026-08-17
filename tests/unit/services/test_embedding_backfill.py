"""Bounded and idempotent processing of the durable embedding backlog."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.embedding_backfill import (
    BackfillReport,
    EmbeddingBackfillJob,
    EntityBackfillReport,
    persist_backfill_metrics,
)
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable

VECTOR = [0.1] * 1536


def _row(entity_type: str) -> dict:
    now = datetime.now(UTC)
    common = {
        "id": uuid.uuid4(),
        "project_key": "brain-v42",
        "created_at": now,
        "updated_at": now,
        "embedding": None,
    }
    fields = {
        "decision": {"title": "T", "description": "D", "reasoning": "R"},
        "learning": {"topic": "T", "insight": "I"},
        "snippet": {"title": "S", "intention": "I"},
        "runbook": {"title": "R", "description": "D", "trigger": "T"},
        "adr": {"title": "A", "context": "C", "decision": "D"},
    }
    return {**common, **fields[entity_type]}


class _Repo:
    def __init__(self, rows: list[dict], *, cas: str = "stored") -> None:
        self.rows = rows
        self.cas = cas
        self.list_calls = 0
        self.stats_calls = 0
        self.set_calls: list[tuple] = []

    async def list_embedding_backlog(self, *, limit, project_key=None):
        self.list_calls += 1
        pending = [
            row
            for row in self.rows
            if row["embedding"] is None
            and (project_key is None or row["project_key"] == project_key)
        ]
        return pending[:limit]

    async def embedding_backlog_stats(self, *, project_key=None):
        self.stats_calls += 1
        pending = [
            row
            for row in self.rows
            if row["embedding"] is None
            and (project_key is None or row["project_key"] == project_key)
        ]
        return SimpleNamespace(count=len(pending), oldest_created_at=None)

    async def set_embedding_if_current(self, entity_id, embedding, *, expected_updated_at):
        self.set_calls.append((entity_id, embedding, expected_updated_at))
        row = next((item for item in self.rows if item["id"] == entity_id), None)
        if self.cas != "stored" or row is None or row["updated_at"] != expected_updated_at:
            return None
        row["embedding"] = embedding
        return dict(row)

    async def get_by_id(self, entity_id):
        if self.cas == "missing":
            return None
        return next((item for item in self.rows if item["id"] == entity_id), None)


class _ScalarResult:
    def __init__(self, value: bool) -> None:
        self._value = value

    def scalar_one(self) -> bool:
        return self._value


class _LockSession:
    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired
        self.execute_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement):
        self.execute_count += 1
        return _ScalarResult(self.acquired if self.execute_count == 1 else True)


class _SessionFactory:
    def __init__(self, acquired: bool = True) -> None:
        self.acquired = acquired
        self.sessions: list[_LockSession] = []

    def __call__(self):
        session = _LockSession(self.acquired)
        self.sessions.append(session)
        return session


async def test_backfill_stores_once_and_second_run_is_idempotent() -> None:
    row = _row("decision")
    repo = _Repo([row])
    embedding_svc = MagicMock()
    embedding_svc.embed_texts = AsyncMock(return_value=[VECTOR])
    feature_linker = MagicMock()
    feature_linker.link_artifact = AsyncMock()
    auto_linker = MagicMock()
    auto_linker.auto_link = AsyncMock()
    job = EmbeddingBackfillJob(
        session_factory=_SessionFactory(),
        repos={"decision": repo},
        embedding_svc=embedding_svc,
        feature_linker=feature_linker,
        auto_linker=auto_linker,
    )

    first = await job.run(entity_types=["decision"], batch_size=5, limit=10)
    second = await job.run(entity_types=["decision"], batch_size=5, limit=10)

    assert first.by_entity_type["decision"].attempted == 1
    assert first.by_entity_type["decision"].stored == 1
    assert second.by_entity_type["decision"].attempted == 0
    embedding_svc.embed_texts.assert_awaited_once_with(["T D R"])
    assert repo.set_calls == [(row["id"], VECTOR, row["updated_at"])]
    feature_linker.link_artifact.assert_awaited_once()
    auto_linker.auto_link.assert_awaited_once()


async def test_dry_run_lists_pending_without_lock_embedding_or_write() -> None:
    repo = _Repo([_row("learning") for _ in range(3)])
    session_factory = MagicMock(side_effect=AssertionError("dry-run must not lock"))
    embedding_svc = MagicMock()
    embedding_svc.embed_texts = AsyncMock()
    job = EmbeddingBackfillJob(
        session_factory=session_factory,
        repos={"learning": repo},
        embedding_svc=embedding_svc,
    )

    report = await job.run(
        entity_types=["learning"],
        limit=1,
        dry_run=True,
        project_key="brain-v42",
    )

    assert report.dry_run is True
    assert report.lock_acquired is None
    assert report.by_entity_type["learning"].pending == 3
    session_factory.assert_not_called()
    embedding_svc.embed_texts.assert_not_awaited()
    assert repo.set_calls == []
    assert repo.list_calls == 0
    assert repo.stats_calls == 1


async def test_lock_contention_skips_all_work() -> None:
    repo = _Repo([_row("snippet")])
    embedding_svc = MagicMock()
    embedding_svc.embed_texts = AsyncMock()
    job = EmbeddingBackfillJob(
        session_factory=_SessionFactory(acquired=False),
        repos={"snippet": repo},
        embedding_svc=embedding_svc,
    )

    report = await job.run(entity_types=["snippet"])

    assert report.lock_acquired is False
    assert repo.list_calls == 0
    embedding_svc.embed_texts.assert_not_awaited()


async def test_embedding_outage_is_bounded_to_one_type_and_processing_continues() -> None:
    decision_repo = _Repo([_row("decision")])
    snippet_repo = _Repo([_row("snippet")])
    embedding_svc = MagicMock()
    embedding_svc.embed_texts = AsyncMock(
        side_effect=[EmbeddingUnavailable("offline", kind="unreachable"), [VECTOR]]
    )
    job = EmbeddingBackfillJob(
        session_factory=_SessionFactory(),
        repos={"decision": decision_repo, "snippet": snippet_repo},
        embedding_svc=embedding_svc,
    )

    report = await job.run(entity_types=["decision", "snippet"], batch_size=5)

    assert report.by_entity_type["decision"].unavailable == 1
    assert report.by_entity_type["snippet"].stored == 1
    assert report.has_failures is True


async def test_compare_and_set_miss_is_classified_stale_or_missing() -> None:
    stale_repo = _Repo([_row("adr")], cas="stale")
    missing_repo = _Repo([_row("runbook")], cas="missing")
    embedding_svc = MagicMock()
    embedding_svc.embed_texts = AsyncMock(side_effect=[[VECTOR], [VECTOR]])
    job = EmbeddingBackfillJob(
        session_factory=_SessionFactory(),
        repos={"adr": stale_repo, "runbook": missing_repo},
        embedding_svc=embedding_svc,
    )

    report = await job.run(entity_types=["adr", "runbook"])

    assert report.by_entity_type["adr"].stale == 1
    assert report.by_entity_type["runbook"].missing == 1


async def test_batch_cardinality_mismatch_fails_without_any_store() -> None:
    repo = _Repo([_row("decision"), _row("decision")])
    embedding_svc = MagicMock()
    embedding_svc.embed_texts = AsyncMock(return_value=[VECTOR])
    job = EmbeddingBackfillJob(
        session_factory=_SessionFactory(),
        repos={"decision": repo},
        embedding_svc=embedding_svc,
    )

    report = await job.run(entity_types=["decision"], batch_size=5)

    assert report.by_entity_type["decision"].failed == 2
    assert repo.set_calls == []


async def test_invalid_vector_dimension_fails_without_store() -> None:
    repo = _Repo([_row("adr")])
    embedding_svc = MagicMock()
    embedding_svc.embed_texts = AsyncMock(return_value=[[0.1, 0.2]])
    job = EmbeddingBackfillJob(
        session_factory=_SessionFactory(),
        repos={"adr": repo},
        embedding_svc=embedding_svc,
    )

    report = await job.run(entity_types=["adr"])

    assert report.by_entity_type["adr"].failed == 1
    assert repo.set_calls == []


@pytest.mark.parametrize(
    ("kind", "invalid_value"),
    [
        ("zero", 0.0),
        ("nan", float("nan")),
        ("positive-infinity", float("inf")),
        ("negative-infinity", float("-inf")),
    ],
)
async def test_non_comparable_vector_fails_closed_without_store(
    kind: str, invalid_value: float
) -> None:
    row = _row("learning")
    repo = _Repo([row])
    embedding_svc = MagicMock()
    vector = [0.0] * 1536 if kind == "zero" else [0.1] * 1536
    if kind != "zero":
        vector[0] = invalid_value
    embedding_svc.embed_texts = AsyncMock(return_value=[vector])
    job = EmbeddingBackfillJob(
        session_factory=_SessionFactory(),
        repos={"learning": repo},
        embedding_svc=embedding_svc,
    )

    report = await job.run(entity_types=["learning"])

    outcome = report.by_entity_type["learning"]
    assert outcome.attempted == 1
    assert outcome.failed == 1
    assert outcome.stored == 0
    assert repo.set_calls == []


@pytest.mark.parametrize("response", [None, [None]])
async def test_malformed_batch_response_is_counted_without_crashing(response) -> None:
    repo = _Repo([_row("decision")])
    embedding_svc = MagicMock()
    embedding_svc.embed_texts = AsyncMock(return_value=response)
    job = EmbeddingBackfillJob(
        session_factory=_SessionFactory(),
        repos={"decision": repo},
        embedding_svc=embedding_svc,
    )

    report = await job.run(entity_types=["decision"])

    assert report.by_entity_type["decision"].failed == 1
    assert repo.set_calls == []


async def test_timeout_has_a_dedicated_outcome() -> None:
    repo = _Repo([_row("learning")])
    embedding_svc = MagicMock()

    async def block(_texts):
        await asyncio.sleep(1)

    embedding_svc.embed_texts = AsyncMock(side_effect=block)
    job = EmbeddingBackfillJob(
        session_factory=_SessionFactory(),
        repos={"learning": repo},
        embedding_svc=embedding_svc,
        timeout_seconds=0.001,
    )

    report = await job.run(entity_types=["learning"])

    assert report.by_entity_type["learning"].timed_out == 1
    assert report.by_entity_type["learning"].failed == 0


async def test_batch_size_and_limit_bound_http_work() -> None:
    repo = _Repo([_row("learning") for _ in range(4)])
    embedding_svc = MagicMock()
    embedding_svc.embed_texts = AsyncMock(side_effect=[[VECTOR, VECTOR], [VECTOR]])
    job = EmbeddingBackfillJob(
        session_factory=_SessionFactory(),
        repos={"learning": repo},
        embedding_svc=embedding_svc,
    )

    report = await job.run(entity_types=["learning"], batch_size=2, limit=3)

    assert report.by_entity_type["learning"].pending == 3
    assert report.by_entity_type["learning"].stored == 3
    assert [len(call.args[0]) for call in embedding_svc.embed_texts.await_args_list] == [2, 1]


async def test_external_cancellation_propagates_and_releases_advisory_lock() -> None:
    repo = _Repo([_row("snippet")])
    embedding_svc = MagicMock()
    embedding_svc.embed_texts = AsyncMock(side_effect=asyncio.CancelledError)
    session_factory = _SessionFactory()
    job = EmbeddingBackfillJob(
        session_factory=session_factory,
        repos={"snippet": repo},
        embedding_svc=embedding_svc,
    )

    with pytest.raises(asyncio.CancelledError):
        await job.run(entity_types=["snippet"])

    assert session_factory.sessions[0].execute_count == 2


async def test_persists_worker_outcomes_for_metrics_collection() -> None:
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session_factory = MagicMock(return_value=session)
    report = BackfillReport(
        by_entity_type={
            "decision": EntityBackfillReport(
                attempted=3,
                stored=1,
                stale=1,
                unavailable=1,
                unavailable_by_kind={"gpu_busy": 1},
                timed_out=1,
                failed=1,
            )
        },
        dry_run=False,
        lock_acquired=True,
    )

    persisted = await persist_backfill_metrics(session_factory, report)

    assert persisted is True
    metric_names = {call.args[1]["metric"] for call in session.execute.await_args_list}
    assert metric_names == {
        "embedding_backfill.attempted",
        "embedding_backfill.stored",
        "embedding_backfill.stale",
        "embedding_backfill.unavailable",
        "embedding_backfill.unavailable.gpu_busy",
        "embedding_backfill.timed_out",
        "embedding_backfill.failed",
    }
    session.commit.assert_awaited_once()
