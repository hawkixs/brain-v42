"""Real PostgreSQL contracts for issuer-declared delivery attestations.

Ticket 04bc1f4a. Brain is the LEDGER: these tests freeze the FORM of an
attestation — participant-only issuance, one row per idempotency key, newest-first
keyset reads, the server-computed canonical digest — and never its policy. The
declared kinds are not judged here, because they are not judged anywhere in brain.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastmcp import Client
from fastmcp.server.auth import AccessToken

from brain_v42.db.tables import delivery_attestations, tickets
from brain_v42.models.delivery import DeliveryError
from brain_v42.models.delivery_hashes import canonical_digest
from brain_v42.models.ticket import TicketCreate, TicketKind
from brain_v42.repositories.pg_ticket import PgTicketRepo
from brain_v42.services.delivery_service import DeliveryService
from tests.integration.db.test_delivery_mcp import delivery_mcp  # noqa: F401 -- fixture
from tests.integration.db.test_delivery_requester_acceptance import _service, _workflow

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_EMITTED = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
_PAYLOAD = {"gate": "unit", "outcome": "passed"}


async def _attest(service: DeliveryService, ticket_id: UUID, **overrides: object):
    values: dict[str, object] = {
        "actor_project": "requester",
        "caller_identity": "attester-client",
        "kind": "gate_passed",
        "payload": dict(_PAYLOAD),
        "idempotency_key": "attestation-key",
        "emitted_at": _EMITTED,
    }
    values.update(overrides)
    return await service.attest(ticket_id, **values)


async def _rows(factory, ticket_id: UUID):
    async with factory() as session:
        return (
            (
                await session.execute(
                    sa.select(delivery_attestations)
                    .where(delivery_attestations.c.ticket_id == ticket_id)
                    .order_by(delivery_attestations.c.emitted_at, delivery_attestations.c.id)
                )
            )
            .mappings()
            .all()
        )


async def test_replaying_the_same_key_and_content_returns_one_stored_row(session_factory):
    """The idempotency canary: an exact replay lands on the SAME row, not a second one."""
    ticket, _binding, service = await _workflow(session_factory)

    first = await _attest(service, ticket.id)
    second = await _attest(service, ticket.id)

    assert first == second
    assert first.id == second.id
    assert first.digest == canonical_digest(_PAYLOAD, domain="attestation")
    assert len(await _rows(session_factory, ticket.id)) == 1


async def test_same_key_with_different_content_is_refused(session_factory):
    """One key names one fact; different content is a reuse, not a replay."""
    ticket, _binding, service = await _workflow(session_factory)
    await _attest(service, ticket.id)

    with pytest.raises(DeliveryError, match="idempotency_key_reused"):
        await _attest(service, ticket.id, payload={"gate": "integration"})

    assert len(await _rows(session_factory, ticket.id)) == 1


async def test_distinct_keys_append_and_list_newest_first_with_filters(session_factory):
    """The read surface: newest first, exact kind, inclusive emission window."""
    ticket, _binding, service = await _workflow(session_factory)
    instants = [_EMITTED + timedelta(minutes=offset) for offset in range(4)]
    kinds = ["gate_passed", "deployed", "gate_passed", "incident_detected"]
    for index, (instant, kind) in enumerate(zip(instants, kinds, strict=True)):
        await _attest(
            service,
            ticket.id,
            kind=kind,
            payload={"index": index},
            idempotency_key=f"key-{index}",
            emitted_at=instant,
        )

    page = await service.list_attestations(ticket.id, actor_project="requester")

    assert [item.kind for item in page.items] == list(reversed(kinds))
    assert page.omitted_count == 0
    assert page.next_cursor is None

    filtered = await service.list_attestations(
        ticket.id, actor_project="requester", kind="gate_passed"
    )
    assert [item.kind for item in filtered.items] == ["gate_passed", "gate_passed"]

    windowed = await service.list_attestations(
        ticket.id,
        actor_project="requester",
        since=instants[1],
        until=instants[2],
    )
    assert [item.kind for item in windowed.items] == ["gate_passed", "deployed"]


async def test_keyset_cursor_pages_without_repeating_and_counts_the_omitted(session_factory):
    ticket, _binding, service = await _workflow(session_factory)
    for index in range(5):
        await _attest(
            service,
            ticket.id,
            payload={"index": index},
            idempotency_key=f"page-{index}",
            emitted_at=_EMITTED + timedelta(minutes=index),
        )

    first = await service.list_attestations(ticket.id, actor_project="requester", limit=2)
    assert len(first.items) == 2
    assert first.next_cursor is not None
    assert first.omitted_count == 3

    second = await service.list_attestations(
        ticket.id, actor_project="requester", limit=2, cursor=first.next_cursor
    )
    assert len(second.items) == 2
    assert second.next_cursor is not None
    assert second.omitted_count == 1
    assert {item.id for item in first.items}.isdisjoint({item.id for item in second.items})

    third = await service.list_attestations(
        ticket.id, actor_project="requester", limit=2, cursor=second.next_cursor
    )
    assert len(third.items) == 1
    assert third.next_cursor is None
    assert third.omitted_count == 0


async def test_a_malformed_cursor_is_refused(session_factory):
    ticket, _binding, service = await _workflow(session_factory)
    await _attest(service, ticket.id)

    with pytest.raises(DeliveryError, match="invalid_cursor"):
        await service.list_attestations(ticket.id, actor_project="requester", cursor="not-a-cursor")


async def test_get_carries_attestations_while_list_leaves_them_unloaded(session_factory):
    """`brain_delivery_get` loads the 20 newest; `brain_delivery_list` never does."""
    ticket, _binding, service = await _workflow(session_factory)
    for index in range(3):
        await _attest(
            service,
            ticket.id,
            payload={"index": index},
            idempotency_key=f"view-{index}",
            emitted_at=_EMITTED + timedelta(minutes=index),
        )

    view = await service.get(ticket.id, actor_project="requester", history_limit=2)
    assert view.attestations is not None
    assert len(view.attestations.items) == 2
    assert view.attestations.omitted_count == 1
    assert all(
        item.digest == canonical_digest(item.payload, domain="attestation")
        for item in view.attestations.items
    )

    page = await service.list(actor_project="requester")
    assert all(item.attestations is None for item in page.items)


async def test_participant_and_reference_guards(session_factory):
    ticket, _binding, service = await _workflow(session_factory)

    with pytest.raises(DeliveryError, match="not_allowed"):
        await _attest(service, ticket.id, actor_project="outsider")

    with pytest.raises(DeliveryError, match="ticket_not_found"):
        await _attest(service, uuid4())

    with pytest.raises(DeliveryError, match="revision_not_found"):
        await _attest(service, ticket.id, contract_revision=99)

    orphan = await PgTicketRepo(session_factory).create(
        TicketCreate(
            kind=TicketKind.REQUEST,
            title=f"no workflow {uuid4()}",
            body="attestation contract_not_found fixture",
            from_project="integ-attest",
            to_project="integ-attest",
        )
    )
    try:
        with pytest.raises(DeliveryError, match="contract_not_found"):
            await _attest(service, orphan.id, actor_project="integ-attest")
    finally:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(sa.delete(tickets).where(tickets.c.id == orphan.id))
    assert await _rows(session_factory, ticket.id) == []


async def test_a_disabled_feature_refuses_attestation_but_still_lists(session_factory):
    """Attestation is a mutation; the ledger read stays available."""
    ticket, _binding, service = await _workflow(session_factory)
    await _attest(service, ticket.id)

    disabled = _service(session_factory, enabled=False)
    with pytest.raises(DeliveryError, match="delivery_disabled"):
        await _attest(disabled, ticket.id)

    listed = await disabled.list_attestations(ticket.id, actor_project="requester")
    assert len(listed.items) == 1


async def test_mcp_attest_requires_a_declared_caller(
    delivery_mcp,  # noqa: F811 - imported fixture
    session_factory,
    monkeypatch,
):
    """No X-Brain-Agent means no issuer; a declared one is stored as the provenance."""
    from brain_v42.mcp import provenance_middleware

    ticket, _binding, _service_instance = await _workflow(session_factory)
    arguments = {
        "ticket_id": str(ticket.id),
        "actor_project": "requester",
        "kind": "gate_passed",
        "payload": dict(_PAYLOAD),
        "idempotency_key": "mcp-attest-key",
        "emitted_at": _EMITTED.isoformat(),
    }
    async with Client(delivery_mcp) as client:
        monkeypatch.setattr(
            provenance_middleware, "get_http_headers", lambda **_kw: {"x-brain-agent": "unknown"}
        )
        unknown = await client.call_tool("brain_delivery_attest", arguments, raise_on_error=False)
        assert unknown.is_error and "invalid_issuer" in str(unknown.content)

        monkeypatch.setattr(
            provenance_middleware,
            "get_http_headers",
            lambda **_kw: {"x-brain-agent": "mcp-attester"},
        )
        stored = await client.call_tool("brain_delivery_attest", arguments, raise_on_error=False)
        assert not stored.is_error, str(stored.content)
        assert stored.structured_content["issuer_identity"] == "mcp-attester"


async def test_dream_scoped_token_is_denied_on_attestation(
    delivery_mcp,  # noqa: F811 - imported fixture
    monkeypatch,
):
    """The mutation goes through `_DeliveryRegistry`, so the phase bound covers it."""
    from brain_v42.mcp import dream_capabilities, tool_catalog

    access = AccessToken(
        token="test-scoped-token",
        client_id="dream-codex-scan",
        scopes=["brain:dream"],
        claims={
            "type": "scoped",
            "agent": "dream-codex-scan",
            "phase": "scan",
            "project_key": "brain-v42",
        },
    )
    monkeypatch.setattr(dream_capabilities, "get_access_token", lambda: access)
    monkeypatch.setattr(tool_catalog, "get_access_token", lambda: access)
    delivery_mcp.add_middleware(dream_capabilities.DreamCapabilityMiddleware())
    async with Client(delivery_mcp) as client:
        result = await client.call_tool(
            "brain_delivery_attest",
            {
                "ticket_id": str(uuid4()),
                "actor_project": "requester",
                "kind": "gate_passed",
                "payload": dict(_PAYLOAD),
                "idempotency_key": "denied",
                "emitted_at": _EMITTED.isoformat(),
            },
            raise_on_error=False,
        )
    assert result.is_error and "Dream capability authorization denied" in str(result.content)
