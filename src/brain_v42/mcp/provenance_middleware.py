"""Provenance middleware — sets the current actor for every tool call.

Installed UNCONDITIONALLY. Provenance must not depend on whether metrics are
enabled, which only happens when a collector exists, and a silently mute
provenance is worse than no provenance.

Does not carry the metrics. Since ticket c352eaaa they no longer go through a
monkey-patch of `mcp.tool`: `brain_v42.metrics.tool_instrumentation` wraps the
already registered tools from `_run_mcp`. That application point was chosen
AGAINST the middleware the ticket proposed, for two measured reasons — a
middleware sits above `call_tool`'s masking and would see only the generic
`ToolError` (the `exception_type` log would degenerate), and `_list_tools()`
excludes the gateways by construction, where a middleware would have required a
denylist of names. Do not reopen "move the metrics here": the question was
settled, not deferred.

Q2 re-measurement of 2026-08-06, which stays true for THIS middleware: under the
`compact` profile, the `brain_call_tool` gateway re-enters the chain
(`FastMCP.call_tool`, `run_middleware=True` by default), so `on_call_tool` ALSO
sees the real tool name — it fires twice per compact call (measured, commit
58329a84). That was harmless while the middleware only set the actor, the same
one twice in a row. It stopped being harmless once `_report` existed: counting at
every level would give x2 under `compact`, which is the production profile, and
x1 under the native profile — two incomparable numbers. Hence the depth guard
(`enter_call`/`exit_call`/`is_outermost_call`), which reserves `_report` for the
outermost level alone.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastmcp.server.dependencies import get_http_headers, get_http_request
from fastmcp.server.middleware import Middleware

from brain_v42.mcp.activity_reporter import get_activity_reporter
from brain_v42.mcp.session_autoopen import get_session_autoopener
from brain_v42.provenance import (
    UNKNOWN_ACTOR,
    enter_call,
    exit_call,
    is_outermost_call,
    normalize_agent,
    normalize_session,
    normalize_transport,
    set_current_actor,
    set_current_session,
    set_current_transport,
)

# ``get_http_headers()`` strips ``mcp-session-id`` from what it returns: the
# header is hardcoded into its ``exclude_headers``. It must be asked for by name,
# otherwise the read returns ``None`` silently and the panel stays anonymous
# without a single test noticing.
_TRANSPORT_HEADER = "mcp-session-id"

#: (peer, User-Agent) pairs already reported. An anonymous client calls here once
#: a minute for weeks on end: the answer fits in the FIRST line, and logging the
#: repetition would only bury the discovery.
_seen_unidentified: set[tuple[str, str]] = set()
#: Hard cap on this memo. The peer is declared by the network and the User-Agent
#: by the client: without a bound, a caller varying its User-Agent would grow
#: this set without end in a long-lived process.
_MAX_UNIDENTIFIED_TRACKED = 32
_UNKNOWN_PEER = "?"


logger = structlog.get_logger(__name__)


class ProvenanceMiddleware(Middleware):
    """Set the declared actor and session, and report the call exactly once."""

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        headers = get_http_headers(include={_TRANSPORT_HEADER}) or {}
        actor = normalize_agent(headers.get("x-brain-agent"))
        session = normalize_session(headers.get("x-brain-session"))
        # DO NOT replace with ``Context.session_id``: in stateless mode it
        # forges a ``uuid4()`` PER REQUEST (measured: three values for three
        # calls from one client), and that shape passes ``normalize_session``.
        # We would get one panel row per tool call, all presented as distinct
        # sessions.
        transport = normalize_transport(headers.get(_TRANSPORT_HEADER))
        set_current_actor(actor)
        set_current_session(session)
        set_current_transport(transport)

        token = enter_call()
        try:
            if is_outermost_call():
                await self._auto_open_session()
                self._report(actor, session, transport)
                if actor == UNKNOWN_ACTOR:
                    self._name_unidentified_client(context)
            return await call_next(context)
        finally:
            exit_call(token)

    async def _auto_open_session(self) -> None:
        """Open this connection's tracer session, BEFORE the tool.

        Three things hold in these five lines, and none is interchangeable:

        - **Before ``call_next``**, not after, and not in a detached task.
          Capture bounds artifacts by ``created_at >= started_at``: a session
          opened after the tool would attribute nothing the tool just created.
          That is what distinguishes this path from ``_report``, which observes
          nothing it must precede.
        - **Under ``is_outermost_call()``**, hence once per client call and not
          twice under the ``compact`` profile (measured, commit 58329a84). The
          opener's memo would not be enough: it would mask the second firing
          instead of preventing it, and the ``memoized`` counter would lie by a
          factor of two.
        - **``ensure_open`` never raises**, by contract. Any ``try`` here would
          therefore be redundant — but the absence of a local guard is a choice
          resting entirely on that contract, held by a fault-injection test with
          a negative control.
        """
        autoopener = get_session_autoopener()
        if autoopener is None:
            return
        await autoopener.ensure_open()

    def _name_unidentified_client(self, context: Any) -> None:
        """Report once a caller that does not declare itself.

        This is the only measurement the server can make ITSELF. The external
        instruments all failed on this traffic (measured 2026-08-12): sampled
        `ss` structurally misses a 4.4 ms call, `ss -E` sees the event but no
        longer the process that opened it, and `access_log` was empty. Here the
        information is present by construction, at the moment the request
        arrives.

        The ``except`` is TOTAL and tightly scoped, for the same reason as
        ``_report``: this path runs on every anonymous call of a shared process,
        and a probe cannot bring down what it observes.
        """
        try:
            headers = get_http_headers(include={"user-agent"}) or {}
            user_agent = (headers.get("user-agent") or "").strip()[:120]
            client = getattr(get_http_request(), "client", None)
            peer = getattr(client, "host", None) or _UNKNOWN_PEER
            key = (peer, user_agent)
            if key in _seen_unidentified:
                return
            if len(_seen_unidentified) >= _MAX_UNIDENTIFIED_TRACKED:
                return
            _seen_unidentified.add(key)
            logger.warning(
                "provenance.unidentified_client",
                peer=peer,
                port=getattr(client, "port", None),
                user_agent=user_agent,
                tool=getattr(getattr(context, "message", None), "name", None),
            )
        except Exception:
            logger.debug("provenance.unidentified_probe_failed", exc_info=True)

    def _report(self, actor: str, session: str | None, transport: str | None = None) -> None:
        """Report the call to the metrics sidecar, bounded fire-and-forget.

        The ``except`` is deliberately TOTAL, and it is tightly scoped to the
        emitter: this path runs on every tool call, in a shared and long-lived
        process, and it is armed in production. An exception escaping it climbed
        up to ``on_call_tool`` — which has only a ``finally`` — and killed the
        call. An OBSERVATION channel cannot be a point of failure for the
        operation it observes: at worst we lose one panel row.

        The log is at ``warning`` and not ``debug``: unlike a refusal from the
        receiver, which can repeat on every call, an exception here signals a
        defect in the emitter itself, not a state of the network.
        """
        reporter = get_activity_reporter()
        if reporter is None:
            return
        try:
            reporter.report(actor, session, transport)
        except Exception:
            logger.warning("activity_reporter.report_failed", exc_info=True)
