"""Une rafale d'appels de tool ne doit plus disparaître (`1c40c36a`).

MESURÉ AVANT correctif, le 2026-08-23, avec `max_in_flight=8` :

    n=  8  émis=8  perdus= 0   (  0,0 %)
    n=  9  émis=8  perdus= 1   ( 11,1 %)
    n= 20  émis=8  perdus=12   ( 60,0 %)   <- le chiffre du briefing, confirmé
    n= 50  émis=8  perdus=42   ( 84,0 %)

Définition de la perte : ``report()`` est SYNCHRONE et les tâches qu'il crée ne
tournent qu'au tour de boucle suivant. Une rafale émise dans le MÊME tour remplit
``_pending`` jusqu'à ``max_in_flight`` et tout le reste était jeté.

Le correctif ne relève pas la borne — il COALESCE. Le format de fil accepte déjà
un lot (``MAX_OBSERVATIONS = 64``) et un compteur (``MAX_CALLS_PER_OBSERVATION =
1 000 000``) ; ``calls=1`` était en dur. Une rafale du même acteur s'effondre donc
en UNE observation portant ``calls=N``.

Ce que ces tests verrouillent en plus du correctif : la perte RÉSIDUELLE au-delà
du tampon reste comptée (sinon on remplace une perte invisible par une perte
invisible plus rapide), et le lot ne peut pas franchir la borne d'octets du
récepteur — sans quoi on échangerait la perte d'UNE observation contre un 413 qui
emporte le lot ENTIER.
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
    """Émet n observations dans le MÊME tour de boucle, puis draine."""
    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock()
        for i in range(n):
            reporter.report(actor(i) if callable(actor) else (actor or f"agent-{i}"), None)
        await reporter.drain()
        return client.post


@pytest.mark.asyncio
async def test_a_burst_of_twenty_no_longer_loses_twelve_observations() -> None:
    """Le chiffre du ticket, retourné : 12 perdues sur 20 devient 0 perdue sur 20."""
    reporter = _reporter()

    post = await _burst(reporter, 20, actor="brain-v42")

    assert reporter.dropped == 0, f"{reporter.dropped} observation(s) encore perdue(s)"
    assert _total_calls(post) == 20, "des appels ont disparu du fil"


@pytest.mark.asyncio
@pytest.mark.parametrize("n", [9, 20, 50, 200])
async def test_no_burst_size_loses_a_single_call(n: int) -> None:
    """La borne ne doit pas seulement reculer : elle ne doit plus perdre du tout."""
    reporter = _reporter()

    post = await _burst(reporter, n, actor="brain-v42")

    assert reporter.dropped == 0
    assert _total_calls(post) == n


@pytest.mark.asyncio
async def test_a_burst_from_one_actor_collapses_into_a_single_observation() -> None:
    """C'est le levier : `calls` était en dur à 1 alors que le fil accepte un compteur."""
    reporter = _reporter()

    post = await _burst(reporter, 40, actor="brain-v42")

    coalesced = [obs for obs in _observations(post) if int(obs["calls"]) > 1]
    assert coalesced, "aucune observation n'a été coalescée : `calls` est resté à 1"
    assert _total_calls(post) == 40
    assert post.await_count < 40, "autant de POST que d'appels : rien n'a été agrégé"


@pytest.mark.asyncio
async def test_distinct_actors_are_never_merged_together() -> None:
    """La coalescence agrège par identité, jamais à travers deux acteurs."""
    reporter = _reporter()

    post = await _burst(reporter, 30, actor=lambda i: f"agent-{i % 3}")

    per_actor: dict[str, int] = {}
    for obs in _observations(post):
        per_actor[str(obs["actor"])] = per_actor.get(str(obs["actor"]), 0) + int(obs["calls"])
    assert per_actor == {"agent-0": 10, "agent-1": 10, "agent-2": 10}


@pytest.mark.asyncio
async def test_nothing_is_coalesced_below_the_in_flight_limit() -> None:
    """TÉMOIN NÉGATIF : sans lui, un correctif qui agrège TOUT passerait les tests."""
    reporter = _reporter()

    post = await _burst(reporter, 8, actor="brain-v42")

    assert post.await_count == 8, "des observations ont été agrégées sans nécessité"
    assert all(int(obs["calls"]) == 1 for obs in _observations(post))
    assert reporter.coalesced == 0


@pytest.mark.asyncio
async def test_the_residual_loss_beyond_the_buffer_is_counted_and_spoken() -> None:
    """Une perte qui subsiste doit être COMPTÉE — sinon on l'a juste rendue plus rapide."""
    reporter = _reporter(max_in_flight=1)
    release = asyncio.Event()
    n = MAX_OBSERVATIONS * 3

    async def slow_post(*_a: Any, **_k: Any) -> None:
        await release.wait()

    with patch.object(reporter, "_client") as client, capture_logs() as records:
        client.post = AsyncMock(side_effect=slow_post)
        reporter.report("agent-000", None)
        await asyncio.sleep(0)
        for i in range(n):  # acteurs TOUS distincts : impossible de coalescer
            reporter.report(f"agent-{i:03d}", None)
        assert reporter.dropped > 0, "le tampon n'a pas de borne : il croît sans fin"
        release.set()
        await reporter.drain()

    assert [r for r in records if r.get("event") == "activity_reporter.dropped"], (
        "perte résiduelle silencieuse"
    )


@pytest.mark.asyncio
async def test_a_batch_never_exceeds_the_receiver_byte_budget() -> None:
    """Sans cette borne, on échangerait la perte d'UNE observation contre un 413
    qui emporte le lot ENTIER — la même famille de problème, en pire."""
    reporter = _reporter(max_in_flight=1)
    release = asyncio.Event()

    async def slow_post(*_a: Any, **_k: Any) -> None:
        await release.wait()

    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(side_effect=slow_post)
        reporter.report("x" * 400, None)
        await asyncio.sleep(0)
        for i in range(MAX_OBSERVATIONS * 2):
            # 400 caractères : MESURÉ nécessaire. À 200, la borne de COMPTE mordait
            # la première et la borne d'OCTETS restait du code jamais emprunté —
            # la mutation de contrôle l'a montrée VERTE une fois retirée.
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
    """L'émetteur ne casse JAMAIS l'appel qu'il observe — y compris le code neuf."""
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
    """Sans ce rappel, la coalescence différerait sans borne au lieu de perdre.

    `drain()` n'est appelé ni en production ni par un client : `close()` n'est
    câblé nulle part. Si seul `drain()` vidait le tampon, une rafale suivie d'un
    silence garderait ses observations en mémoire jusqu'au prochain appel de
    tool — remplaçant une perte visible par une latence invisible.
    """
    reporter = _reporter(max_in_flight=2)

    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock()
        for _ in range(20):
            reporter.report("brain-v42", None)
        # AUCUN drain() : on laisse seulement la boucle tourner.
        for _ in range(12):
            await asyncio.sleep(0)
        assert not reporter._buffer, "le tampon ne se vide que sur drain()"
        assert _total_calls(client.post) == 20


def test_the_count_bound_alone_keeps_a_batch_under_the_receiver_limit() -> None:
    """MESURÉ : en entrée normalisée, la borne d'OCTETS ne peut jamais mordre.

    Un acteur est plafonné à ``MAX_ACTOR_LENGTH`` (64), une session est un UUID
    (36) et un transport une chaîne hexadécimale de longueur fixe. Le pire lot
    possible pèse donc bien moins que la borne du récepteur, et c'est la borne de
    COMPTE qui borne réellement le lot.

    Ce test est la vraie garde : il rougit si quelqu'un relève
    ``MAX_ACTOR_LENGTH`` ou ``MAX_OBSERVATIONS`` sans revérifier que le lot tient
    encore. La borne d'octets de l'émetteur reste, mais comme défense pour un
    appelant futur qui n'appellerait PAS ``normalize_agent`` — pas comme le
    chemin de production.
    """
    worst = _observation(
        ("A" * MAX_ACTOR_LENGTH, "00000000-0000-4000-8000-000000000000", "ab" * 16), 1
    )
    cost = len(json.dumps(worst).encode()) + 1
    envelope = len(json.dumps({"observations": []}).encode())
    # `_MAX_BUFFERED` observations du tampon, PLUS celle qui déclenche l'émission.
    worst_batch = envelope + cost * (_MAX_BUFFERED + 1)

    assert worst_batch <= MAX_OBSERVATION_BYTES, (
        f"le pire lot normalisé pèse {worst_batch} o pour une borne récepteur "
        f"de {MAX_OBSERVATION_BYTES} o : relever MAX_ACTOR_LENGTH ou "
        f"MAX_OBSERVATIONS demande de revoir le découpage des lots"
    )
