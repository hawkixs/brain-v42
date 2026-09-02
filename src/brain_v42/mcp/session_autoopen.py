"""Auto-opening of an `agent` tracer session, one per connection.

The **signed** shape (`ae0d0475`, ADR §0ter) and its four properties, in the
order they were settled:

1. **Synchronous and BEFORE the tool.** Fire-and-forget attributes nothing:
   capture bounds artifacts by ``created_at >= started_at``, so the session must
   exist at the moment the call creates its own. That is the difference with the
   client activity emitter (`1c40c36a`), which observes nothing it must precede.
2. **Fail-open: failure is NEVER propagated.** Fail-open is not asynchronous —
   we wait for the opening, and let the call through if it fails. The price is
   written once and for all in `SPEC-M-G` §6: artifacts created before a failed
   opening fall outside the capture window, and B5 bites again occasionally.
3. **Memoized per connection.** The memo is a FAST PATH, never the authority:
   that is 046's PARTIAL UNIQUE index ``WHERE status = 'open'``, which makes
   reopening natural after a closure. A cache deciding "already done" without
   the database would lie from the first auto-closure onwards.
4. **Idempotence through the depth guard.** Under the `compact` profile,
   ``on_call_tool`` fires twice per client call (measured, commit 58329a84): it
   is ``is_outermost_call()``, on the middleware side, that reserves the opening
   for the outermost level. The memo cannot play that role — it would mask the
   second firing instead of preventing it.

**Nothing at all under stdio** (§0ter.2, signed). Auto-opening exists only over
HTTP, on the ``(project, connection)`` key, because ``Mcp-Session-Id`` is the
only one of the three identifiers the client does not declare: it is minted
server-side. Falling back on ``(project, actor)`` was explicitly REJECTED — it
would attribute on something declared, where the whole value of the model is
attributing on the one non-falsifiable signal. Here, the absence of a connection
identifier is therefore not a degraded case to compensate for: it is the
contract.

Hard precondition, inherited and not re-signed: ``mcp_http_stateless=False``. In
stateless mode there is no connection identifier, and this key falls away.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import structlog

from brain_v42.config import get_settings
from brain_v42.models.project_key import canonicalize_project_key
from brain_v42.provenance import (
    MAX_ACTOR_LENGTH,
    UNEXPANDED_ACTOR,
    UNKNOWN_ACTOR,
    get_current_actor,
    get_current_transport,
)

logger = structlog.get_logger(__name__)

#: Width of ``brain_sessions.connection_id`` (046). Truncate rather than let PG
#: raise a 22001: the identifier is minted by the server as ``uuid4().hex`` (32
#: characters), so truncation is out of reach nominally — it bounds a hostile
#: transport, not the nominal path.
MAX_CONNECTION_ID_LENGTH = 64

#: Cap on the memo. One connection = one entry; without a bound, a long-lived
#: process watching connections go by would grow this dict without end. Same
#: reasoning as ``_MAX_UNIDENTIFIED_TRACKED`` in the middleware.
DEFAULT_MAX_MEMOIZED_CONNECTIONS = 512

_autoopener: SessionAutoOpener | None = None


@dataclass(frozen=True, slots=True)
class AutoOpenIdentity:
    """What an auto-opened session carries, and nothing more.

    Four of 046's five columns travel here and **not** in ``BrainSession``:
    FastMCP derives the tools' output schema from that model, and letting them in
    would cost the schema budget
    ``test_discovery_contract_keeps_tool_identity_inputs_and_schema_budget``
    guarantees. Only ``nature`` is in the public contract.

    ``intent`` stays ``None``: it is the human field for triaging ghosts, and
    ``NULL`` there means "not measured", never "empty". The server does not
    manufacture judgement (objection C9).
    """

    project_key: str
    connection_id: str
    started_by_actor: str
    nature: Literal["agent"] = "agent"
    intent: str | None = None


#: An opener receives the resolved identity and returns the UUID of the opened
#: session (fresh or already open for this connection), or ``None`` when there is
#: nothing to open — for example when the project has no context.
SessionOpener = Callable[[AutoOpenIdentity], Awaitable[UUID | None]]

#: An observer stamps a memoized session and returns "it was still open". The
#: boolean is what makes the memo survive the sweep: ``False`` means "closed from
#: under us", so a memo to discard, not a session to lose.
SessionObserver = Callable[[UUID], Awaitable[bool]]


def resolve_auto_open_identity() -> tuple[AutoOpenIdentity | None, str]:
    """Resolve the current connection's identity, or say why not.

    Returns ``(identity, "")`` or ``(None, reason)``. The three reasons are
    disjoint and counted separately: conflating them would make "stdio" and
    "anonymous client" indistinguishable in the only instrument we will have.
    """
    connection = (get_current_transport() or "").strip()
    if not connection:
        return None, "no_connection"

    actor = get_current_actor()
    if actor in (UNKNOWN_ACTOR, UNEXPANDED_ACTOR) or not actor.strip():
        return None, "no_actor"

    try:
        project_key = canonicalize_project_key(actor)
    except (TypeError, ValueError):
        # `strict=True` INTENDED: this is the write path. `strict=False` would
        # let a malformed key through, which would create a ghost project
        # invisible to the scoped briefing (learning 7bc821a1).
        return None, "no_project"

    return (
        AutoOpenIdentity(
            project_key=project_key,
            connection_id=connection[:MAX_CONNECTION_ID_LENGTH],
            started_by_actor=actor[:MAX_ACTOR_LENGTH],
        ),
        "",
    )


class SessionAutoOpener:
    """Keeps one open `agent` session per connection, without ever raising."""

    def __init__(
        self,
        opener: SessionOpener,
        observer: SessionObserver,
        *,
        max_connections: int = DEFAULT_MAX_MEMOIZED_CONNECTIONS,
    ) -> None:
        self._opener = opener
        self._observer = observer
        self._max_connections = max_connections
        self._memo: OrderedDict[str, UUID] = OrderedDict()
        self.opened = 0
        self.memoized = 0
        self.reopened = 0
        self.failed = 0
        self.observe_failed = 0
        self.skipped: defaultdict[str, int] = defaultdict(int)

    async def ensure_open(self) -> UUID | None:
        """Open or re-observe. **Never raises** — that is the whole contract."""
        identity, reason = resolve_auto_open_identity()
        if identity is None:
            self.skipped[reason] += 1
            return None

        memoized = self._memo.get(identity.connection_id)
        if memoized is not None:
            observed = await self._observe(memoized, identity)
            if observed is not False:
                # ``None`` = the observation failed. We keep the memo: losing a
                # stamp costs one clock line, losing the memo would cost this
                # connection's session.
                self._memo.move_to_end(identity.connection_id)
                self.memoized += 1
                return memoized
            # The session was closed from under us — the case the signed shape
            # names. Nothing to repair: the UNIQUE key is PARTIAL
            # (``WHERE status = 'open'``), so the closed row does not block, and
            # reopening is the normal path, not a recovery.
            del self._memo[identity.connection_id]
            self.reopened += 1

        try:
            session_id = await self._opener(identity)
        except Exception:
            # A TOTAL, tightly scoped ``except``, the same posture as
            # ``_report``: this path runs on EVERY outermost tool call of a
            # shared process. A database hiccup cannot bring down the call it
            # accompanies.
            #
            # ``warning`` and not ``debug``: unlike a receiver refusal, which can
            # repeat on every call, a failure here signals a defect in the
            # opener itself.
            self.failed += 1
            logger.warning(
                "session_autoopen.failed",
                project_key=identity.project_key,
                connection_id=identity.connection_id,
                exc_info=True,
            )
            return None

        if session_id is None:
            # No opening possible (project context absent): this is NOT a
            # failure, and above all not a memo — the context may be born later,
            # and the connection must be able to benefit from it.
            self.skipped["no_session"] += 1
            return None

        self._remember(identity.connection_id, session_id)
        self.opened += 1
        return session_id

    async def _observe(self, session_id: UUID, identity: AutoOpenIdentity) -> bool | None:
        """Stamp the memoized session. Returns ``None`` if the observation failed.

        Three outcomes, and conflating them would cost dearly: ``True`` (still
        open), ``False`` (closed in the meantime, memo to discard) and ``None``
        (the write failed). Treating ``None`` as ``False`` would reopen a
        perfectly live session on every database hiccup, and one duplicate per
        hiccup is worse than one lost stamp.
        """
        try:
            return await self._observer(session_id)
        except Exception:
            self.observe_failed += 1
            logger.warning(
                "session_autoopen.observe_failed",
                project_key=identity.project_key,
                connection_id=identity.connection_id,
                exc_info=True,
            )
            return None

    def _remember(self, connection_id: str, session_id: UUID) -> None:
        self._memo[connection_id] = session_id
        self._memo.move_to_end(connection_id)
        while len(self._memo) > self._max_connections:
            # LRU rather than refusing to insert: an evicted entry costs one
            # database round trip, never a lost session — the partial index makes
            # this path idempotent.
            self._memo.popitem(last=False)


def get_session_autoopener() -> SessionAutoOpener | None:
    """Return the opener, or ``None`` while the flag is closed.

    Never raises: the caller is the provenance middleware, on the path of EVERY
    tool call. A settings resolution that fails is treated as an unavailability,
    not as a call error.
    """
    global _autoopener
    if _autoopener is None:
        try:
            settings = get_settings()
            if not settings.brain_session_auto_open_enabled:
                return None
            _autoopener = SessionAutoOpener(*_build_default_writers())
        except Exception as exc:
            # Type only: the frames traversed carry the configuration, DSN
            # included.
            logger.debug("session_autoopen.unavailable", error=type(exc).__name__)
            return None
    return _autoopener


def reset_session_autoopener() -> None:
    """Forget the memoized opener — a test entry point, never a production one."""
    global _autoopener
    _autoopener = None


def _build_default_writers() -> tuple[SessionOpener, SessionObserver]:
    """Wire the production opener AND observer onto the session repository.

    Both come from the SAME repository, hence the same engine: an opener writing
    somewhere other than the observer would produce a session nobody stamps.
    """
    from brain_v42.db.engine import get_session_factory  # noqa: PLC0415
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo  # noqa: PLC0415

    repo = PgBrainSessionRepo(get_session_factory())

    async def _open(identity: AutoOpenIdentity) -> UUID | None:
        return await repo.auto_open(identity)

    async def _observe(session_id: UUID) -> bool:
        return await repo.observe(session_id)

    return _open, _observe
