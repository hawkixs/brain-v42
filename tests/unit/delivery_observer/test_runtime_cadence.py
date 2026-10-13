"""Observation cadence: what is settled is not polled every minute, what is gone backs off.

Measured on 2026-09-12 (ticket bd1879f6): with no binding at all, the five pinned
context documents were reconfirmed every 60 s, and an integrated binding was polled
every minute until its explicit acceptance. The scheduler read only poll_seconds;
freshness_seconds was read by the evaluator alone. A repository the App cannot see
(provider_not_found / provider_forbidden) retried every 300 s forever, spending the
shared request budget with nothing turning red.

Ruling (coordination, 2026-10-13): an open pull request keeps poll_seconds so a merge
is seen within a minute; an integrated binding and a repository context are
reobserved at max(poll_seconds, freshness_seconds // 2), half the freshness window,
because the loop only picks due work every poll_seconds and evidence refreshed at
exactly freshness_seconds would read stale at the end of every cycle.
"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from brain_v42.delivery_config import DeliverySettings
from brain_v42.delivery_observer.runtime import DeliveryObserverRuntime
from brain_v42.delivery_observer.transport import ProviderError
from brain_v42.models.delivery import PullRequestEvidence, RepositoryContextEvidence
from brain_v42.models.delivery_evaluator import _binding_health_states
from tests.unit.delivery_observer.test_runtime_job_isolation import (
    FakeEvidenceRepo,
    FakeOwner,
    FakeQueue,
    make_job,
)

INTEGRATION_SHA = "a" * 40


class Clock:
    """Stands in for the runtime's datetime: time moves only while collecting."""

    def __init__(self) -> None:
        self.now_value = datetime(2026, 10, 13, tzinfo=UTC)

    def now(self, tz=None):
        return self.now_value


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> Clock:
    """Collections take no time unless a test says so: delays are exact."""
    frozen = Clock()
    monkeypatch.setattr("brain_v42.delivery_observer.runtime.datetime", frozen)
    return frozen


class RecordingQueue(FakeQueue):
    def __init__(self, jobs):
        super().__init__(jobs)
        self.delays: list[float] = []

    async def schedule_after(self, session, job, *, delay_seconds, expected_version):
        self.delays.append(delay_seconds)
        return True


class ContextRepo(FakeEvidenceRepo):
    async def publish_repository_context(self, session, *args):
        pass

    async def record_repository_context_error(self, session, *args):
        pass


class Client:
    def __init__(self, *, integration_sha=None, error=None):
        self.integration_sha = integration_sha
        self.error = error

    async def collect(self, binding, contract, previous=None):
        if self.error is not None:
            raise self.error
        evidence = MagicMock(spec=PullRequestEvidence)
        evidence.integration_sha = self.integration_sha
        return evidence

    async def collect_repository_context(self, contract):
        if self.error is not None:
            raise self.error
        return MagicMock(spec=RepositoryContextEvidence, complete=True)


def context_job() -> SimpleNamespace:
    job = make_job("context")
    job.binding = None
    job.identity = f"repository_context:{uuid4()}"
    job.ticket_id = uuid4()
    job.attempt = 1
    job.context_set_digest = "d" * 64
    return job


async def delay_after(job, client, settings=None) -> float:
    queue = RecordingQueue([job])
    runtime = DeliveryObserverRuntime(
        settings=settings or DeliverySettings(enabled=True),
        owner=FakeOwner(),
        client=client,
        evidence_repository=ContextRepo(),
        queue=queue,
    )
    await runtime.run_once()
    [delay] = queue.delays
    return delay


async def test_an_open_pull_request_keeps_the_poll_cadence():
    assert await delay_after(make_job("open"), Client()) == 60


async def test_an_integrated_binding_is_reobserved_at_half_the_freshness_window():
    delay = await delay_after(make_job("merged"), Client(integration_sha=INTEGRATION_SHA))
    assert delay == 300


async def test_a_repository_context_is_reobserved_at_half_the_freshness_window():
    assert await delay_after(context_job(), Client()) == 300


async def test_the_settled_cadence_follows_the_configured_freshness():
    settings = DeliverySettings(enabled=True, poll_seconds=30, freshness_seconds=1200)
    delay = await delay_after(make_job("merged"), Client(integration_sha=INTEGRATION_SHA), settings)
    assert delay == 600


async def test_the_settled_cadence_never_polls_faster_than_the_loop():
    settings = DeliverySettings(enabled=True, poll_seconds=60, freshness_seconds=100)
    assert await delay_after(context_job(), Client(), settings) == 60


class SlowClient(Client):
    """A collection that takes `seconds`: sequential requests, admission waits."""

    def __init__(self, clock: Clock, seconds: float, **kwargs):
        super().__init__(**kwargs)
        self.clock, self.seconds = clock, seconds

    async def collect(self, binding, contract, previous=None):
        self.clock.now_value += timedelta(seconds=self.seconds)
        return await super().collect(binding, contract, previous)

    async def collect_repository_context(self, contract):
        self.clock.now_value += timedelta(seconds=self.seconds)
        return await super().collect_repository_context(contract)


def seen_by_the_evaluator(settings, age_seconds: float) -> list[str]:
    finished = datetime.now(UTC) - timedelta(hours=1)
    inputs = SimpleNamespace(
        freshness_seconds=settings.freshness_seconds,
        active_bindings=[
            SimpleNamespace(
                last_attempt_outcome="success",
                confirmation=SimpleNamespace(
                    evidence=object(),
                    collection_started_at=finished - timedelta(seconds=1),
                    collection_finished_at=finished,
                ),
            )
        ],
    )
    return _binding_health_states(inputs, finished + timedelta(seconds=age_seconds))


@pytest.mark.parametrize(
    "settings",
    [
        DeliverySettings(enabled=True),
        DeliverySettings(enabled=True, poll_seconds=30, freshness_seconds=1200),
    ],
)
@pytest.mark.parametrize("job_factory", [lambda: make_job("merged"), context_job])
@pytest.mark.parametrize("collection_share", [0.0, 0.05, 0.25, 0.5, 0.615, 0.8, 1.0])
async def test_evidence_refreshed_at_the_settled_cadence_is_never_seen_stale(
    settings, job_factory, collection_share, clock
):
    """Worst case for the next confirmation: the job becomes due just after a loop
    tick, so it waits one whole poll interval more, and the next collection takes as
    long as this one. A collection is many sequential requests plus admission waits
    (codex review of #362: 41 requests at 9 s each is 369 s), so its duration counts.
    Covered: every collection the poll cadence itself kept fresh, i.e. up to
    freshness - 2 * poll seconds; a slower one is stale at any cadence."""
    seconds = collection_share * (settings.freshness_seconds - 2 * settings.poll_seconds)
    client = SlowClient(clock, seconds, integration_sha=INTEGRATION_SHA)
    delay = await delay_after(job_factory(), client, settings)
    worst_age = delay + settings.poll_seconds + seconds
    assert seen_by_the_evaluator(settings, worst_age) == ["fresh"]


@pytest.mark.parametrize("code", ["provider_not_found", "provider_forbidden"])
@pytest.mark.parametrize(
    ("prior_failures", "expected"),
    [(0, 300), (1, 1200), (2, 4800), (3, 19200), (4, 76800), (5, 86400), (6, 86400)],
)
async def test_an_unreachable_subject_backs_off_up_to_a_day(code, prior_failures, expected):
    job = make_job("gone")
    job.failure_count = prior_failures
    assert await delay_after(job, Client(error=ProviderError(code))) == expected


async def test_an_unreachable_context_backs_off_too():
    job = context_job()
    job.failure_count = 4
    assert await delay_after(job, Client(error=ProviderError("provider_not_found"))) == 76800


async def test_an_outage_keeps_its_short_retry():
    job = make_job("outage")
    job.failure_count = 6
    assert await delay_after(job, Client(error=ProviderError("provider_unavailable"))) == 300


def backoff_lines(capsys):
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    return [line for line in lines if line["event"] == "observation_backoff"]


async def test_a_long_unreachable_subject_raises_a_backoff_line(capsys):
    job = make_job("gone")
    job.failure_count = 3
    await delay_after(job, Client(error=ProviderError("provider_forbidden")))
    [line] = backoff_lines(capsys)
    assert line == {
        "event": "observation_backoff",
        "subject": job.identity,
        "error_code": "provider_forbidden",
        "prior_failures": 3,
        "delay_seconds": 19200,
    }


@pytest.mark.parametrize(
    ("code", "prior_failures"),
    [("provider_not_found", 2), ("provider_unavailable", 6)],
)
async def test_no_backoff_line_before_the_threshold_or_for_an_outage(code, prior_failures, capsys):
    job = make_job("quiet")
    job.failure_count = prior_failures
    await delay_after(job, Client(error=ProviderError(code)))
    assert backoff_lines(capsys) == []
