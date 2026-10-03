"""Containment budgets must keep progressing across immutable negative results."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from brain_v42.delivery_config import DeliverySettings
from brain_v42.delivery_observer.github import ReleaseTag
from brain_v42.delivery_observer.runtime import DeliveryObserverRuntime
from brain_v42.delivery_observer.transport import ProviderError
from brain_v42.facts.model import ReleaseIdentity
from brain_v42.models.delivery import DeliveryError
from brain_v42.repositories.pg_release_derivation import (
    BRAIN_V42_REPOSITORY_ID,
    PgReleaseDerivationRepo,
    ReleaseCandidate,
)
from tests.unit.delivery_observer.test_runtime_job_isolation import FakeOwner

NOW = datetime(2026, 10, 3, tzinfo=UTC)
TAG = ReleaseTag("v0.6.3", "0.6.3", "a" * 40)


def candidate(sha: str = "b" * 40) -> ReleaseCandidate:
    return ReleaseCandidate(uuid4(), "brain-v42", 1, "implementation", BRAIN_V42_REPOSITORY_ID, sha)


class Releases:
    def __init__(self, candidates):
        self.candidates = candidates
        self.record_released = AsyncMock()
        self.record_deployed = AsyncMock()

    async def unreleased(self, session, *, limit, exclude=(), **kwargs):
        return [c for c in self.candidates if (c.ticket_id, c.deliverable_key) not in exclude][
            :limit
        ]

    undeployed = unreleased


def make_runtime(candidates):
    return DeliveryObserverRuntime(
        settings=DeliverySettings(enabled=True),
        owner=FakeOwner(),
        client=AsyncMock(
            release_tags=AsyncMock(return_value=[TAG]),
            commit_date=AsyncMock(return_value=NOW),
            contains=AsyncMock(return_value=True),
        ),
        evidence_repository=AsyncMock(),
        releases=Releases(candidates),
        release_identity=lambda: ReleaseIdentity(TAG.sha, TAG.version),
    )


async def run_pass(runtime, kind):
    if kind == "released":
        return await runtime._release_pass()
    await runtime._deployed_pass(0)


@pytest.mark.parametrize("kind", ["released", "deployed"])
async def test_negative_compare_does_not_starve_the_next_candidate(kind, monkeypatch):
    from brain_v42.delivery_observer import runtime as module

    monkeypatch.setattr(module, "_MAX_RELEASE_COMPARES_PER_PASS", 1)
    first, second = candidate(), candidate("c" * 40)
    runtime = make_runtime([first, second])
    runtime.client.contains.side_effect = [False, True]

    await run_pass(runtime, kind)
    await run_pass(runtime, kind)

    assert [call.args[1] for call in runtime.client.contains.call_args_list] == [
        first.integration_sha,
        second.integration_sha,
    ]
    record = getattr(runtime.releases, f"record_{kind}")
    assert [call.args[1] for call in record.call_args_list] == [second]


@pytest.mark.parametrize("kind", ["released", "deployed"])
async def test_conflicting_key_is_diagnosed_once_and_never_recompared(kind, capsys):
    runtime = make_runtime([candidate()])
    record = getattr(runtime.releases, f"record_{kind}")
    record.side_effect = DeliveryError("idempotency_key_reused", "private fixture")

    await run_pass(runtime, kind)
    await run_pass(runtime, kind)

    assert runtime.client.contains.await_count == 1
    assert record.await_count == 1
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [line["error_code"] for line in lines] == ["idempotency_key_reused"]


async def test_conflicting_earliest_release_key_never_falls_through_to_a_later_tag():
    runtime = make_runtime([candidate()])
    later = ReleaseTag("v0.6.4", "0.6.4", "c" * 40)
    runtime.client.release_tags.return_value = [TAG, later]
    runtime.releases.record_released.side_effect = [
        DeliveryError("idempotency_key_reused", "private fixture"),
        None,
    ]

    await runtime._release_pass()
    await runtime._release_pass()

    runtime.releases.record_released.assert_awaited_once()
    assert runtime.releases.record_released.await_args.args[2] == TAG
    runtime.client.contains.assert_awaited_once_with(
        BRAIN_V42_REPOSITORY_ID, runtime.releases.candidates[0].integration_sha, TAG.sha
    )


@pytest.mark.parametrize("kind", ["released", "deployed"])
async def test_other_delivery_errors_remain_retryable(kind, capsys):
    runtime = make_runtime([candidate()])
    record = getattr(runtime.releases, f"record_{kind}")
    record.side_effect = DeliveryError("revision_not_found", "private fixture")
    await run_pass(runtime, kind)
    await run_pass(runtime, kind)
    assert record.await_count == 2


@pytest.mark.parametrize("stage", ["release_tags", "commit_date"])
async def test_failed_tag_fetch_is_attempted_once_per_repository_and_pass(stage, capsys):
    runtime = make_runtime([candidate(), candidate()])
    getattr(runtime.client, stage).side_effect = ProviderError("provider_unavailable")
    await runtime._release_pass()
    assert runtime.client.release_tags.await_count == 1
    assert runtime.client.contains.await_count == 0
    await runtime._release_pass()
    assert runtime.client.release_tags.await_count == 2


async def test_equal_tag_dates_use_numeric_version_order():
    runtime = make_runtime([candidate()])
    earlier = ReleaseTag("v0.6.9", "0.6.9", "d" * 40)
    later = ReleaseTag("v0.6.10", "0.6.10", "e" * 40)
    runtime.client.release_tags.return_value = [later, earlier]
    await runtime._release_pass()
    assert runtime.releases.record_released.call_args.args[2] == earlier


@pytest.mark.parametrize("kind", ["released", "deployed"])
async def test_ownership_loss_stops_before_the_next_candidate(kind):
    runtime = make_runtime([candidate(), candidate()])

    async def contains(*args):
        runtime.owner.lost.set()
        return False

    runtime.client.contains.side_effect = contains
    await run_pass(runtime, kind)
    assert runtime.client.contains.await_count == 1


async def test_undeployed_rejects_other_repository_ids_without_querying():
    assert (
        await PgReleaseDerivationRepo().undeployed(
            AsyncMock(),
            repository_id=BRAIN_V42_REPOSITORY_ID + 1,
            live_release_sha=TAG.sha,
            limit=10,
        )
        == []
    )
