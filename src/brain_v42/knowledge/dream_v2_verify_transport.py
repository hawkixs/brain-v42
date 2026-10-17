"""Fail-closed loopback Messages gate for the Dream-only SDK worker.

Durability belongs to the injected ledger: reserve() MUST return only after an
atomic hard-envelope check and fsync/commit; settle() MUST be unique per run.
Neither an SDK budget callback nor a tool hook can substitute for this gate.
The HTTP client is supplied by the bench's fixed-route public-egress adapter;
this module never creates a direct provider client or follows redirects.
"""

import asyncio
import json
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Any, Protocol

import httpx
from aiohttp import web

from brain_v42.knowledge.dream_v2_prices import (
    MAX_OUTPUT_TOKENS,
    MODEL,
    Usage,
    message_bound,
    real_cost_usd,
)

SDK_VERSION = "0.2.168"
RUNTIME_VERSION = "2.1.301"


class GateRefused(RuntimeError):
    """A sanitized stop reason; never includes upstream bodies or headers."""


@dataclass(frozen=True)
class Caps:
    run_usd: Decimal = Decimal("0.25")
    tokens: int = 300000
    turns: int = 30
    output: int = MAX_OUTPUT_TOKENS
    seconds: int = 600
    p3: Decimal = Decimal("1.50")
    night: Decimal = Decimal("2")
    d1: Decimal = Decimal("10")
    credit: Decimal = Decimal("180")
    month: Decimal = Decimal("60")

    def __post_init__(self) -> None:
        maximums: tuple[Decimal | int, ...] = (
            Decimal("0.25"),
            300000,
            30,
            MAX_OUTPUT_TOKENS,
            600,
            Decimal("1.50"),
            Decimal("2"),
            Decimal("10"),
            Decimal("180"),
            Decimal("60"),
        )
        for item, maximum in zip(fields(self), maximums, strict=True):
            value = getattr(self, item.name)
            if isinstance(maximum, int) and type(value) is not int:
                raise ValueError("invalid_cap")
            try:
                parsed = Decimal(str(value))
                if not parsed.is_finite() or not 0 < parsed <= maximum:
                    raise ValueError("invalid_cap")
            except (ArithmeticError, TypeError):
                raise ValueError("invalid_cap") from None


def wet(policy: Mapping[str, str]) -> bool:
    return (
        policy.get("BRAIN_DREAM_V2_VERIFY_ENABLED") == "true"
        and policy.get("BRAIN_DREAM_V2_VERIFY_DRY_RUN") == "false"
    )


def _billing_modes(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _billing_modes(item)
    elif isinstance(value, dict):
        cache = value.get("cache_control")
        if cache is not None and (
            not isinstance(cache, dict)
            or cache.get("type") != "ephemeral"
            or cache.get("ttl", "5m") != "5m"
            or set(cache) - {"type", "ttl"}
        ):
            raise ValueError("unsupported_cache_mode")
        for item in value.values():
            _billing_modes(item)


def usage_from_response(body: bytes, stream: bool) -> Usage:
    """Merge cumulative SSE usage by field, never add repeated event totals."""
    usage: dict[str, Any] = {}
    if stream:
        started = stopped = False
        for block in body.decode().replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(
                line[5:].strip() for line in block.splitlines() if line.startswith("data:")
            )
            if not data:
                continue
            event = json.loads(data)
            kind = event.get("type")
            if kind == "error":
                raise ValueError("stream_error")
            if kind == "message_start":
                if started:
                    raise ValueError("duplicate_message")
                started = True
                usage.update(event["message"]["usage"])
            elif kind == "message_delta":
                usage.update(event.get("usage", {}))
            elif kind == "message_stop":
                stopped = True
        if not started or not stopped:
            raise ValueError("interrupted_stream")
    else:
        usage = json.loads(body)["usage"]
    # Missing fields are unknown, never silently assumed to be free.
    result = Usage(
        usage["input_tokens"],
        usage["cache_creation_input_tokens"],
        usage["cache_read_input_tokens"],
        usage["output_tokens"],
    )
    if usage.get("server_tool_use") or usage.get("service_tier", "standard") != "standard":
        raise ValueError("unsupported_usage")
    creation = usage.get("cache_creation")
    if creation and creation.get("ephemeral_1h_input_tokens", 0):
        raise ValueError("unsupported_usage")
    return result


Reserve = Callable[[str, Decimal], Awaitable[None]]
Settle = Callable[[Decimal], Awaitable[None]]
Totals = Callable[[], Awaitable[Mapping[str, Decimal]]]


class PRReaders(Protocol):
    """P2's fixed-repository, public-only read path; never an arbitrary URL."""

    repository: str

    async def view(self, number: int) -> Mapping[str, Any]: ...

    async def search(self, query: str) -> list[Mapping[str, Any]]: ...


class Gate:
    """One gate per run; serialized messages retain reservations until settlement."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        open_attempt: Callable[[Decimal], Awaitable[None]],
        reserve: Reserve,
        settle: Settle,
        totals: Totals,
        policy_reader: Callable[[], Mapping[str, str]],
        night_policy: Mapping[str, str],
        caps: Caps | None = None,
    ) -> None:
        self.client, self.reserve, self.settle, self.totals = client, reserve, settle, totals
        self.open_attempt = open_attempt
        self.policy_reader, self.caps = policy_reader, caps or Caps()
        self._night_wet = wet(night_policy)
        self.stop_reason: str | None = None
        self.outstanding = Decimal(0)
        self.cost = Decimal(0)
        self.tokens = self.messages = 0
        self.largest_message = Decimal(0)
        self.records: list[dict[str, Any]] = []
        self._started = self._finished = self._unknown = False
        self._lock = asyncio.Lock()
        self._deadline = 0.0
        self._runner: web.AppRunner | None = None
        self.endpoint = ""
        self.secret = secrets.token_urlsafe(32)
        self.requests_received = 0
        self.source_readers: PRReaders | None = None

    def _refuse(self, reason: str) -> None:
        self.stop_reason = self.stop_reason or reason
        raise GateRefused(self.stop_reason)

    async def _room(self, amount: Decimal) -> None:
        totals = await self.totals()
        if totals.get("unresolved") != Decimal(0):
            self._refuse("accounting_unknown")
        for scope in ("p3", "night", "d1", "credit", "month"):
            value = totals[scope]
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                self._refuse("accounting_unknown")
            if value + amount > getattr(self.caps, scope):
                self._refuse("hard_" + scope + "_cap")

    async def start(self) -> None:
        if (
            self._started
            or self.stop_reason
            or not self._night_wet
            or not wet(self.policy_reader())
        ):
            self._refuse("disarmed")
        try:
            await self._room(self.caps.run_usd)
            # Opens a zero-valued attempt after atomically admitting the soft
            # cap. Only per-message extensions reserve money for this run.
            await self.open_attempt(self.caps.run_usd)
        except GateRefused:
            raise
        except Exception:
            self._refuse("accounting_unknown")
        self._started = True
        self._deadline = time.monotonic() + self.caps.seconds

    async def forward_reserved_message(self, payload: dict[str, Any]) -> httpx.Response:
        async with self._lock:
            if self.stop_reason or not self._started or self._finished:
                self._refuse("gate_closed")
            if not wet(self.policy_reader()):
                self._refuse("disarmed")
            if time.monotonic() >= self._deadline:
                self._refuse("timeout")
            if self.messages >= self.caps.turns:
                self._refuse("turn_cap")
            try:
                body = json.loads(json.dumps(payload))
                if set(body) - {
                    "model",
                    "messages",
                    "system",
                    "tools",
                    "tool_choice",
                    "thinking",
                    "max_tokens",
                    "stream",
                    "metadata",
                    "stop_sequences",
                    "temperature",
                    "top_k",
                    "top_p",
                    "output_config",
                    "cache_control",
                    "context_management",
                    "service_tier",
                }:
                    raise ValueError("unsupported_message_parameter")
                if body["model"] != MODEL:
                    raise ValueError("unknown_model_price")
                if body.get("service_tier", "auto") not in {"standard_only", "auto"}:
                    raise ValueError("unsupported_billing_mode")
                body["service_tier"] = "standard_only"
                if body.get("speed", "standard") != "standard" or body.get("container"):
                    raise ValueError("unsupported_billing_mode")
                _billing_modes(body)
                for tool in body.get("tools", []):
                    if tool.get("type", "custom") != "custom":
                        raise ValueError("unsupported_server_tool")
                output = body["max_tokens"]
                if type(output) is not int or output <= 0:
                    raise ValueError("invalid_output_limit")
                body["max_tokens"] = min(output, self.caps.output)
                count_body = {
                    k: body[k]
                    for k in (
                        "model",
                        "messages",
                        "system",
                        "tools",
                        "tool_choice",
                        "thinking",
                        "output_config",
                        "cache_control",
                    )
                    if k in body
                }
                count_response = await self.client.post(
                    "/v1/messages/count_tokens", json=count_body, follow_redirects=False
                )
                count_response.raise_for_status()
                bound = message_bound(
                    MODEL, count_response.json()["input_tokens"], body["max_tokens"]
                )
                # The ledger's totals include earlier extensions of THIS run too.
                await self._room(bound)
                if not wet(self.policy_reader()):
                    self._refuse("disarmed")
                message_id = secrets.token_hex(16)
                self._unknown = True
                await self.reserve(message_id, bound)
                self._unknown = False
            except GateRefused:
                raise
            except Exception:
                self._refuse("reservation_failed")
            self.outstanding += bound
            record: dict[str, Any] = {"message_id": message_id, "reserved_usd": str(bound)}
            self.records.append(record)
            self.messages += 1
            try:
                async with asyncio.timeout(max(0.001, self._deadline - time.monotonic())):
                    response = await self.client.post(
                        "/v1/messages", json=body, follow_redirects=False
                    )
                if response.status_code != 200:
                    # Let the real runtime exercise its retry path, but keep
                    # this attempt outstanding and deny every subsequent send.
                    self._unknown = True
                    self.stop_reason = "usage_unknown"
                    return response
                usage = usage_from_response(response.content, bool(body.get("stream")))
                cost = real_cost_usd(MODEL, usage)
            except BaseException as exc:
                self._unknown = True
                if isinstance(exc, asyncio.CancelledError):
                    self.stop_reason = "usage_unknown"
                    raise
                self._refuse("usage_unknown")
            self.cost += cost
            self.tokens += usage.total_tokens
            self.largest_message = max(self.largest_message, cost)
            record.update(
                {
                    "usage": {f.name: getattr(usage, f.name) for f in fields(usage)},
                    "actual_usd": str(cost),
                }
            )
            if cost > bound:
                self.stop_reason = "reservation_exceeded"
            elif self.cost >= self.caps.run_usd:
                self.stop_reason = "usd_cap"
            elif self.tokens >= self.caps.tokens:
                self.stop_reason = "token_cap"
            elif self.messages >= self.caps.turns:
                self.stop_reason = "turn_cap"
            return response

    async def finish(self) -> None:
        if not self._finished:
            self._finished = True
            if self._started and not self._unknown:
                try:
                    await self.settle(self.cost)
                except Exception:
                    self._unknown = True
                    self.stop_reason = "settlement_failed"

    async def _handle(self, request: web.Request) -> web.Response:
        self.requests_received += 1
        if request.headers.get("x-api-key") != self.secret:
            return web.json_response(
                {"error": {"type": "permission_error", "message": "gate authentication required"}},
                status=403,
            )
        try:
            response = await self.forward_reserved_message(await request.json())
            return web.Response(
                body=response.content,
                status=response.status_code,
                headers={"retry-after": "0"} if response.status_code == 529 else None,
                content_type="text/event-stream"
                if "text/event-stream" in response.headers.get("content-type", "")
                else "application/json",
            )
        except Exception:
            return web.json_response(
                {
                    "type": "error",
                    "error": {"type": "permission_error", "message": "reservation gate refused"},
                },
                status=403,
            )

    async def _pr(self, request: web.Request) -> web.Response:
        if request.headers.get("x-api-key") != self.secret:
            return web.json_response({"error": "refused"}, status=403)
        try:
            if self.source_readers is None or self.source_readers.repository != "hawkixs/brain-v42":
                raise ValueError("source_unavailable")
            params = await request.json()
            if (
                request.path == "/p3/pr/view"
                and set(params) == {"number"}
                and type(params["number"]) is int
                and params["number"] > 0
            ):
                data: Any = await self.source_readers.view(params["number"])
            elif (
                request.path == "/p3/pr/search"
                and set(params) == {"query"}
                and isinstance(params["query"], str)
                and len(params["query"]) <= 1000
                and not any(
                    qualifier in params["query"].lower() for qualifier in ("repo:", "org:", "user:")
                )
            ):
                data = await self.source_readers.search(params["query"])
            else:
                raise ValueError("invalid_source_query")
            return web.json_response(data)
        except Exception:
            return web.json_response({"error": "source_unavailable"}, status=403)

    async def __aenter__(self) -> "Gate":
        await self.start()
        app = web.Application(client_max_size=32 * 1024 * 1024)
        app.router.add_post("/v1/messages", self._handle)
        app.router.add_post("/p3/pr/view", self._pr)
        app.router.add_post("/p3/pr/search", self._pr)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        try:
            await site.start()
            addresses = self._runner.addresses
            self.endpoint = f"http://127.0.0.1:{addresses[0][1]}"
        except BaseException:
            await self._runner.cleanup()
            raise
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._runner:
            await self._runner.cleanup()
        await self.finish()
