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
        rows = [
            (identity, error_type, None)
            for identity, error_type in self.undecodable
            if identity not in exclude
        ] + [(job.identity, None, job) for job in self.jobs if job.identity not in exclude]
        decoded = []
        for identity, error_type, job in rows[:limit]:
            if error_type is not None:
                if on_undecodable is not None:
                    on_undecodable(identity, error_type)
            else:
                decoded.append(job)
        return tuple(decoded)

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
        # spec= does not see pydantic fields; the runtime reads this one to pace
        # the next observation, and a real open pull request carries None.
        return MagicMock(spec=PullRequestEvidence, integration_sha=None)


def runtime_for(jobs, repo, *, undecodable=(), failing_jobs=()):
    return DeliveryObserverRuntime(
        settings=DeliverySettings(enabled=True),
        owner=FakeOwner(),
        client=FakeClient(failing_jobs),
        evidence_repository=repo,
        queue=FakeQueue(jobs, undecodable=undecodable),
    )


async def test_heartbeat_written_after_completed_pass_and_persisted_observation(tmp_path):
    heartbeat = tmp_path / "observer.heartbeat"
    job = make_job("healthy")
    runtime = DeliveryObserverRuntime(
        settings=DeliverySettings(enabled=True, heartbeat_path=heartbeat),
        owner=FakeOwner(),
        client=FakeClient([]),
        evidence_repository=FakeEvidenceRepo(),
        queue=FakeQueue([job]),
    )
    result = await runtime.run_once()
    assert result.exit_code == 0
    assert datetime.fromisoformat(heartbeat.read_text()).tzinfo is not None


async def test_heartbeat_refreshes_after_one_persisted_observation(tmp_path):
    heartbeat = tmp_path / "observer.heartbeat"
    runtime = DeliveryObserverRuntime(
        settings=DeliverySettings(enabled=True, heartbeat_path=heartbeat),
        owner=FakeOwner(),
        client=FakeClient([]),
        evidence_repository=FakeEvidenceRepo(),
        queue=FakeQueue([]),
    )
    outcome = await runtime._observe(make_job("healthy"))
    assert outcome.status == "collected"
    assert datetime.fromisoformat(heartbeat.read_text()).tzinfo is not None


async def test_heartbeat_absent_without_configured_path(tmp_path):
    await runtime_for([], FakeEvidenceRepo()).run_once()
    assert list(tmp_path.iterdir()) == []


async def test_run_refreshes_heartbeat_after_first_cycle_then_stops(tmp_path):
    """The long-running loop refreshes the file between cycles. A shutdown requested
    during a cycle skips that cycle's refresh (it may have been cut short), so the
    file is checked when the second cycle starts, before stopping."""
    heartbeat = tmp_path / "observer.heartbeat"
    stop = asyncio.Event()
    runtime = DeliveryObserverRuntime(
        settings=DeliverySettings(enabled=True, heartbeat_path=heartbeat, poll_seconds=1),
        owner=FakeOwner(),
        client=FakeClient([]),
        evidence_repository=FakeEvidenceRepo(),
        queue=FakeQueue([]),
    )
    original = runtime._cycle
    seen_before_second_cycle: list[bool] = []

    async def stop_on_second_cycle(*args, **kwargs):
        if seen_before_second_cycle or heartbeat.exists():
            seen_before_second_cycle.append(heartbeat.exists())
            stop.set()
        return await original(*args, **kwargs)

    runtime._cycle = stop_on_second_cycle
    assert await asyncio.wait_for(runtime.run(stop), timeout=10) == 0
    assert seen_before_second_cycle == [True]
    assert datetime.fromisoformat(heartbeat.read_text()).tzinfo is not None


async def test_heartbeat_not_written_after_aborted_batch(tmp_path, monkeypatch):
    heartbeat = tmp_path / "observer.heartbeat"
    runtime = runtime_for([make_job("healthy")], FakeEvidenceRepo())
    runtime.settings.heartbeat_path = heartbeat
    stop = asyncio.Event()

    async def aborted(jobs, stop_event):
        stop_event.set()
        return None

    monkeypatch.setattr(runtime, "_batch", aborted)
    assert await runtime.run(stop) == 0
    assert not heartbeat.exists()


async def test_heartbeat_write_failure_does_not_change_pass_result(tmp_path):
    heartbeat = tmp_path / "heartbeat-directory"
    heartbeat.mkdir()
    runtime = DeliveryObserverRuntime(
        settings=DeliverySettings(enabled=True, heartbeat_path=heartbeat),
        owner=FakeOwner(),
        client=FakeClient([]),
        evidence_repository=FakeEvidenceRepo(),
        queue=FakeQueue([]),
    )
    result = await runtime.run_once()
    assert result.exit_code == 0


async def test_heartbeat_write_failure_is_reported_only_as_json_on_stderr(tmp_path, capsys):
    heartbeat = tmp_path / "heartbeat-directory"
    heartbeat.mkdir()
    runtime = DeliveryObserverRuntime(
        settings=DeliverySettings(enabled=True, heartbeat_path=heartbeat),
        owner=FakeOwner(),
        client=FakeClient([]),
        evidence_repository=FakeEvidenceRepo(),
        queue=FakeQueue([]),
    )
    runtime._write_heartbeat()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "event": "observer_heartbeat_write_failed",
        "error_type": "IsADirectoryError",
    }


async def test_heartbeat_is_not_written_after_ownership_loss(tmp_path):
    heartbeat = tmp_path / "observer.heartbeat"
    owner = FakeOwner()
    owner.owned = False

    async def cannot_acquire():
        return False

    owner.acquire = cannot_acquire  # type: ignore[method-assign]
    runtime = DeliveryObserverRuntime(
        settings=DeliverySettings(enabled=True, heartbeat_path=heartbeat),
        owner=owner,
        client=FakeClient([]),
        evidence_repository=FakeEvidenceRepo(),
        queue=FakeQueue([]),
    )
    assert (await runtime.run_once()).exit_code == 2
    assert not heartbeat.exists()


async def test_heartbeat_is_not_written_when_cycle_returns_ownership_exit(tmp_path, monkeypatch):
    from brain_v42.delivery_observer.runtime import ObservationRunResult

    heartbeat = tmp_path / "observer.heartbeat"
    runtime = runtime_for([], FakeEvidenceRepo())
    runtime.settings.heartbeat_path = heartbeat

    async def lost_cycle(*args, **kwargs):
        return ObservationRunResult(exit_code=2)

    monkeypatch.setattr(runtime, "_cycle", lost_cycle)
    assert (await runtime.run_once()).exit_code == 2
    assert not heartbeat.exists()


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


async def test_hundred_twenty_old_undecodable_rows_do_not_starve_healthy_work(capsys):
    healthy = make_job("healthy")
    bad = [(f"artifact_binding:{uuid4()}", "ValueError") for _ in range(120)]
    repo = FakeEvidenceRepo()

    result = await runtime_for([healthy], repo, undecodable=bad).run_once()

    assert (result.collected, result.failed) == (1, 120)
    assert repo.published == [healthy.binding.id]
    assert len(stderr_lines(capsys)) == 120


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
        "failure_count": 0,
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


@pytest.mark.parametrize(
    ("failure_fields", "error_type"),
    [
        ({"failure_count": None}, "TypeError"),
        ({"failure_count": "x"}, "ValueError"),
        ({}, "KeyError"),
    ],
    ids=["null", "non_integer", "missing"],
)
async def test_queue_skips_an_invalid_failure_count_and_names_it(
    monkeypatch, failure_fields, error_type
):
    from brain_v42.repositories import pg_delivery_queue

    monkeypatch.setattr(
        pg_delivery_queue,
        "_contract_from_json",
        lambda payload: SimpleNamespace(contract_revision=1),
    )
    bad, good = _row("fine"), _row("fine")
    del bad["failure_count"]
    bad.update(failure_fields)
    good["failure_count"] = 3
    reported = []

    jobs = await pg_delivery_queue.PgDeliveryQueue().due(
        _Session([bad, good]),
        on_undecodable=lambda identity, error_type: reported.append((identity, error_type)),
    )

    assert [j.subject_id for j in jobs] == [good["subject_id"]]
    assert jobs[0].failure_count == 3
    assert reported == [(f"repository_context:{bad['subject_id']}", error_type)]


async def test_queue_without_a_listener_still_raises_on_an_undecodable_row(monkeypatch):
    from brain_v42.repositories import pg_delivery_queue

    def decode(payload):
        raise ValueError("poison")

    monkeypatch.setattr(pg_delivery_queue, "_contract_from_json", decode)

    with pytest.raises(ValueError):
        await pg_delivery_queue.PgDeliveryQueue().due(_Session([_row("x")]))
