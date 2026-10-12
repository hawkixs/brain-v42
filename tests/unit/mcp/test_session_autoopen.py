"""Session auto-opening — synchronous, fail-open, memoized, AND OBSERVING.

Signed shape `ae0d0475` / ADR §0ter. Four properties, each with its test: the
write happens BEFORE the tool; failure is NEVER propagated; one opening per
client call despite `on_call_tool` firing twice under the `compact` profile; and
nothing at all under stdio, where no connection identifier exists (§0ter.2).

**Fifth property, the one without which M-G is inert**: a memoized path is not a
mute path. Guarantee 2 of `§0bis.3` is literal — `last_observed_at` moves on
EVERY tool call — and it is the only column the sweep's 4 h rule can read. A memo
returning the UUID without stamping would leave the column NULL across the whole
table, hence the rule with no row to take: green, silent, and wrong.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextvars import Context
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from brain_v42.config import Settings
from brain_v42.mcp.provenance_middleware import ProvenanceMiddleware
from brain_v42.mcp.session_autoopen import (
    AutoOpenIdentity,
    SessionAutoOpener,
    get_session_autoopener,
    reset_session_autoopener,
)
from brain_v42.provenance import (
    UNEXPANDED_ACTOR,
    set_current_actor,
    set_current_transport,
)

_CONNECTION = "3f2b1a0c9d8e7f6a5b4c3d2e1f0a9b8c"
_OTHER_CONNECTION = "aaaa1111bbbb2222cccc3333dddd4444"
#: What `normalize_agent` returns from this repository's `${PWD}`: the basename.
_ACTOR = "brain_v42"
#: A throwaway DSN — `Settings` requires one, nothing connects to it in these tests.
_DSN = "postgresql+asyncpg://brain:brain@127.0.0.1:5433/brain_test"


def _context(tool_name: str = "brain_get") -> MagicMock:
    context = MagicMock()
    context.message.name = tool_name
    return context


def _headers(
    *,
    agent: str | None = "/home/user/hawkixs_infra/git_repo/brain_v42",
    connection: str | None = _CONNECTION,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if agent is not None:
        headers["x-brain-agent"] = agent
    if connection is not None:
        headers["mcp-session-id"] = connection
    return headers


class _RecordingOpener:
    """Test opener: records every identity, returns a fresh UUID."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.seen: list[AutoOpenIdentity] = []
        self._raises = raises

    async def __call__(self, identity: AutoOpenIdentity) -> UUID | None:
        self.seen.append(identity)
        if self._raises is not None:
            raise self._raises
        return uuid4()


class _RecordingObserver:
    """Test observer: records the stamped UUIDs, returns "still open"."""

    def __init__(self, *, still_open: bool = True, raises: BaseException | None = None) -> None:
        self.seen: list[UUID] = []
        self.still_open = still_open
        self._raises = raises

    async def __call__(self, session_id: UUID) -> bool:
        self.seen.append(session_id)
        if self._raises is not None:
            raise self._raises
        return self.still_open


def _opener(
    opener: _RecordingOpener | None = None,
    observer: _RecordingObserver | None = None,
) -> SessionAutoOpener:
    """Build an opener with both its writers, so neither is forgotten."""
    return SessionAutoOpener(opener or _RecordingOpener(), observer or _RecordingObserver())


@pytest.fixture(autouse=True)
def _isolate_autoopener() -> Iterator[None]:
    reset_session_autoopener()
    set_current_actor(_ACTOR)
    set_current_transport(_CONNECTION)
    yield
    reset_session_autoopener()
    set_current_transport(None)


class TestClosedByDefault:
    def test_flag_default_is_false(self) -> None:
        """The flag ships CLOSED — requirement R3, not a preference."""
        assert Settings.model_fields["brain_session_auto_open_enabled"].default is False

    def test_getter_returns_none_when_flag_is_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Flag closed ⇒ no opener, so the middleware does nothing.

        ``get_settings`` is ``lru_cache(maxsize=1)``: without neutralizing that
        cache, this test would stay GREEN whatever the field default — it would
        read the settings of an earlier call. MEASURED: the first draft of this
        test survived flipping the default to ``True``, so it proved nothing. The
        substitution below and the REVERSE direction are what make it bite.
        """
        monkeypatch.setattr(
            "brain_v42.mcp.session_autoopen.get_settings",
            lambda: Settings(postgres_url=_DSN, brain_session_auto_open_enabled=False),
        )
        assert get_session_autoopener() is None

    def test_getter_builds_an_opener_when_flag_is_armed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reverse direction — without it, the test above would pass over dead code."""
        monkeypatch.setattr(
            "brain_v42.mcp.session_autoopen.get_settings",
            lambda: Settings(postgres_url=_DSN, brain_session_auto_open_enabled=True),
        )
        assert isinstance(get_session_autoopener(), SessionAutoOpener)


class TestIdentityResolution:
    async def test_writes_agent_nature_and_connection_identity(self) -> None:
        opener = _RecordingOpener()
        assert await _opener(opener).ensure_open() is not None
        assert len(opener.seen) == 1
        identity = opener.seen[0]
        # `nature` is the ONLY 046 column in the public MCP contract; the other
        # four travel here, in the internal identity.
        assert identity.nature == "agent"
        assert identity.connection_id == _CONNECTION
        assert identity.started_by_actor == _ACTOR
        # basename `brain_v42` -> canonical key `brain-v42`.
        assert identity.project_key == "brain-v42"
        # A NULL `intent` means "not measured", never "empty".
        assert identity.intent is None

    async def test_stdio_opens_nothing(self) -> None:
        """§0ter.2: NO AUTOMATIC SESSION at all under stdio."""
        set_current_transport(None)
        opener = _RecordingOpener()
        auto = _opener(opener)
        assert await auto.ensure_open() is None
        assert opener.seen == []
        assert auto.skipped["no_connection"] == 1

    async def test_unexpanded_actor_opens_nothing(self) -> None:
        """Without a normalizable actor there is no honest project: we do not invent one."""
        set_current_actor(UNEXPANDED_ACTOR)
        opener = _RecordingOpener()
        auto = _opener(opener)
        assert await auto.ensure_open() is None
        assert opener.seen == []
        assert auto.skipped["no_actor"] == 1

    async def test_non_canonical_actor_opens_nothing(self) -> None:
        """An actor that is not a valid project key does not become a project."""
        set_current_actor("Not A Project Key")
        opener = _RecordingOpener()
        auto = _opener(opener)
        assert await auto.ensure_open() is None
        assert opener.seen == []
        assert auto.skipped["no_project"] == 1


@pytest.mark.parametrize("mode", ["credentials", "shared_token"])
@pytest.mark.parametrize("actor", ["red-rail", "ha-workers", "workstation-claude"])
async def test_non_project_trace_is_skipped_only_in_credentials_mode(mode: str, actor: str) -> None:
    from starlette.responses import JSONResponse
    from starlette.types import Receive, Scope, Send

    from brain_v42.mcp.credentials_http import CredentialGuard
    from tests.unit.mcp.test_credentials_http import TOKEN, issuer_verifier, request

    opener = _RecordingOpener()
    auto = _opener(opener)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        set_current_actor(scope.get("state", {}).get("brain_actor", actor))
        opened = await auto.ensure_open()
        if mode == "credentials":
            assert opened is None
        else:
            assert opened is not None
        await JSONResponse({})(scope, receive, send)

    guard = (
        CredentialGuard(app, verifier=await issuer_verifier([actor]))
        if mode == "credentials"
        else app
    )
    with capture_logs() as logs:
        response = await request(guard, token=TOKEN, agent=actor)
    assert response.status_code == 200
    assert logs == []
    if mode == "credentials":
        assert opener.seen == []
        assert auto.skipped["no_project"] == 1
        assert auto.failed == 0
    else:
        assert len(opener.seen) == 1
        assert opener.seen[0].project_key == actor


@pytest.mark.parametrize(
    ("raw", "expected_actor", "opens"),
    [
        ("/checkout/brain_v42/.claude/worktrees/topic/src", "brain-v42", True),
        ("/checkout/brain_v42/.worktrees/topic", "brain-v42", True),
        ("brain", "brain", True),
        ("/checkout/unknown-key", "red-rail", False),
    ],
)
async def test_credential_actor_reaches_provenance_and_trace(
    raw: str, expected_actor: str, opens: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.types import Receive, Scope, Send

    from brain_v42.mcp.credentials_http import CredentialGuard
    from brain_v42.provenance import get_current_actor, get_current_principal
    from tests.unit.mcp.test_credentials_http import TOKEN, issuer_verifier, request

    opener = _RecordingOpener()
    auto = _opener(opener)
    monkeypatch.setattr("brain_v42.mcp.provenance_middleware.get_session_autoopener", lambda: auto)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["state"]["brain_actor"] == expected_actor
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_request", lambda: Request(scope)
        )
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **kwargs: {"x-brain-agent": raw, "mcp-session-id": _CONNECTION},
        )

        async def tool(context: object) -> str:
            assert get_current_actor() == expected_actor
            assert get_current_principal() == "red-rail"
            return "done"

        # The SDK tool task does not inherit the current guard's verification.
        # Only request state crosses this boundary; raw headers are insufficient.
        task = asyncio.create_task(
            ProvenanceMiddleware().on_call_tool(_context(), tool), context=Context()
        )
        assert await task == "done"
        await JSONResponse({})(scope, receive, send)

    response = await request(
        CredentialGuard(app, verifier=await issuer_verifier(["@project", "brain"])),
        token=TOKEN,
        agent=raw,
    )
    assert response.status_code == 200
    if opens:
        assert len(opener.seen) == 1
        assert opener.seen[0].project_key == "brain-v42"
        assert opener.seen[0].started_by_actor == expected_actor
    else:
        assert opener.seen == []
        assert auto.skipped["no_project"] == 1


class TestIdempotence:
    async def test_memoized_per_connection(self) -> None:
        opener = _RecordingOpener()
        auto = _opener(opener)
        first = await auto.ensure_open()
        second = await auto.ensure_open()
        assert first is not None
        assert second == first
        assert len(opener.seen) == 1
        assert auto.memoized == 1

    async def test_distinct_connections_open_distinct_sessions(self) -> None:
        opener = _RecordingOpener()
        auto = _opener(opener)
        first = await auto.ensure_open()
        set_current_transport(_OTHER_CONNECTION)
        second = await auto.ensure_open()
        assert first != second
        assert [identity.connection_id for identity in opener.seen] == [
            _CONNECTION,
            _OTHER_CONNECTION,
        ]

    async def test_double_dispatch_opens_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`compact` profile: `on_call_tool` fires TWICE per client call.

        It is `is_outermost_call()` that makes auto-opening idempotent — the depth
        guard, not the memo. The witness is therefore a counter of CALLS to
        `ensure_open`, not the opener: the memo would mask a second firing instead
        of proving it did not happen.
        """
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: _headers(),
        )
        calls: list[str] = []

        class _Counting(SessionAutoOpener):
            async def ensure_open(self) -> UUID | None:
                calls.append("ensure_open")
                return None

        auto = _Counting(_RecordingOpener(), _RecordingObserver())
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_session_autoopener",
            lambda: auto,
        )
        middleware = ProvenanceMiddleware()

        async def inner(_ctx: object) -> str:
            return "inner"

        async def outer(ctx: object) -> str:
            # The `brain_call_tool` gateway re-enters the chain.
            return await middleware.on_call_tool(ctx, inner)

        assert await middleware.on_call_tool(_context(), outer) == "inner"
        assert calls == ["ensure_open"]


class TestSynchronousBeforeTheTool:
    async def test_session_exists_before_the_tool_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fire-and-forget attributes nothing: the opening precedes the tool.

        An ORDER witness in the shared log — if the opening were asynchronous,
        `call_next` would run before it.
        """
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: _headers(),
        )
        order: list[str] = []

        async def opener(_identity: AutoOpenIdentity) -> UUID:
            order.append("open")
            return uuid4()

        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_session_autoopener",
            lambda: SessionAutoOpener(opener, _RecordingObserver()),
        )

        async def call_next(_ctx: object) -> str:
            order.append("tool")
            return "ok"

        assert await ProvenanceMiddleware().on_call_tool(_context(), call_next) == "ok"
        assert order == ["open", "tool"]


class TestFailOpen:
    @pytest.mark.parametrize("failure_path", ["opener", "observer", "closer"])
    async def test_failure_logs_redact_connection_id_and_exception_message(
        self, failure_path: str
    ) -> None:
        connection_id = "0123456789abcdef0123456789abcdef01234567"
        error = RuntimeError(f"database failed for {connection_id}")
        set_current_transport(connection_id)

        class _RaisingCloser:
            async def __call__(self, _connection_id: str) -> list[UUID]:
                raise error

        opener = _RecordingOpener(raises=error if failure_path == "opener" else None)
        observer = _RecordingObserver(raises=error if failure_path == "observer" else None)
        auto = SessionAutoOpener(
            opener,
            observer,
            _RaisingCloser() if failure_path == "closer" else None,
        )
        with capture_logs() as logs:
            if failure_path == "observer":
                observer._raises = None
                session_id = await auto.ensure_open()
                assert session_id is not None
                observer._raises = error
                assert await auto.ensure_open() == session_id
            elif failure_path == "closer":
                await auto.close(connection_id)
            else:
                assert await auto.ensure_open() is None

        assert len(logs) == 1
        event = logs[0]
        assert event["connection_id"] == connection_id[:8] + "…"
        assert event["error_type"] == "RuntimeError"
        assert all(connection_id not in repr(captured) for captured in logs)
        assert all("exc_info" not in captured for captured in logs)

    async def test_open_failure_never_breaks_the_tool_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-open: the call goes through anyway. With a NEGATIVE WITNESS.

        The witness is indispensable: a test observing only "the call succeeded"
        would stay green if auto-opening were never attempted. So we prove BOTH
        directions in the same test — the opener was called and did raise, and the
        tool call still returned its value.
        """
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_http_headers",
            lambda **_kw: _headers(),
        )
        boom = _RecordingOpener(raises=RuntimeError("database is down"))
        auto = _opener(boom)
        monkeypatch.setattr(
            "brain_v42.mcp.provenance_middleware.get_session_autoopener",
            lambda: auto,
        )

        async def call_next(_ctx: object) -> str:
            return "ok"

        result = await ProvenanceMiddleware().on_call_tool(_context(), call_next)

        assert result == "ok"  # the call goes through
        assert len(boom.seen) == 1  # NEGATIVE WITNESS: the opening WAS attempted
        assert auto.failed == 1  # and it did FAIL

    async def test_failure_is_not_memoized(self) -> None:
        """A failure lays down no memo: otherwise the connection would lose its session for life."""
        boom = _RecordingOpener(raises=RuntimeError("transient"))
        auto = _opener(boom)
        await auto.ensure_open()
        await auto.ensure_open()
        assert len(boom.seen) == 2
        assert auto.memoized == 0


class TestObservation:
    """`last_observed_at` moves on EVERY call — otherwise the 4 h rule is dead."""

    async def test_a_fresh_open_does_not_also_observe(self) -> None:
        """The INSERT already stamps the row: a second write would be free of purpose."""
        opener, observer = _RecordingOpener(), _RecordingObserver()
        auto = _opener(opener, observer)
        assert await auto.ensure_open() is not None
        assert len(opener.seen) == 1
        assert observer.seen == []

    async def test_the_memoized_path_dates_the_same_session(self) -> None:
        """The fast path is not a mute path.

        NEGATIVE WITNESS inside the test: we ALSO check the opener did not replay.
        Without it, an observer called by a silent reopening would turn this test
        green while proving the opposite of its name.
        """
        opener, observer = _RecordingOpener(), _RecordingObserver()
        auto = _opener(opener, observer)
        first = await auto.ensure_open()
        second = await auto.ensure_open()
        third = await auto.ensure_open()
        assert first is not None
        assert (second, third) == (first, first)
        assert observer.seen == [first, first]
        assert len(opener.seen) == 1
        assert auto.memoized == 2

    async def test_a_session_closed_under_us_is_reopened(self) -> None:
        """The case the signed shape names: the sweep closes, the connection lives.

        The memo must survive it. The authority is the **PARTIAL** UNIQUE index
        `WHERE status = 'open'`: the closed row does not block, so reopening is the
        normal path. A cache deciding "already done" without the database would
        make this connection mute for life.
        """
        opener = _RecordingOpener()
        observer = _RecordingObserver(still_open=False)
        auto = _opener(opener, observer)
        first = await auto.ensure_open()
        second = await auto.ensure_open()
        assert first is not None
        assert second is not None
        assert second != first
        assert len(opener.seen) == 2
        assert auto.reopened == 1
        assert auto.memoized == 0

    async def test_an_observation_failure_keeps_the_memo_and_never_raises(self) -> None:
        """`None` is not `False`: a hiccup must not manufacture a duplicate.

        Confusing the two would reopen a perfectly live session on every transient
        error — one duplicate per hiccup, where the real loss is a single stamp.
        """
        opener = _RecordingOpener()
        observer = _RecordingObserver(raises=RuntimeError("database is down"))
        auto = _opener(opener, observer)
        first = await auto.ensure_open()
        second = await auto.ensure_open()
        assert second == first
        assert len(opener.seen) == 1  # WITNESS: no reopening
        assert observer.seen == [first]  # and the observation WAS attempted
        assert auto.observe_failed == 1
        assert auto.reopened == 0
