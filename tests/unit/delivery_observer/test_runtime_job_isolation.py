"""A failure confined to one observation job must not end the observer process.

On 2026-10-02 one job whose attempt could not be persisted exited the whole
process on every pass, and the systemd start limit turned that into an hour of
observation blackout (ticket b78b5144).
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from brain_v42.delivery_config import DeliverySettings
from brain_v42.delivery_observer.ownership import ObserverOwnershipLost
from brain_v42.delivery_observer.runtime import DeliveryObserverRuntime
from brain_v42.delivery_observer.transport import ProviderError
from brain_v42.models.delivery import DeliveryError, PullRequestEvidence

SECRET = "message-that-must-never-reach-the-journal"


def make_job(name: str) -> SimpleNamespace:
    now = datetime.now(UTC)
    subject = uuid4()
    return SimpleNamespace(
        identity=f"artifact_binding:{subject}",
        name=name,
        binding=SimpleNamespace(id=subject),
        version=1,
        failure_count=0,
        captured_at=now,
        due_at=now,
        contract=SimpleNamespace(contract_revision=1),
        previous=None,
    )


class FakeOwner:
    def __init__(self) -> None:
        self.owned = True
        self.lost = asyncio.Event()

    async def acquire(self) -> bool:
        return True

    async def release(self) -> None:
        pass

    async def heartbeat(self) -> None:
        pass

    @asynccontextmanager
    async def transaction(self):
        yield object()


class FakeQueue:
    def __init__(self, jobs, *, undecodable=()):
        self.jobs = list(jobs)
        self.undecodable = list(undecodable)

    async def due(self, session, *, limit, exclude, project_key=None, on_undecodable=None):
        for identity, error_type in self.undecodable:
            on_undecodable(identity, error_type)
        self.undecodable = []
        return tuple(j for j in self.jobs if j.identity not in exclude)[:limit]

    async def schedule_after(self, session, job, **_):
        return True


class FakeEvidenceRepo:
    def __init__(self):
        self.published = []

    async def publish_observation(self, session, binding_id, version, evidence, started, finished):
        self.published.append(binding_id)

    async def record_observation_error(self, session, binding_id, version, code, started, finished):
        self.published.append(binding_id)


class FakeClient:
    """The poisoned job fails at the provider, so it takes the record-error path."""

    def __init__(self, jobs_failing_at_provider):
        self.failing = {j.binding.id for j in jobs_failing_at_provider}

    async def collect(self, binding, contract, previous=None):
        if binding.id in self.failing:
            raise ProviderError("provider_unavailable")
        return MagicMock(spec=PullRequestEvidence)


def runtime_for(jobs, repo, *, undecodable=(), failing_jobs=()):
    return DeliveryObserverRuntime(
        settings=DeliverySettings(enabled=True),
        owner=FakeOwner(),
        client=FakeClient(failing_jobs),
        evidence_repository=repo,
        queue=FakeQueue(jobs, undecodable=undecodable),
    )


def stderr_lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().err.splitlines()]


async def test_unexpected_delivery_error_on_one_job_does_not_end_the_pass(capsys):
    poisoned, healthy = make_job("poisoned"), make_job("healthy")

    class Repo(FakeEvidenceRepo):
        async def record_observation_error(self, session, binding_id, *args):
            raise DeliveryError("some_unforeseen_code", SECRET)

    runtime = runtime_for([poisoned, healthy], Repo(), failing_jobs=[poisoned])

    result = await runtime.run_once()

    assert (result.collected, result.failed, result.deferred) == (1, 1, 0)
    assert result.exit_code == 1
    lines = stderr_lines(capsys)
    assert [(x["subject"], x["error_code"], x["diagnostic"]) for x in lines] == [
        (poisoned.identity, "observer_persist_error", "DeliveryError")
    ]


async def test_failing_job_diagnostic_never_carries_the_exception_message(capsys):
    poisoned = make_job("poisoned")

    class Repo(FakeEvidenceRepo):
        async def record_observation_error(self, session, binding_id, *args):
            raise DeliveryError("some_unforeseen_code", SECRET)

    await runtime_for([poisoned], Repo(), failing_jobs=[poisoned]).run_once()

    err = capsys.readouterr().err
    assert SECRET not in err and "some_unforeseen_code" not in err


@pytest.mark.parametrize(
    "code",
    [
        "binding_conflict",
        "binding_superseded",
        "repository_context_conflict",
        "repository_context_superseded",
    ],
)
async def test_known_conflict_codes_still_defer_the_job(code, capsys):
    job = make_job("racing")

    class Repo(FakeEvidenceRepo):
        async def publish_observation(self, session, *args):
            raise DeliveryError(code, "raced")

    result = await runtime_for([job], Repo()).run_once()

    assert (result.collected, result.failed, result.deferred) == (0, 0, 1)
    assert result.exit_code == 0
    assert capsys.readouterr().err == ""


async def test_ownership_loss_while_persisting_still_exits_with_code_two():
    job = make_job("owned")

    class Repo(FakeEvidenceRepo):
        async def publish_observation(self, session, *args):
            raise ObserverOwnershipLost

    result = await runtime_for([job], Repo()).run_once()

    assert result.exit_code == 2


async def test_undecodable_row_is_reported_and_the_other_jobs_still_run(capsys):
    healthy = make_job("healthy")
    bad = f"artifact_binding:{uuid4()}"
    repo = FakeEvidenceRepo()

    result = await runtime_for([healthy], repo, undecodable=[(bad, "ValidationError")]).run_once()

    assert (result.collected, result.failed) == (1, 1)
    assert result.exit_code == 1
    assert repo.published == [healthy.binding.id]
    assert [(x["subject"], x["error_code"], x["diagnostic"]) for x in stderr_lines(capsys)] == [
        (bad, "observer_undecodable_row", "ValidationError")
    ]


async def test_pass_continues_past_a_batch_made_only_of_undecodable_rows():
    healthy = make_job("healthy")

    class Queue(FakeQueue):
        calls = 0

        async def due(self, session, *, limit, exclude, project_key=None, on_undecodable=None):
            self.calls += 1
            if self.calls == 1:
                on_undecodable(f"artifact_binding:{uuid4()}", "KeyError")
                return ()
            return await super().due(
                session,
                limit=limit,
                exclude=exclude,
                project_key=project_key,
                on_undecodable=on_undecodable,
            )

    runtime = runtime_for([healthy], FakeEvidenceRepo())
    runtime.queue = Queue([healthy])

    result = await runtime.run_once()

    assert (result.collected, result.failed) == (1, 1)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self.rows


class _Session:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, query):
        return _Rows(self.rows)

    async def scalar(self, query):
        return 0


def _row(contract):
    now = datetime.now(UTC)
    return {
        "kind": "repository_context",
        "subject_id": uuid4(),
        "ticket_id": uuid4(),
        "attempt": 1,
        "context_set_digest": "d",
        "contract": contract,
        "project_key": "p",
        "version": 1,
        "due_at": now,
        "binding_row": None,
        "previous": None,
        "last_success_at": None,
        "captured_at": now,
    }


async def test_queue_skips_an_undecodable_row_and_names_it(monkeypatch):
    from brain_v42.repositories import pg_delivery_queue

    def decode(payload):
        if payload == "poison":
            raise ValueError(SECRET)
        return SimpleNamespace(contract_revision=1)

    monkeypatch.setattr(pg_delivery_queue, "_contract_from_json", decode)
    bad, good = _row("poison"), _row("fine")
    reported = []

    jobs = await pg_delivery_queue.PgDeliveryQueue().due(
        _Session([bad, good]),
        on_undecodable=lambda identity, error_type: reported.append((identity, error_type)),
    )

    assert [j.subject_id for j in jobs] == [good["subject_id"]]
    assert reported == [(f"repository_context:{bad['subject_id']}", "ValueError")]


async def test_queue_without_a_listener_still_raises_on_an_undecodable_row(monkeypatch):
    from brain_v42.repositories import pg_delivery_queue

    def decode(payload):
        raise ValueError("poison")

    monkeypatch.setattr(pg_delivery_queue, "_contract_from_json", decode)

    with pytest.raises(ValueError):
        await pg_delivery_queue.PgDeliveryQueue().due(_Session([_row("x")]))
