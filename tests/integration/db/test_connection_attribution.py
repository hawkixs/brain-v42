"""The connection ledger records credentials, never client-declared actor labels."""

from uuid import uuid4

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import brain_session_connections
from brain_v42.models.brain_session import BrainSessionIdentityConflictError
from brain_v42.provenance import set_current_actor, set_current_principal, set_current_transport
from tests.integration.db.test_attribution_lock import (
    AttributionCase,
)
from tests.integration.db.test_attribution_lock import (
    attribution_case as _attribution_case,  # noqa: F401 -- register the shared fixture
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("command", ["bind", "resume"])
async def test_connection_records_principal_instead_of_declared_actor(
    attribution_case: AttributionCase, command: str
) -> None:
    case = attribution_case
    started = await case.repo.start(case.project, uuid4().hex)
    connection_id = uuid4().hex
    set_current_transport(connection_id)
    set_current_principal("workstation-claude")
    set_current_actor("red-rail")
    if command == "bind":
        await case.service.bind(started.session.id, started.session.client_key, case.slot)
    else:
        await case.service.resume(started.session.id, started.session.client_key)
    async with case.factory() as session:
        client_ids = (
            (
                await session.execute(
                    sa.select(brain_session_connections.c.client_id).where(
                        brain_session_connections.c.session_id == started.session.id,
                        brain_session_connections.c.connection_id == connection_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert client_ids == ["workstation-claude"]


@pytest.mark.parametrize("command", ["bind", "resume"])
async def test_second_principal_cannot_overwrite_first_connection_attribution(
    attribution_case: AttributionCase, command: str
) -> None:
    case = attribution_case
    set_current_principal("workstation-claude")
    started = await case.service.start(case.project, uuid4().hex)
    before = await case.snapshot(started.session.id)
    set_current_principal("red-rail")
    set_current_actor("workstation-claude")
    with pytest.raises(BrainSessionIdentityConflictError):
        if command == "bind":
            await case.service.bind(started.session.id, started.session.client_key, case.slot)
        else:
            await case.service.resume(started.session.id, started.session.client_key)
    assert await case.snapshot(started.session.id) == before
