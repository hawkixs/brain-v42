"""End to end on PostgreSQL: rows the tools write, served by GET /programmes, as frozen.

The repository writes, the collector reads through the same serializer, the cache
stamps `generated_at`, and the body passes every check of the contract test
(docs/contracts/programmes_v1.md): shape, vocabularies, limits, graph, timestamps.
Runs on the module-private head: the live-programme cap is global.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from aiohttp.test_utils import make_mocked_request
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain_v42.db.tables import programmes, project_contexts
from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.server import MetricsServer
from brain_v42.repositories.pg_programme import PgProgrammeRepo
from tests.unit.test_programmes_v1_contract import SHAPE, check_semantics, conforms

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def factory(private_head_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker]:
    session_factory = async_sessionmaker(
        private_head_engine, class_=AsyncSession, expire_on_commit=False
    )
    yield session_factory
    async with session_factory.begin() as session:
        await session.execute(sa.delete(programmes))


async def _project(factory: async_sessionmaker, stem: str) -> str:
    key = f"{stem}-{uuid4().hex[:8]}"
    async with factory.begin() as session:
        await session.execute(
            pg_insert(project_contexts)
            .values(project_key=key, name=key, description="sidecar fixture")
            .on_conflict_do_nothing(index_elements=["project_key"])
        )
    return key


async def _served(factory: async_sessionmaker) -> dict:
    collector = MetricsCollector.__new__(MetricsCollector)
    collector._session_factory = factory
    server = MetricsServer(collector, MagicMock(), port=0, host="127.0.0.1")
    response = await server._handle_programmes(make_mocked_request("GET", "/programmes"))
    assert response.status == 200
    return json.loads(response.body)


async def test_what_the_tools_write_is_served_exactly_as_frozen(factory) -> None:
    coordinator = await _project(factory, "coord")
    owner = await _project(factory, "owner")
    repo = PgProgrammeRepo(factory)
    await repo.upsert_programme(caller=coordinator, key="brain-move", title="Brain to red-base")
    await repo.upsert_gate(
        caller=coordinator, programme="brain-move", gate="G6", title="t", owner=owner
    )
    await repo.upsert_gate(
        caller=coordinator,
        programme="brain-move",
        gate="G7",
        title="red-backup deployed on red-base",
        owner=owner,
        depends_on=["G6"],
        weight=4,
    )
    await repo.report_gate(
        caller=owner,
        programme="brain-move",
        gate="G7",
        state="in_progress",
        progress={"done": 6, "total": 15, "unit": "tasks"},
        note="first deploy without the brain target",
    )
    await repo.request(caller=owner, programme="brain-move", gate="G7", ask="validate spec")
    await repo.upsert_programme(caller=coordinator, key="lot-done", title="Done", state="done")

    body = await _served(factory)

    assert set(body) == {"programmes"}
    block = body["programmes"]
    conforms(block, SHAPE["shape"], SHAPE["objects"])
    check_semantics(block)
    assert [p["key"] for p in block["programmes"]] == ["brain-move", "lot-done"]
    move = block["programmes"][0]
    assert move["last_change"]["by"] == owner and move["gates"][1]["updated_by"] == owner
    assert len(json.dumps(body).encode()) < 16 * 1024


async def test_nothing_running_is_published_as_an_empty_list(factory) -> None:
    body = await _served(factory)
    assert body["programmes"]["programmes"] == []
    assert set(body["programmes"]) == {"version", "generated_at", "programmes"}
