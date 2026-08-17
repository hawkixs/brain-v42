"""Tests du shim embedding (services/embedding_shim/) — backends + app.

Le shim n'est pas un package installé : on l'importe via sys.path.
Contrat de référence : services/embedding/main.py v2.0.0 (PyTorch legacy).
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import httpx
import pytest

SHIM_DIR = Path(__file__).resolve().parents[2] / "services" / "embedding_shim"
sys.path.insert(0, str(SHIM_DIR))

import shim_app as shim_app_module  # noqa: E402
from shim_app import create_app  # noqa: E402
from shim_backends import (  # noqa: E402
    MAX_TEXT_CHARS,
    RETRY_TEXT_CHARS,
    LlamaEmbedBackend,
    OnnxRerankBackend,  # noqa: E402
    UpstreamError,
)


def _openai_payload(vecs_by_index: dict[int, list[float]]) -> dict:
    """Réponse /v1/embeddings au format OpenAI (index volontairement mélangés)."""
    return {
        "data": [
            {"object": "embedding", "index": i, "embedding": v} for i, v in vecs_by_index.items()
        ]
    }


def _make_backend(handler) -> LlamaEmbedBackend:
    return LlamaEmbedBackend("http://llama-test", transport=httpx.MockTransport(handler))


async def test_embed_sorts_by_index_and_normalizes():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/v1/embeddings"
        assert body["input"] == ["aaa", "bbb"]
        # Réponse dans le désordre + vecteurs non normalisés
        return httpx.Response(200, json=_openai_payload({1: [0.0, 2.0], 0: [3.0, 4.0]}))

    vecs = await _make_backend(handler).embed(["aaa", "bbb"])
    assert len(vecs) == 2
    # index 0 en premier, L2-normalisé : [3,4] → [0.6, 0.8]
    assert vecs[0] == pytest.approx([0.6, 0.8])
    assert vecs[1] == pytest.approx([0.0, 1.0])
    for v in vecs:
        assert math.isclose(sum(x * x for x in v), 1.0, rel_tol=1e-6)


async def test_embed_empty_returns_empty_without_call():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("aucun appel HTTP attendu pour texts=[]")

    assert await _make_backend(handler).embed([]) == []


async def test_embed_truncates_oversized_text():
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["input"])
        return httpx.Response(200, json=_openai_payload({0: [1.0, 0.0]}))

    await _make_backend(handler).embed(["x" * (MAX_TEXT_CHARS + 5000)])
    assert len(seen[0][0]) == MAX_TEXT_CHARS


async def test_embed_retries_shorter_on_upstream_500():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(len(body["input"][0]))
        if len(calls) == 1:
            # ex. "input is larger than the max context size"
            return httpx.Response(500, json={"error": "context overflow"})
        return httpx.Response(200, json=_openai_payload({0: [1.0, 0.0]}))

    vecs = await _make_backend(handler).embed(["y" * MAX_TEXT_CHARS])
    assert vecs == [[1.0, 0.0]]
    assert calls == [MAX_TEXT_CHARS, RETRY_TEXT_CHARS]


async def test_embed_raises_upstream_error_if_retry_also_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(UpstreamError):
        await _make_backend(handler).embed(["zz"])


async def test_healthy_true_false():
    def ok(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok"})

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert await _make_backend(ok).healthy() is True
    assert await _make_backend(down).healthy() is False


def test_rerank_empty_candidates_short_circuits(tmp_path):
    # Chemins volontairement inexistants : si le lazy-load se déclenche
    # sur candidates=[], le test explose — c'est le comportement testé.
    backend = OnnxRerankBackend(str(tmp_path / "nope.onnx"), str(tmp_path / "nope.json"))
    assert backend.rerank("query", []) == []


def test_rerank_builds_pairs_and_returns_raw_logits():
    np = pytest.importorskip("numpy")

    class FakeInput:
        def __init__(self, name: str) -> None:
            self.name = name

    class FakeSession:
        def get_inputs(self):
            # export Xenova : token_type_ids présent
            return [
                FakeInput("input_ids"),
                FakeInput("attention_mask"),
                FakeInput("token_type_ids"),
            ]

        def run(self, _out, feeds):
            assert set(feeds) == {
                "input_ids",
                "attention_mask",
                "token_type_ids",
            }
            n = feeds["input_ids"].shape[0]
            return [np.array([[float(-i)] for i in range(n)])]

    class FakeEncoding:
        ids = [101, 7, 102, 9, 102]
        attention_mask = [1, 1, 1, 1, 1]
        type_ids = [0, 0, 0, 1, 1]

    class FakeTokenizer:
        def __init__(self) -> None:
            self.batches: list[list[tuple[str, str]]] = []

        def encode_batch(self, pairs):
            self.batches.append(pairs)
            return [FakeEncoding() for _ in pairs]

    backend = OnnxRerankBackend("unused.onnx", "unused.json")
    tok = FakeTokenizer()
    backend._session = FakeSession()
    backend._tokenizer = tok

    scores = backend.rerank("q", ["cand a", "cand b"])
    assert scores == [0.0, -1.0]
    assert tok.batches == [[("q", "cand a"), ("q", "cand b")]]


class FakeEmbedBackend:
    def __init__(self, healthy: bool = True) -> None:
        self._healthy = healthy
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]

    async def healthy(self) -> bool:
        return self._healthy


class FakeRerankBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        self.calls.append((query, candidates))
        return [float(-i) for i in range(len(candidates))]


@asynccontextmanager
async def _client(
    healthy: bool = True,
    *,
    embed_backend: Any | None = None,
    rerank_backend: Any | None = None,
    limits: Any | None = None,
) -> AsyncIterator[tuple[httpx.AsyncClient, Any, Any]]:
    embed = embed_backend or FakeEmbedBackend(healthy=healthy)
    rerank = rerank_backend or FakeRerankBackend()
    kwargs = {} if limits is None else {"limits": limits}
    app = create_app(embed, rerank, **kwargs)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://shim") as client:
        yield client, embed, rerank


def _limits(**overrides: object) -> Any:
    limits_cls = getattr(shim_app_module, "ShimLimits", None)
    assert limits_cls is not None, "shim_app.ShimLimits must expose the request-limit contract"
    missing = set(overrides) - set(limits_cls.__dataclass_fields__)
    assert not missing, f"shim_app.ShimLimits must expose limits: {sorted(missing)}"
    return limits_cls(**overrides)


def _embed_body(total_bytes: int, marker: bytes = b"") -> bytes:
    prefix = b'{"texts":["'
    suffix = b'"]}'
    filler_size = total_bytes - len(prefix) - len(marker) - len(suffix)
    assert filler_size >= 0
    return prefix + (b"x" * filler_size) + marker + suffix


def _route_payload(route: str, depth: int, marker: str) -> bytes:
    assert depth >= 2
    if route == "/embed":
        prefix = '{"texts":["depth-ok"],"padding":'
    elif route in {"/embed/query", "/embed/single"}:
        prefix = '{"text":"depth-ok","padding":'
    else:
        assert route == "/rerank"
        prefix = '{"query":"depth-ok","candidates":["candidate"],"padding":'
    nested_value = "[" * (depth - 1) + json.dumps(marker) + "]" * (depth - 1)
    return (prefix + nested_value + "}").encode()


def _escaped_quote_payload(slash_count: int) -> tuple[bytes, str]:
    assert 1 <= slash_count <= 4
    raw_value = '"' + "\\" * slash_count + '"'
    expected_value = "\\" * (slash_count // 2)
    if slash_count % 2:
        raw_value += "[{" + '"'
        expected_value += '"[{'
    body = (
        '{"texts":[' + raw_value + '],"literal_delimiters":"[[[[{{{{","unicode_escape":"\\u005B"}'
    )
    return body.encode(), expected_value


async def _stream_body(body: bytes) -> AsyncIterator[bytes]:
    midpoint = len(body) // 2
    yield body[:midpoint]
    await asyncio.sleep(0)
    yield body[midpoint:]


def _assert_busy(response: httpx.Response, error: str) -> None:
    assert response.status_code == 503
    assert response.json() == {"error": error}
    assert response.headers["retry-after"] == "1"


async def _wait_for_thread_event(event: threading.Event, timeout: float = 1.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while not event.is_set():
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.001)
    return True


async def _post_after_busy_releases(
    client: httpx.AsyncClient,
    route: str,
    payload: dict[str, Any],
    busy_error: str,
    timeout: float = 1.0,
) -> httpx.Response:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        response = await client.post(route, json=payload)
        if response.status_code != 503:
            return response
        _assert_busy(response, busy_error)
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail(f"{busy_error} lease was not released within {timeout}s")
        await asyncio.sleep(0.001)


async def test_app_embed_batch():
    async with _client() as (client, backend, _):
        resp = await client.post("/embed", json={"texts": ["a", "b"]})
    assert resp.status_code == 200
    assert resp.json() == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]
    assert backend.calls == [["a", "b"]]


async def test_app_embed_empty_list():
    async with _client() as (client, backend, _):
        resp = await client.post("/embed", json={"texts": []})
    assert resp.status_code == 200
    assert resp.json() == []
    assert backend.calls == []


async def test_app_embed_missing_texts_is_400():
    async with _client() as (client, _, _):
        response = await client.post("/embed", json={})
    assert response.status_code == 400


async def test_app_embed_query_json_body():
    async with _client() as (client, backend, _):
        resp = await client.post("/embed/query", json={"text": "hello"})
    assert resp.status_code == 200
    assert resp.json() == [0.1, 0.2, 0.3]
    assert backend.calls == [["hello"]]


async def test_app_embed_query_legacy_query_param():
    async with _client() as (client, backend, _):
        resp = await client.post("/embed/query", params={"text": "legacy"})
    assert resp.status_code == 200
    assert backend.calls == [["legacy"]]


async def test_app_embed_query_body_wins_over_param():
    async with _client() as (client, backend, _):
        await client.post("/embed/query", params={"text": "param"}, json={"text": "body"})
    assert backend.calls == [["body"]]


async def test_app_embed_query_missing_text_is_400():
    async with _client() as (client, _, _):
        response = await client.post("/embed/query")
    assert response.status_code == 400


async def test_app_embed_single_same_contract():
    async with _client() as (client, backend, _):
        resp = await client.post("/embed/single", json={"text": "doc"})
    assert resp.status_code == 200
    assert resp.json() == [0.1, 0.2, 0.3]
    assert backend.calls == [["doc"]]


async def test_app_rerank():
    async with _client() as (client, _, backend):
        resp = await client.post("/rerank", json={"query": "q", "candidates": ["a", "b", "c"]})
    assert resp.status_code == 200
    assert resp.json() == {"scores": [0.0, -1.0, -2.0]}
    assert backend.calls == [("q", ["a", "b", "c"])]


async def test_app_rerank_empty_candidates():
    async with _client() as (client, _, backend):
        resp = await client.post("/rerank", json={"query": "q", "candidates": []})
    assert resp.status_code == 200
    assert resp.json() == {"scores": []}
    assert backend.calls == [("q", [])]


async def test_app_rerank_bad_payload_is_400():
    async with _client() as (client, _, backend):
        response = await client.post("/rerank", json={"query": "q"})
    assert response.status_code == 400
    assert backend.calls == []


async def test_app_healthz_ok_when_upstream_healthy():
    async with _client(healthy=True) as (client, _, _):
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_app_healthz_503_when_upstream_down():
    async with _client(healthy=False) as (client, _, _):
        response = await client.get("/healthz")
    assert response.status_code == 503


async def test_app_health_legacy_reranker_compat():
    async with _client() as (client, _, _):
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_app_info():
    async with _client() as (client, _, _):
        body = (await client.get("/")).json()
    assert body["dims"] == 1536
    assert body["embed_model"] == "Qodo/Qodo-Embed-1-1.5B"
    assert "runtime" in body


def test_shim_limits_defaults_are_immutable():
    limits = _limits()
    assert limits.max_body_bytes == 8 * 1024 * 1024
    assert limits.max_ingress_requests == 8
    assert limits.body_read_timeout_seconds == 5.0
    assert limits.max_embed_batch == 100
    assert limits.max_rerank_batch == 128
    assert getattr(limits, "max_json_depth", None) == 64
    assert limits.max_embed_compute == 1
    assert limits.max_rerank_compute == 1
    with pytest.raises((FrozenInstanceError, AttributeError)):
        limits.max_body_bytes = 1


async def test_app_accepts_max_production_embed_batch_with_worst_case_utf8():
    text = "🧠" * MAX_TEXT_CHARS
    texts = [text] * 100
    serialized = httpx.Request("POST", "http://shim/embed", json={"texts": texts}).content
    assert len(serialized) <= 8 * 1024 * 1024

    async with _client() as (client, backend, _):
        response = await client.post(
            "/embed", content=serialized, headers={"content-type": "application/json"}
        )
    assert response.status_code == 200
    assert len(backend.calls) == 1
    assert len(backend.calls[0]) == 100


async def test_content_length_body_limit_accepts_n_and_rejects_n_plus_one(caplog):
    limit = 8 * 1024 * 1024
    marker = b"SEC2_CONTENT_LENGTH_SECRET"
    accepted = _embed_body(limit)
    oversized = _embed_body(limit + 1, marker)
    caplog.set_level(10)

    async with _client() as (client, backend, _):
        ok = await client.post(
            "/embed", content=accepted, headers={"content-type": "application/json"}
        )
        rejected = await client.post(
            "/embed", content=oversized, headers={"content-type": "application/json"}
        )

    assert ok.status_code == 200
    assert rejected.status_code == 413
    assert rejected.json() == {"detail": "Request body too large"}
    assert len(backend.calls) == 1
    assert marker.decode() not in rejected.text
    assert marker.decode() not in "\n".join(record.getMessage() for record in caplog.records)


async def test_streamed_body_limit_accepts_n_and_rejects_n_plus_one():
    limit = 64
    limits = _limits(max_body_bytes=limit)
    async with _client(limits=limits) as (client, backend, _):
        ok = await client.post(
            "/embed",
            content=_stream_body(_embed_body(limit)),
            headers={"content-type": "application/json"},
        )
        rejected = await client.post(
            "/embed",
            content=_stream_body(_embed_body(limit + 1)),
            headers={"content-type": "application/json"},
        )

    assert ok.status_code == 200
    assert rejected.status_code == 413
    assert rejected.json() == {"detail": "Request body too large"}
    assert len(backend.calls) == 1


async def test_slow_streamed_body_times_out_before_backend():
    async def slow_body() -> AsyncIterator[bytes]:
        yield b'{"texts":["'
        await asyncio.sleep(0.05)
        yield b'slow"]}'

    limits = _limits(body_read_timeout_seconds=0.01)
    async with _client(limits=limits) as (client, backend, _):
        response = await client.post(
            "/embed", content=slow_body(), headers={"content-type": "application/json"}
        )

    assert response.status_code == 408
    assert response.json() == {"detail": "Request body timeout"}
    assert backend.calls == []


@pytest.mark.parametrize("route", ["/embed", "/embed/query", "/embed/single", "/rerank"])
@pytest.mark.parametrize("body", [b"SEC2_INVALID_JSON_SECRET", b" \n\t"])
async def test_nonempty_invalid_json_is_exact_and_does_not_leak(route, body, caplog):
    marker = body.decode().strip()
    caplog.set_level(10)
    async with _client() as (client, embed_backend, rerank_backend):
        response = await client.post(
            route, content=body, headers={"content-type": "application/json"}
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid JSON body"}
    if marker:
        assert marker not in response.text
        assert marker not in "\n".join(record.getMessage() for record in caplog.records)
    assert embed_backend.calls == []
    assert rerank_backend.calls == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"9" * 5000, id="integer-digit-limit"),
    ],
)
async def test_json_decoder_resource_errors_are_exact_and_do_not_leak(body, caplog):
    caplog.set_level(10)
    async with _client() as (client, embed_backend, rerank_backend):
        response = await client.post(
            "/embed",
            content=body,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid JSON body"}
    assert body[:80].decode() not in response.text
    assert body[:80].decode() not in "\n".join(record.getMessage() for record in caplog.records)
    assert embed_backend.calls == []
    assert rerank_backend.calls == []


@pytest.mark.parametrize("route", ["/embed", "/embed/query", "/embed/single", "/rerank"])
async def test_json_depth_accepts_64_and_rejects_65_before_backend(route, caplog):
    rejected_marker = f"JSON_DEPTH_65_SECRET_{route}"
    caplog.set_level(10)

    async with _client() as (client, embed_backend, rerank_backend):
        accepted = await client.post(
            route,
            content=_route_payload(route, 64, "depth-64"),
            headers={"content-type": "application/json"},
        )
        rejected = await client.post(
            route,
            content=_route_payload(route, 65, rejected_marker),
            headers={"content-type": "application/json"},
        )

    assert accepted.status_code == 200
    assert rejected.status_code == 400
    assert rejected.json() == {"detail": "Invalid JSON body"}
    assert rejected_marker not in rejected.text
    assert rejected_marker not in "\n".join(record.getMessage() for record in caplog.records)
    expected_embed_calls = [] if route == "/rerank" else [["depth-ok"]]
    expected_rerank_calls = [("depth-ok", ["candidate"])] if route == "/rerank" else []
    assert embed_backend.calls == expected_embed_calls
    assert rerank_backend.calls == expected_rerank_calls


@pytest.mark.parametrize("route", ["/embed", "/embed/query", "/embed/single", "/rerank"])
async def test_json_depth_gate_precedes_module_local_json_loads(route, monkeypatch):
    real_loads = shim_app_module.json.loads
    loads_call_count = 0

    def counted_loads(value, *args, **kwargs):
        nonlocal loads_call_count
        loads_call_count += 1
        return real_loads(value, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shim_app_module.json, "loads", counted_loads)
        async with _client() as (client, embed_backend, rerank_backend):
            rejected = await client.post(
                route,
                content=_route_payload(route, 65, "too-deep"),
                headers={"content-type": "application/json"},
            )
            assert loads_call_count == 0
            accepted_body = _route_payload(route, 64, "allowed")
            accepted = await client.post(
                route,
                content=accepted_body,
                headers={"content-type": "application/json"},
            )
            assert loads_call_count == 1

    assert rejected.status_code == 400
    assert rejected.json() == {"detail": "Invalid JSON body"}
    assert accepted.status_code == 200
    expected_embed_calls = [] if route == "/rerank" else [["depth-ok"]]
    expected_rerank_calls = [("depth-ok", ["candidate"])] if route == "/rerank" else []
    assert embed_backend.calls == expected_embed_calls
    assert rerank_backend.calls == expected_rerank_calls


async def test_health_remains_responsive_while_json_depth_scan_is_blocked(monkeypatch):
    scan_entered = threading.Event()
    scan_release = threading.Event()
    watchdog_expired = threading.Event()

    def blocked_depth_scan(_text: str, _limit: int) -> bool:
        scan_entered.set()
        if not scan_release.wait(timeout=1.0):
            watchdog_expired.set()
        return False

    with monkeypatch.context() as patch:
        patch.setattr(shim_app_module, "_json_depth_exceeds_limit", blocked_depth_scan)
        async with _client() as (client, backend, _):
            pending_post = asyncio.create_task(client.post("/embed", json={"texts": ["blocked"]}))
            try:
                assert await _wait_for_thread_event(scan_entered)
                assert not watchdog_expired.is_set(), (
                    "JSON depth scan blocked the event loop until the test watchdog expired"
                )
                health = await asyncio.wait_for(client.get("/health"), timeout=0.1)
                assert health.status_code == 200
                assert health.json()["status"] == "ok"
                assert not pending_post.done()
            finally:
                scan_release.set()
                post = await asyncio.wait_for(pending_post, timeout=1.0)

    assert post.status_code == 200
    assert backend.calls == [["blocked"]]


async def test_json_decode_scan_and_load_run_off_the_event_loop(monkeypatch):
    event_loop_thread = threading.get_ident()
    call_threads: dict[str, list[int]] = {
        "detect_encoding": [],
        "depth_scan": [],
        "loads": [],
    }
    real_detect_encoding = shim_app_module.json.detect_encoding
    real_depth_scan = shim_app_module._json_depth_exceeds_limit
    real_loads = shim_app_module.json.loads

    def tracked_detect_encoding(raw):
        call_threads["detect_encoding"].append(threading.get_ident())
        return real_detect_encoding(raw)

    def tracked_depth_scan(text: str, limit: int) -> bool:
        call_threads["depth_scan"].append(threading.get_ident())
        return real_depth_scan(text, limit)

    def tracked_loads(value, *args, **kwargs):
        call_threads["loads"].append(threading.get_ident())
        return real_loads(value, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shim_app_module.json, "detect_encoding", tracked_detect_encoding)
        patch.setattr(shim_app_module, "_json_depth_exceeds_limit", tracked_depth_scan)
        patch.setattr(shim_app_module.json, "loads", tracked_loads)
        async with _client() as (client, backend, _):
            response = await client.post("/embed", json={"texts": ["threaded"]})

    assert response.status_code == 200
    assert backend.calls == [["threaded"]]
    assert all(len(thread_ids) == 1 for thread_ids in call_threads.values())
    assert all(
        thread_id != event_loop_thread
        for thread_ids in call_threads.values()
        for thread_id in thread_ids
    )


async def test_json_parsing_is_serialized_across_concurrent_ingress(monkeypatch):
    scan_entered = threading.Event()
    scan_release = threading.Event()
    counter_lock = threading.Lock()
    active_scans = 0
    max_active_scans = 0
    real_depth_scan = shim_app_module._json_depth_exceeds_limit

    def tracked_depth_scan(text: str, limit: int) -> bool:
        nonlocal active_scans, max_active_scans
        with counter_lock:
            active_scans += 1
            max_active_scans = max(max_active_scans, active_scans)
            scan_entered.set()
        try:
            scan_release.wait(timeout=0.2)
            return real_depth_scan(text, limit)
        finally:
            with counter_lock:
                active_scans -= 1

    with monkeypatch.context() as patch:
        patch.setattr(shim_app_module, "_json_depth_exceeds_limit", tracked_depth_scan)
        async with _client() as (client, backend, _):
            requests = [
                asyncio.create_task(client.post("/embed", json={"texts": []})) for _ in range(8)
            ]
            try:
                assert await _wait_for_thread_event(scan_entered)
                await asyncio.sleep(0.02)
                assert max_active_scans == 1
            finally:
                scan_release.set()
                responses = await asyncio.gather(*requests)

    assert all(response.status_code == 200 for response in responses)
    assert max_active_scans == 1
    assert backend.calls == []


async def test_queued_json_parse_holds_the_ingress_lease(monkeypatch):
    scan_entered = threading.Event()
    scan_release = threading.Event()
    scan_calls = 0

    def block_first_depth_scan(_text: str, _limit: int) -> bool:
        nonlocal scan_calls
        scan_calls += 1
        if scan_calls == 1:
            scan_entered.set()
            assert scan_release.wait(timeout=1.0)
        return False

    limits = _limits(max_ingress_requests=2)
    with monkeypatch.context() as patch:
        patch.setattr(shim_app_module, "_json_depth_exceeds_limit", block_first_depth_scan)
        async with _client(limits=limits) as (client, backend, _):
            active = asyncio.create_task(client.post("/embed", json={"texts": []}))
            queued = None
            try:
                assert await _wait_for_thread_event(scan_entered)
                queued = asyncio.create_task(client.post("/embed", json={"texts": []}))
                await asyncio.sleep(0.02)
                assert not queued.done()
                rejected = await asyncio.wait_for(
                    client.post("/embed", json={"texts": []}),
                    timeout=0.2,
                )
                _assert_busy(rejected, "ingress_busy")
            finally:
                scan_release.set()
                first = await asyncio.wait_for(active, timeout=1.0)
                second = await asyncio.wait_for(queued, timeout=1.0) if queued else None

    assert first.status_code == 200
    assert second is not None and second.status_code == 200
    assert backend.calls == []


async def test_cancelled_json_parse_keeps_ingress_until_worker_finishes(monkeypatch):
    scan_entered = threading.Event()
    scan_release = threading.Event()
    scan_calls = 0

    def block_first_depth_scan(_text: str, _limit: int) -> bool:
        nonlocal scan_calls
        scan_calls += 1
        if scan_calls == 1:
            scan_entered.set()
            assert scan_release.wait(timeout=1.0)
        return False

    limits = _limits(max_ingress_requests=1)
    with monkeypatch.context() as patch:
        patch.setattr(shim_app_module, "_json_depth_exceeds_limit", block_first_depth_scan)
        async with _client(limits=limits) as (client, backend, _):
            cancelled = asyncio.create_task(client.post("/embed", json={"texts": ["cancelled"]}))
            try:
                assert await _wait_for_thread_event(scan_entered)
                cancelled.cancel()
                await asyncio.sleep(0)
                assert not cancelled.done()
                rejected = await client.post("/embed", json={"texts": ["still-busy"]})
                _assert_busy(rejected, "ingress_busy")
            finally:
                scan_release.set()

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(cancelled, timeout=1.0)
            recovered = await client.post("/embed", json={"texts": ["recovered"]})

    assert recovered.status_code == 200
    assert backend.calls == [["recovered"]]


async def test_injected_json_depth_limit_accepts_n_and_rejects_n_plus_one():
    limits = _limits(max_json_depth=3)
    async with _client(limits=limits) as (client, embed_backend, rerank_backend):
        accepted = await client.post(
            "/embed",
            content=_route_payload("/embed", 3, "allowed"),
            headers={"content-type": "application/json"},
        )
        rejected = await client.post(
            "/embed",
            content=_route_payload("/embed", 4, "too-deep"),
            headers={"content-type": "application/json"},
        )

    assert accepted.status_code == 200
    assert rejected.status_code == 400
    assert rejected.json() == {"detail": "Invalid JSON body"}
    assert embed_backend.calls == [["depth-ok"]]
    assert rerank_backend.calls == []


@pytest.mark.parametrize("slash_count", [1, 2, 3, 4])
async def test_json_depth_ignores_escaped_quotes_and_string_delimiters(slash_count):
    body, expected_text = _escaped_quote_payload(slash_count)
    limits = _limits(max_json_depth=2)
    async with _client(limits=limits) as (client, embed_backend, rerank_backend):
        response = await client.post(
            "/embed",
            content=body,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 200
    assert embed_backend.calls == [[expected_text]]
    assert rerank_backend.calls == []


@pytest.mark.parametrize(
    "encoding",
    [
        pytest.param("utf-8", id="utf-8"),
        pytest.param("utf-8-sig", id="utf-8-bom"),
        pytest.param("utf-16", id="utf-16-bom"),
        pytest.param("utf-16-le", id="utf-16-le"),
        pytest.param("utf-16-be", id="utf-16-be"),
        pytest.param("utf-32", id="utf-32-bom"),
        pytest.param("utf-32-le", id="utf-32-le"),
        pytest.param("utf-32-be", id="utf-32-be"),
    ],
)
async def test_json_depth_preserves_supported_byte_encodings(encoding):
    body = '{"texts":["encoded"]}'.encode(encoding)
    async with _client() as (client, embed_backend, rerank_backend):
        response = await client.post(
            "/embed",
            content=body,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 200
    assert embed_backend.calls == [["encoded"]]
    assert rerank_backend.calls == []


async def test_invalid_utf8_is_exact_and_does_not_reach_backend():
    body = b'{"texts":["\xff"]}'
    async with _client() as (client, embed_backend, rerank_backend):
        response = await client.post(
            "/embed",
            content=body,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid JSON body"}
    assert embed_backend.calls == []
    assert rerank_backend.calls == []


@pytest.mark.parametrize("route", ["/embed/query", "/embed/single"])
@pytest.mark.parametrize("body", [b"{}", b"null", b"[]"])
async def test_embed_single_falls_back_for_json_without_value(route, body):
    async with _client() as (client, backend, _):
        response = await client.post(
            route,
            params={"text": "fallback"},
            content=body,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 200
    assert response.json() == [0.1, 0.2, 0.3]
    assert backend.calls == [["fallback"]]


async def test_ingress_lease_is_released_after_terminal_responses():
    async def slow_body() -> AsyncIterator[bytes]:
        yield b'{"texts":["'
        await asyncio.sleep(0.05)
        yield b'slow"]}'

    limits = _limits(
        max_body_bytes=64,
        max_ingress_requests=1,
        body_read_timeout_seconds=0.01,
    )
    valid = {"texts": ["ok"]}
    async with _client(limits=limits) as (client, backend, _):
        first = await client.post("/embed", json=valid)
        after_success = await client.post("/embed", json=valid)

        oversized = await client.post(
            "/embed",
            content=_stream_body(_embed_body(65)),
            headers={"content-type": "application/json"},
        )
        after_oversized = await client.post("/embed", json=valid)

        timed_out = await client.post(
            "/embed",
            content=slow_body(),
            headers={"content-type": "application/json"},
        )
        after_timeout = await client.post("/embed", json=valid)

        invalid = await client.post(
            "/embed",
            content=b"not-json",
            headers={"content-type": "application/json"},
        )
        after_invalid = await client.post("/embed", json=valid)

    assert [
        first.status_code,
        after_success.status_code,
        oversized.status_code,
        after_oversized.status_code,
        timed_out.status_code,
        after_timeout.status_code,
        invalid.status_code,
        after_invalid.status_code,
    ] == [200, 200, 413, 200, 408, 200, 400, 200]
    assert backend.calls == [["ok"]] * 5


async def test_embed_batch_limit_accepts_100_and_rejects_101_without_backend():
    accepted = [str(i) for i in range(100)]
    rejected = [str(i) for i in range(101)]
    async with _client() as (client, backend, _):
        ok = await client.post("/embed", json={"texts": accepted})
        too_many = await client.post("/embed", json={"texts": rejected})

    assert ok.status_code == 200
    assert too_many.status_code == 400
    assert too_many.json() == {"detail": "texts must contain at most 100 items"}
    assert backend.calls == [accepted]


async def test_rerank_batch_limit_accepts_128_and_rejects_129_without_backend():
    accepted = [str(i) for i in range(128)]
    rejected = [str(i) for i in range(129)]
    async with _client() as (client, _, backend):
        ok = await client.post("/rerank", json={"query": "q", "candidates": accepted})
        too_many = await client.post("/rerank", json={"query": "q", "candidates": rejected})

    assert ok.status_code == 200
    assert too_many.status_code == 400
    assert too_many.json() == {"detail": "candidates must contain at most 128 items"}
    assert backend.calls == [("q", accepted)]


async def _blocking_body(
    entered: asyncio.Event, release: asyncio.Event, value: str
) -> AsyncIterator[bytes]:
    yield b'{"texts":["'
    entered.set()
    await release.wait()
    yield value.encode() + b'"]}'


async def test_ninth_body_is_rejected_as_ingress_busy_without_backend():
    entered = [asyncio.Event() for _ in range(8)]
    release = asyncio.Event()
    limits = _limits(max_embed_compute=8)
    async with _client(limits=limits) as (client, backend, _):
        active = [
            asyncio.create_task(
                client.post(
                    "/embed",
                    content=_blocking_body(event, release, str(index)),
                    headers={"content-type": "application/json"},
                )
            )
            for index, event in enumerate(entered)
        ]
        try:
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in entered)), timeout=2.0
            )
            rejected = await client.post("/embed", json={"texts": ["ninth"]})
            _assert_busy(rejected, "ingress_busy")
        finally:
            release.set()
            completed = await asyncio.gather(*active, return_exceptions=True)

    assert all(isinstance(response, httpx.Response) for response in completed)
    assert all(response.status_code == 200 for response in completed)
    assert len(backend.calls) == 8


class FirstBlockingEmbedBackend(FakeEmbedBackend):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if len(self.calls) == 1:
            self.entered.set()
            await self.release.wait()
        return [[0.1, 0.2, 0.3] for _ in texts]


class FailOnceEmbedBackend(FakeEmbedBackend):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if len(self.calls) == 1:
            raise RuntimeError("synthetic embed failure")
        return [[0.1, 0.2, 0.3] for _ in texts]


class CancellationBlockingEmbedBackend(FakeEmbedBackend):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if len(self.calls) > 1:
            return [[0.1, 0.2, 0.3] for _ in texts]
        self.entered.set()
        await self.release.wait()
        return [[0.1, 0.2, 0.3] for _ in texts]


class CancellationFailingEmbedBackend(FakeEmbedBackend):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        self.entered.set()
        await self.release.wait()
        raise RuntimeError("SEC2_DETACHED_SECRET")


async def test_real_embed_probe_reports_gpu_busy_then_recovers():
    backend = FirstBlockingEmbedBackend()
    async with _client(embed_backend=backend) as (client, _, _):
        active = asyncio.create_task(client.post("/embed", json={"texts": ["hold"]}))
        await asyncio.wait_for(backend.entered.wait(), timeout=1.0)
        try:
            busy_probe = await client.post("/embed", json={"texts": ["healthcheck"]})
            _assert_busy(busy_probe, "gpu_busy")
        finally:
            backend.release.set()
            first = await active

        assert first.status_code == 200
        recovered_probe = await client.post("/embed", json={"texts": ["healthcheck"]})

    assert recovered_probe.status_code == 200
    assert backend.calls == [["hold"], ["healthcheck"]]


async def test_embed_compute_lease_is_released_after_exception():
    backend = FailOnceEmbedBackend()
    async with _client(embed_backend=backend) as (client, _, _):
        failed = await client.post("/embed", json={"texts": ["fail"]})
        recovered = await client.post("/embed", json={"texts": ["recover"]})
    assert failed.status_code == 500
    assert recovered.status_code == 200
    assert backend.calls == [["fail"], ["recover"]]


async def test_embed_compute_lease_survives_cancellation_until_backend_finishes():
    backend = CancellationBlockingEmbedBackend()
    async with _client(embed_backend=backend) as (client, _, _):
        active = asyncio.create_task(client.post("/embed", json={"texts": ["hold"]}))
        await asyncio.wait_for(backend.entered.wait(), timeout=1.0)
        active.cancel()
        await asyncio.sleep(0)
        try:
            busy = await client.post("/embed", json={"texts": ["too-early"]})
            _assert_busy(busy, "gpu_busy")
        finally:
            backend.release.set()
            with suppress(asyncio.CancelledError):
                await active
        recovered = await _post_after_busy_releases(
            client,
            "/embed",
            {"texts": ["recovered"]},
            "gpu_busy",
        )

    assert recovered.status_code == 200
    assert backend.calls == [["hold"], ["recovered"]]


async def test_detached_backend_failure_is_logged_without_exception_message(caplog):
    backend = CancellationFailingEmbedBackend()
    caplog.set_level("ERROR", logger=shim_app_module.__name__)
    async with _client(embed_backend=backend) as (client, _, _):
        active = asyncio.create_task(client.post("/embed", json={"texts": ["hold"]}))
        await asyncio.wait_for(backend.entered.wait(), timeout=1.0)
        active.cancel()
        await asyncio.sleep(0)
        backend.release.set()
        with suppress(asyncio.CancelledError):
            await active

        deadline = asyncio.get_running_loop().time() + 1.0
        while not [record for record in caplog.records if record.name == shim_app_module.__name__]:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.001)

    records = [record for record in caplog.records if record.name == shim_app_module.__name__]
    assert len(records) == 1
    assert records[0].backend_gate == "gpu_busy"
    assert records[0].exception_type == "RuntimeError"
    assert "SEC2_DETACHED_SECRET" not in records[0].getMessage()


async def test_detached_failure_race_is_logged_once(caplog):
    release = asyncio.Event()
    active_tasks: set[asyncio.Task[Any]] = set()
    gate = shim_app_module._TryGate(1)

    async def fail_after_release() -> list[list[float]]:
        await release.wait()
        raise RuntimeError("SEC2_RACE_SECRET")

    caplog.set_level("ERROR", logger=shim_app_module.__name__)
    caller = asyncio.create_task(
        shim_app_module._run_physical(
            gate,
            fail_after_release,
            busy_error="gpu_busy",
            active_tasks=active_tasks,
        )
    )
    while not active_tasks:
        await asyncio.sleep(0)

    release.set()
    asyncio.get_running_loop().call_soon(caller.cancel)
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.sleep(0)

    records = [record for record in caplog.records if record.name == shim_app_module.__name__]
    assert len(records) == 1
    assert records[0].backend_gate == "gpu_busy"
    assert records[0].exception_type == "RuntimeError"
    assert "SEC2_RACE_SECRET" not in records[0].getMessage()
    assert active_tasks == set()
    lease = gate.try_acquire()
    assert lease is not None
    lease.release()


class FirstBlockingRerankBackend(FakeRerankBackend):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        self.calls.append((query, candidates))
        if len(self.calls) == 1:
            self.entered.set()
            if not self.release.wait(timeout=5.0):
                raise TimeoutError("test did not release the synthetic ONNX worker")
        return [float(-i) for i in range(len(candidates))]


class FailOnceRerankBackend(FakeRerankBackend):
    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        self.calls.append((query, candidates))
        if len(self.calls) == 1:
            raise RuntimeError("synthetic rerank failure")
        return [float(-i) for i in range(len(candidates))]


async def test_thread_bridge_cancellation_keeps_event_loop_responsive():
    entered = threading.Event()
    release = threading.Event()
    loop_progressed = asyncio.Event()

    def blocking_operation() -> int:
        entered.set()
        assert release.wait(timeout=0.5)
        return 1

    async def release_from_event_loop() -> None:
        await asyncio.sleep(0.01)
        loop_progressed.set()
        release.set()

    physical = asyncio.create_task(shim_app_module._run_sync_in_thread(blocking_operation))
    assert await _wait_for_thread_event(entered)
    release_task = asyncio.create_task(release_from_event_loop())
    started = asyncio.get_running_loop().time()
    physical.cancel()
    with pytest.raises(asyncio.CancelledError):
        await physical
    elapsed = asyncio.get_running_loop().time() - started
    await release_task

    assert loop_progressed.is_set()
    assert elapsed < 0.2


async def test_rerank_reports_service_busy_then_recovers_after_thread_finishes():
    backend = FirstBlockingRerankBackend()
    async with _client(rerank_backend=backend) as (client, _, _):
        active = asyncio.create_task(
            client.post("/rerank", json={"query": "hold", "candidates": ["candidate"]})
        )
        assert await _wait_for_thread_event(backend.entered)
        try:
            independent_embed = await client.post("/embed", json={"texts": ["still-free"]})
            assert independent_embed.status_code == 200
            busy = await client.post("/rerank", json={"query": "busy", "candidates": ["candidate"]})
            _assert_busy(busy, "service_busy")
        finally:
            backend.release.set()
            first = await active

        assert first.status_code == 200
        recovered = await client.post(
            "/rerank", json={"query": "recovered", "candidates": ["candidate"]}
        )

    assert recovered.status_code == 200
    assert backend.calls == [
        ("hold", ["candidate"]),
        ("recovered", ["candidate"]),
    ]


async def test_rerank_compute_lease_is_released_after_exception():
    backend = FailOnceRerankBackend()
    async with _client(rerank_backend=backend) as (client, _, _):
        failed = await client.post("/rerank", json={"query": "fail", "candidates": ["candidate"]})
        recovered = await client.post(
            "/rerank", json={"query": "recover", "candidates": ["candidate"]}
        )
    assert failed.status_code == 500
    assert recovered.status_code == 200
    assert backend.calls == [
        ("fail", ["candidate"]),
        ("recover", ["candidate"]),
    ]


async def test_cancelled_rerank_keeps_lease_until_physical_thread_finishes():
    backend = FirstBlockingRerankBackend()
    async with _client(rerank_backend=backend) as (client, _, _):
        active = asyncio.create_task(
            client.post("/rerank", json={"query": "hold", "candidates": ["candidate"]})
        )
        assert await _wait_for_thread_event(backend.entered)
        active.cancel()
        await asyncio.sleep(0)
        try:
            busy = await client.post(
                "/rerank", json={"query": "too-early", "candidates": ["candidate"]}
            )
            _assert_busy(busy, "service_busy")
        finally:
            backend.release.set()
            with suppress(asyncio.CancelledError):
                await active
        recovered = await _post_after_busy_releases(
            client,
            "/rerank",
            {"query": "recovered", "candidates": ["candidate"]},
            "service_busy",
        )

    assert recovered.status_code == 200
    assert backend.calls == [
        ("hold", ["candidate"]),
        ("recovered", ["candidate"]),
    ]
