"""Attribution follows verified identity across lifecycle calls and savepoints."""

from collections.abc import Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from brain_v42 import provenance
from brain_v42.credentials.reasons import refusal_counts, reset_refusal_counts
from brain_v42.db.session_derived_capture import absorb_tracer_ledger
from brain_v42.models import brain_session as models
from brain_v42.provenance import set_current_principal, set_current_transport
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
from brain_v42.repositories.pg_client_credentials import ForeignClientAttachError
from brain_v42.services.brain_session_service import BrainSessionService


@pytest.fixture(autouse=True)
def attribution_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "brain_v42.db.session_derived_capture.get_settings",
        lambda: SimpleNamespace(brain_session_derived_capture_enabled=True),
    )
    monkeypatch.setattr(
        "brain_v42.services.brain_session_service.get_settings",
        lambda: SimpleNamespace(
            brain_session_derived_capture_enabled=True,
            elevatable_client_ids=frozenset({"workstation-claude"}),
        ),
    )
    set_current_principal("workstation-claude")
    set_current_transport(uuid4().hex)
    reset_refusal_counts()
    yield
    set_current_principal(None)
    set_current_transport(None)
    reset_refusal_counts()


def repository_double() -> MagicMock:
    repo = MagicMock()
    result = MagicMock()
    result.session.id = uuid4()
    for name in ("start", "resume", "bind", "relay", "capture", "heartbeat", "end"):
        setattr(repo, name, AsyncMock(return_value=result))
    repo.absorb_derived_capture_outcome = AsyncMock(return_value=None)
    repo.attributed_knowledge_ids = AsyncMock(return_value=[])
    repo.record_seen_connection = AsyncMock()
    return repo


async def test_start_passes_verified_opener() -> None:
    repo = repository_double()
    await BrainSessionService(repo).start("brain-v42", "key")
    repo.start.assert_awaited_once_with("brain-v42", "key", client_id="workstation-claude")


@pytest.mark.parametrize("command", ["resume", "capture", "heartbeat", "end", "relay", "start"])
async def test_lifecycle_passes_principal_to_every_absorption(command: str) -> None:
    repo = repository_double()
    service = BrainSessionService(repo)
    session_id = uuid4()
    if command == "start":
        await service.start("brain-v42", "key")
    elif command == "relay":
        await service.relay(
            session_id,
            "key",
            summary="summary",
            handover="focus",
            expected_focus_revision=0,
            new_client_key="next",
            initiator="operator",
            nothing_to_capture_reason="empty",
        )
    elif command == "end":
        await service.end(session_id, "key", "summary", "focus", 0)
    elif command == "capture":
        await service.capture(session_id, "key", [uuid4()])
    else:
        await getattr(service, command)(session_id, "key")
    for call in repo.absorb_derived_capture_outcome.await_args_list:
        assert call.kwargs["client_id"] == "workstation-claude"
        assert call.kwargs["elevatable_client_ids"] == {"workstation-claude"}
    assert repo.absorb_derived_capture_outcome.await_count == (2 if command == "relay" else 1)


async def test_relay_passes_successor_opener() -> None:
    repo = repository_double()
    await BrainSessionService(repo).relay(
        uuid4(),
        "key",
        summary="summary",
        handover="focus",
        expected_focus_revision=0,
        new_client_key="next",
        initiator="operator",
        nothing_to_capture_reason="empty",
    )
    assert repo.relay.await_args.kwargs["client_id"] == "workstation-claude"


async def test_bind_records_connection_inside_binding_transaction() -> None:
    repo = repository_double()
    await BrainSessionService(repo).bind(uuid4(), "key", uuid4())
    assert repo.bind.await_args.kwargs["client_id"] == "workstation-claude"
    assert repo.bind.await_args.kwargs["connection_id"] is not None
    repo.record_seen_connection.assert_not_awaited()


@pytest.mark.parametrize("command", ["bind", "resume"])
async def test_foreign_attach_surfaces_once_as_identity_error(command: str) -> None:
    repo = repository_double()
    error = models.BrainSessionForeignClientAttachError("red-rail", "workstation-claude")
    getattr(
        repo, "bind" if command == "bind" else "absorb_derived_capture_outcome"
    ).side_effect = error
    set_current_principal("red-rail")
    session_id = uuid4()
    with capture_logs() as events, pytest.raises(models.BrainSessionIdentityConflictError) as exc:
        if command == "bind":
            await BrainSessionService(repo).bind(session_id, "key", uuid4())
        else:
            await BrainSessionService(repo).resume(session_id, "key")
    assert exc.value.code == "foreign_client_attach"
    refused = [event for event in events if event["event"] == "mcp_auth.refused"]
    assert len(refused) == 1
    assert refused[0]["status"] == 403
    assert refused[0]["requesting_client_id"] == "red-rail"
    assert refused[0]["owner_client_id"] == "workstation-claude"
    assert refused[0]["session_id"] == str(session_id)
    assert refusal_counts()["foreign_client_attach"] == 1
    repo.resume.assert_not_awaited()


async def test_savepoint_does_not_swallow_foreign_attach() -> None:
    session = MagicMock()
    session.begin_nested.return_value.__aenter__ = AsyncMock()
    session.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock()
    error = models.BrainSessionForeignClientAttachError("red-rail", None)
    check_owner = AsyncMock(side_effect=error)
    with pytest.raises(models.BrainSessionIdentityConflictError):
        await absorb_tracer_ledger(
            session,
            SimpleNamespace(id=uuid4()),
            uuid4().hex,
            client_id="red-rail",
            check_owner=check_owner,
        )
    session.execute.assert_not_awaited()


async def test_connection_writer_checks_owner_before_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = MagicMock()
    session.execute = AsyncMock()
    session_id = uuid4()
    repo = PgBrainSessionRepo()

    @asynccontextmanager
    async def transaction(*args: object, **kwargs: object):
        yield session

    monkeypatch.setattr(repo, "_maybe_session", transaction)
    claim = AsyncMock(side_effect=ForeignClientAttachError("red-rail", "workstation-claude"))
    monkeypatch.setattr(
        "brain_v42.repositories.pg_client_credentials.PgClientCredentialRepo.claim_or_check_session_owner",
        claim,
    )
    with pytest.raises(models.BrainSessionIdentityConflictError) as exc:
        await repo.record_seen_connection(
            session_id,
            uuid4().hex,
            client_id="red-rail",
            elevatable_client_ids={"workstation-claude"},
        )
    assert exc.value.code == "foreign_client_attach"
    assert claim.await_args.kwargs["session"] is session
    session.execute.assert_not_awaited()


async def test_identity_refusal_reaches_masked_mcp_client() -> None:
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError

    from brain_v42.mcp.business_errors import surface_business_errors

    server = FastMCP("attribution-test", mask_error_details=True)

    @server.tool
    async def attach() -> str:
        raise models.BrainSessionForeignClientAttachError("red-rail", "workstation-claude")

    await surface_business_errors(server)
    with pytest.raises(ToolError, match="foreign_client_attach"):
        await server.call_tool("attach", {})


async def test_refusal_carries_transport_peer() -> None:
    repo = repository_double()
    repo.absorb_derived_capture_outcome.side_effect = models.BrainSessionForeignClientAttachError(
        "red-rail", "workstation-claude"
    )
    provenance.set_current_peer("127.0.0.1")
    try:
        with capture_logs() as events, pytest.raises(models.BrainSessionIdentityConflictError):
            await BrainSessionService(repo).resume(uuid4(), "key")
        refused = [event for event in events if event["event"] == "mcp_auth.refused"]
        assert refused[0]["peer"] == "127.0.0.1"
    finally:
        provenance.set_current_peer(None)


def test_absorption_log_does_not_disclose_connection_id() -> None:
    from brain_v42.db.session_derived_capture import AbsorptionOutcome, _log_absorption

    connection_id = uuid4().hex
    with capture_logs() as events:
        _log_absorption(
            SimpleNamespace(id=uuid4(), project_key="brain-v42"),
            connection_id,
            AbsorptionOutcome(reason="nothing_to_absorb"),
        )
    assert connection_id not in str(events)


async def test_attribution_still_runs_when_derived_capture_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "brain_v42.services.brain_session_service.get_settings",
        lambda: SimpleNamespace(
            brain_session_derived_capture_enabled=False,
            elevatable_client_ids=frozenset({"workstation-claude"}),
        ),
    )
    repo = repository_double()
    await BrainSessionService(repo).resume(uuid4(), "key")
    assert (
        repo.absorb_derived_capture_outcome.await_args.kwargs["client_id"] == "workstation-claude"
    )


async def test_attributed_absorption_requires_owner_check() -> None:
    with pytest.raises(models.BrainSessionIdentityConflictError):
        await absorb_tracer_ledger(
            MagicMock(), SimpleNamespace(id=uuid4()), uuid4().hex, client_id="red-rail"
        )
