"""Tests of the provenance middleware — setting the actor on on_call_tool."""

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
        """The actor must be set BEFORE call_next, not after."""
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: {"x-brain-agent": "red-lab"},
        )
        call_next = AsyncMock(return_value="ok")
        await ProvenanceMiddleware().on_call_tool(_context(), call_next)
        call_next.assert_awaited_once()

    async def test_path_like_header_is_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The middleware must NORMALIZE, not merely copy the header.

        Without this test, replacing `normalize_agent(...)` with the raw header
        would leave the other five tests green: they all send values that are
        already normalized or absent. In production every Claude session would
        then land as `/home/.../brain_v42` instead of `brain_v42`, with an
        unbounded actor cardinality in access_log.
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
        """Daemon session without PWD: a single bucket, not one actor per literal."""
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
    """Observability must NEVER be able to break the operation it observes.

    Ticket 1c40c36a. This path is hot: ``_report`` runs on every tool call. It is
    also ARMED in production — the drop-in
    ``brain-mcp-http.service.d/client-activity.conf`` sets
    ``CLIENT_ACTIVITY_REPORTING_ENABLED=true``, verified on the live unit AND in
    the process environment. An exception escaping it does not break ONE tool, it
    breaks ALL the tools of the shared process, hence the six phases of the
    night's ten projects.

    ``_report`` was called inside a ``try`` that had only a ``finally``.
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
        """The ANTI-TAUTOLOGY probe: we swallow only THE EMITTER's failures.

        Without it, a ``try/except`` placed too widely would make the middleware
        mute about real tool errors — we would trade a loud failure mode for a
        silent one, which is worse.
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
    """The `unknown` poller has been calling once a minute for weeks.

    Measured on 2026-08-12: `brain_ticket_list` carries 1239 calls when everything
    else is in single digits, and NOTHING says who is calling. The instruments
    that failed: sampled `ss` misses a 4.4 ms call by construction, `ss -E` sees
    the event but no longer the process, and `access_log` is empty. What remains
    is the only measurement the server can make itself — the source IP and the
    User-Agent, at the moment the call arrives.
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
        """One call a minute for days would make an unreadable log — and the
        answer fits in the FIRST line. We log on discovery, not on
        repetition."""
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
        """The probe targets the anonymous. Logging named clients would teach
        nothing and would add volume on the hot path."""
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
        """Same posture as `_report`: an OBSERVATION channel cannot bring down
        the operation it observes."""
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers", lambda **_kw: {}
        )

        def _boom() -> None:
            raise RuntimeError("pas de contexte HTTP")

        monkeypatch.setattr("brain_v42.mcp.provenance_middleware.get_http_request", _boom)
        monkeypatch.setattr("brain_v42.mcp.provenance_middleware._seen_unidentified", set())

        result = await ProvenanceMiddleware().on_call_tool(_context(), AsyncMock(return_value="ok"))

        assert result == "ok"
