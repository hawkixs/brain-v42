"""Persisted scheduling through actual GitHub HTTP, fenced owner and PG publisher."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import delivery_receipts, delivery_workflows
from brain_v42.delivery_observer.ownership import ObserverOwnership
from tests.integration.db.delivery_observer_cases import ROOT, H, ObserverCase, X
from tests.integration.db.delivery_observer_cases import (
    observer_queue_isolation as observer_queue_isolation,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.usefixtures("observer_queue_isolation"),
]


async def test_actual_http_to_atomic_proof_and_receipts_then_restart_is_idle(
    engine, session_factory
):
    case = ObserverCase(engine, session_factory)
    _, binding, _ = await case.create()
    async with case.runtime() as runtime:
        result = await runtime.run_once()
    assert (result.collected, result.failed, result.deferred, result.exit_code) == (1, 0, 0, 0)
    row, confirmations, snapshots, _ = await case.state(binding)
    assert row["head_sha"] == H and len(confirmations) == snapshots == 1
    assert result.last_success_at is not None and result.max_lag_seconds >= 0
    async with session_factory() as session:
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(delivery_receipts)
                .where(delivery_receipts.c.ticket_id == binding.ticket_id)
            )
            == 2
        )
    requests = len(case.requests)
    async with case.runtime() as restarted:
        result = await restarted.run_once()
    assert result.collected == 0 and len(case.requests) == requests


async def test_refresh_while_offline_survives_restart_and_reuses_snapshot(engine, session_factory):
    case = ObserverCase(engine, session_factory)
    ticket, binding, _ = await case.create()
    async with case.runtime() as runtime:
        await runtime.run_once()
    first, _, _, _ = await case.state(binding)
    await case.refresh(ticket.id)
    async with case.runtime() as restarted:
        assert (await restarted.run_once()).collected == 1
    row, confirmations, snapshots, _ = await case.state(binding)
    assert row["latest_success_confirmation_id"] != first["latest_success_confirmation_id"]
    assert len(confirmations) == 2 and snapshots == 1


async def test_refresh_during_http_is_not_overwritten_and_next_process_observes_it(
    engine, session_factory
):
    case = ObserverCase(engine, session_factory)
    ticket, binding, _ = await case.create()
    case.block = True
    async with case.runtime() as runtime:
        job = asyncio.create_task(runtime.run_once())
        await asyncio.wait_for(case.http_entered.wait(), 5)
        await case.refresh(ticket.id)
        refreshed, _, _, workflow_refreshed = await case.state(binding)
        case.allow_http.set()
        result = await asyncio.wait_for(job, 10)
    row, _, _, workflow_after = await case.state(binding)
    assert result.collected == 1
    assert row["due_at"] == refreshed["due_at"]
    assert row["row_version"] == refreshed["row_version"] + 1
    assert workflow_after["row_version"] == workflow_refreshed["row_version"] + 1
    case.block = False
    async with case.runtime() as restarted:
        assert (await restarted.run_once()).collected == 1
    assert len((await case.state(binding))[1]) == 2


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "provider_forbidden"),
        (403, "provider_forbidden"),
        (404, "provider_not_found"),
        (429, "provider_rate_limited"),
        (500, "provider_unavailable"),
    ],
)
async def test_provider_error_retains_success_records_failure_and_schedules_retry(
    engine, session_factory, status, code
):
    case = ObserverCase(engine, session_factory)
    ticket, binding, _ = await case.create()
    async with case.runtime() as runtime:
        await runtime.run_once()
    previous, _, _, _ = await case.state(binding)
    await case.refresh(ticket.id)
    case.status = status
    async with case.runtime() as runtime:
        result = await runtime.run_once()
    row, confirmations, snapshots, _ = await case.state(binding)
    assert (result.collected, result.failed, result.exit_code) == (0, 1, 1)
    assert row["latest_success_confirmation_id"] == previous["latest_success_confirmation_id"]
    assert len(confirmations) == 2 and snapshots == 1
    assert confirmations[-1]["error_code"] == code
    assert row["due_at"] > datetime.now(UTC)
    assert "private fixture body" not in result.model_dump_json()
    assert (
        await case.service.get(ticket.id, actor_project="brain-v42")
    ).assessment.observation_health == "error"


async def test_revision_drift_is_mapped_to_persistable_error(engine, session_factory):
    case = ObserverCase(engine, session_factory)
    _, binding, _ = await case.create()
    case.drift = True
    async with case.runtime() as runtime:
        result = await runtime.run_once()
    row, confirmations, snapshots, _ = await case.state(binding)
    assert result.failed == 1 and result.exit_code == 1
    assert snapshots == 0 and row["latest_success_confirmation_id"] is None
    assert confirmations[0]["error_code"] == "provider_invalid_response"


@pytest.mark.parametrize("bind", [False, True])
async def test_required_context_is_observed_before_pr_and_without_a_binding(
    engine, session_factory, bind
):
    case = ObserverCase(engine, session_factory)
    ticket, _, _ = await case.create(context=True, bind=bind)
    async with case.runtime() as runtime:
        result = await runtime.run_once()
    assert result.collected == 1 + int(bind) and result.failed == 0
    assert [path for _, path in case.requests][:3] == [
        ROOT,
        f"{ROOT}/git/commits/{H}",
        f"{ROOT}/git/trees/{'b' * 40}",
    ]
    view = await case.service.get(ticket.id, actor_project="brain-v42")
    assert view.contexts[0].status == "available"
    if not bind:
        assert {work.kind for work in view.assessment.eligible_work} == {"implement"}


async def test_optional_only_context_needs_no_job(engine, session_factory):
    case = ObserverCase(engine, session_factory)
    await case.create(optional=True, bind=False)
    async with case.runtime() as runtime:
        result = await runtime.run_once()
    assert result.collected == result.failed == 0 and not case.requests


async def test_missing_required_path_is_failed_attempt_without_partial_snapshot(
    engine, session_factory
):
    case = ObserverCase(engine, session_factory)
    ticket, _, _ = await case.create(context=True, bind=False)
    case.missing_document = True
    async with case.runtime() as runtime:
        result = await runtime.run_once()
    assert result.failed == 1
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(delivery_workflows).where(delivery_workflows.c.ticket_id == ticket.id)
                )
            )
            .mappings()
            .one()
        )
        assert row["latest_context_success_confirmation_id"] is None
        assert row["context_last_error_code"] == "provider_not_found"
    assert not (
        await case.service.get(ticket.id, actor_project="brain-v42")
    ).assessment.eligible_work


async def test_duplicate_process_does_not_request_provider(engine, session_factory):
    case = ObserverCase(engine, session_factory)
    await case.create()
    owner = ObserverOwnership(engine)
    assert await owner.acquire()
    try:
        async with case.runtime() as duplicate:
            result = await duplicate.run_once()
        assert result.exit_code == 2 and not case.requests
    finally:
        await owner.release()


async def test_backend_lost_during_collection_stops_runtime_without_publication(
    engine, session_factory
):
    case = ObserverCase(engine, session_factory)
    _, binding, _ = await case.create()
    case.block = True
    async with case.runtime() as runtime:
        job = asyncio.create_task(runtime.run_once())
        await asyncio.wait_for(case.http_entered.wait(), 5)
        async with engine.begin() as connection:
            await connection.scalar(
                sa.text("SELECT pg_terminate_backend(:pid)"), {"pid": runtime.owner.backend_pid}
            )
        case.allow_http.set()
        result = await asyncio.wait_for(job, 5)
    assert result.exit_code == 2 and runtime.owner.lost.is_set()
    assert not (await case.state(binding))[1]


async def test_stop_during_http_cancels_work_releases_lock_and_allows_restart(
    engine, session_factory
):
    case = ObserverCase(engine, session_factory)
    _, binding, _ = await case.create()
    case.block = True
    stop = asyncio.Event()
    async with case.runtime() as runtime:
        job = asyncio.create_task(runtime.run(stop))
        await asyncio.wait_for(case.http_entered.wait(), 5)
        stop.set()
        assert await asyncio.wait_for(job, 5) == 0
        assert case.active_responses == 0
    assert not (await case.state(binding))[1]
    next_owner = ObserverOwnership(engine)
    assert await next_owner.acquire()
    await next_owner.release()


async def test_disabled_runtime_does_not_collect_or_hold_ownership(engine, session_factory):
    case = ObserverCase(engine, session_factory)
    await case.create()
    async with case.runtime(
        settings=case.settings.model_copy(update={"enabled": False})
    ) as runtime:
        result = await runtime.run_once()
        assert not runtime.owner.owned
    assert result.collected == 0 and not case.requests


async def test_project_filter_does_not_observe_another_projects_work(engine, session_factory):
    case = ObserverCase(engine, session_factory)
    await case.create()
    async with case.runtime() as runtime:
        assert (await runtime.run_once(project_key="different-project")).collected == 0
    assert not case.requests


async def test_twenty_changed_prs_visible_within_budget_without_future_pg_evidence(
    engine, session_factory, record_property
):
    case = ObserverCase(engine, session_factory)
    case.merged = False
    bindings = [(await case.create(number=100 + i))[1] for i in range(20)]
    async with case.runtime() as runtime:
        assert (await runtime.run_once()).collected == 20
    for ticket in case.tickets:
        await case.refresh(ticket.id)
    case.requests.clear()
    case.elapsed = 0.0
    case.head = X
    started_at = datetime.now(UTC)
    async with case.runtime() as restarted:
        result = await restarted.run_once()
    finished_at = datetime.now(UTC)
    assert result.collected == 20 and result.failed == result.deferred == 0
    record_property("changed_prs", 20)
    record_property("provider_requests", len(case.requests))
    record_property("simulated_seconds", case.elapsed)
    assert case.elapsed <= 300 and len(case.requests) == 60
    for at, _ in case.requests:
        assert sum(at <= other < at + 60 for other, _ in case.requests) <= 40
    for binding in bindings:
        row, confirmations, snapshots, _ = await case.state(binding)
        assert row["head_sha"] == X and snapshots == 2
        assert started_at <= confirmations[-1]["collection_started_at"] <= finished_at
        assert confirmations[-1]["collection_finished_at"] <= finished_at + timedelta(seconds=1)


async def test_fatal_publication_joins_and_cancels_sibling_http_task(
    engine, session_factory, monkeypatch
):
    from brain_v42.models.delivery import DeliveryError

    case = ObserverCase(engine, session_factory)
    await case.create(number=41)
    await case.create(number=42)
    original = case.handle
    slow_started, slow_cancelled = asyncio.Event(), asyncio.Event()
    waiting = asyncio.Event()
    sibling = []

    async def handle(request):
        if request.url.path.endswith("/pulls/42"):
            sibling.append(asyncio.current_task())
            slow_started.set()
            try:
                await waiting.wait()
            finally:
                slow_cancelled.set()
        return await original(request)

    async def fail_publication(*args, **kwargs):
        await asyncio.wait_for(slow_started.wait(), 3)
        raise DeliveryError("injected_publication_failure", "safe injected failure")

    case.handle = handle
    async with case.runtime() as runtime:
        monkeypatch.setattr(runtime.evidence_repository, "publish_observation", fail_publication)
        try:
            with pytest.raises(DeliveryError, match="injected_publication_failure"):
                await runtime.run_once()
            assert slow_cancelled.is_set(), "fatal publication returned with an orphaned HTTP task"
            assert sibling[0].done()
        finally:
            for task in sibling:
                task.cancel()
            await asyncio.gather(*sibling, return_exceptions=True)


@pytest.mark.parametrize("action", ["refresh", "amend"])
async def test_context_request_during_http_survives_old_job_and_restart(
    engine, session_factory, action
):
    from tests.integration.db.test_delivery_receipt_publication import _contract

    case = ObserverCase(engine, session_factory)
    ticket, _, contract = await case.create(context=True, bind=False)
    case.block = True
    async with case.runtime() as runtime:
        running = asyncio.create_task(runtime.run_once())
        await asyncio.wait_for(case.http_entered.wait(), 5)
        if action == "amend":
            await case.service.set_contract(
                ticket.id,
                actor_project="brain-v42",
                expected_revision=1,
                idempotency_key=f"amend-during-http-{ticket.id}",
                contract=_contract(refs=contract.context_refs),
            )
        else:
            await case.refresh(ticket.id)
        async with session_factory() as session:
            queued = (
                (
                    await session.execute(
                        sa.select(delivery_workflows).where(
                            delivery_workflows.c.ticket_id == ticket.id
                        )
                    )
                )
                .mappings()
                .one()
            )
        case.allow_http.set()
        result = await asyncio.wait_for(running, 10)
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    sa.select(delivery_workflows).where(delivery_workflows.c.ticket_id == ticket.id)
                )
            )
            .mappings()
            .one()
        )
    assert row["context_due_at"] == queued["context_due_at"]
    if action == "amend":
        assert result.deferred == 1 and result.collected == 0
        assert row["latest_context_success_confirmation_id"] is None
    else:
        assert result.collected == 1 and result.deferred == 0
        assert row["context_row_version"] == queued["context_row_version"] + 1
    case.block = False
    async with case.runtime() as restarted:
        assert (await restarted.run_once()).collected == 1


async def test_repeated_provider_failure_uses_persisted_bounded_backoff(engine, session_factory):
    case = ObserverCase(engine, session_factory)
    case.settings = case.settings.model_copy(update={"poll_seconds": 1})
    ticket, binding, _ = await case.create()
    case.status = 500
    for minimum in (5, 10):
        await case.refresh(ticket.id)
        before = datetime.now(UTC)
        async with case.runtime() as runtime:
            assert (await runtime.run_once()).failed == 1
        row, _, _, _ = await case.state(binding)
        assert minimum <= (row["due_at"] - before).total_seconds() < 3601
