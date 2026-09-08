"""Complete public PG read surface: filters, immutable history and amendment intent."""

from uuid import uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import delivery_contract_revisions
from brain_v42.models.delivery import DeliveryError
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from tests.integration.db.delivery_observer_cases import ObserverCase
from tests.integration.db.delivery_observer_cases import (
    observer_queue_isolation as observer_queue_isolation,
)
from tests.integration.db.test_delivery_receipt_issuance import RID, _contract
from tests.integration.db.test_delivery_requester_acceptance import (
    _accept,
    _now,
    _proof,
    _publish_and_issue,
    _service,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class ReadCase:
    def __init__(self, factory):
        self.factory = factory
        self.actor = "reads-" + uuid4().hex
        self.service = _service(factory)

    async def create(self):
        ticket = await PgTicketRepo(self.factory).create(
            TicketCreate(
                kind=TicketKind.REQUEST,
                title="read surface fixture",
                body="isolated PG",
                from_project=self.actor,
                to_project="executor",
            )
        )
        await self.service.set_contract(
            ticket.id,
            actor_project=self.actor,
            expected_revision=0,
            idempotency_key=f"read-contract-{ticket.id}",
            contract=_contract(mode="explicit"),
        )
        return ticket

    async def bind(self, ticket):
        return await self.service.bind_pr(
            ticket.id,
            actor_project="executor",
            deliverable_key="implementation",
            repository_id=RID,
            pr_number=42,
            expected_revision=1,
            expected_workflow_version=1,
            idempotency_key=f"read-bind-{ticket.id}",
        )

    async def verify(self, ticket, *, merged=False):
        binding = await self.bind(ticket)
        if merged:
            return await _publish_and_issue(self.factory, ticket.id, binding)
        async with self.factory.begin() as session:
            now = await _now(session)
            proof = _proof(now).model_copy(
                update={"state": "open", "integration_sha": None, "mergeable": True}
            )
            await PgDeliveryEvidenceRepo(self.factory).publish_observation(
                session,
                binding.id,
                binding.binding_version,
                proof,
                now,
                now,
            )
        return binding


async def test_caller_reason_is_immutable_visible_and_part_of_idempotency(session_factory):
    case = ReadCase(session_factory)
    ticket = await case.create()
    args = {
        "actor_project": case.actor,
        "contract": _contract(mode="explicit"),
        "expected_revision": 1,
        "idempotency_key": f"read-amend-{ticket.id}",
        "reason": "Add the production rollout acceptance step.",
    }
    amended = await case.service.set_contract(ticket.id, **args)
    replay = await case.service.set_contract(ticket.id, **args)
    assert replay == amended and amended.amendment_reason == args["reason"]
    view = await case.service.get(ticket.id, actor_project=case.actor)
    assert view.contract.amendment_reason == args["reason"]
    assert view.history.items[0].contract.amendment_reason == args["reason"]
    assert not view.history.items[0].superseded
    with pytest.raises(DeliveryError) as error:
        await case.service.set_contract(ticket.id, **{**args, "reason": "Different intent."})
    assert error.value.code == "idempotency_key_reused"
    async with session_factory() as session:
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(delivery_contract_revisions)
                .where(delivery_contract_revisions.c.ticket_id == ticket.id)
            )
            == 2
        )


@pytest.mark.parametrize(
    "reason", ["", "   ", "x" * 4001, 12], ids=["empty", "blank", "oversized", "nonstring"]
)
async def test_invalid_amendment_reason_is_refused_without_revision(session_factory, reason):
    case = ReadCase(session_factory)
    ticket = await case.create()
    with pytest.raises(DeliveryError) as error:
        await case.service.set_contract(
            ticket.id,
            actor_project=case.actor,
            contract=_contract(mode="explicit"),
            expected_revision=1,
            idempotency_key=f"invalid-reason-{ticket.id}",
            reason=reason,
        )
    assert error.value.code == "invalid_reason"
    assert (
        await case.service.get(ticket.id, actor_project=case.actor)
    ).contract.contract_revision == 1


@pytest.mark.parametrize(
    "filters, expected",
    [
        ({"work": "implement"}, "waiting"),
        ({"work": "integrate"}, "verified"),
        ({"work": "accept"}, "integrated"),
        ({"blocker": "binding_missing"}, "waiting"),
        ({"stage": "verified"}, "verified"),
        ({"work": "integrate", "blocker": "pr_not_merged", "stage": "verified"}, "verified"),
    ],
)
async def test_filters_apply_shared_assessment_with_project_isolation(
    session_factory, filters, expected
):
    case = ReadCase(session_factory)
    waiting, verified, integrated = [await case.create() for _ in range(3)]
    await case.verify(verified)
    await case.verify(integrated, merged=True)
    other = ReadCase(session_factory)
    await other.create()
    page = await case.service.list(actor_project=case.actor, **filters)
    assert [view.contract.ticket_id for view in page.items] == [
        {"waiting": waiting, "verified": verified, "integrated": integrated}[expected].id
    ]
    assert page.omitted_count == 0 and page.next_cursor is None


async def test_filter_finds_matches_beyond_first_unfiltered_page_and_counts_continuation(
    session_factory,
):
    case = ReadCase(session_factory)
    tickets = sorted([await case.create() for _ in range(24)], key=lambda ticket: ticket.id)
    targets = tickets[-2:]
    for ticket in targets:
        await case.verify(ticket)
    first = await case.service.list(actor_project=case.actor, work="integrate", limit=1)
    assert [view.contract.ticket_id for view in first.items] == [targets[0].id]
    assert first.omitted_count == 1 and first.next_cursor is not None
    second = await case.service.list(
        actor_project=case.actor, work="integrate", limit=1, cursor=first.next_cursor
    )
    assert [view.contract.ticket_id for view in second.items] == [targets[1].id]
    assert second.omitted_count == 0 and second.next_cursor is None
    default = await case.service.list(actor_project=case.actor)
    assert len(default.items) == 20 and default.omitted_count == 4
    assert all(view.history is None for view in default.items)
    empty = await case.service.list(actor_project=case.actor, work="repair")
    assert not empty.items and empty.next_cursor is None and empty.omitted_count == 0


@pytest.mark.parametrize(
    "options, code",
    [
        ({"limit": 0}, "invalid_limit"),
        ({"limit": 101}, "invalid_limit"),
        ({"limit": True}, "invalid_limit"),
        ({"limit": 1.5}, "invalid_limit"),
        ({"cursor": "not-a-uuid"}, "invalid_cursor"),
        ({"cursor": ""}, "invalid_cursor"),
        ({"work": "launch_agent"}, "invalid_filter"),
        ({"stage": "shipped"}, "invalid_filter"),
        ({"blocker": "bad\ncode"}, "invalid_filter"),
    ],
)
async def test_invalid_list_inputs_return_safe_domain_errors(session_factory, options, code):
    case = ReadCase(session_factory)
    await case.create()
    with pytest.raises(DeliveryError) as error:
        await case.service.list(actor_project=case.actor, **options)
    assert error.value.code == code


@pytest.mark.parametrize("changed", ["project", "work", "blocker", "stage"])
async def test_list_cursor_refuses_a_different_project_or_filter_set(session_factory, changed):
    case = ReadCase(session_factory)
    for _ in range(3):
        await case.create()
    first = await case.service.list(actor_project=case.actor, work="implement", limit=1)
    assert first.next_cursor is not None
    options = {"actor_project": case.actor, "work": "implement", "cursor": first.next_cursor}
    if changed == "project":
        options["actor_project"] = "another-project"
    else:
        options[changed] = {
            "work": "repair",
            "blocker": "binding_missing",
            "stage": "awaiting_artifact",
        }[changed]
    with pytest.raises(DeliveryError) as error:
        await case.service.list(**options)
    assert error.value.code == "invalid_cursor"
    continuation = await case.service.list(
        actor_project=case.actor, work="implement", cursor=first.next_cursor, limit=100
    )
    assert len(continuation.items) == 2 and continuation.omitted_count == 0


async def test_history_pages_preserve_current_and_superseded_contracts_and_full_receipts(
    session_factory,
):
    case = ReadCase(session_factory)
    ticket = await case.create()
    integration = await case.verify(ticket, merged=True)
    fulfillment = await _accept(case.service, ticket.id, integration, actor_project=case.actor)
    before = await case.service.get(ticket.id, actor_project=case.actor)
    assert before.integration_receipt == integration and before.fulfillment_receipt == fulfillment
    assert len(before.history.items) == 3
    assert not any(item.superseded for item in before.history.items)
    await case.service.set_contract(
        ticket.id,
        actor_project=case.actor,
        expected_revision=1,
        idempotency_key=f"history-amend-{ticket.id}",
        contract=_contract(mode="explicit"),
        reason="New acceptance scope.",
    )
    current = await case.service.get(ticket.id, actor_project=case.actor, history_limit=2)
    assert current.integration_receipt is None and current.fulfillment_receipt is None
    assert current.history.omitted_count == 2 and current.history.next_cursor is not None
    more = await case.service.get(
        ticket.id,
        actor_project=case.actor,
        history_limit=2,
        history_cursor=current.history.next_cursor,
    )
    entries = [*current.history.items, *more.history.items]
    assert more.history.next_cursor is None and more.history.omitted_count == 0
    contracts = [item for item in entries if item.kind == "contract_revision"]
    receipts = [item for item in entries if item.kind == "receipt"]
    assert [(item.contract.contract_revision, item.superseded) for item in contracts] == [
        (2, False),
        (1, True),
    ]
    assert all(item.superseded for item in receipts)
    assert {item.receipt.id for item in receipts} == {integration.id, fulfillment.id}
    assert {item.receipt.model_dump_json() for item in receipts} == {
        integration.model_dump_json(),
        fulfillment.model_dump_json(),
    }


@pytest.mark.parametrize(
    "options, code",
    [
        ({"history_limit": 0}, "invalid_limit"),
        ({"history_limit": 101}, "invalid_limit"),
        ({"history_limit": True}, "invalid_limit"),
        ({"history_limit": 1.5}, "invalid_limit"),
        ({"history_cursor": "bad-cursor"}, "invalid_cursor"),
    ],
)
async def test_invalid_history_bounds_and_cursor_are_safe(session_factory, options, code):
    case = ReadCase(session_factory)
    ticket = await case.create()
    with pytest.raises(DeliveryError) as error:
        await case.service.get(ticket.id, actor_project=case.actor, **options)
    assert error.value.code == code


async def test_history_cursor_is_scoped_to_ticket_and_reads_do_not_expose_claim_secret(
    session_factory, monkeypatch
):
    from brain_v42.delivery_observer.github import GitHubClient

    async def unavailable(*args, **kwargs):
        raise AssertionError("PG reads must not query GitHub")

    monkeypatch.setattr(GitHubClient, "collect", unavailable)
    case = ReadCase(session_factory)
    first, second = await case.create(), await case.create()
    view = await case.service.get(first.id, actor_project=case.actor)
    claim = await case.service.claim(
        first.id,
        actor_project="executor",
        owner_key="external-orchestrator",
        work_kind="implement",
        expected_workflow_version=view.assessment.assessment_version,
        expected_assessment_id=view.assessment.assessment_id,
    )
    view = await case.service.get(first.id, actor_project=case.actor)
    page = await case.service.list(actor_project=case.actor)
    for encoded in (view.model_dump_json(), page.model_dump_json()):
        assert (
            claim.claim_token not in encoded
            and '"claim_token"' not in encoded
            and '"claim_digest"' not in encoded
        )
    await case.service.set_contract(
        first.id,
        actor_project=case.actor,
        expected_revision=1,
        idempotency_key=f"cursor-amend-{first.id}",
        contract=_contract(mode="explicit"),
        reason="Create history.",
    )
    history = (await case.service.get(first.id, actor_project=case.actor, history_limit=1)).history
    with pytest.raises(DeliveryError) as error:
        await case.service.get(
            second.id, actor_project=case.actor, history_cursor=history.next_cursor
        )
    assert error.value.code == "invalid_cursor"
    with pytest.raises(DeliveryError) as error:
        await case.service.get(first.id, actor_project="unrelated-project")
    assert error.value.code == "not_allowed"


@pytest.mark.usefixtures("observer_queue_isolation")
async def test_context_failure_exposes_real_last_attempt_without_replacing_success_before_any_pr(
    engine, session_factory
):
    case = ObserverCase(engine, session_factory)
    ticket, _, _ = await case.create(context=True, bind=False)
    async with case.runtime() as runtime:
        assert (await runtime.run_once()).collected == 1
    first = (await case.service.get(ticket.id, actor_project="brain-v42")).contexts[0]
    assert first.last_success_at == first.last_attempt_at == first.collection_finished_at
    await case.refresh(ticket.id)
    case.status = 503
    async with case.runtime() as runtime:
        assert (await runtime.run_once()).failed == 1
    view = await case.service.get(ticket.id, actor_project="brain-v42")
    latest = view.contexts[0]
    assert not view.bindings and latest.status == "error"
    assert latest.last_attempt_at > first.last_attempt_at
    assert latest.last_success_at == first.last_success_at
    assert latest.success_confirmation_id == first.success_confirmation_id
    assert latest.latest_attempt_confirmation_id != first.latest_attempt_confirmation_id
