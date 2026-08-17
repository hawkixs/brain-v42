"""Tests du middleware de provenance — pose de l'acteur sur on_call_tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.mcp.provenance_middleware import ProvenanceMiddleware
from brain_v42.provenance import UNKNOWN_ACTOR, get_current_actor, set_current_actor


def _context(tool_name: str = "brain_get") -> MagicMock:
    context = MagicMock()
    context.message.name = tool_name
    return context


class TestProvenanceMiddleware:
    async def test_sets_actor_from_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: {"x-brain-agent": "dream-codex-reorg"},
        )
        seen: list[str] = []

        async def call_next(_ctx: object) -> str:
            seen.append(get_current_actor())
            return "ok"

        result = await ProvenanceMiddleware().on_call_tool(_context(), call_next)

        assert result == "ok"
        assert seen == ["dream-codex-reorg"]

    async def test_missing_header_yields_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: {},
        )
        set_current_actor("red-lab")
        seen: list[str] = []

        async def call_next(_ctx: object) -> str:
            seen.append(get_current_actor())
            return "ok"

        await ProvenanceMiddleware().on_call_tool(_context(), call_next)
        assert seen == [UNKNOWN_ACTOR]

    async def test_no_http_context_yields_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """En stdio, get_http_headers() retourne None — repli fail-closed."""
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: None,
        )
        seen: list[str] = []

        async def call_next(_ctx: object) -> str:
            seen.append(get_current_actor())
            return "ok"

        await ProvenanceMiddleware().on_call_tool(_context(), call_next)
        assert seen == [UNKNOWN_ACTOR]

    async def test_actor_is_set_before_handler_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """L'acteur doit être posé AVANT call_next, pas après."""
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: {"x-brain-agent": "red-lab"},
        )
        call_next = AsyncMock(return_value="ok")
        await ProvenanceMiddleware().on_call_tool(_context(), call_next)
        call_next.assert_awaited_once()

    async def test_path_like_header_is_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Le middleware doit NORMALISER, pas seulement recopier le header.

        Sans ce test, remplacer `normalize_agent(...)` par le header brut
        laisserait les cinq autres tests verts : ils envoient tous des valeurs
        déjà normalisées ou absentes. En production chaque session Claude
        atterrirait alors comme `/home/.../brain_v42` au lieu de `brain_v42`,
        avec une cardinalité d'acteurs non bornée dans access_log.
        """
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: {"x-brain-agent": "/home/hawixs/hawkixs_infra/git_repo/red-lab"},
        )
        seen: list[str] = []

        async def call_next(_ctx: object) -> str:
            seen.append(get_current_actor())
            return "ok"

        await ProvenanceMiddleware().on_call_tool(_context(), call_next)
        assert seen == ["red-lab"]

    async def test_unexpanded_template_is_collapsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Session démon sans PWD : un seul seau, pas un acteur par littéral."""
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: {"x-brain-agent": "${PWD}"},
        )
        seen: list[str] = []

        async def call_next(_ctx: object) -> str:
            seen.append(get_current_actor())
            return "ok"

        await ProvenanceMiddleware().on_call_tool(_context(), call_next)
        assert seen == ["_unexpanded"]

    async def test_exception_propagates_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: {"x-brain-agent": "red-lab"},
        )

        async def call_next(_ctx: object) -> str:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await ProvenanceMiddleware().on_call_tool(_context(), call_next)


class TestActivityReportingNeverBreaksTheToolCall:
    """L'observabilité ne doit JAMAIS pouvoir casser l'opération qu'elle observe.

    Ticket 1c40c36a. Ce chemin est chaud : ``_report`` s'exécute à chaque appel de
    tool. Il est aussi ARMÉ en production — le drop-in
    ``brain-mcp-http.service.d/client-activity.conf`` pose
    ``CLIENT_ACTIVITY_REPORTING_ENABLED=true``, vérifié sur l'unité vivante ET dans
    l'environnement du process. Une exception qui en sort ne casse pas UN tool, elle
    casse TOUS les tools du process partagé, donc les six phases des dix projets de
    la nuit.

    ``_report`` était appelé dans un ``try`` qui n'avait qu'un ``finally``.
    """

    async def test_a_raising_reporter_does_not_break_the_tool_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: {"x-brain-agent": "red-lab"},
        )
        exploding = MagicMock()
        exploding.report.side_effect = RuntimeError("le sidecar a explosé")
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_activity_reporter",
            lambda: exploding,
        )

        async def call_next(_ctx: object) -> str:
            return "ok"

        result = await ProvenanceMiddleware().on_call_tool(_context(), call_next)

        assert result == "ok", (
            "une panne de l'émetteur d'activité a tué l'appel de tool — un canal "
            "d'observation ne peut pas être un point de défaillance de l'outil"
        )
        assert exploding.report.called, "le test passerait sur du vide sans cet appel"

    async def test_a_genuine_tool_failure_still_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La sonde ANTI-TAUTOLOGIE : on n'avale que les pannes de L'ÉMETTEUR.

        Sans elle, un ``try/except`` posé trop large rendrait le middleware muet sur
        les vraies erreurs d'outil — on échangerait un mode de panne bruyant contre
        un mode de panne silencieux, ce qui est pire.
        """
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: {"x-brain-agent": "red-lab"},
        )
        exploding = MagicMock()
        exploding.report.side_effect = RuntimeError("le sidecar a explosé")
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_activity_reporter",
            lambda: exploding,
        )

        async def call_next(_ctx: object) -> str:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await ProvenanceMiddleware().on_call_tool(_context(), call_next)


class TestAnUnidentifiedClientIsNamedOnce:
    """Le poller `unknown` monte à un appel par minute depuis des semaines.

    Mesuré le 2026-08-12 : `brain_ticket_list` porte 1239 appels quand tout le
    reste est à un chiffre, et RIEN ne dit qui appelle. Les instruments qui ont
    échoué : `ss` échantillonné rate un appel de 4,4 ms par construction,
    `ss -E` voit l'événement mais plus le processus, et `access_log` est vide.
    Ce qui reste est la seule mesure que le serveur peut faire lui-même — l'IP
    source et le User-Agent, au moment où l'appel arrive.
    """

    def _http(
        self, monkeypatch: pytest.MonkeyPatch, agent: str | None, ua: str = "python-httpx/0.27"
    ) -> None:
        headers = {"user-agent": ua}
        if agent is not None:
            headers["x-brain-agent"] = agent
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: dict(headers),
        )
        request = MagicMock()
        request.client.host = "127.0.0.1"
        request.client.port = 45902
        monkeypatch.setattr("brain_v42.mcp.provenance_middleware.get_http_request", lambda: request)

    async def test_an_unidentified_caller_is_logged_with_peer_and_user_agent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from structlog.testing import capture_logs

        self._http(monkeypatch, agent=None)
        monkeypatch.setattr("brain_v42.mcp.provenance_middleware._seen_unidentified", set())
        with capture_logs() as logs:
            await ProvenanceMiddleware().on_call_tool(
                _context("brain_ticket_list"), AsyncMock(return_value="ok")
            )

        entries = [entry for entry in logs if entry["event"] == "provenance.unidentified_client"]
        assert len(entries) == 1, "un appel anonyme doit se dénoncer"
        assert entries[0]["peer"] == "127.0.0.1"
        assert entries[0]["user_agent"] == "python-httpx/0.27"
        assert entries[0]["tool"] == "brain_ticket_list", (
            "sans le nom du tool on ne relie pas la ligne aux 1239 brain_ticket_list"
        )

    async def test_the_same_client_is_not_logged_twice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un appel par minute pendant des jours ferait un journal illisible —
        et la réponse tient dans la PREMIÈRE ligne. On journalise à la
        découverte, pas à la répétition."""
        from structlog.testing import capture_logs

        self._http(monkeypatch, agent=None)
        monkeypatch.setattr("brain_v42.mcp.provenance_middleware._seen_unidentified", set())
        with capture_logs() as logs:
            for _ in range(5):
                await ProvenanceMiddleware().on_call_tool(
                    _context("brain_ticket_list"), AsyncMock(return_value="ok")
                )

        entries = [entry for entry in logs if entry["event"] == "provenance.unidentified_client"]
        assert len(entries) == 1

    async def test_an_identified_caller_is_never_logged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La sonde vise les anonymes. Journaliser les clients nommés
        n'apprendrait rien et ferait du volume sur le chemin chaud."""
        from structlog.testing import capture_logs

        self._http(monkeypatch, agent="brain-v42")
        monkeypatch.setattr("brain_v42.mcp.provenance_middleware._seen_unidentified", set())
        with capture_logs() as logs:
            await ProvenanceMiddleware().on_call_tool(
                _context("brain_search"), AsyncMock(return_value="ok")
            )

        assert [e for e in logs if e["event"] == "provenance.unidentified_client"] == []

    async def test_an_unreadable_request_never_breaks_the_tool_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Même posture que `_report` : un canal d'OBSERVATION ne peut pas
        faire tomber l'opération observée."""
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers", lambda **_kw: {}
        )

        def _boom() -> None:
            raise RuntimeError("pas de contexte HTTP")

        monkeypatch.setattr("brain_v42.mcp.provenance_middleware.get_http_request", _boom)
        monkeypatch.setattr("brain_v42.mcp.provenance_middleware._seen_unidentified", set())

        result = await ProvenanceMiddleware().on_call_tool(_context(), AsyncMock(return_value="ok"))

        assert result == "ok"
