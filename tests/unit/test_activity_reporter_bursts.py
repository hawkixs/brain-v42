"""A burst of tool calls must no longer disappear (`1c40c36a`).

MEASURED BEFORE the fix, on 2026-08-23, with `max_in_flight=8`:

    n=  8  emitted=8  lost= 0   (  0.0 %)
    n=  9  emitted=8  lost= 1   ( 11.1 %)
    n= 20  emitted=8  lost=12   ( 60.0 %)   <- the briefing's figure, confirmed
    n= 50  emitted=8  lost=42   ( 84.0 %)

Definition of the loss: ``report()`` is SYNCHRONOUS and the tasks it creates only
run on the next loop iteration. A burst emitted in the SAME iteration fills
``_pending`` up to ``max_in_flight`` and everything else was discarded.

The fix does not raise the bound — it COALESCES. The wire format already accepts a
batch (``MAX_OBSERVATIONS = 64``) and a counter (``MAX_CALLS_PER_OBSERVATION =
1 000 000``); ``calls=1`` was hard-coded. A burst from the same actor therefore
collapses into ONE observation carrying ``calls=N``.

What these tests lock in beyond the fix: the RESIDUAL loss beyond the buffer stays
counted (otherwise an invisible loss is replaced by a faster invisible loss), and
the batch cannot cross the receiver's byte bound — without which the loss of ONE
observation would be traded for a 413 taking away the WHOLE batch.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from brain_v42.mcp.activity_reporter import (
    _MAX_BUFFERED,
    ActivityReporter,
    _observation,
)
from brain_v42.metrics.client_observation import (
    MAX_OBSERVATION_BYTES,
    MAX_OBSERVATIONS,
)
from brain_v42.provenance import MAX_ACTOR_LENGTH

_URL = "http://127.0.0.1:9200/v1/client-activity"


def _reporter(max_in_flight: int = 8) -> ActivityReporter:
    return ActivityReporter(url=_URL, max_in_flight=max_in_flight)


def _bodies(post: AsyncMock) -> list[dict[str, Any]]:
    return [json.loads(call.kwargs["content"]) for call in post.await_args_list]


def _observations(post: AsyncMock) -> list[dict[str, Any]]:
    return [obs for body in _bodies(post) for obs in body["observations"]]


def _total_calls(post: AsyncMock) -> int:
    return sum(int(obs["calls"]) for obs in _observations(post))


async def _burst(reporter: ActivityReporter, n: int, *, actor: Any = None) -> AsyncMock:
    """Emit n observations in the SAME loop iteration, then drain."""
    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock()
        for i in range(n):
            reporter.report(actor(i) if callable(actor) else (actor or f"agent-{i}"), None)
        await reporter.drain()
        return client.post


@pytest.mark.asyncio
async def test_a_burst_of_twenty_no_longer_loses_twelve_observations() -> None:
    """The ticket's figure, turned around: 12 lost out of 20 becomes 0 lost out of 20."""
    reporter = _reporter()

    post = await _burst(reporter, 20, actor="brain-v42")

    assert reporter.dropped == 0, f"{reporter.dropped} observation(s) encore perdue(s)"
    assert _total_calls(post) == 20, "des appels ont disparu du fil"


@pytest.mark.asyncio
@pytest.mark.parametrize("n", [9, 20, 50, 200])
async def test_no_burst_size_loses_a_single_call(n: int) -> None:
    """The bound must not merely move back: it must stop losing anything at all."""
    reporter = _reporter()

    post = await _burst(reporter, n, actor="brain-v42")

    assert reporter.dropped == 0
    assert _total_calls(post) == n


@pytest.mark.asyncio
async def test_a_burst_from_one_actor_collapses_into_a_single_observation() -> None:
    """This is the lever: `calls` was hard-coded to 1 while the wire accepts a counter."""
    reporter = _reporter()

    post = await _burst(reporter, 40, actor="brain-v42")

    coalesced = [obs for obs in _observations(post) if int(obs["calls"]) > 1]
    assert coalesced, "aucune observation n'a été coalescée : `calls` est resté à 1"
    assert _total_calls(post) == 40
    assert post.await_count < 40, "autant de POST que d'appels : rien n'a été agrégé"


@pytest.mark.asyncio
async def test_distinct_actors_are_never_merged_together() -> None:
    """Coalescing aggregates by identity, never across two actors."""
    reporter = _reporter()

    post = await _burst(reporter, 30, actor=lambda i: f"agent-{i % 3}")

    per_actor: dict[str, int] = {}
    for obs in _observations(post):
        per_actor[str(obs["actor"])] = per_actor.get(str(obs["actor"]), 0) + int(obs["calls"])
    assert per_actor == {"agent-0": 10, "agent-1": 10, "agent-2": 10}


@pytest.mark.asyncio
async def test_nothing_is_coalesced_below_the_in_flight_limit() -> None:
    """NEGATIVE WITNESS: without it, a fix aggregating EVERYTHING would pass the tests."""
    reporter = _reporter()

    post = await _burst(reporter, 8, actor="brain-v42")

    assert post.await_count == 8, "des observations ont été agrégées sans nécessité"
    assert all(int(obs["calls"]) == 1 for obs in _observations(post))
    assert reporter.coalesced == 0


@pytest.mark.asyncio
async def test_the_residual_loss_beyond_the_buffer_is_counted_and_spoken() -> None:
    """A loss that remains must be COUNTED — otherwise it has only been made faster."""
    reporter = _reporter(max_in_flight=1)
    release = asyncio.Event()
    n = MAX_OBSERVATIONS * 3

    async def slow_post(*_a: Any, **_k: Any) -> None:
        await release.wait()

    with patch.object(reporter, "_client") as client, capture_logs() as records:
        client.post = AsyncMock(side_effect=slow_post)
        reporter.report("agent-000", None)
        await asyncio.sleep(0)
        for i in range(n):  # ALL distinct actors: impossible to coalesce
            reporter.report(f"agent-{i:03d}", None)
        assert reporter.dropped > 0, "le tampon n'a pas de borne : il croît sans fin"
        release.set()
        await reporter.drain()

    assert [r for r in records if r.get("event") == "activity_reporter.dropped"], (
        "perte résiduelle silencieuse"
    )


@pytest.mark.asyncio
async def test_a_batch_never_exceeds_the_receiver_byte_budget() -> None:
    """Without this bound, the loss of ONE observation would be traded for a 413
    taking away the WHOLE batch — the same family of problem, but worse."""
    reporter = _reporter(max_in_flight=1)
    release = asyncio.Event()

    async def slow_post(*_a: Any, **_k: Any) -> None:
        await release.wait()

    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(side_effect=slow_post)
        reporter.report("x" * 400, None)
        await asyncio.sleep(0)
        for i in range(MAX_OBSERVATIONS * 2):
            # 400 characters: MEASURED as necessary. At 200, the COUNT bound bit
            # first and the BYTE bound stayed code never taken — the control
            # mutation showed it GREEN once removed.
            reporter.report(f"{i:03d}" + "y" * 400, None)
        release.set()
        await reporter.drain()

        for body in _bodies(client.post):
            encoded = json.dumps(body).encode()
            assert len(encoded) <= MAX_OBSERVATION_BYTES, (
                f"lot de {len(encoded)} octets > borne récepteur {MAX_OBSERVATION_BYTES}"
            )
            assert len(body["observations"]) <= MAX_OBSERVATIONS


@pytest.mark.asyncio
async def test_report_never_raises_even_if_the_coalescing_machinery_explodes() -> None:
    """The emitter NEVER breaks the call it observes — the new code included."""
    reporter = _reporter(max_in_flight=1)

    def _explode(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("la coalescence est cassée")

    async def slow_post(*_a: Any, **_k: Any) -> None:
        await asyncio.sleep(3600)

    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(side_effect=slow_post)
        reporter.report("brain-v42", None)
        await asyncio.sleep(0)
        with patch.object(ActivityReporter, "_coalesce", _explode):
            reporter.report("brain-v42", None)  # ne doit RIEN lever
        assert reporter.dropped >= 1, "une panne de coalescence doit compter la perte"


@pytest.mark.asyncio
async def test_the_buffer_flushes_on_its_own_when_a_slot_frees() -> None:
    """Without this callback, coalescing would defer without bound instead of losing.

    `drain()` is called neither in production nor by a client: `close()` is wired
    nowhere. If only `drain()` emptied the buffer, a burst followed by silence would
    keep its observations in memory until the next tool call — replacing a visible
    loss with an invisible latency.
    """
    reporter = _reporter(max_in_flight=2)

    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock()
        for _ in range(20):
            reporter.report("brain-v42", None)
        # NO drain() at all: we only let the loop run.
        for _ in range(12):
            await asyncio.sleep(0)
        assert not reporter._buffer, "le tampon ne se vide que sur drain()"
        assert _total_calls(client.post) == 20


def test_the_count_bound_alone_keeps_a_batch_under_the_receiver_limit() -> None:
    """MEASURED: on normalised input, the BYTE bound can never bite.

    An actor is capped at ``MAX_ACTOR_LENGTH`` (64), a session is a UUID (36) and a
    transport a fixed-length hex string. The worst possible batch therefore weighs
    far less than the receiver's bound, and it is the COUNT bound that really bounds
    the batch.

    This test is the real guard: it reddens if someone raises ``MAX_ACTOR_LENGTH``
    or ``MAX_OBSERVATIONS`` without re-checking that the batch still fits. The
    emitter's byte bound stays, but as a defence for a future caller that would NOT
    call ``normalize_agent`` — not as the production path.
    """
    worst = _observation(
        ("A" * MAX_ACTOR_LENGTH, "00000000-0000-4000-8000-000000000000", "ab" * 16), 1
    )
    cost = len(json.dumps(worst).encode()) + 1
    envelope = len(json.dumps({"observations": []}).encode())
    # `_MAX_BUFFERED` buffered observations, PLUS the one that triggers the flush.
    worst_batch = envelope + cost * (_MAX_BUFFERED + 1)

    assert worst_batch <= MAX_OBSERVATION_BYTES, (
        f"le pire lot normalisé pèse {worst_batch} o pour une borne récepteur "
        f"de {MAX_OBSERVATION_BYTES} o : relever MAX_ACTOR_LENGTH ou "
        f"MAX_OBSERVATIONS demande de revoir le découpage des lots"
    )
