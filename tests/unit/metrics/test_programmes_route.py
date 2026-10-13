"""The sidecar's `GET /programmes` (contract docs/contracts/programmes_v1.md §1, §3, §4).

The block leaves `/metrics` for its own route so the payload red-monitor stores on
every brain-card probe does not grow. Body: `{"programmes": <block>}` while brain
publishes, `{}` when this cycle's read failed. The block is memoised with the
other slow blocks (decision 1669d429): single flight, `generated_at` stamped once.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import make_mocked_request

from brain_v42.metrics.collector import MetricsCollector
from brain_v42.metrics.server import MetricsServer
from brain_v42.metrics.slow_block_cache import SlowBlockCache
from tests.unit.test_programmes_v1_contract import SHAPE, check_semantics, conforms

NOW = datetime(2026, 10, 13, 9, tzinfo=UTC)
GATE = {
    "key": "G7",
    "title": "red-backup deployed on red-base",
    "owner": "red-backup",
    "state": "in_progress",
    "depends_on": [],
    "weight": 4,
    "waiting_on_operator": {"ask": "validate spec", "since": "2026-10-13T08:00:00+00:00"},
    "progress": {"done": 6, "total": 15, "unit": "tasks"},
    "note": "first deploy",
    "updated_at": "2026-10-13T08:30:00+00:00",
    "updated_by": "red-backup",
}
BLOCK = {
    "version": "programmes/v1",
    "programmes": [
        {
            "key": "brain-move-red-base",
            "title": "Brain to red-base",
            "coordinator": "brain-v42",
            "state": "active",
            "last_change": None,
            "gates": [GATE],
        }
    ],
}


def _server(collect: AsyncMock) -> tuple[MetricsServer, AsyncMock]:
    collector = MagicMock(spec=MetricsCollector)
    collector.collect_programmes = collect
    cache = SlowBlockCache(ttl_seconds=30, error_ttl_seconds=5, wall_clock=lambda: NOW)
    server = MetricsServer(collector, MagicMock(), port=0, host="127.0.0.1", slow_block_cache=cache)
    return server, collect


async def _get(server: MetricsServer) -> tuple[int, dict]:
    response = await server._handle_programmes(make_mocked_request("GET", "/programmes"))
    return response.status, json.loads(response.body)


def test_the_route_is_a_plain_get() -> None:
    server, _ = _server(AsyncMock(return_value=BLOCK))
    app = server._build_app()
    routes = {(route.method, route.resource.canonical) for route in app.router.routes()}
    assert ("GET", "/programmes") in routes


async def test_the_body_wraps_the_contract_block_stamped_once() -> None:
    server, collect = _server(AsyncMock(return_value=BLOCK))
    status, body = await _get(server)
    assert status == 200
    assert set(body) == {"programmes"}
    block = body["programmes"]
    assert block["generated_at"] == NOW.isoformat()
    conforms(block, SHAPE["shape"], SHAPE["objects"])
    check_semantics(block)
    await _get(server)
    collect.assert_awaited_once()  # memoised: one read for both requests


async def test_an_empty_publication_is_present_not_absent() -> None:
    server, _ = _server(AsyncMock(return_value={"version": "programmes/v1", "programmes": []}))
    _, body = await _get(server)
    assert body["programmes"]["programmes"] == []
    assert body["programmes"]["generated_at"] == NOW.isoformat()


async def test_a_failed_read_answers_an_empty_object_never_null() -> None:
    server, _ = _server(AsyncMock(side_effect=RuntimeError("database down")))
    status, body = await _get(server)
    assert (status, body) == (200, {})
