"""Emitter of activity observations towards the metrics sidecar.

Bounded fire-and-forget: the registry lives in another process, and a slow or
stopped sidecar must never slow down nor break a tool call. Under LOCAL
saturation — every in-flight slot taken — we prefer losing an observation to
making the caller wait; that loss, and it alone, is counted in ``dropped``.

It is neither the only loss mode nor the dominant one. The receiver can refuse
the observation (404 when the route is not registered, 403, 413, 415, 400,
503…), and ``httpx`` does not raise on an error status: those refusals come back
through the nominal path, not through the ``except``. They are therefore read
explicitly, counted separately in ``refused`` and logged with their status alone
— never the response body, which may echo the observation back.

No replay, no backoff: the contract stays fire-and-forget. A refusal is made
OBSERVABLE, not recovered from.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
import time
from pathlib import Path

import httpx
import structlog

from brain_v42.config import get_settings
from brain_v42.metrics.client_observation import (
    MAX_CALLS_PER_OBSERVATION,
    MAX_OBSERVATION_BYTES,
    MAX_OBSERVATIONS,
)

logger = structlog.get_logger(__name__)

_reporter: ActivityReporter | None = None


def _is_a_decade(count: int) -> bool:
    """True on the 1st, 10th, 100th… occurrence, never in between.

    On a counter strictly increasing by one, the guard cannot skip a decade.

    Written in integers rather than with ``log10``. MEASURED, against the
    temptation to overstate this choice: the floating-point variant misses NO
    power of 10 up to 10^24 — it produces false positives on their neighbours
    (999999999999999 declared a decade) from 10^15 on. On a counter incremented
    by one per tool call, that gap is out of reach, and the suite does not catch
    it: substituting ``log10`` here leaves the tests green. The integer choice
    therefore rests on it being exact and import-free, not on a bug anyone
    observed.
    """
    if count < 1:
        return False
    while count % 10 == 0:
        count //= 10
    return count == 1


_ObservationKey = tuple[str, str | None, str | None]

# Margin under the RECEIVER's bound, not a chosen value: the
# ``{"observations":[…]}`` envelope, the commas, and the immediate observation
# that joins the buffer at emission time must all fit inside it.
_BATCH_BYTE_BUDGET = MAX_OBSERVATION_BYTES - 1_024

# The buffer keeps one free slot: the observation that triggers the emission
# joins the batch, and the total must stay under the decoder's bound.
_MAX_BUFFERED = MAX_OBSERVATIONS - 1


def _observation(key: _ObservationKey, calls: int) -> dict[str, object]:
    actor, session_id, transport = key
    observation: dict[str, object] = {"actor": actor, "calls": calls}
    # Key ABSENT rather than ``null``: the decoder distinguishes "not declared"
    # from "declared empty", and a ``null`` on the wire would suggest a
    # measurement where there is none.
    if session_id is not None:
        observation["session"] = session_id
    if transport is not None:
        observation["transport"] = transport
    return observation


class ActivityReporter:
    def __init__(
        self,
        url: str,
        timeout: float = 1.0,
        max_in_flight: int = 8,
        *,
        token_file: str | Path | None = None,
    ) -> None:
        self._url = url
        self._client = httpx.AsyncClient(timeout=timeout)
        self._max_in_flight = max_in_flight
        self._pending: set[asyncio.Task[None]] = set()
        self._token_file = Path(token_file) if token_file is not None else None
        self._token: str | None = None
        self._next_token_check = 0.0
        self.unauthenticated_suspended = 0
        self._suspension_logged = False
        # Two loss modes, two counters: conflating them would make "no call"
        # and "every observation refused" indistinguishable.
        self.dropped = 0  # local back-pressure: in-flight slots saturated
        self.refused = 0  # the receiver answered something other than a 2xx
        self.lost = 0  # observations not accepted by the receiver or transport
        # These counters live in the MCP process; the panel reads the
        # SIDECAR's registry, another process. They therefore reached no human.
        # And they CANNOT travel through the POST they measure: under a
        # permanent 404 — the very scenario that makes them useful — that POST
        # is precisely the one being refused. The log is the only honest
        # channel.
        #
        # Keep outage logs useful without generating one line per tool call.
        self._loss_warning_signatures: dict[tuple[str, object], float] = {}
        # Coalescing buffer. The in-flight bound is not raised: it protects the
        # sidecar. What changes is what we do BEHIND it — aggregate instead of
        # discard. The wire format already carried the two necessary levers and
        # neither was used: a batch (``MAX_OBSERVATIONS``) and a counter
        # (``MAX_CALLS_PER_OBSERVATION``), while ``calls`` was hardcoded to 1.
        self._buffer: dict[_ObservationKey, int] = {}
        self._buffer_bytes = 0
        # Third counter, distinct from the other two: ``coalesced`` measures
        # what would have been LOST before this fix. Conflating it with
        # ``dropped`` would make the fix invisible in its own metrics.
        self.coalesced = 0

    def report(
        self,
        actor: str,
        session_id: str | None,
        transport: str | None = None,
    ) -> None:
        """Report a client call. Never blocks, never raises.

        ``transport`` defaults to ``None`` so that two-argument callers stay
        valid: stateless mode mints no connection identifier, and that absence
        must remain expressible.
        """
        key: _ObservationKey = (actor, session_id, transport)
        # A TOTAL ``try``: everything added here inherits the emitter's promise
        # — never break the observed call. A buffer failure degrades into a
        # COUNTED loss, never into an exception surfaced to the caller.
        try:
            if len(self._pending) < self._max_in_flight:
                self._emit(self._take_buffer() + [_observation(key, 1)])
                return
            if self._coalesce(key):
                self.coalesced += 1
                return
        except Exception:  # noqa: BLE001 - degrades into a counted loss, never a failure
            logger.debug("activity_reporter.coalesce_failed")
        self._count_drop()

    def _count_drop(self) -> None:
        self.dropped += 1
        if _is_a_decade(self.dropped):
            logger.warning(
                "activity_reporter.dropped",
                dropped=self.dropped,
                max_in_flight=self._max_in_flight,
            )

    def _coalesce(self, key: _ObservationKey) -> bool:
        """Fold an observation into the buffer. False if a bound is reached.

        Both bounds are the DECODER's, imported and not copied: crossing the
        byte bound would get the ENTIRE batch refused with a ``413``, thereby
        trading the loss of ONE observation for that of sixty-three.
        """
        buffered = self._buffer.get(key)
        if buffered is not None:
            if buffered >= MAX_CALLS_PER_OBSERVATION:
                return False
            self._buffer[key] = buffered + 1
            return True
        cost = len(json.dumps(_observation(key, 1)).encode()) + 1
        if len(self._buffer) >= _MAX_BUFFERED:
            return False
        if self._buffer_bytes + cost > _BATCH_BYTE_BUDGET:
            return False
        self._buffer[key] = 1
        self._buffer_bytes += cost
        return True

    def _take_buffer(self) -> list[dict[str, object]]:
        if not self._buffer:
            return []
        batch = [_observation(key, calls) for key, calls in self._buffer.items()]
        self._buffer.clear()
        self._buffer_bytes = 0
        return batch

    def _emit(self, observations: list[dict[str, object]]) -> None:
        body = json.dumps({"observations": observations})
        # Counted here, from the batch just built, so that ``_post`` never has
        # to parse a body outside its own guard. One observation per reported
        # call, as ``dropped`` and ``coalesced`` count them: a coalesced entry
        # carries its ``calls``.
        observation_count = sum(
            calls if isinstance(calls := item.get("calls", 1), int) else 1 for item in observations
        )
        # Reference retained in _pending: without it, the GC can collect the
        # task before it runs (the loop holds only a weakref).
        task = asyncio.create_task(self._post(body, observation_count))
        self._pending.add(task)
        task.add_done_callback(self._on_post_done)

    def _on_post_done(self, task: asyncio.Task[None]) -> None:
        """Return the slot, then drain the buffer if anything is left.

        This is what makes coalescing safe without a timer: the buffer is
        drained by the event that frees a slot, not by a clock. Without this
        callback, a burst followed by silence would leave its observations in
        the buffer until the next tool call — deferred, not lost, but invisible
        for an unbounded time.

        ``suppress``: this callback runs in the loop, outside any caller. An
        exception escaping it would go to the loop's exception handler, where
        nobody reads it.
        """
        self._pending.discard(task)
        with contextlib.suppress(Exception):
            if self._buffer and len(self._pending) < self._max_in_flight:
                self._emit(self._take_buffer())

    async def _post(self, body: str, observation_count: int = 0) -> None:
        try:
            headers = {"Content-Type": "application/json"}
            token = None
            if self._token_file is not None:
                token = await self._get_token()
                if token is None:
                    self.unauthenticated_suspended += observation_count
                    if not self._suspension_logged:
                        self._suspension_logged = True
                        logger.info("client_activity.unauthenticated_suspended")
                    return
                headers["Authorization"] = "Bearer " + token
            response = await self._client.post(
                self._url,
                content=body,
                headers=headers,
            )
            if response.status_code == 401 and token is not None and self._token == token:
                # A rotated file in the mounted directory is visible on the next call.
                # An older in-flight refusal must not invalidate a newer credential.
                self._token = None
                self._next_token_check = 0.0
            if response.is_success:
                self._loss_warning_signatures.clear()
            else:
                # ``httpx`` does not raise on 4xx/5xx: without this read, a
                # refusal comes back through the nominal path and disappears
                # without a trace. The measured case is permanent, not
                # transient: a non-loopback bind of the sidecar does not
                # register ``/v1/client-activity``, so every POST receives a
                # 404, the panel's "brain" half stays empty forever and the
                # whole chain declares itself healthy. Status alone, never
                # ``response.text``: the body of a refusal is uncontrolled
                # input, and many receivers echo the request back into it —
                # session UUID included.
                self.refused += observation_count
                self.lost += observation_count
                self._warn_loss(
                    "activity_reporter.refused",
                    status=response.status_code,
                    refused=self.refused,
                    lost=self.lost,
                )
        except Exception as exc:
            self.lost += observation_count
            # Do not pass exc_info: traceback frame locals can contain the session UUID.
            self._warn_loss(
                "activity_reporter.post_failed", error=type(exc).__name__, lost=self.lost
            )

    async def _get_token(self) -> str | None:
        """Retry absent files at most once per five seconds, without blocking a tool."""
        if self._token is not None:
            return self._token
        now = time.monotonic()
        if now < self._next_token_check:
            return None
        # Reserve before awaiting: concurrent posts share the bounded file check.
        self._next_token_check = now + 5.0
        try:
            self._token = await asyncio.to_thread(self._read_token_file)
        except Exception:  # noqa: BLE001 - file errors never interrupt the observed tool
            self._token = None
        return self._token

    def _read_token_file(self) -> str | None:
        """Read a bounded private regular file; reject links and special files safely."""
        if self._token_file is None:
            return None
        descriptor = os.open(self._token_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                return None
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                return None
            token = raw.decode("utf-8").strip()
            return token if token and not any(character.isspace() for character in token) else None
        finally:
            os.close(descriptor)

    def _warn_loss(self, event: str, **fields: object) -> None:
        """Report each new loss signature; throttle repeats for one minute."""
        now = time.monotonic()
        discriminator = fields.get("status", fields.get("error", ""))
        signature = (event, str(discriminator))
        last = self._loss_warning_signatures.get(signature)
        if last is None or now - last >= 60.0:
            self._loss_warning_signatures[signature] = now
            logger.warning(event, **fields)

    async def drain(self) -> None:
        """Wait for in-flight emissions. Reserved for tests and shutdown.

        It removes the awaited tasks from ``_pending`` itself rather than
        relying on their ``done_callback`` (``self._pending.discard``) to do it.
        On Python 3.12+, ``asyncio.gather()`` handles already-``done()`` futures
        eagerly: awaiting an already finished task never yields to the event
        loop, so if the callback has not had its turn yet (it needs a second
        loop iteration), `while self._pending: await asyncio.gather(...)` loops
        indefinitely without ever letting that callback run — a 100 % CPU
        livelock that never yields, not even to an enclosing `asyncio.wait_for`.

        The loop continues while `_pending` is non-empty, to cover the emissions
        started during the wait itself.
        """
        while self._pending or self._buffer:
            if self._buffer and len(self._pending) < self._max_in_flight:
                self._emit(self._take_buffer())
            if not self._pending:
                break
            batch = tuple(self._pending)
            await asyncio.gather(*batch, return_exceptions=True)
            self._pending.difference_update(batch)

    async def close(self) -> None:
        await self.drain()
        await self._client.aclose()


def set_activity_reporter(reporter: ActivityReporter | None) -> None:
    """Install an explicit emitter. Reserved for tests."""
    global _reporter
    _reporter = reporter


async def close_activity_reporter() -> None:
    """Close the emitter if it was built — wired into ``app_lifecycle``.

    Ticket ``d5e4bd73``, second hole: in-flight POSTs died at shutdown without
    being counted. ``close()`` drains the buffer and waits for the in-flight
    emissions (bounded by the httpx timeout), so every loss ends up in a counter
    instead of disappearing with the process. The global memo is reset to
    ``None`` BEFORE closing: a late tool call would rebuild a fresh emitter
    rather than post on a closed client. Never raises — a sidecar dead at
    shutdown must not make the shutdown fail.
    """
    global _reporter
    reporter = _reporter
    _reporter = None
    if reporter is None:
        return
    with contextlib.suppress(Exception):
        await reporter.close()


def get_activity_reporter() -> ActivityReporter | None:
    """Return the emitter, built on first use.

    Lazy construction rather than wiring into the server lifecycle: `mcp` is
    built at module level (see the comment at `mcp/server.py:170`), and the first
    emission happens in an already running loop. Closing, on the other hand, is
    wired: `close_activity_reporter` lives in `app_lifecycle`'s AsyncExitStack,
    so that emissions in flight at shutdown end up counted instead of dying with
    the process (d5e4bd73, second hole).

    Never raises: the caller is the provenance middleware, on the path of EVERY
    tool call. A settings resolution or a client construction that fails is
    treated as an unavailability (returns ``None``) rather than breaking the call
    in progress.
    """
    global _reporter
    if _reporter is None:
        try:
            settings = get_settings()
            if not settings.client_activity_reporting_enabled:
                return None
            _reporter = ActivityReporter(
                url=settings.client_activity_url,
                token_file=getattr(
                    settings, "client_activity_token_file", "/etc/brain-v42/activity/token"
                ),
            )
        except Exception as exc:
            # Type only, same reason as in ``_post``: the frames traversed here
            # are those of settings construction, whose local variables carry
            # the configuration (DSN included). And the failure repeats on every
            # tool call while the resolution fails, since ``_reporter`` stays
            # ``None``.
            logger.debug("activity_reporter.unavailable", error=type(exc).__name__)
            return None
    return _reporter
