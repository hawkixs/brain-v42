"""Starlette app of the shim — brain-v42 legacy embedding contract (port 8003).

Exact parity with services/embedding/main.py v2.0.0 (PyTorch):
  POST /embed         {"texts": [...]}          -> [[float,...],...]
  POST /embed/query   {"text": "..."} | ?text=  -> [float,...]
  POST /embed/single  idem /embed/query         -> [float,...]
  POST /rerank        {"query","candidates"}    -> {"scores": [...]}
  GET  /              -> model/runtime info
  GET  /healthz       -> 200 if the llama upstream is healthy, else 503
  GET  /health        -> 200 (compat with RerankerClient.is_available)

One deliberate difference: /healthz probes the upstream (the old /healthz
never touched the GPU — that is the false-green bug of the 2026-04-12
incident, learning 410eb227; fixed here).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import stat
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

EMBED_MODEL = "Qodo/Qodo-Embed-1-1.5B"
RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
EXPECTED_DIMS = 1536
RUNTIME = "llama.cpp-gguf-q8_0+onnx-cpu"
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ShimLimits:
    """Resource bounds enforced by one shim instance."""

    max_body_bytes: int = 8 * 1024 * 1024
    max_ingress_requests: int = 8
    body_read_timeout_seconds: float = 5.0
    max_embed_batch: int = 100
    max_rerank_batch: int = 128
    max_json_depth: int = 64
    max_embed_compute: int = 1
    max_rerank_compute: int = 1


_DEFAULT_LIMITS = ShimLimits()

#: Patterned on src/brain_v42/codex_gateway/auth.py — not an import: the shim
#: image does not ship the brain_v42 package.
MIN_BEARER_TOKEN_BYTES = 32

#: The health endpoints stay open even in armed mode: the systemd watchdog and
#: RerankerClient.is_available carry no token, and a 401 on /healthz would turn
#: the arming into a fake upstream outage.
_AUTH_EXEMPT_PATHS = frozenset({"/healthz", "/health"})

#: The inside of the netns is WITHIN the process's trust boundary. Explicit
#: assumption: only a process in the container's network namespace can source
#: 127.0.0.1 — the kernel drops loopback-sourced packets arriving on a non-lo
#: interface (martian filtering, route_localnet=0), and connections published
#: by Docker arrive with the bridge gateway's address, never 127.0.0.1. The
#: ONLY production prober lives there: the compose healthcheck (POST /embed
#: with no Authorization, run inside the container) — without this exemption,
#: arming would leave it unhealthy forever while /healthz stayed green
#: (PR 43 review, reproduced).
_LOOPBACK_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1"})

#: The census knows only the shim's real routes: an unknown path is controlled
#: by the caller (percent-decoded, %0A becomes a real newline) and, logged raw,
#: turns into an injection channel. An unknown path returns its 404 (or 401 in
#: armed mode) without a single log line.
_GUARDED_CENSUS_PATHS = frozenset({"/embed", "/embed/query", "/embed/single", "/rerank", "/"})


@dataclass(frozen=True, slots=True)
class BearerGuard:
    """Bearer statique du shim — ticket 530d796a, point (a).

    ``required=False`` is the OPTIONAL mode: a missing or wrong header is
    accepted but logged, so that clients without a token can be counted without
    breaking a single one (both ``auto-discord`` are alive on ``brain-net``).
    Arming — ``required=True`` — is a separate operator gesture, to be taken
    only after client ticket 9ef5c69d.
    """

    token: bytes
    required: bool


def load_bearer_token(path: Path | str) -> bytes:
    """Read the secret from a 0600 file, with codex_gateway's guards.

    A secret readable beyond its owner, too short, or still carrying its
    ``REPLACE_`` placeholder is refused at startup: better a service that does
    not start than a decorative authentication.

    Read ONCE, at startup: rotating the secret requires a container restart —
    the operator gesture must know it.
    """
    token_path = Path(path)
    mode = stat.S_IMODE(token_path.stat().st_mode)
    if mode & 0o077:
        raise ValueError(
            f"{token_path} must be private (0600); "
            f"current mode {oct(mode)} is readable beyond its owner"
        )
    value = token_path.read_text().strip()
    if value.upper().startswith("REPLACE_"):
        raise ValueError(f"{token_path} still carries a REPLACE_ placeholder, not a secret")
    encoded = value.encode()
    if len(encoded) < MIN_BEARER_TOKEN_BYTES:
        raise ValueError(
            f"{token_path} must hold a generated secret of at least {MIN_BEARER_TOKEN_BYTES} bytes"
        )
    return encoded


def bearer_from_env(environ: Mapping[str, str] = os.environ) -> BearerGuard | None:
    """Env wiring of the bearer — shipped closed.

    Without ``SHIM_BEARER_TOKEN_FILE`` or ``SHIM_BEARER_MODE``, there is no
    guard: the shim's current contract does not move. A mode set without a
    secret file, or an unknown mode, kills startup rather than opening in
    silence — a typo must never amount to "optional".
    """
    token_file = environ.get("SHIM_BEARER_TOKEN_FILE", "")
    mode = environ.get("SHIM_BEARER_MODE", "")
    if not token_file:
        if mode:
            raise ValueError("SHIM_BEARER_MODE is set but SHIM_BEARER_TOKEN_FILE is not")
        return None
    if mode and mode not in ("optional", "required"):
        raise ValueError(f"SHIM_BEARER_MODE must be 'optional' or 'required', got {mode!r}")
    return BearerGuard(token=load_bearer_token(token_file), required=mode == "required")


class _BearerMiddleware:
    """Pure ASGI, never BaseHTTPMiddleware: the shim streams request bodies,
    and this check reads nothing but the scope's headers."""

    def __init__(self, app: ASGIApp, guard: BearerGuard) -> None:
        self._app = app
        self._guard = guard

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _AUTH_EXEMPT_PATHS:
            await self._app(scope, receive, send)
            return

        client = scope.get("client")
        if client and client[0] in _LOOPBACK_CLIENT_HOSTS:
            # Netns-internal (compose healthcheck): the same trust boundary
            # as the process. No census either — it fires every 60 s and would
            # drown the observation log.
            await self._app(scope, receive, send)
            return

        outcome = self._outcome(scope)
        if outcome is not None:
            if self._guard.required:
                response = JSONResponse(
                    {"detail": "Unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
            if scope["path"] in _GUARDED_CENSUS_PATHS:
                # Never the presented value: a log is not an exfiltration
                # channel. The user agent is caller-controlled: %r quotes it,
                # and a newline stays \n inside.
                client_address = f"{client[0]}:{client[1]}" if client else "client-inconnu"
                _LOGGER.warning(
                    "shim bearer %s on %s from %s ua=%r — accepted (optional mode)",
                    outcome,
                    scope["path"],
                    client_address,
                    self._user_agent(scope),
                )
        await self._app(scope, receive, send)

    @staticmethod
    def _user_agent(scope: Scope) -> str:
        for name, value in scope["headers"]:
            if name == b"user-agent":
                return bytes(value).decode("latin-1")
        return "-"

    def _outcome(self, scope: Scope) -> str | None:
        """``None`` if the presented token is right, else the reason to log."""
        raw: bytes | None = None
        for name, value in scope["headers"]:
            if name == b"authorization":
                raw = value
                break
        if raw is None:
            return "missing"
        prefix = b"Bearer "
        if not raw.startswith(prefix):
            return "invalid"
        presented = raw[len(prefix) :]
        if not secrets.compare_digest(presented, self._guard.token):
            return "invalid"
        return None


class _RejectedRequest(Exception):
    def __init__(
        self,
        status_code: int,
        payload: dict[str, str],
        *,
        retry_after: bool = False,
    ) -> None:
        super().__init__(payload)
        self.status_code = status_code
        self.payload = payload
        self.retry_after = retry_after


class _Lease:
    def __init__(self, limiter: anyio.CapacityLimiter, borrower: object) -> None:
        self._limiter = limiter
        self._borrower = borrower
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._limiter.release_on_behalf_of(self._borrower)
        self._released = True


class _TryGate:
    """A CapacityLimiter with atomic, non-waiting acquisition."""

    def __init__(self, capacity: int) -> None:
        self._limiter = anyio.CapacityLimiter(capacity)

    def try_acquire(self) -> _Lease | None:
        borrower = object()
        try:
            self._limiter.acquire_on_behalf_of_nowait(borrower)
        except anyio.WouldBlock:
            return None
        return _Lease(self._limiter, borrower)


def _declared_body_size(request: Request) -> int | None:
    value = request.headers.get("content-length")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _json_depth_exceeds_limit(text: str, limit: int) -> bool:
    depth = 0
    in_string = False
    escaped = False

    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue

        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > limit:
                return True
        elif character in "]}" and depth:
            depth -= 1

    return False


def _decode_json_with_depth_limit(raw: bytes | bytearray, limit: int) -> Any:
    encoding = json.detect_encoding(raw)
    text = raw.decode(encoding, errors="surrogatepass")
    if _json_depth_exceeds_limit(text, limit):
        raise ValueError("maximum JSON depth exceeded")
    return json.loads(text)


async def _read_and_validate[Result](
    request: Request,
    validator: Callable[[dict[str, Any] | None], Result],
    *,
    limits: ShimLimits,
    ingress_gate: _TryGate,
    json_parse_limiter: anyio.CapacityLimiter,
) -> Result:
    declared_size = _declared_body_size(request)
    if declared_size is not None and declared_size > limits.max_body_bytes:
        raise _RejectedRequest(413, {"detail": "Request body too large"})

    lease = ingress_gate.try_acquire()
    if lease is None:
        raise _RejectedRequest(
            503,
            {"error": "ingress_busy"},
            retry_after=True,
        )

    try:
        raw = bytearray()
        try:
            async with asyncio.timeout(limits.body_read_timeout_seconds):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > limits.max_body_bytes:
                        raise _RejectedRequest(
                            413,
                            {"detail": "Request body too large"},
                        )
                    raw.extend(chunk)
        except TimeoutError as exc:
            raise _RejectedRequest(
                408,
                {"detail": "Request body timeout"},
            ) from exc

        if not raw:
            payload = None
        else:
            try:
                async with json_parse_limiter:
                    decoded = await _run_sync_in_thread(
                        lambda: _decode_json_with_depth_limit(raw, limits.max_json_depth)
                    )
            except (ValueError, RecursionError) as exc:
                raise _RejectedRequest(
                    400,
                    {"detail": "Invalid JSON body"},
                ) from exc
            payload = decoded if isinstance(decoded, dict) else None
        return validator(payload)
    finally:
        lease.release()


async def _run_physical[Result](
    gate: _TryGate,
    operation: Callable[[], Coroutine[Any, Any, Result]],
    *,
    busy_error: str,
    active_tasks: set[asyncio.Task[Any]],
) -> Result:
    lease = gate.try_acquire()
    if lease is None:
        raise _RejectedRequest(
            503,
            {"error": busy_error},
            retry_after=True,
        )

    async def leased_operation() -> Result:
        try:
            return await operation()
        finally:
            lease.release()

    try:
        task = asyncio.create_task(leased_operation())
    except BaseException:
        lease.release()
        raise

    active_tasks.add(task)
    handler_detached = False
    detached_failure_reported = False

    def completed(done: asyncio.Task[Any]) -> None:
        nonlocal detached_failure_reported
        active_tasks.discard(done)
        if done.cancelled():
            return
        error = done.exception()
        if handler_detached and error is not None and not detached_failure_reported:
            detached_failure_reported = True
            _LOGGER.error(
                "Detached embedding backend task failed",
                extra={
                    "backend_gate": busy_error,
                    "exception_type": type(error).__name__,
                },
            )

    task.add_done_callback(completed)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        handler_detached = True
        if task.done():
            completed(task)
        raise


async def _run_sync_in_thread[Result](operation: Callable[[], Result]) -> Result:
    """Run a blocking computation without detaching its physical lifetime."""

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embedding-shim")
    try:
        future = executor.submit(operation)
        cancellation: asyncio.CancelledError | None = None
        while not future.done():
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError as exc:
                cancellation = exc
        if cancellation is not None:
            try:
                future.result()
            except BaseException:
                pass
            raise cancellation
        return future.result()
    finally:
        executor.shutdown(wait=False, cancel_futures=False)


def _validate_embed(
    payload: dict[str, Any] | None,
    *,
    max_batch: int,
) -> list[str]:
    texts = payload.get("texts") if payload else None
    if not isinstance(texts, list):
        raise _RejectedRequest(400, {"detail": "texts must be a list"})
    if len(texts) > max_batch:
        raise _RejectedRequest(
            400,
            {"detail": f"texts must contain at most {max_batch} items"},
        )
    return [str(text) for text in texts]


def _validate_embed_single(
    payload: dict[str, Any] | None,
    *,
    query_text: str | None,
) -> str:
    text = payload.get("text") if payload else None
    if not text:
        text = query_text
    if not text:
        raise _RejectedRequest(
            400,
            {
                "detail": (
                    'Missing \'text\' — provide via JSON body {"text": "..."} or ?text= query param'
                )
            },
        )
    return str(text)


def _validate_rerank(
    payload: dict[str, Any] | None,
    *,
    max_batch: int,
) -> tuple[str, list[str]]:
    query = payload.get("query") if payload else None
    candidates = payload.get("candidates") if payload else None
    if not isinstance(query, str) or not isinstance(candidates, list):
        raise _RejectedRequest(
            400,
            {"detail": "query (str) and candidates (list) required"},
        )
    if len(candidates) > max_batch:
        raise _RejectedRequest(
            400,
            {"detail": f"candidates must contain at most {max_batch} items"},
        )
    return query, [str(candidate) for candidate in candidates]


def create_app(
    embed_backend: Any,
    rerank_backend: Any,
    *,
    limits: ShimLimits = _DEFAULT_LIMITS,
    bearer: BearerGuard | None = None,
) -> Starlette:
    ingress_gate = _TryGate(limits.max_ingress_requests)
    json_parse_limiter = anyio.CapacityLimiter(1)
    embed_gate = _TryGate(limits.max_embed_compute)
    rerank_gate = _TryGate(limits.max_rerank_compute)
    active_tasks: set[asyncio.Task[Any]] = set()

    async def embed(request: Request) -> JSONResponse:
        texts = await _read_and_validate(
            request,
            lambda payload: _validate_embed(payload, max_batch=limits.max_embed_batch),
            limits=limits,
            ingress_gate=ingress_gate,
            json_parse_limiter=json_parse_limiter,
        )
        if not texts:
            return JSONResponse([])
        vecs = await _run_physical(
            embed_gate,
            lambda: embed_backend.embed(texts),
            busy_error="gpu_busy",
            active_tasks=active_tasks,
        )
        return JSONResponse(vecs)

    async def embed_single(request: Request) -> JSONResponse:
        text = await _read_and_validate(
            request,
            lambda payload: _validate_embed_single(
                payload,
                query_text=request.query_params.get("text"),
            ),
            limits=limits,
            ingress_gate=ingress_gate,
            json_parse_limiter=json_parse_limiter,
        )
        vecs = await _run_physical(
            embed_gate,
            lambda: embed_backend.embed([text]),
            busy_error="gpu_busy",
            active_tasks=active_tasks,
        )
        return JSONResponse(vecs[0])

    async def rerank(request: Request) -> JSONResponse:
        query, candidates = await _read_and_validate(
            request,
            lambda payload: _validate_rerank(payload, max_batch=limits.max_rerank_batch),
            limits=limits,
            ingress_gate=ingress_gate,
            json_parse_limiter=json_parse_limiter,
        )

        async def run_rerank() -> list[float]:
            return await _run_sync_in_thread(lambda: rerank_backend.rerank(query, candidates))

        scores = await _run_physical(
            rerank_gate,
            run_rerank,
            busy_error="service_busy",
            active_tasks=active_tasks,
        )
        return JSONResponse({"scores": scores})

    async def info(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "embed_model": EMBED_MODEL,
                "rerank_model": RERANK_MODEL,
                "dims": EXPECTED_DIMS,
                "device": "cuda",
                "runtime": RUNTIME,
                "cuda_available": True,
            }
        )

    async def healthz(request: Request) -> JSONResponse:
        if await embed_backend.healthy():
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "degraded", "upstream": "unreachable"}, status_code=503)

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "model": RERANK_MODEL})

    async def rejected_request(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        if not isinstance(exc, _RejectedRequest):
            raise exc
        headers = {"Retry-After": "1"} if exc.retry_after else None
        return JSONResponse(
            exc.payload,
            status_code=exc.status_code,
            headers=headers,
        )

    middleware = [Middleware(_BearerMiddleware, guard=bearer)] if bearer is not None else []
    return Starlette(
        routes=[
            Route("/embed", embed, methods=["POST"]),
            Route("/embed/query", embed_single, methods=["POST"]),
            Route("/embed/single", embed_single, methods=["POST"]),
            Route("/rerank", rerank, methods=["POST"]),
            Route("/", info, methods=["GET"]),
            Route("/healthz", healthz, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
        ],
        exception_handlers={_RejectedRequest: rejected_request},
        middleware=middleware,
    )
