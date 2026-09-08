"""Bounded GitHub HTTP admission, origin and error contracts."""

from __future__ import annotations

import asyncio
import gzip
import traceback

import httpx
import pytest
from pydantic import ValidationError

from brain_v42.delivery_config import DeliverySettings
from brain_v42.models.delivery import DeliveryError


def _transport(http, **kwargs):
    from brain_v42.delivery_observer.transport import GitHubTransport

    settings = kwargs.pop("settings", DeliverySettings())
    return GitHubTransport(http, settings, **kwargs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_budget_per_minute", 41),
        ("max_concurrent_requests", 3),
        ("request_timeout_seconds", 11),
        ("freshness_seconds", 86401),
        ("github_api_origin", "http://api.github.com"),
        ("github_api_origin", "https://user:password@api.github.com"),
        ("github_api_origin", "https://api.github.com/unexpected-path"),
    ],
)
def test_configuration_cannot_relax_hard_limits(field, value):
    with pytest.raises(ValidationError):
        DeliverySettings(**{field: value})


async def test_explicit_headers_and_auth_override_borrowed_http_defaults():
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), auth=("unrelated", "credential")
    ) as http:
        client = _transport(http)
        assert await client.request_json(
            "GET", "/repos/owner/repo", headers={"Authorization": "Bearer fixture"}
        ) == {"ok": True}
    assert len(seen) == 1
    assert seen[0].headers["Authorization"] == "Bearer fixture"
    assert seen[0].headers["Accept"] == "application/vnd.github+json"
    assert seen[0].headers["X-GitHub-Api-Version"] == "2022-11-28"


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "provider_forbidden"),
        (403, "provider_forbidden"),
        (404, "provider_not_found"),
        (429, "provider_rate_limited"),
        (500, "provider_unavailable"),
        (503, "provider_unavailable"),
    ],
)
async def test_http_errors_are_safe_typed_and_schedulable(status, code):
    marker = "private-response-body-marker"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, text=marker))
    ) as http:
        client = _transport(http)
        with pytest.raises(DeliveryError) as caught:
            await client.request_json("GET", "/repos/o/r")
    assert caught.value.code == code
    assert caught.value.status_code == status
    assert marker not in "".join(traceback.format_exception(caught.value))
    assert marker not in repr(caught.value)
    assert 0 < caught.value.retry_after_seconds <= 3600


@pytest.mark.parametrize(
    "headers,delay",
    [
        ({"Retry-After": "120"}, 120),
        ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1060"}, 60),
    ],
)
async def test_rate_limit_honors_retry_after_and_reset(headers, delay):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(403, headers=headers))
    ) as http:
        client = _transport(http, wall_time=lambda: 1000)
        with pytest.raises(DeliveryError) as caught:
            await client.request_json("GET", "/repos/o/r")
    assert caught.value.code == "provider_rate_limited"
    assert caught.value.retry_after_seconds == delay


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/path",
        "http://api.github.com/path",
        "https://api.github.com:444/path",
        "https://user:password@api.github.com/path",
        "//evil.example/path",
    ],
)
async def test_unsafe_absolute_urls_are_refused_before_sending(url):
    seen = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: seen.append(req))
    ) as http:
        client = _transport(http)
        with pytest.raises(DeliveryError, match="provider_invalid_response"):
            await client.request_json("GET", url)
    assert seen == []


async def test_redirect_never_forwards_bearer_to_other_origin():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/private"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        client = _transport(http)
        with pytest.raises(DeliveryError, match="provider_invalid_response"):
            await client.request_json("GET", "/start", headers={"Authorization": "Bearer fixture"})
    assert len(seen) == 1
    assert seen[0].url.host == "api.github.com"


async def test_same_origin_redirects_are_bounded():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "/again"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DeliveryError, match="provider_invalid_response"):
            await _transport(http).request_json("GET", "/start")
    assert len(seen) == 3


@pytest.mark.parametrize(
    "body", [b"not-json", b"null", b'{"value":NaN}', b'"' + b"x" * 2048 + b'"']
)
async def test_malformed_or_oversized_json_is_refused(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    ) as http:
        client = _transport(http, settings=DeliverySettings(max_response_bytes=1024))
        with pytest.raises(DeliveryError, match="provider_invalid_response"):
            await client.request_json("GET", "/payload")


async def test_total_http_timeout_includes_all_stream_chunks_and_releases_admission():
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in (b'{"value":', b"1}"):
                await asyncio.sleep(0.6)
                yield chunk

    def handler(request):
        return (
            httpx.Response(200, stream=SlowBody())
            if request.url.path == "/slow"
            else httpx.Response(200, json={"ok": True})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = _transport(
            http, settings=DeliverySettings(request_timeout_seconds=1, max_concurrent_requests=1)
        )
        with pytest.raises(DeliveryError, match="provider_timeout"):
            await client.request_json("GET", "/slow")
        assert await asyncio.wait_for(client.request_json("GET", "/fast"), 1) == {"ok": True}


async def test_network_error_does_not_expose_original_exception():
    marker = "private-network-error-marker"

    def handler(request):
        raise httpx.ConnectError(marker, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DeliveryError, match="provider_unavailable") as caught:
            await _transport(http).request_json("GET", "/error")
    assert marker not in "".join(traceback.format_exception(caught.value))


async def test_admission_allows_at_most_two_concurrent_http_calls():
    active = maximum = entered = 0
    two = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        nonlocal active, maximum, entered
        active += 1
        entered += 1
        maximum = max(maximum, active)
        if entered == 2:
            two.set()
        await release.wait()
        active -= 1
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = _transport(http)
        tasks = [asyncio.create_task(client.request_json("GET", "/data")) for _ in range(5)]
        try:
            await asyncio.wait_for(two.wait(), 1)
            await asyncio.sleep(0)
            assert entered == 2
            release.set()
            await asyncio.gather(*tasks)
            assert maximum == 2
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_shared_budget_progresses_across_refill_without_http_timeout():
    instant = 0.0
    requests = []
    sleeps = []

    async def sleep(delay):
        nonlocal instant
        sleeps.append(delay)
        instant += delay
        await asyncio.sleep(0)

    def handler(request):
        requests.append(instant)
        return httpx.Response(200, json={"ordinal": len(requests)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = _transport(http, monotonic=lambda: instant, sleep=sleep)
        for ordinal in range(42):
            assert await client.request_json("GET", "/data") == {"ordinal": ordinal + 1}
    assert requests[:40] == [0.0] * 40
    assert requests[40:] == [60.0, 60.0]
    assert sleeps == [60.0]


async def test_page_exposes_validated_same_origin_next_link():
    link = '<https://api.github.com/items?page=2>; rel="next"'
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=[{"id": 1}], headers={"Link": link})
        )
    ) as http:
        page = await _transport(http).request_page("GET", "/items")
    assert page.data == [{"id": 1}]
    assert page.next_url == "https://api.github.com/items?page=2"


async def test_unsafe_pagination_link_fails_closed():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json=[], headers={"Link": '<https://evil.example/private>; rel="next"'}
            )
        )
    ) as http:
        with pytest.raises(DeliveryError, match="provider_invalid_response"):
            await _transport(http).request_page("GET", "/items")


async def test_cancelling_an_admitted_request_frees_the_http_slot():
    entered = asyncio.Event()
    never = asyncio.Event()

    async def handler(request):
        if request.url.path == "/wait":
            entered.set()
            await never.wait()
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = _transport(http, settings=DeliverySettings(max_concurrent_requests=1))
        task = asyncio.create_task(client.request_json("GET", "/wait"))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.wait_for(client.request_json("GET", "/next"), 1) == {"ok": True}


async def test_missing_explicit_auth_does_not_borrow_client_authorization_header():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer unrelated-default-marker"},
    ) as http:
        await _transport(http).request_json("GET", "/data")
    assert "Authorization" not in seen[0].headers


@pytest.mark.parametrize("encoding", ["compressed", "large-content-length"])
async def test_response_encoding_and_length_cannot_bypass_body_bound(encoding):
    def handler(_request):
        if encoding == "compressed":
            return httpx.Response(
                200,
                stream=httpx.ByteStream(gzip.compress(b'{"ok":true}')),
                headers={"Content-Encoding": "gzip"},
            )
        return httpx.Response(200, content=b"{}", headers={"Content-Length": "99999999"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DeliveryError, match="provider_invalid_response"):
            await _transport(http).request_json("GET", "/data")
