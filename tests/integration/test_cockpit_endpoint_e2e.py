"""End-to-end: 100 requests in ~10s against /api/cockpit → assert p95 < 100ms.

Skipped unless the brain-v42 sidecar is reachable on 127.0.0.1:9200.
Validates success criterion #3 + schema conformance vs the red-monitor handoff.
"""

from __future__ import annotations

import asyncio
import re
import time

import aiohttp
import pytest

pytestmark = pytest.mark.asyncio


async def _hit(session: aiohttp.ClientSession, url: str) -> float:
    t0 = time.monotonic()
    async with session.get(url) as resp:
        await resp.json()
        assert resp.status == 200
    return (time.monotonic() - t0) * 1000


async def test_cockpit_p95_under_100ms_under_load() -> None:
    url = "http://127.0.0.1:9200/api/cockpit"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=2.0)) as resp:
                if resp.status != 200:
                    pytest.skip(f"sidecar not ready (status {resp.status})")
        except Exception:
            pytest.skip("sidecar unreachable")

        latencies: list[float] = []
        for _ in range(10):
            batch = await asyncio.gather(*(_hit(session, url) for _ in range(10)))
            latencies.extend(batch)
            await asyncio.sleep(1.0)

    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95)]
    assert p95 < 100, f"p95={p95:.1f}ms exceeds 100ms budget"


async def test_cockpit_schema_matches_contract() -> None:
    url = "http://127.0.0.1:9200/api/cockpit"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=2.0)) as resp:
                if resp.status != 200:
                    pytest.skip(f"sidecar not ready (status {resp.status})")
                data = await resp.json()
        except Exception:
            pytest.skip("sidecar unreachable")

    required = {
        "version",
        "pid",
        "uptime_s",
        "endpoint",
        "metrics",
        "activeConvs",
        "tools",
        "skills",
        "memory",
        "retrieval",
        "latencyBuckets",
        "cost",
        "handoff",
        "rpsHistory",
        "p95History",
        "errHistory",
        "costHistory",
        "recent",
    }
    assert required <= set(data.keys()), f"missing keys: {required - set(data.keys())}"
    assert "episodes" in data["memory"]
    assert "p95" in data["metrics"]
    active_conversations = data["activeConvs"]
    assert isinstance(active_conversations, list)
    assert data["metrics"]["active_convs"] == len(active_conversations)
    assert data["metrics"]["ctx_tokens"] == sum(
        conversation["tokens"] for conversation in active_conversations
    )
    for conversation in active_conversations:
        assert set(conversation) == {
            "id",
            "topic",
            "agent",
            "started",
            "turns",
            "tokens",
            "model",
            "cost",
        }
        assert re.fullmatch(r"codex-[0-9a-f]{32}", conversation["id"])
        assert conversation["topic"] == "[redacted]"
        assert conversation["agent"] == "codex"
        assert isinstance(conversation["started"], str)
        assert isinstance(conversation["turns"], int) and conversation["turns"] >= 0
        assert isinstance(conversation["tokens"], int) and conversation["tokens"] >= 0
        assert isinstance(conversation["model"], str) and conversation["model"]
        assert conversation["cost"] is None
    assert data["skills"] == []
    assert data["handoff"] == []
