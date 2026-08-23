"""L'émetteur d'activité ne doit ni bloquer ni casser un appel de tool."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import socket
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from brain_v42.mcp.activity_reporter import _MAX_BUFFERED, ActivityReporter, _is_a_decade

FAKE_UUID = "12345678-1234-4abc-8def-1234567890ab"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@contextlib.contextmanager
def _production_logging_into(buffer: io.StringIO) -> Iterator[None]:
    """Configurer structlog comme la production, mais vers un tampon.

    Recopie de ``mcp/server.py::_configure_stdio_logging`` : c'est cette
    chaîne-là qui rend les exceptions, et son rendu par défaut (rich, quand il
    est installé) affiche les variables locales de chaque cadre.

    Piège mesuré : ``PrintLoggerFactory(file=...)`` fige le flux à l'appel de
    ``configure()``. Remplacer ``sys.stderr`` après coup ne capture rien, et un
    test qui affirmerait « pas d'identifiant dans le journal » passerait au vert
    sur un journal vide. Le tampon est donc passé à la fabrique elle-même.
    """
    saved = structlog.get_config()
    structlog.reset_defaults()
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )
    try:
        yield
    finally:
        structlog.reset_defaults()
        structlog.configure(**saved)


def _closed_loopback_port() -> int:
    """Un port de loopback sur lequel personne n'écoute."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _longest_leaked_fragment(log: str, secret: str, minimum: int = 8) -> str:
    """Le plus long fragment de ``secret`` d'au moins ``minimum`` signes présent.

    Chercher le secret *entier* ne suffirait pas : le formateur rich tronque
    chaque variable locale à 80 caractères, si bien que le corps d'observation
    ne laisse fuir qu'un préfixe de l'UUID de session. Une assertion
    « l'UUID entier est absent » passerait donc au vert avec quinze signes
    d'identifiant brut dans journald.
    """
    for size in range(len(secret), minimum - 1, -1):
        for start in range(len(secret) - size + 1):
            fragment = secret[start : start + size]
            if fragment in log:
                return fragment
    return ""


async def _log_of_one_failed_post(buffer: io.StringIO, session_id: str | None) -> str:
    """Provoquer un vrai échec de POST et rendre le journal produit."""
    url = f"http://127.0.0.1:{_closed_loopback_port()}/v1/client-activity"
    reporter = ActivityReporter(url=url, timeout=1.0)
    with _production_logging_into(buffer):
        reporter.report("brain-v42", session_id)
        await reporter.drain()
    await reporter.close()
    return _ANSI.sub("", buffer.getvalue())


async def _one_answered_post(
    buffer: io.StringIO, status: int, body: str
) -> tuple[ActivityReporter, str]:
    """Émettre une observation à laquelle le récepteur RÉPOND ``status``.

    ``httpx`` ne lève pas sur 4xx/5xx : la réponse revient par le chemin
    nominal, pas par le ``except``. Le double rend donc une vraie
    ``httpx.Response`` — un ``AsyncMock`` nu rendrait un ``MagicMock`` dont
    tout attribut est vrai, et ``is_success`` serait vrai pour un 404.
    """
    url = "http://127.0.0.1:9200/v1/client-activity"
    reporter = ActivityReporter(url=url)
    response = httpx.Response(
        status_code=status,
        text=body,
        request=httpx.Request("POST", url),
    )
    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(return_value=response)
        with _production_logging_into(buffer):
            reporter.report("brain-v42", FAKE_UUID)
            await reporter.drain()
    await reporter.close()
    return reporter, _ANSI.sub("", buffer.getvalue())


# La remise à zéro du global ``_reporter`` est une fixture autouse partagée,
# dans tests/unit/conftest.py : ce module n'est plus le seul à injecter un
# double.


@pytest.mark.asyncio
async def test_report_posts_the_observation() -> None:
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")
    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock()
        reporter.report("brain-v42", FAKE_UUID)
        await reporter.drain()
        client.post.assert_awaited_once()
        sent = json.loads(client.post.await_args.kwargs["content"])
    assert sent == {"observations": [{"actor": "brain-v42", "session": FAKE_UUID, "calls": 1}]}
    await reporter.close()


@pytest.mark.asyncio
async def test_absent_session_is_omitted_from_the_wire() -> None:
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")
    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock()
        reporter.report("codex", None)
        await reporter.drain()
        sent = json.loads(client.post.await_args.kwargs["content"])
    assert sent == {"observations": [{"actor": "codex", "calls": 1}]}
    await reporter.close()


@pytest.mark.asyncio
async def test_transport_failure_is_swallowed() -> None:
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")
    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(side_effect=OSError("sidecar down"))
        reporter.report("brain-v42", None)
        await reporter.drain()  # ne doit pas lever
    await reporter.close()


@pytest.mark.asyncio
async def test_saturation_coalesces_instead_of_blocking_or_losing() -> None:
    """Sous saturation, ``report()`` rend la main AUSSITÔT — et ne perd plus rien.

    Ce test s'appelait ``test_saturation_drops_instead_of_blocking`` et assertait
    ``reporter.dropped == 10``. Il ÉPINGLAIT donc la perte que le ticket
    ``1c40c36a`` dénonçait : le corriger faisait forcément rougir la suite.
    L'assertion de non-blocage — sa vraie raison d'être — est conservée et
    RENFORCÉE : on vérifie en plus que les dix appels arrivent sur le fil.
    """
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity", max_in_flight=1)
    release = asyncio.Event()

    async def slow_post(*_args: Any, **_kwargs: Any) -> None:
        await release.wait()

    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(side_effect=slow_post)
        reporter.report("brain-v42", None)
        await asyncio.sleep(0)
        for _ in range(10):
            reporter.report("brain-v42", None)  # doit rendre la main aussitôt
        assert reporter.dropped == 0, "la contre-pression jette encore"
        assert reporter.coalesced == 10, "les dix appels n'ont pas été repliés"
        release.set()
        await reporter.drain()
        posted = sum(
            int(observation["calls"])
            for call in client.post.await_args_list
            for observation in json.loads(call.kwargs["content"])["observations"]
        )
    assert posted == 11, f"onze appels émis, {posted} arrivés sur le fil"
    await reporter.close()


@pytest.mark.asyncio
async def test_drain_returns_even_if_done_callback_has_not_run_yet() -> None:
    """Livelock potentiel : sur Python 3.12+, ``asyncio.gather()`` traite les
    futures déjà ``done()`` de façon eager, et attendre une future déjà
    terminée ne cède jamais la main à la boucle événementielle. Si une tâche
    ``_post()`` se termine avant que son ``done_callback``
    (``self._pending.discard``) n'ait eu son tour — ce qui demande un second
    tour de boucle — alors ``while self._pending: await asyncio.gather(...)``
    boucle indéfiniment sans jamais laisser ce callback s'exécuter.

    ``asyncio.wait_for`` avec un délai court : un ``drain()`` qui livelock ne
    doit pas bloquer la suite entière, juste faire échouer ce test.
    """
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")
    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock()  # résout instantanément, sans suspension réelle
        reporter.report("brain-v42", None)
        await asyncio.sleep(0)  # laisse _post tourner, mais pas forcément le callback
        await asyncio.wait_for(reporter.drain(), timeout=3)
    await reporter.close()


def test_construction_failure_never_breaks_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_activity_reporter() ne doit jamais lever.

    En production ``get_settings()`` ne peut pas échouer (POSTGRES_URL est
    requis au démarrage). Mais l'appelant de ``get_activity_reporter()`` est
    le middleware de provenance, sur le chemin de TOUT appel de tool — si la
    résolution de settings ou la construction du client lève pour une autre
    raison, ça ne doit jamais casser l'appel en cours.
    """
    from brain_v42.mcp import activity_reporter

    def _boom() -> Any:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(activity_reporter, "get_settings", _boom)

    assert activity_reporter.get_activity_reporter() is None


def test_closed_killswitch_silences_the_emitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Porte fermée : aucun émetteur, donc aucune émission.

    Épingler le seul défaut dans ``test_config`` ne prouverait rien. Une
    valeur de configuration n'est une frontière de sûreté que si le point de
    consommation la lit — c'est le motif du faux témoin (learning a6e1dd1f) :
    une valeur capturée que personne ne relit.
    """
    from brain_v42.mcp import activity_reporter

    class _Closed:
        client_activity_reporting_enabled = False
        client_activity_url = "http://127.0.0.1:9200/v1/client-activity"

    monkeypatch.setattr(activity_reporter, "get_settings", lambda: _Closed())

    assert activity_reporter.get_activity_reporter() is None


def test_open_killswitch_builds_the_emitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contrôle positif du test précédent.

    Sans lui, ``get_activity_reporter() is None`` passerait au vert pour
    n'importe quelle raison — y compris un émetteur cassé pour de bon.
    """
    from brain_v42.mcp import activity_reporter

    class _Open:
        client_activity_reporting_enabled = True
        client_activity_url = "http://127.0.0.1:9999/v1/probe"

    monkeypatch.setattr(activity_reporter, "get_settings", lambda: _Open())

    reporter = activity_reporter.get_activity_reporter()

    assert reporter is not None
    assert reporter._url == "http://127.0.0.1:9999/v1/probe"


# ──────────────────────────────────────────────────────────────────────────────
# Le journal de l'émetteur est lui-même une sortie : ce qu'il écrit part dans
# journald, sur le chemin de TOUT appel de tool.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_failure_log_carries_no_raw_identifier() -> None:
    """Un sidecar injoignable ne doit pas recopier l'UUID de session dans journald.

    La conception affirme qu'aucun identifiant brut ne quitte le registre ;
    personne n'avait regardé le journal de l'émetteur. Le déclencheur est le
    mode NOMINAL d'un feu-et-oubli : sidecar arrêté, redémarrage, timeout.

    L'assertion sur la présence de l'événement est le contrôle positif : sans
    elle, « pas d'identifiant dans le journal » passerait au vert pour un
    logger muet, une chaîne mal configurée ou un POST qui n'a jamais échoué.
    """
    buffer = io.StringIO()

    log = await _log_of_one_failed_post(buffer, FAKE_UUID)

    assert "activity_reporter.post_failed" in log, (
        "l'échec doit rester observable — sinon l'absence d'UUID ne prouve rien"
    )
    leaked = _longest_leaked_fragment(log, FAKE_UUID)
    assert leaked == "", f"fragment d'identifiant brut dans le journal : {leaked!r}"


@pytest.mark.asyncio
async def test_post_failure_log_stays_within_a_few_lines() -> None:
    """``_report`` est sur le chemin de tout appel de tool : le journal est borné.

    Sidecar absent et porte ouverte au rollout, une trace rendue par rich coûte
    des centaines de lignes par appel — payées sur le chemin chaud, alors que le
    module promet qu'un sidecar arrêté « ne doit jamais ralentir » l'appel.
    """
    buffer = io.StringIO()

    log = await _log_of_one_failed_post(buffer, FAKE_UUID)

    lines = log.count("\n")
    assert 0 < lines <= 3, f"journal de {lines} lignes pour un seul POST raté"


@pytest.mark.asyncio
async def test_post_failure_log_names_the_exception_type() -> None:
    """Diagnostiquer un sidecar mort demande le type de l'exception, pas la trace."""
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")
    buffer = io.StringIO()

    class SidecarUnplugged(OSError):
        pass

    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(side_effect=SidecarUnplugged("connection refused"))
        with _production_logging_into(buffer):
            reporter.report("brain-v42", None)
            await reporter.drain()
    await reporter.close()

    log = _ANSI.sub("", buffer.getvalue())
    assert "SidecarUnplugged" in log


def test_unavailable_log_carries_no_local_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Même défaut sur le second point de journalisation du module.

    ``get_activity_reporter()`` est appelé par le middleware de provenance à
    chaque appel de tool ; tant que la résolution des settings échoue, elle
    échoue à chaque fois. Les cadres traversés sont ceux de la construction des
    settings — leurs variables locales portent la configuration, DSN compris.
    """
    from brain_v42.mcp import activity_reporter

    def _boom() -> Any:
        dsn = "postgresql+asyncpg://brain:s3cret-p4ssw0rd@127.0.0.1:5433/brain"  # noqa: F841
        raise RuntimeError("settings unavailable")

    # Remise à zéro explicite du global : un émetteur laissé par un test
    # précédent court-circuiterait la construction, ``get_settings`` ne serait
    # jamais appelé, et l'assertion « pas de DSN dans le journal » passerait au
    # vert sur un journal vide.
    activity_reporter.set_activity_reporter(None)
    monkeypatch.setattr(activity_reporter, "get_settings", _boom)
    buffer = io.StringIO()

    with _production_logging_into(buffer):
        assert activity_reporter.get_activity_reporter() is None

    log = _ANSI.sub("", buffer.getvalue())
    assert "activity_reporter.unavailable" in log, (
        "contrôle positif : l'indisponibilité doit rester observable"
    )
    assert "s3cret-p4ssw0rd" not in log
    assert log.count("\n") <= 3


# ──────────────────────────────────────────────────────────────────────────────
# Un REFUS du récepteur n'est pas un échec de transport. ``httpx`` ne lève pas
# sur 4xx/5xx : sans lecture explicite du statut, 404, 403, 413, 415, 400 et 503
# reviennent tous par le chemin nominal, et la perte n'est ni comptée ni
# journalisée. Le cas mesuré : ``METRICS_HOST`` non-loopback — valeur que la
# configuration autorise — n'enregistre pas la route ``/v1/client-activity``,
# donc chaque POST reçoit 404 et la moitié « brain » du panneau reste vide pour
# toujours pendant que toute la chaîne MCP se déclare saine.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_route_absent_404_is_counted_apart_from_local_backpressure() -> None:
    """Le mode de perte dominant au rollout doit avoir son propre compteur.

    ``dropped`` ne compte que la contre-pression LOCALE (créneaux pris). Le
    confondre avec le refus rendrait « aucun appel » et « toutes les
    observations refusées » indiscernables — exactement la confusion que la
    doctrine du dépôt interdit, déplacée dans l'émetteur.
    """
    buffer = io.StringIO()

    reporter, log = await _one_answered_post(buffer, 404, "404: Not Found")

    assert reporter.refused == 1
    assert reporter.dropped == 0, "un refus n'est pas une contre-pression locale"
    assert "activity_reporter.refused" in log
    assert "status=404" in log


@pytest.mark.asyncio
async def test_receiver_saturation_503_is_counted_and_logged() -> None:
    """Un récepteur saturé refuse aussi par le chemin nominal, pas par le ``except``."""
    buffer = io.StringIO()

    reporter, log = await _one_answered_post(buffer, 503, "receiver saturated")

    assert reporter.refused == 1
    assert reporter.dropped == 0
    assert "activity_reporter.refused" in log
    assert "status=503" in log


@pytest.mark.asyncio
async def test_accepted_200_counts_nothing_and_stays_silent() -> None:
    """Contrôle positif des deux tests précédents.

    Sans lui, « le compteur a bougé » passerait au vert pour un compteur qui
    s'incrémente à chaque POST, et « le refus est journalisé » pour un émetteur
    qui journalise toutes ses émissions — sur le chemin chaud de TOUT appel de
    tool.
    """
    buffer = io.StringIO()

    reporter, log = await _one_answered_post(buffer, 200, '{"accepted": 1}')

    assert reporter.refused == 0
    assert reporter.dropped == 0
    assert "activity_reporter.refused" not in log
    assert log == "", f"un POST accepté ne doit rien écrire, journal : {log!r}"


@pytest.mark.asyncio
async def test_refusal_log_carries_neither_body_nor_identifier() -> None:
    """Le statut seul. Le corps du refus est une entrée non maîtrisée.

    Même raison qu'au correctif qui a supprimé ``exc_info`` : le journal part
    dans journald à chaque appel de tool. Un récepteur peut renvoyer la requête
    en écho — UUID de session compris — et beaucoup de proxys le font.
    """
    buffer = io.StringIO()
    echoed = f'{{"error": "no route", "echo": "SECRET-BODY-MARKER-{FAKE_UUID}"}}'

    reporter, log = await _one_answered_post(buffer, 404, echoed)

    assert reporter.refused == 1, "contrôle positif : le refus doit avoir été vu"
    assert "SECRET-BODY-MARKER" not in log
    leaked = _longest_leaked_fragment(log, FAKE_UUID)
    assert leaked == "", f"fragment d'identifiant brut dans le journal : {leaked!r}"
    lines = log.count("\n")
    assert lines <= 1, f"journal de {lines} lignes pour un seul refus"


FAKE_TRANSPORT = "0f9d2c1b3a4e5f60718293a4b5c6d7e8"


class TestTransportOnTheWire:
    """L'émetteur porte le transport, et l'omet quand il n'y en a pas."""

    @staticmethod
    async def _captured_body(**kwargs: Any) -> dict[str, Any]:
        reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")
        with patch.object(reporter._client, "post", new=AsyncMock()) as post:
            post.return_value = httpx.Response(200, request=httpx.Request("POST", "http://x"))
            reporter.report(**kwargs)
            await reporter.drain()
        (observation,) = json.loads(post.call_args.kwargs["content"])["observations"]
        return dict(observation)

    @pytest.mark.asyncio
    async def test_transport_is_emitted_when_present(self) -> None:
        body = await self._captured_body(actor="red-lab", session_id=None, transport=FAKE_TRANSPORT)
        assert body["transport"] == FAKE_TRANSPORT

    @pytest.mark.asyncio
    async def test_transport_key_absent_when_none(self) -> None:
        """Absent, jamais ``null`` : le décodeur distingue « non déclaré » de « vide »."""
        body = await self._captured_body(actor="red-lab", session_id=None, transport=None)
        assert "transport" not in body

    @pytest.mark.asyncio
    async def test_transport_defaults_to_absent(self) -> None:
        """Les appelants existants (2 arguments) restent valides et n'émettent rien."""
        body = await self._captured_body(actor="red-lab", session_id=None)
        assert "transport" not in body

    @pytest.mark.asyncio
    async def test_session_and_transport_coexist(self) -> None:
        body = await self._captured_body(
            actor="red-lab", session_id=FAKE_UUID, transport=FAKE_TRANSPORT
        )
        assert body["session"] == FAKE_UUID
        assert body["transport"] == FAKE_TRANSPORT


@pytest.mark.asyncio
async def test_local_backpressure_warns_once_then_stays_silent() -> None:
    """La contre-pression locale n'était comptée par PERSONNE.

    `dropped` s'incrémentait et `report()` rendait la main, sans une ligne.
    Mesuré : au-delà de 8 appels concurrents, N-8 observations disparaissent
    — 12 appels → 4 perdues, 20 → 12. Le panneau sous-compte donc précisément
    sur les pics qu'il existe pour montrer, et rien ne le dit.

    UNE seule ligne, à la PREMIÈRE perte. C'est le chemin chaud de TOUT appel
    de tool : une ligne par perte transformerait une rafale en tempête de
    journal, et s'apprendrait à être sautée.

    Le déclencheur a changé avec le correctif de ``1c40c36a`` : répéter le MÊME
    acteur ne perd plus rien, c'est coalescé. La perte résiduelle vit désormais
    au-delà de la borne du tampon, donc on la provoque avec des acteurs TOUS
    DISTINCTS. L'assertion, elle, est inchangée — c'est l'escalade qui est
    protégée ici, pas la façon de la déclencher.
    """
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity", max_in_flight=1)
    release = asyncio.Event()

    async def slow_post(*_args: Any, **_kwargs: Any) -> None:
        await release.wait()

    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(side_effect=slow_post)
        with capture_logs() as logs:
            reporter.report("filler-000", None)
            await asyncio.sleep(0)
            for i in range(_MAX_BUFFERED):  # sature le tampon, sans perte
                reporter.report(f"filler-{i:03d}", None)
            for i in range(10):  # au-delà : dix pertes, acteurs tous distincts
                reporter.report(f"overflow-{i:03d}", None)
        release.set()
        await reporter.drain()
    await reporter.close()

    warnings = [e for e in logs if e["log_level"] == "warning"]
    assert [w["dropped"] for w in warnings] == [1, 10], (
        f"dix pertes = la 1re et la 10e, jamais les huit du milieu, vu : {warnings}"
    )
    assert warnings[0]["event"] == "activity_reporter.dropped"
    assert reporter.dropped == 10


@pytest.mark.asyncio
async def test_a_repeated_refusal_warns_once_per_distinct_status() -> None:
    """Le cas mesuré est PERMANENT : un 404 par appel de tool, pour toujours.

    Journaliser chaque refus au même niveau produirait une ligne par appel de
    tool jusqu'à la fin des temps. La signature — le statut — parle une fois.
    """
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")

    with patch.object(reporter, "_client") as client:
        with capture_logs() as logs:
            for status in (404, 404, 404, 503, 503):
                client.post = AsyncMock(return_value=httpx.Response(status_code=status))
                reporter.report("brain-v42", None)
                await reporter.drain()
    await reporter.close()

    warned = [e["status"] for e in logs if e["log_level"] == "warning"]
    assert warned == [404, 503], f"une ligne par signature, vu : {warned}"
    assert reporter.refused == 5


@pytest.mark.asyncio
async def test_a_run_that_lost_nothing_closes_silently() -> None:
    """LE CAS NOMINAL EST MUET. Rien perdu, rien dit — pas même un « 0 »."""
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity")

    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(return_value=httpx.Response(status_code=204))
        with capture_logs() as logs:
            reporter.report("brain-v42", None)
            await reporter.drain()
    with capture_logs() as close_logs:
        await reporter.close()
    logs = logs + close_logs

    assert [e for e in logs if e["log_level"] == "warning"] == []


def test_a_decade_is_the_first_the_tenth_the_hundredth_and_nothing_between() -> None:
    """L'escalade doit être exacte : `log10` raterait ou doublerait des bornes."""
    shouted = [n for n in range(1, 1001) if _is_a_decade(n)]
    assert shouted == [1, 10, 100, 1000]
    assert not _is_a_decade(0)


@pytest.mark.asyncio
async def test_the_magnitude_of_the_loss_stays_visible_without_a_line_per_loss() -> None:
    """Ni une ligne par perte, ni une seule ligne pour toujours.

    `close()` n'est câblé NULLE PART en production — « le client meurt avec
    le processus » — donc un décompte à la fermeture ne serait jamais rendu.
    L'ordre de grandeur doit voyager dans les lignes elles-mêmes.

    Le déclencheur a changé avec le correctif de ``1c40c36a`` : répéter le MÊME
    acteur ne perd plus rien, c'est coalescé. La perte résiduelle vit désormais
    au-delà de la borne du tampon, donc on la provoque avec des acteurs TOUS
    DISTINCTS. L'assertion, elle, est inchangée — c'est l'escalade qui est
    protégée ici, pas la façon de la déclencher.
    """
    reporter = ActivityReporter(url="http://127.0.0.1:9200/v1/client-activity", max_in_flight=1)
    release = asyncio.Event()

    async def slow_post(*_args: Any, **_kwargs: Any) -> None:
        await release.wait()

    with patch.object(reporter, "_client") as client:
        client.post = AsyncMock(side_effect=slow_post)
        reporter.report("filler-000", None)
        await asyncio.sleep(0)
        for i in range(_MAX_BUFFERED):  # sature le tampon, sans perte
            reporter.report(f"filler-{i:03d}", None)
        with capture_logs() as logs:
            for i in range(100):  # au-delà : cent pertes, acteurs tous distincts
                reporter.report(f"overflow-{i:03d}", None)
        release.set()
        await reporter.drain()
    await reporter.close()

    counts = [e["dropped"] for e in logs if e["log_level"] == "warning"]
    assert counts == [1, 10, 100], f"cent pertes, trois lignes attendues, vu : {counts}"
    assert reporter.dropped == 100
