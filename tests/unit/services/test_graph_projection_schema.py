"""Neo4j schema required by the durable PostgreSQL projection."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_projection_schema_covers_every_projected_node_identity() -> None:
    from brain_v42.services.graph_projection_schema import (
        ensure_graph_projection_schema,
    )

    session = MagicMock()
    session.run = AsyncMock()
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=None)
    driver = MagicMock()
    driver.session.return_value = session_context

    await ensure_graph_projection_schema(driver)

    queries = [call.args[0] for call in session.run.await_args_list]
    assert len(queries) == 11
    assert any("(n:Feature) REQUIRE n.id IS UNIQUE" in query for query in queries)
    assert any("(n:Plan) REQUIRE n.id IS UNIQUE" in query for query in queries)
    assert any("(n:Domain) REQUIRE n.name IS UNIQUE" in query for query in queries)
    assert any("(n:Project) REQUIRE n.project_key IS UNIQUE" in query for query in queries)
    assert any("(n:BrainProjectionFence) REQUIRE n.name IS UNIQUE" in query for query in queries)
    assert any(
        "(n:BrainProjectionCursor) REQUIRE n.aggregate_key IS UNIQUE" in query for query in queries
    )


@pytest.mark.asyncio
async def test_projection_schema_failure_is_fail_closed() -> None:
    from brain_v42.services.graph_projection_schema import (
        ensure_graph_projection_schema,
    )

    session = MagicMock()
    session.run = AsyncMock(side_effect=RuntimeError("neo schema unavailable"))
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=None)
    driver = MagicMock()
    driver.session.return_value = session_context

    with pytest.raises(RuntimeError, match="neo schema unavailable"):
        await ensure_graph_projection_schema(driver)
