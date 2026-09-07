"""Shared, bounded GitHub HTTP transport with safe provider failures."""

from __future__ import annotations

import asyncio
import json as json_module
import math
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, NoReturn
from urllib.parse import urljoin, urlsplit

import httpx

from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import DeliveryError

JsonData = dict[str, Any] | list[Any]


class ProviderError(DeliveryError):
    """Only a stable code, HTTP status and bounded retry delay cross this boundary."""

    def __init__(
        self, code: str, *, status_code: int | None = None, retry_after_seconds: float = 30
    ) -> None:
        super().__init__(code, "GitHub observation could not be confirmed")
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class GitHubPage:
    data: JsonData
    next_url: str | None


class _Admission:
    def __init__(
        self,
        settings: DeliverySettings,
        monotonic: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._limit = min(settings.request_budget_per_minute, 40)
        self._concurrency = min(settings.max_concurrent_requests, 2)
        self._monotonic, self._sleep = monotonic, sleep
        self._starts: deque[float] = deque()
        self._active = 0
        self._condition = asyncio.Condition()

    async def acquire(self) -> None:
        while True:
            async with self._condition:
                now = self._monotonic()
                while self._starts and now - self._starts[0] >= 60:
                    self._starts.popleft()
                if self._active >= self._concurrency:
                    await self._condition.wait()
                    continue
                if len(self._starts) < self._limit:
                    self._starts.append(now)
                    self._active += 1
                    return
                delay = max(0.001, 60 - (now - self._starts[0]))
            # A local budget wait consumes neither an HTTP slot nor its timeout.
            await self._sleep(delay)

    async def release(self) -> None:
        async with self._condition:
            self._active -= 1
            self._condition.notify_all()


def _reject_constant(_value: str) -> NoReturn:
    raise ValueError("non-finite JSON number")


class GitHubTransport:
    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: DeliverySettings,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        self.http = http
        # Revalidate bypassed DTOs at the outbound boundary, with redacted inputs.
        self.settings = DeliverySettings.model_validate(settings.model_dump())
        self._origin = self.settings.github_api_origin
        self._origin_parts = urlsplit(self._origin)
        self._monotonic, self._wall_time = monotonic, wall_time
        self._admission = _Admission(self.settings, monotonic, sleep)

    def validate_url(self, value: str) -> str:
        try:
            if not isinstance(value, str) or value != value.strip() or "\\" in value:
                raise ValueError
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise ValueError
            if value.startswith("//"):
                raise ValueError
            url = urljoin(self._origin + "/", value)
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or parts.username is not None
                or parts.password is not None
                or parts.hostname != self._origin_parts.hostname
                or (parts.port or 443) != (self._origin_parts.port or 443)
                or parts.fragment
            ):
                raise ValueError
            return url
        except (ValueError, TypeError):
            raise ProviderError("provider_invalid_response") from None

    def _retry_delay(self, response: httpx.Response, failure_count: int) -> float:
        default = min(300.0, 5.0 * 2.0 ** max(0, min(failure_count, 6)))
        raw = response.headers.get("Retry-After")
        try:
            if raw is not None:
                try:
                    delay = float(raw)
                except ValueError:
                    delay = parsedate_to_datetime(raw).timestamp() - self._wall_time()
            elif response.headers.get("X-RateLimit-Remaining") == "0":
                delay = float(response.headers["X-RateLimit-Reset"]) - self._wall_time()
            else:
                delay = default
            return max(1.0, min(delay, 3600.0)) if math.isfinite(delay) else default
        except (ValueError, TypeError, KeyError, OverflowError):
            return default

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: dict[str, Any] | None = None,
        failure_count: int = 0,
    ) -> JsonData:
        return (
            await self.request_page(
                method, path, headers=headers, json=json, failure_count=failure_count
            )
        ).data

    @asynccontextmanager
    async def _stream(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> AsyncIterator[httpx.Response]:
        # A fresh Request avoids inheriting unrelated client headers or cookies.
        request = httpx.Request(
            method,
            url,
            headers=headers,
            json=payload,
            extensions={"timeout": httpx.Timeout(timeout).as_dict()},
        )
        response = await self.http.send(request, stream=True, auth=None, follow_redirects=False)
        try:
            yield response
        finally:
            await response.aclose()

    async def request_page(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: dict[str, Any] | None = None,
        failure_count: int = 0,
    ) -> GitHubPage:
        url = self.validate_url(path)
        request_headers = {
            **(headers or {}),
            "Accept": "application/vnd.github+json",
            "Accept-Encoding": "identity",
            "User-Agent": "brain-v42-delivery-observer",
            "X-GitHub-Api-Version": self.settings.github_api_version,
        }
        remaining = float(self.settings.request_timeout_seconds)
        for redirect_count in range(3):
            await self._admission.acquire()
            started = self._monotonic()
            redirect: str | None = None
            page: GitHubPage | None = None
            try:
                async with asyncio.timeout(remaining):
                    async with self._stream(
                        method, url, request_headers, json, remaining
                    ) as response:
                        status = response.status_code
                        if status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if not location or redirect_count == 2:
                                raise ProviderError("provider_invalid_response", status_code=status)
                            redirect = self.validate_url(urljoin(url, location))
                        elif not 200 <= status < 300:
                            limited = status == 429 or (
                                status == 403
                                and (
                                    response.headers.get("X-RateLimit-Remaining") == "0"
                                    or "Retry-After" in response.headers
                                )
                            )
                            code = (
                                "provider_rate_limited"
                                if limited
                                else "provider_forbidden"
                                if status in {401, 403}
                                else "provider_not_found"
                                if status == 404
                                else "provider_unavailable"
                                if status >= 500
                                else "provider_invalid_response"
                            )
                            raise ProviderError(
                                code,
                                status_code=status,
                                retry_after_seconds=self._retry_delay(response, failure_count),
                            )
                        else:
                            if (
                                response.headers.get("Content-Encoding", "identity").lower()
                                != "identity"
                            ):
                                raise ProviderError("provider_invalid_response")
                            length = response.headers.get("Content-Length")
                            if (
                                length is not None
                                and not 0 <= int(length) <= self.settings.max_response_bytes
                            ):
                                raise ProviderError("provider_invalid_response")
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                if len(body) + len(chunk) > self.settings.max_response_bytes:
                                    raise ProviderError("provider_invalid_response")
                                body.extend(chunk)
                            data = json_module.loads(body, parse_constant=_reject_constant)
                            if not isinstance(data, (dict, list)):
                                raise ProviderError("provider_invalid_response")
                            next_link = response.links.get("next", {}).get("url")
                            next_url = (
                                self.validate_url(urljoin(url, next_link)) if next_link else None
                            )
                            page = GitHubPage(data, next_url)
            except ProviderError:
                raise
            except (TimeoutError, httpx.TimeoutException):
                raise ProviderError("provider_timeout") from None
            except httpx.HTTPError:
                raise ProviderError("provider_unavailable") from None
            except (ValueError, UnicodeError, RecursionError):
                raise ProviderError("provider_invalid_response") from None
            finally:
                remaining -= max(0.0, self._monotonic() - started)
                await self._admission.release()
            if remaining <= 0:
                raise ProviderError("provider_timeout")
            if redirect is not None:
                url = redirect
                continue
            if page is None:
                raise ProviderError("provider_invalid_response")
            return page
        raise ProviderError("provider_invalid_response")
