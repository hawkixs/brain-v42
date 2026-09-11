"""Actual observer/HTTP/PostgreSQL composition with a controlled provider clock."""

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa

from brain_v42.db.tables import (
    delivery_artifact_bindings,
    delivery_confirmations,
    delivery_snapshots,
    delivery_workflows,
)
from brain_v42.delivery_config import DeliverySettings
from brain_v42.delivery_observer.github import GitHubClient
from brain_v42.delivery_observer.ownership import ObserverOwnership
from brain_v42.delivery_observer.transport import GitHubTransport
from brain_v42.models.delivery import RepositoryDocumentReference, RequiredCheck
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_delivery import PgDeliveryRepo
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService
from tests.integration.db.test_delivery_receipt_publication import _contract, _issuer
from tests.unit.delivery_observer.github_cases import APP, ROOT, B, H, M, S, X, check

RID = 1337360966


class _MeasuredBody(httpx.AsyncByteStream):
    def __init__(self, case, payload):
        self.case = case
        self.payload = json.dumps(payload).encode()
        self.closed = False

    async def __aiter__(self):
        yield self.payload

    async def aclose(self):
        if not self.closed:
            self.closed = True
            self.case.active_responses -= 1


class ObserverCase:
    def __init__(self, engine, factory):
        self.engine, self.factory = engine, factory
        self.settings = DeliverySettings(enabled=True, poll_seconds=60)
        self.service = DeliveryService(PgDeliveryRepo(factory), settings=self.settings)
        self.requests = []
        self.elapsed = 0.0
        self.active_responses = 0
        self.head = H
        self.merged = True
        self.status = 200
        self.drift = False
        self.missing_document = False
        self.block = False
        self.invalidated: list[dict[str, str]] = []
        self.http_entered, self.allow_http = asyncio.Event(), asyncio.Event()
        self.pr_reads = {}
        self.bindings = []
        self.tickets = []
        self.runtime_instance = None

    async def create(self, *, number=42, context=False, optional=False, bind=True):
        ticket = await PgTicketRepo(self.factory).create(
            TicketCreate(
                kind=TicketKind.REQUEST,
                title=f"observer fixture {uuid4()}",
                body="isolated HTTP/PG test",
                from_project="brain-v42",
                to_project="brain-v42",
            )
        )
        refs = (
            (
                RepositoryDocumentReference(
                    kind="repository_document",
                    repository_id=RID,
                    sha=H,
                    path="docs/spec.md",
                    required=not optional,
                ),
            )
            if context or optional
            else ()
        )
        contract = await self.service.set_contract(
            ticket.id,
            actor_project="brain-v42",
            expected_revision=0,
            idempotency_key=f"observer-contract-{ticket.id}",
            contract=_contract(
                refs=refs,
                checks=(
                    RequiredCheck(
                        kind="check_run",
                        name="test-unit",
                        provider_id=APP,
                        app_slug="github-actions",
                    ),
                ),
            ),
        )
        binding = None
        if bind:
            binding = await self.service.bind_pr(
                ticket.id,
                actor_project="brain-v42",
                deliverable_key="implementation",
                repository_id=RID,
                pr_number=number,
                expected_revision=1,
                expected_workflow_version=1,
                idempotency_key=f"observer-binding-{ticket.id}",
            )
            self.bindings.append(binding)
        self.tickets.append(ticket)
        return ticket, binding, contract

    async def sleep(self, delay):
        target = self.elapsed + delay
        # A virtual rate-budget wait cannot jump over another admitted HTTP
        # response. Let active bodies finish before advancing to the deadline.
        while self.active_responses:
            await asyncio.sleep(0)
        self.elapsed = max(self.elapsed, target)
        await asyncio.sleep(0)

    async def handle(self, request):
        self.active_responses += 1
        started, ready = self.elapsed, False
        try:
            assert request.method == "GET"
            self.requests.append((self.elapsed, request.url.path))
            if self.block:
                self.http_entered.set()
                await self.allow_http.wait()
            await asyncio.sleep(0)
            self.elapsed = max(self.elapsed, started + 1.0)
            if self.status != 200:
                ready = True
                return httpx.Response(
                    self.status, stream=_MeasuredBody(self, {"message": "private fixture body"})
                )
            path = request.url.path
            if path == ROOT:
                payload = {"id": RID, "full_name": "hawkixs/brain-v42"}
            elif path == f"{ROOT}/git/commits/{H}":
                payload = {"sha": H, "tree": {"sha": B}, "parents": []}
            elif path == f"{ROOT}/git/trees/{B}":
                payload = {
                    "sha": B,
                    "truncated": False,
                    "tree": [
                        {"path": "docs", "mode": "040000", "type": "tree", "sha": S},
                        *(
                            []
                            if self.missing_document
                            else [
                                {"path": "docs/spec.md", "mode": "100644", "type": "blob", "sha": X}
                            ]
                        ),
                    ],
                }
            elif path.startswith(f"{ROOT}/pulls/"):
                number = int(path.rsplit("/", 1)[1])
                self.pr_reads[number] = self.pr_reads.get(number, 0) + 1
                head = X if self.drift and self.pr_reads[number] % 2 == 0 else self.head
                payload = {
                    "id": 7000 + number,
                    "number": number,
                    "state": "closed" if self.merged else "open",
                    "merged": self.merged,
                    "draft": False,
                    "mergeable": True,
                    "merge_commit_sha": M if self.merged else None,
                    "user": {"id": 800, "login": "author"},
                    "head": {
                        "sha": head,
                        "ref": "feature",
                        "repo": {"id": RID + 1, "full_name": "contributor/fork"},
                    },
                    "base": {
                        "sha": B,
                        "ref": "main",
                        "repo": {"id": RID, "full_name": "hawkixs/brain-v42"},
                    },
                }
            elif path == f"{ROOT}/commits/{self.head}/check-runs":
                payload = {"total_count": 1, "check_runs": [check(sha=self.head)]}
            else:
                raise AssertionError(f"unexpected provider endpoint {path}")
            ready = True
            return httpx.Response(200, stream=_MeasuredBody(self, deepcopy(payload)))
        finally:
            if not ready:
                self.active_responses -= 1

    @asynccontextmanager
    async def runtime(self, *, settings=None, owner=None):
        from brain_v42.delivery_observer.runtime import DeliveryObserverRuntime

        selected_settings = settings or self.settings

        async def headers():
            return {"Authorization": "Bearer fixture"}

        async def invalidate(refused_headers):
            self.invalidated.append(dict(refused_headers))

        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handle)) as http:
            transport = GitHubTransport(
                http, selected_settings, monotonic=lambda: self.elapsed, sleep=self.sleep
            )
            auth = SimpleNamespace(
                transport=transport, authorization_headers=headers, invalidate=invalidate
            )
            client = GitHubClient(http, selected_settings, auth, now=lambda: datetime.now(UTC))
            runtime = DeliveryObserverRuntime(
                settings=selected_settings,
                owner=owner or ObserverOwnership(self.engine),
                client=client,
                evidence_repository=_issuer(self.factory),
            )
            self.runtime_instance = runtime
            try:
                yield runtime
            finally:
                await runtime.owner.release()

    async def refresh(self, ticket_id):
        await PgDeliveryRepo(self.factory).refresh(ticket_id)

    async def state(self, binding):
        async with self.factory() as session:
            row = (
                (
                    await session.execute(
                        sa.select(delivery_artifact_bindings).where(
                            delivery_artifact_bindings.c.id == binding.id
                        )
                    )
                )
                .mappings()
                .one()
            )
            confirmations = (
                (
                    await session.execute(
                        sa.select(delivery_confirmations)
                        .where(delivery_confirmations.c.binding_id == binding.id)
                        .order_by(delivery_confirmations.c.collection_finished_at)
                    )
                )
                .mappings()
                .all()
            )
            snapshots = await session.scalar(
                sa.select(sa.func.count())
                .select_from(delivery_snapshots)
                .where(delivery_snapshots.c.binding_id == binding.id)
            )
            workflow = (
                (
                    await session.execute(
                        sa.select(delivery_workflows).where(
                            delivery_workflows.c.ticket_id == binding.ticket_id
                        )
                    )
                )
                .mappings()
                .one()
            )
            return row, confirmations, snapshots, workflow


@pytest.fixture
async def observer_queue_isolation(session_factory):
    """The global observer queue needs a quiet, disposable DB before each case.

    Other DB tests retain their UUID-scoped facts. Only schedule fields from
    earlier cases are cleared; current cases still exercise real global reads.
    This fixture is opt-in for the two observer test modules, never production.
    """
    async with session_factory.begin() as session:
        await session.execute(delivery_artifact_bindings.update().values(due_at=None))
        await session.execute(delivery_workflows.update().values(context_due_at=None))
