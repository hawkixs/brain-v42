"""Corpus provenance — who touched which entity.

The ``X-Brain-Agent`` header is declared by the client, hence falsifiable: it is
a hygiene signal, not a security boundary — the same posture as the session
``client_key``, "declared, not proven".

A leaf module on purpose: no MCP and no database dependency, so it is importable
from the transport layer as well as from the services.
"""

from __future__ import annotations

import os
import uuid
from contextvars import ContextVar, Token

UNKNOWN_ACTOR = "unknown"
UNEXPANDED_ACTOR = "_unexpanded"

# Width of the access_log.actor column (VARCHAR(64)): an event longer than that
# makes the batched executemany fail (PG 22001), and the failure loses the whole
# batch, not just the offending event.
MAX_ACTOR_LENGTH = 64

# Prefixes of the system actors that declare themselves. We recognize the
# `dream-` FAMILY, never a named rail: the three wired runners emit
# `dream-codex-{phase}`, `dream-claude-{phase}` and `dream-agy-{phase}`, and any
# future rail will follow the same template. Enumerating a single rail — which
# `("dream-codex-",)` did — let the other two count their nightly re-reads as
# HUMAN reads.
#
# The guarantee does not come from this tuple but from
# `test_every_dream_rail_header_is_machine`, which re-reads the runners and
# requires every emitted header to be classified as machine: a fourth rail
# straying from the template reddens the suite instead of slipping through.
#
# Public: the SQL predicate of `db/session_derived_capture.py` is the MIRROR of
# `is_human_actor` — it imports these constants instead of redeclaring them,
# without which the two classifications would diverge only at read time, on some
# paths only (the in-house failure mode).
SYSTEM_ACTOR_PREFIXES = ("dream-",)
_SYSTEM_ACTOR_PREFIXES = SYSTEM_ACTOR_PREFIXES

# Machine actors OUTSIDE the `dream-` family, surveyed BY CALL SITE on
# 2026-08-29 (ticket 6878077f). EXACT names, never a prefix: `red-` would
# swallow human basenames (`red-games` launched interactively).
#
# - `red-shrik`: an active bot (`systemctl is-active` → active), `brain_search`
#   in a loop, declaring itself through
#   `red-shrik/src/shrik/mcp_client.py:83`;
# - `antigravity`: the same client, agy deployment
#   (`deploy/agy/settings.mcp.example.json`);
# - `red-lab-factory`: the actor red-lab MUST set (ticket a3fa6696) —
#   pre-classified here so that the cross-repo fix, the day it lands, does not
#   flip this traffic from `unknown` (machine) to a name counted as human:
#   closing one hole must not open another;
# - `pc-dev-red`: the dev PC's scripted client, measured on
#   `brain_ticket_list`.
#
# An accepted cost, in the conservative direction: an interactive session
# launched FROM one of these services' directories declares the same basename
# and counts as machine. The error costs human coverage (a floor), never a false
# write — the direction 6878077f blesses.
SYSTEM_ACTOR_NAMES = frozenset(
    {
        "red-shrik",
        "antigravity",
        "red-lab-factory",
        "pc-dev-red",
    }
)
_NON_HUMAN = frozenset({UNKNOWN_ACTOR, UNEXPANDED_ACTOR, ""})

_current_actor: ContextVar[str] = ContextVar(
    "brain_v42_current_actor",
    default=UNKNOWN_ACTOR,
)


def normalize_agent(value: str | None) -> str:
    """Reduce a raw ``X-Brain-Agent`` to a clean actor name.

    Interactive Claude Code sessions send ``${PWD}``, which Claude Code expands
    into the project's absolute path: we keep the basename. Static service
    labels pass through unchanged. A daemon session (no ``PWD`` in the
    environment) leaves the template unexpanded, which we collapse onto a single
    bucket rather than invent one actor per literal. The result is truncated to
    ``MAX_ACTOR_LENGTH``: the ``access_log.actor`` column is ``VARCHAR(64)`` and
    a longer value would fail the batched insert for the whole batch. The
    truncation is deterministic and preserves the human/system classification —
    a legitimate project name that is too long stays counted as human rather than
    silently flipping to ``unknown``.
    """
    value = (value or "").strip()
    if not value:
        return UNKNOWN_ACTOR
    if "${" in value:
        return UNEXPANDED_ACTOR
    if value.startswith("/"):
        value = os.path.basename(value.rstrip("/")) or UNKNOWN_ACTOR
    return value[:MAX_ACTOR_LENGTH]


_MAX_SESSION_CHARS = 36

_current_session: ContextVar[str | None] = ContextVar(
    "brain_v42_current_session",
    default=None,
)


def normalize_session(value: str | None) -> str | None:
    """Reduce a raw ``X-Brain-Session`` to a canonical UUID, or ``None``.

    Only the canonical lowercase form is accepted: the value serves as a join
    key, and two spellings of the same session would produce two rows. Anything
    that is not a UUID — an unexpanded template, a label, an oversized value —
    is ``None``, that is, "no session declared".
    """
    value = (value or "").strip()
    if not value or len(value) > _MAX_SESSION_CHARS:
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    return value if str(parsed) == value else None


_TRANSPORT_CHARS = 32
_HEX_DIGITS = frozenset("0123456789abcdef")

_current_transport: ContextVar[str | None] = ContextVar(
    "brain_v42_current_transport",
    default=None,
)


def normalize_transport(value: str | None) -> str | None:
    """Reduce a raw ``Mcp-Session-Id`` to a transport identifier, or ``None``.

    A key domain SEPARATE from ``normalize_session``, and deliberately so.
    ``Mcp-Session-Id`` is minted by the SERVER (``uuid4().hex`` in
    ``streamable_http_manager``): 32 lowercase hex characters, no dashes. It
    identifies a CONNECTION, not an agent conversation, and has no counterpart in
    the OTLP streams — it can therefore join nothing. ``normalize_session``, for
    its part, keeps the agent-session space, the only legitimate home of a real
    join the day a client knows how to declare one.

    Strictly fail-closed on the shape: the value stays bounded even if the
    transport guard fails upstream. Length is checked BEFORE content — this
    header is uncontrolled input and nothing obliges a caller to be reasonable.
    """
    value = (value or "").strip()
    if len(value) != _TRANSPORT_CHARS:
        return None
    return value if _HEX_DIGITS.issuperset(value) else None


def set_current_transport(transport: str | None) -> None:
    """Set the transport identifier for the duration of the current context."""
    _current_transport.set(transport)


def get_current_transport() -> str | None:
    """Read the current transport. ``None`` outside a context or when stateless."""
    return _current_transport.get()


def set_current_session(session: str | None) -> None:
    """Set the session for the duration of the current context."""
    _current_session.set(session)


def get_current_session() -> str | None:
    """Read the current session. ``None`` outside a context or undeclared."""
    return _current_session.get()


def set_current_actor(actor: str) -> None:
    """Set the actor for the duration of the current context."""
    _current_actor.set(actor or UNKNOWN_ACTOR)


def get_current_actor() -> str:
    """Lire l'acteur courant. ``unknown`` hors contexte de requête."""
    return _current_actor.get()


def is_human_actor(actor: str | None) -> bool:
    """True if the actor is a human session.

    Fail-closed on the sentinels AND on the system family: an actor that is
    unknown, unexpanded or prefixed `dream-` is NOT human, and therefore cannot
    push an entity past PROMOTE's maturity threshold.

    EXACT SCOPE, not to be overstated — this is what `test_promote_prepare`
    already reminds the PROMOTE judge: this is not an allowlist of humans. A
    human actor is the calling project's basename, arbitrary by construction and
    hence unenumerable; requiring a human to declare themselves would break the
    legitimate case. ANOTHER project's bot setting its own `X-Brain-Agent` is
    therefore still counted as human. The guard covers the `dream-` family AND
    the SURVEYED machine actors (`SYSTEM_ACTOR_NAMES`, by call site, dated in
    their comment) — not every conceivable machine: the rest of the world is
    human by default, that is the price of an unenumerable namespace, and it is
    paid in coverage floor, never in false writes.
    """
    value = (actor or "").strip()
    if value in _NON_HUMAN:
        return False
    if value in SYSTEM_ACTOR_NAMES:
        return False
    return not value.startswith(SYSTEM_ACTOR_PREFIXES)


_call_depth: ContextVar[int] = ContextVar("brain_v42_call_depth", default=0)


def enter_call() -> Token[int]:
    """Descend one level into the tool call chain."""
    return _call_depth.set(_call_depth.get() + 1)


def exit_call(token: Token[int]) -> None:
    """Ressortir du niveau ouvert par ``enter_call``."""
    _call_depth.reset(token)


def is_outermost_call() -> bool:
    """True at the outermost nesting level only.

    Under the ``compact`` profile the ``brain_call_tool`` gateway re-enters the
    middleware chain (measured, commit 58329a84): ``on_call_tool`` fires twice
    per client call. Counting at every level would inflate the counter x2 in
    production and x1 under the native profile.
    """
    return _call_depth.get() == 1
