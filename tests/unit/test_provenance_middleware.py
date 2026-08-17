"""Le middleware de provenance face à la ré-entrance du profil compact."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from brain_v42.mcp.activity_reporter import set_activity_reporter
from brain_v42.mcp.provenance_middleware import ProvenanceMiddleware
from brain_v42.provenance import get_current_actor, get_current_session

SESSION_UUID = "3d7a88d7-791b-45da-b8b9-75727e3c9eec"


TRANSPORT_ID = "0f9d2c1b3a4e5f60718293a4b5c6d7e8"


class _SpyReporter:
    """Double d'émetteur qui note ce qu'on lui passe, dans l'ordre reçu.

    ``calls`` reste un couple ``(acteur, session)`` : les assertions qui le
    lisent épinglent l'ordre des deux positions historiques, et y greffer le
    transport les rendrait toutes vraies pour une mauvaise raison. Le troisième
    argument est donc noté à part.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.transports: list[str | None] = []

    def report(
        self,
        actor: str,
        session_id: str | None,
        transport: str | None = None,
    ) -> None:
        self.calls.append((actor, session_id))
        self.transports.append(transport)


@pytest.mark.asyncio
async def test_compact_gateway_reports_one_outermost_call() -> None:
    """Un appel compact déclenche on_call_tool deux fois : passerelle puis
    tool interne. La garde ne doit en retenir qu'un.

    Ce test DOIT simuler l'imbrication réelle. Un test qui appelle le
    middleware deux fois à plat passerait au vert sans rien prouver.
    """
    middleware = ProvenanceMiddleware()

    async def inner_call_next(_context: Any) -> str:
        return "inner"

    async def outer_call_next(_context: Any) -> str:
        # Le tool passerelle ré-entre dans la chaîne de middlewares.
        return await middleware.on_call_tool(object(), inner_call_next)

    with patch.object(ProvenanceMiddleware, "_report", autospec=True) as report:
        with patch(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            return_value={"x-brain-agent": "brain-v42"},
        ):
            result = await middleware.on_call_tool(object(), outer_call_next)

    assert result == "inner"
    assert report.call_count == 1


@pytest.mark.asyncio
async def test_actor_and_session_are_posted_from_headers() -> None:
    middleware = ProvenanceMiddleware()
    captured: dict[str, object] = {}

    async def call_next(_context: Any) -> None:
        captured["actor"] = get_current_actor()
        captured["session"] = get_current_session()

    with patch.object(ProvenanceMiddleware, "_report", autospec=True):
        with patch(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            return_value={
                "x-brain-agent": "/home/hawixs/git/red-lab",
                "x-brain-session": "3d7a88d7-791b-45da-b8b9-75727e3c9eec",
            },
        ):
            await middleware.on_call_tool(object(), call_next)

    assert captured["actor"] == "red-lab"
    assert captured["session"] == "3d7a88d7-791b-45da-b8b9-75727e3c9eec"


@pytest.mark.asyncio
async def test_absent_session_header_leaves_session_none() -> None:
    middleware = ProvenanceMiddleware()
    captured: dict[str, object] = {}

    async def call_next(_context: Any) -> None:
        captured["session"] = get_current_session()

    with patch.object(ProvenanceMiddleware, "_report", autospec=True):
        with patch(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            return_value={"x-brain-agent": "codex"},
        ):
            await middleware.on_call_tool(object(), call_next)

    assert captured["session"] is None


@pytest.mark.asyncio
async def test_reporter_receives_actor_then_session_in_that_order() -> None:
    """Le VRAI ``_report`` doit atteindre l'émetteur, chaque valeur à sa place.

    Les trois tests ci-dessus remplacent ``_report`` par un mock, et les tests
    de tests/unit/mcp/ le traversent killswitch fermé, donc
    ``get_activity_reporter()`` rend ``None`` et le corps ne fait rien
    d'observable. Résultat mesuré : neutraliser entièrement le corps de
    ``_report``, ou échanger ses deux arguments, laissait la suite complète
    verte. C'est le motif du faux témoin (learning a6e1dd1f) dans sa version
    dure — la valeur n'est pas seulement capturée sans être relue, elle est
    remplacée par un mock dans 100% des tests qui touchent le point
    d'application.

    L'échange des arguments n'est pas cosmétique : il ferait passer l'UUID de
    session brut en position acteur sur le fil. ``normalize_agent`` côté
    sidecar le laisserait tel quel (pas de ``${``, pas de ``/``, 36 <= 64
    caractères) et le cockpit publierait
    ``"id": "unattributed:<uuid>"`` — la propriété centrale de la conception,
    « aucun identifiant brut ne sort du registre », contournée par le seul
    chemin jamais exécuté.
    """
    middleware = ProvenanceMiddleware()
    spy = _SpyReporter()
    set_activity_reporter(spy)  # type: ignore[arg-type]

    async def call_next(_context: Any) -> str:
        return "ok"

    with patch(
        "brain_v42.mcp.provenance_middleware.get_http_headers",
        return_value={
            "x-brain-agent": "/home/hawixs/git/red-lab",
            "x-brain-session": SESSION_UUID,
        },
    ):
        await middleware.on_call_tool(object(), call_next)

    assert spy.calls == [("red-lab", SESSION_UUID)]


@pytest.mark.asyncio
async def test_reporter_receives_a_null_session_when_none_is_declared() -> None:
    """Sans session déclarée, l'acteur reste en position acteur.

    Contrôle négatif du test précédent : il interdit de remplir la position
    session avec l'acteur faute de mieux. ``None`` veut dire « pas de session
    déclarée » et doit rester distinguable.
    """
    middleware = ProvenanceMiddleware()
    spy = _SpyReporter()
    set_activity_reporter(spy)  # type: ignore[arg-type]

    async def call_next(_context: Any) -> str:
        return "ok"

    with patch(
        "brain_v42.mcp.provenance_middleware.get_http_headers",
        return_value={"x-brain-agent": "codex"},
    ):
        await middleware.on_call_tool(object(), call_next)

    assert spy.calls == [("codex", None)]


@pytest.mark.asyncio
async def test_transport_reaches_the_reporter_from_the_mcp_header() -> None:
    """``Mcp-Session-Id`` doit traverser jusqu'à l'émetteur.

    Il n'y arrive pas tout seul : ``get_http_headers()`` EXCLUT
    ``mcp-session-id`` par défaut (vérifié dans fastmcp 3.x, il figure en dur
    dans ``exclude_headers``). Sans ``include=``, ce test resterait vert sur un
    ``None`` — le motif du faux témoin que documente déjà ce fichier.
    """
    middleware = ProvenanceMiddleware()
    spy = _SpyReporter()
    set_activity_reporter(spy)  # type: ignore[arg-type]

    async def call_next(_context: Any) -> str:
        return "ok"

    with patch(
        "brain_v42.mcp.provenance_middleware.get_http_headers",
        return_value={"x-brain-agent": "codex", "mcp-session-id": TRANSPORT_ID},
    ) as headers:
        await middleware.on_call_tool(object(), call_next)

    assert spy.transports == [TRANSPORT_ID]
    assert spy.calls == [("codex", None)], "le transport ne doit PAS occuper la place session"
    # Le contrôle qui interdit de croire l'en-tête accessible par défaut.
    assert headers.call_args.kwargs.get("include") == {"mcp-session-id"}


@pytest.mark.asyncio
async def test_stateless_mode_leaves_transport_none() -> None:
    """Sans en-tête frappé par le serveur, l'absence doit rester dicible."""
    middleware = ProvenanceMiddleware()
    spy = _SpyReporter()
    set_activity_reporter(spy)  # type: ignore[arg-type]

    async def call_next(_context: Any) -> str:
        return "ok"

    with patch(
        "brain_v42.mcp.provenance_middleware.get_http_headers",
        return_value={"x-brain-agent": "codex"},
    ):
        await middleware.on_call_tool(object(), call_next)

    assert spy.transports == [None]


@pytest.mark.asyncio
async def test_client_forged_transport_is_refused_by_shape() -> None:
    """Une valeur qui n'a pas la forme frappée par le serveur vaut ``None``.

    En mode sans état le serveur sert un ``mcp-session-id`` inventé par le
    client sans le valider : la seule chose qui tienne à ce niveau est la
    FORME. Un panneau piloté par l'appelant serait pire qu'un panneau vide.
    """
    middleware = ProvenanceMiddleware()
    spy = _SpyReporter()
    set_activity_reporter(spy)  # type: ignore[arg-type]

    async def call_next(_context: Any) -> str:
        return "ok"

    for forged in ("../../etc/passwd", SESSION_UUID, TRANSPORT_ID.upper(), "x" * 4096):
        spy.transports.clear()
        with patch(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            return_value={"x-brain-agent": "codex", "mcp-session-id": forged},
        ):
            await middleware.on_call_tool(object(), call_next)
        assert spy.transports == [None], f"forme acceptée à tort : {forged!r}"
