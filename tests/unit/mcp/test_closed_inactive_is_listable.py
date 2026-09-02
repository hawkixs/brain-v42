"""The 4th session state is LISTABLE — otherwise the night closes without a witness.

046 delivered `closed_inactive` in BOTH `CHECK`s of `brain_sessions`, and the
nightly sweep can set it. But `SessionStatusFilter`, the only filter published in
the MCP catalogue, did not name it (`24ca3b73`): an operator therefore could not
list what the night had closed automatically.

**The service and the repository already supported it.** `_normalize_status`
accepts any member of `BrainSessionStatus`, and `list_sessions` filters on
`brain_sessions.c.status == status` without enumerating. The hole lived in the
single published literal — a state reachable in the database, writable by the
server, and unrequestable by a client. Another schema laid down with no reader.

**Why it is urgent now**: the inactivity sweep flag is armed on the wrong systemd
unit, so `inactive_cutoff=off` and zero closures. The day the operator places the
drop-in on `brain-v42-dream`, unobserved sessions will be closed from the first
night — and without this filter, nobody will be able to say which.

The anti-drift witness is `test_the_filter_covers_every_persisted_status`: it
derives its expectations from the enumeration itself, so a 5th state added to
`BrainSessionStatus` without being published in the filter **turns red here**,
instead of being discovered by an operator who cannot find their sessions.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from pydantic import ValidationError

from brain_v42.models.brain_session import BrainSessionStatus

#: The two DERIVED filters, which are not persisted statuses: `stale` is computed
#: from `last_heartbeat_at`, `all` is the absence of a filter. Distinguishing them
#: matters — conflating them with the statuses would let a test pass that only
#: counts entries, without checking which.
DERIVED_FILTERS = frozenset({"stale", "all"})


def _server(service: Any) -> FastMCP:
    from brain_v42.mcp.tools.session_lifecycle_tools import register_session_lifecycle_tools

    server = FastMCP("closed-inactive-listable")
    register_session_lifecycle_tools(server, service, AsyncMock(return_value=""))
    return server


def _service() -> Any:
    service = MagicMock()
    service.list = AsyncMock(return_value=MagicMock())
    return service


async def _status_enum() -> list[str]:
    server = _server(_service())
    tool = await server.get_tool("brain_session_list")
    assert tool is not None
    return list(tool.parameters["properties"]["status"]["enum"])


async def test_closed_inactive_is_an_accepted_list_filter() -> None:
    """The published filter names `closed_inactive`.

    The assertion is on the PUBLISHED schema: that is what a client can request,
    and the only level at which the omission was observable.
    """
    assert BrainSessionStatus.CLOSED_INACTIVE.value in await _status_enum()


async def test_the_filter_covers_every_persisted_status() -> None:
    """Anti-drift: the filter covers ALL persisted statuses, derived from the enum.

    Nothing is hardcoded here. A 5th state added to `BrainSessionStatus` and
    forgotten in the filter turns this line red — it is the witness that was
    missing when 046 added the 4th.
    """
    published = set(await _status_enum())
    persisted = {status.value for status in BrainSessionStatus}

    assert persisted <= published, f"statuts persistés absents du filtre : {persisted - published}"
    # Negative witness: the filter publishes NOTHING beyond the persisted
    # statuses and the two derived filters. Without this half, publishing
    # anything (a nonexistent state, a typo) would pass.
    assert published == persisted | DERIVED_FILTERS, (
        f"le filtre publie des valeurs inattendues : {published - persisted - DERIVED_FILTERS}"
    )


async def test_listing_closed_inactive_reaches_the_service() -> None:
    """The path is TAKEN, not merely declared.

    An `enum` containing the value does not prove a call traverses it: validation
    could refuse it further down, or the tool could rewrite it. This test CALLS
    the tool and reads what the service actually received.
    """
    service = _service()
    tool = await _server(service).get_tool("brain_session_list")
    assert tool is not None

    await tool.run({"project_key": "brain-v42", "status": "closed_inactive"})

    service.list.assert_called_once()
    assert service.list.call_args.kwargs["status"] == "closed_inactive"


async def test_an_unknown_status_is_still_refused() -> None:
    """Widening the filter is not opening it. Negative witness for the widening.

    Without it, replacing the `Literal` with a bare `str` would turn the three
    previous tests green while removing validation — exactly the misreading this
    batch must avoid.
    """
    tool = await _server(_service()).get_tool("brain_session_list")
    assert tool is not None

    with pytest.raises(ValidationError):
        await tool.run({"project_key": "brain-v42", "status": "closed-inactive"})
