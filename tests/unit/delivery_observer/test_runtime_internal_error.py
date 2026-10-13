"""A programming error in an observation must not be recorded as a GitHub outage.

On 2026-09-11 a test double without `invalidate` raised AttributeError inside the
GitHub client; `_observe` caught it with `except Exception` and persisted
`provider_unavailable` with a backoff, exactly as for a provider outage (ticket
e8c26eed). In production the same branch would disguise any TypeError, KeyError or
AttributeError as "GitHub unavailable" and replan the binding forever.
"""

import json

import httpx
import pytest

from brain_v42.delivery_config import DeliverySettings
from brain_v42.delivery_observer.runtime import DeliveryObserverRuntime
from brain_v42.models.delivery import SAFE_OBSERVATION_ERROR_CODES, DeliveryError
from tests.unit.delivery_observer.test_runtime_job_isolation import (
    SECRET,
    FakeEvidenceRepo,
    FakeOwner,
    FakeQueue,
    make_job,
)


class RecordingRepo(FakeEvidenceRepo):
    def __init__(self):
        super().__init__()
        self.codes = []

    async def record_observation_error(self, session, binding_id, version, code, started, finished):
        self.codes.append(code)


class RaisingClient:
    def __init__(self, error: BaseException):
        self.error = error

    async def collect(self, binding, contract, previous=None):
        raise self.error


def runtime_raising(error: BaseException, repo: RecordingRepo) -> DeliveryObserverRuntime:
    return DeliveryObserverRuntime(
        settings=DeliverySettings(enabled=True),
        owner=FakeOwner(),
        client=RaisingClient(error),
        evidence_repository=repo,
        queue=FakeQueue([make_job("bugged")]),
    )


def stderr_lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().err.splitlines()]


def test_internal_error_code_is_persistable():
    assert "observer_internal_error" in SAFE_OBSERVATION_ERROR_CODES


async def test_attribute_error_is_recorded_as_an_internal_error_not_an_outage(capsys):
    repo = RecordingRepo()

    result = await runtime_raising(AttributeError(SECRET), repo).run_once()

    assert repo.codes == ["observer_internal_error"]
    assert result.failed == 1 and result.internal_errors == 1
    assert result.exit_code == 1
    [line] = stderr_lines(capsys)
    assert line["error_code"] == "observer_internal_error"
    assert line["provider_code"] is None


async def test_internal_error_diagnostic_locates_the_fault_without_its_message(capsys):
    def buggy():
        raise KeyError(SECRET)

    class BuggyClient:
        async def collect(self, binding, contract, previous=None):
            buggy()

    runtime = DeliveryObserverRuntime(
        settings=DeliverySettings(enabled=True),
        owner=FakeOwner(),
        client=BuggyClient(),
        evidence_repository=RecordingRepo(),
        queue=FakeQueue([make_job("bugged")]),
    )

    await runtime.run_once()

    err = capsys.readouterr().err
    assert SECRET not in err
    [line] = [json.loads(x) for x in err.splitlines()]
    assert line["diagnostic"].startswith("KeyError")
    assert "test_runtime_internal_error.py" in line["diagnostic"]
    assert "buggy" in line["diagnostic"]


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError(SECRET),
        httpx.ReadTimeout(SECRET),
        TimeoutError(SECRET),
    ],
)
async def test_network_and_timeout_errors_stay_provider_unavailable(error, capsys):
    repo = RecordingRepo()

    result = await runtime_raising(error, repo).run_once()

    assert repo.codes == ["provider_unavailable"]
    assert result.internal_errors == 0
    assert SECRET not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("persist_error", "status"),
    [
        (DeliveryError("binding_conflict", SECRET), "deferred"),
        (DeliveryError("some_unforeseen_code", SECRET), "failed"),
    ],
)
async def test_internal_error_survives_a_failed_persistence(persist_error, status, capsys):
    """Codex review of #354: an internal error lost its signal when its record failed."""

    class FailingRepo(RecordingRepo):
        async def record_observation_error(self, *args):
            raise persist_error

    result = await runtime_raising(AttributeError(SECRET), FailingRepo()).run_once()

    assert result.internal_errors == 1
    assert getattr(result, status) == 1
    err = capsys.readouterr().err
    assert SECRET not in err
    codes = [json.loads(line)["error_code"] for line in err.splitlines()]
    assert "observer_internal_error" in codes
    [internal] = [json.loads(x) for x in err.splitlines() if "observer_internal_error" in x]
    assert internal["diagnostic"].startswith("AttributeError at ")
