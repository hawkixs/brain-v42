# Embedding GGUF Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace PyTorch fp16 embedding serving (structural VRAM drift, 4.9/6 GiB) with llama.cpp GGUF Q8_0 (static allocation, 2.9 GiB, 2x faster) + a Starlette shim preserving the legacy port 8003 contract + a CPU ONNX reranker, validated by gold bench v1 against a freshly re-measured PyTorch baseline, with one-command rollback.

**Architecture:** Two new compose services: `embedding-llama` (image `ghcr.io/ggml-org/llama.cpp:server-cuda`, bind-mounted GGUF Q8_0, `--embedding --pooling last`, OpenAI API `/v1/embeddings`, internal network) and `embedding-shim` (Starlette + uvicorn, publishes 8003, translates the legacy contract `/embed` `/embed/query` `/embed/single` to llama, runs `/rerank` locally via onnxruntime CPU on ms-marco-MiniLM-L-6-v2 pre-exported by Xenova). The legacy `embedding` service (PyTorch) moves under `profiles: ["legacy"]` — instant rollback. The clients (`GPUEmbeddingService`, `RerankerClient`, MCP, metrics sidecar) do NOT change: same URL `http://localhost:8003`, same response shapes.

**Tech Stack:** Starlette (direct repo dep — NOT FastAPI), httpx, anyio, uvicorn, onnxruntime + tokenizers (container only, lazy-imports), llama.cpp server-cuda, existing gold bench v1 (`bench/embedding_v1/`).

## Global Constraints

- ruff `==0.15.18`: `ruff check src/ tests/ scripts/` AND `ruff format --check src/ tests/ scripts/` must pass (CI runs both; `services/` is NOT linted by CI but we write clean code anyway).
- `mypy src/` must stay clean (the shim lives in `services/`, outside mypy scope — add nothing to `src/`).
- `pytest tests/unit/ ` 100% green; CI coverage ≥ 60% (the shim, tested via `tests/unit/test_embedding_shim.py`, counts toward the run but not toward `--cov=brain_v42`).
- NO new dependency in `pyproject.toml`: the shim tests use only starlette/httpx/anyio/pytest (already present). `onnxruntime`/`tokenizers`/`numpy` are lazy-imported (container only) — the test exercising the numpy path uses `pytest.importorskip`.
- Legacy API contract to preserve IDENTICALLY (source of truth: `services/embedding/main.py` v2.0.0):
  - `POST /embed {"texts": [...]}` → raw `[[float,...],...]` (empty list → `[]`)
  - `POST /embed/query` and `POST /embed/single`: JSON body `{"text": "..."}` takes priority, legacy `?text=` accepted, neither → 400; raw `[float,...]` response
  - `POST /rerank {"query": str, "candidates": [...]}` → `{"scores": [float,...]}` — **raw logits** (no sigmoid; parity with `CrossEncoder.predict`, verified in production: scores -1.37/-11.34)
  - `GET /healthz` → `{"status":"ok"}` (200); `GET /health` → `{"status":"ok","model":...}` (compat `RerankerClient.is_available`); `GET /` → info JSON
- Vectors L2-normalized server-side (clients do not do this).
- llama.cpp: `--pooling last` REQUIRED (model's sentence-transformers config: `pooling_mode_lasttoken: true`), `-ub 1024` (VRAM lever #1, sweep 2026-07-06: 2841 MiB vs 4683 at ub=4096, identical latencies; ub<n_tokens chunking is correct for a CAUSAL Qwen2 embedder), `-c 8192` (covers the client cap of 15000 chars ≈ 4-5k tokens).
- Canonical GGUF: `/home/hawixs/models/qodo-gguf/qodo-embed-1.5b-q8_0.gguf` (1.6 GiB, produced 2026-07-06, cosine 0.9998 vs PyTorch).
- Validation gates (vs PyTorch baseline re-measured the same day, same harness): ΔMRR_self ≥ −0.01, Δrecall@10_self ≥ −0.005, ΔMRR_cross ≥ −0.01, Pearson rerank ≥ 0.995. FAIL → rollback, no forcing through.
- Atomic Conventional Commits; never commit to main (branch `feat/embedding-gguf-cutover`).
- The cutover (Task 6) is a production operation: executed INLINE by the coordinator (not by a subagent), with the rollback shown before acting.

## File Structure

| File | Role |
|---|---|
| `services/embedding_shim/shim_backends.py` | Create — `LlamaEmbedBackend` (proxy /v1/embeddings, sort by index, L2-norm, truncation guard + retry), `OnnxRerankBackend` (onnxruntime CPU cross-encoder, lazy-load), `UpstreamError` |
| `services/embedding_shim/shim_app.py` | Create — Starlette factory `create_app(embed_backend, rerank_backend)`, 7 legacy routes |
| `services/embedding_shim/main.py` | Create — env wiring (`LLAMA_URL`, `ONNX_DIR`) + uvicorn entrypoint |
| `services/embedding_shim/Dockerfile` | Create — python:3.12-slim + deps + download ONNX Xenova at build time |
| `tests/unit/test_embedding_shim.py` | Create — TDD tests for backends + app (fakes + MockTransport) |
| `docker-compose.yml` | Modify — `embedding` → `profiles: ["legacy"]`; add `embedding-llama` + `embedding-shim` |
| `scripts/embedding_cutover_check.py` | Create — self/cross/rerank-parity bench vs baseline, PASS/FAIL gates |
| `scripts/embedding_gguf_build.sh` | Create — reproducibility of the GGUF conversion (download → convert F16 → quantize Q8_0) |
| `bench/embedding_v1/.gitignore` | Modify — ignore `cutover/` (run artifacts) |

---

### Task 1: LlamaEmbedBackend (proxy /v1/embeddings)

**Files:**
- Create: `services/embedding_shim/shim_backends.py`
- Test: `tests/unit/test_embedding_shim.py`

**Interfaces:**
- Produces: `LlamaEmbedBackend(base_url: str, timeout: float = 60.0, transport: httpx.AsyncBaseTransport | None = None)` with `async embed(texts: list[str]) -> list[list[float]]`, `async healthy() -> bool`, `async close() -> None`; exception `UpstreamError`; constants `MAX_TEXT_CHARS = 20_000`, `RETRY_TEXT_CHARS = 8_000`. Consumed by Task 3 (app) and Task 4 (container).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_embedding_shim.py`:

```python
"""Embedding shim tests (services/embedding_shim/) — backends + app.

The shim is not an installed package: it is imported via sys.path.
Reference contract: services/embedding/main.py v2.0.0 (PyTorch legacy).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import httpx
import pytest

SHIM_DIR = Path(__file__).resolve().parents[2] / "services" / "embedding_shim"
sys.path.insert(0, str(SHIM_DIR))

from shim_backends import (  # noqa: E402
    MAX_TEXT_CHARS,
    RETRY_TEXT_CHARS,
    LlamaEmbedBackend,
    UpstreamError,
)


def _openai_payload(vecs_by_index: dict[int, list[float]]) -> dict:
    """Response from /v1/embeddings in OpenAI format (indices intentionally shuffled)."""
    return {
        "data": [
            {"object": "embedding", "index": i, "embedding": v}
            for i, v in vecs_by_index.items()
        ]
    }


def _make_backend(handler) -> LlamaEmbedBackend:
    return LlamaEmbedBackend(
        "http://llama-test", transport=httpx.MockTransport(handler)
    )


async def test_embed_sorts_by_index_and_normalizes():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/v1/embeddings"
        assert body["input"] == ["aaa", "bbb"]
        # Response out of order + non-normalized vectors
        return httpx.Response(
            200, json=_openai_payload({1: [0.0, 2.0], 0: [3.0, 4.0]})
        )

    vecs = await _make_backend(handler).embed(["aaa", "bbb"])
    assert len(vecs) == 2
    # index 0 first, L2-normalized: [3,4] → [0.6, 0.8]
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
            # e.g. "input is larger than the max context size"
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
```

- [ ] **Step 2: Verify the failure**

Run: `.venv/bin/python -m pytest tests/unit/test_embedding_shim.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'shim_backends'`

- [ ] **Step 3: Minimal implementation**

Create `services/embedding_shim/shim_backends.py`:

```python
"""Backends for the brain-v42 embedding shim.

LlamaEmbedBackend — proxy to the llama.cpp server (/v1/embeddings, OpenAI
shape). Sorts by index, L2-normalizes (defensive — llama already normalizes
with --embd-normalize by default), bounds text size, and retries once
shorter if the upstream returns 500 (token context overflow).

OnnxRerankBackend — ms-marco-MiniLM-L-6-v2 cross-encoder via onnxruntime
CPU (Task 2). Lazy-load: onnxruntime/tokenizers/numpy are only imported
on first call — absent from the dev venv, present in the container.
"""

from __future__ import annotations

import math
import threading

import httpx

# ~5-7k tokens: stays under the llama server's n_ctx=8192. The historical
# client cap is 15000 chars (ADR #7); 20000 leaves headroom for direct
# calls outside MCP.
MAX_TEXT_CHARS = 20_000
# Retry after an upstream 500 (token-dense text that overflows despite
# the char guard): at 8000 chars we're always < 4096 tokens.
RETRY_TEXT_CHARS = 8_000


class UpstreamError(Exception):
    """The llama server responded 5xx (after the truncation retry)."""


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


class LlamaEmbedBackend:
    """Async client for the llama.cpp server's /v1/embeddings."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            )
        return self._client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        bounded = [t[:MAX_TEXT_CHARS] for t in texts]
        try:
            return await self._call(bounded)
        except UpstreamError:
            return await self._call([t[:RETRY_TEXT_CHARS] for t in bounded])

    async def _call(self, texts: list[str]) -> list[list[float]]:
        resp = await self._get_client().post(
            "/v1/embeddings", json={"model": "qodo", "input": texts}
        )
        if resp.status_code >= 500:
            raise UpstreamError(resp.text[:200])
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [_l2_normalize(d["embedding"]) for d in data]

    async def healthy(self) -> bool:
        try:
            resp = await self._get_client().get("/health")
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
```

- [ ] **Step 4: Verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_embedding_shim.py -v`
Expected: 6 PASS

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check services/embedding_shim/ tests/unit/test_embedding_shim.py
.venv/bin/ruff format services/embedding_shim/ tests/unit/test_embedding_shim.py
.venv/bin/python -m pytest tests/unit/ -q
git add services/embedding_shim/shim_backends.py tests/unit/test_embedding_shim.py
git commit -m "feat(shim): LlamaEmbedBackend — proxy /v1/embeddings llama.cpp (TDD)"
```

---

### Task 2: OnnxRerankBackend (CPU cross-encoder)

**Files:**
- Modify: `services/embedding_shim/shim_backends.py` (append)
- Test: `tests/unit/test_embedding_shim.py` (append)

**Interfaces:**
- Produces: `OnnxRerankBackend(model_path: str, tokenizer_path: str, max_length: int = 512)` with `rerank(query: str, candidates: list[str]) -> list[float]` (SYNC — the app runs it via `anyio.to_thread.run_sync`). Consumed by Task 3.

- [ ] **Step 1: Failing tests** (append to `tests/unit/test_embedding_shim.py`)

```python
from shim_backends import OnnxRerankBackend  # noqa: E402


def test_rerank_empty_candidates_short_circuits(tmp_path):
    # Paths intentionally nonexistent: if lazy-load fires on
    # candidates=[], the test blows up — that's the behavior under test.
    backend = OnnxRerankBackend(
        str(tmp_path / "nope.onnx"), str(tmp_path / "nope.json")
    )
    assert backend.rerank("query", []) == []


def test_rerank_builds_pairs_and_returns_raw_logits():
    np = pytest.importorskip("numpy")

    class FakeInput:
        def __init__(self, name: str) -> None:
            self.name = name

    class FakeSession:
        def get_inputs(self):
            # Xenova export: token_type_ids present
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
```

- [ ] **Step 2: Verify the failure**

Run: `.venv/bin/python -m pytest tests/unit/test_embedding_shim.py -v -k rerank`
Expected: FAIL — `ImportError: cannot import name 'OnnxRerankBackend'`

- [ ] **Step 3: Implementation** (append to `shim_backends.py`)

```python
class OnnxRerankBackend:
    """ms-marco-MiniLM-L-6-v2 cross-encoder via onnxruntime (CPU).

    Returns RAW logits (no sigmoid) — exact parity with
    CrossEncoder.predict from the legacy PyTorch service (scores observed
    in production: -1.37 relevant / -11.34 not relevant).
    """

    def __init__(
        self, model_path: str, tokenizer_path: str, max_length: int = 512
    ) -> None:
        self._model_path = model_path
        self._tokenizer_path = tokenizer_path
        self._max_length = max_length
        self._session = None
        self._tokenizer = None
        # rerank() runs in the anyio threadpool: two concurrent first
        # calls could race the lazy-load without this lock.
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._session is None:
                import onnxruntime  # container only (lazy)

                from tokenizers import Tokenizer  # container only (lazy)

                self._session = onnxruntime.InferenceSession(
                    self._model_path, providers=["CPUExecutionProvider"]
                )
                tokenizer = Tokenizer.from_file(self._tokenizer_path)
                tokenizer.enable_truncation(max_length=self._max_length)
                tokenizer.enable_padding()
                self._tokenizer = tokenizer
        return self._session, self._tokenizer

    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        if not candidates:
            return []
        session, tokenizer = self._load()
        import numpy as np  # container only (lazy)

        encodings = tokenizer.encode_batch([(query, c) for c in candidates])
        feeds = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array(
                [e.attention_mask for e in encodings], dtype=np.int64
            ),
            "token_type_ids": np.array(
                [e.type_ids for e in encodings], dtype=np.int64
            ),
        }
        wanted = {i.name for i in session.get_inputs()}
        feeds = {k: v for k, v in feeds.items() if k in wanted}
        logits = session.run(None, feeds)[0]
        return [float(x) for x in logits.reshape(-1)]
```

- [ ] **Step 4: Verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_embedding_shim.py -v`
Expected: 8 PASS (or 7 PASS + 1 SKIP if numpy is absent from the venv)

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check services/embedding_shim/ tests/unit/test_embedding_shim.py
.venv/bin/ruff format services/embedding_shim/ tests/unit/test_embedding_shim.py
git add -u && git commit -m "feat(shim): OnnxRerankBackend — cross-encoder onnxruntime CPU (TDD)"
```

---

### Task 3: Starlette app (legacy contract) + wiring

**Files:**
- Create: `services/embedding_shim/shim_app.py`
- Create: `services/embedding_shim/main.py`
- Test: `tests/unit/test_embedding_shim.py` (append)

**Interfaces:**
- Consumes: `LlamaEmbedBackend.embed/healthy` (Task 1), `OnnxRerankBackend.rerank` (Task 2).
- Produces: `create_app(embed_backend, rerank_backend) -> Starlette`; `main.py` exposes `app` at module level for `uvicorn main:app` (Task 4). Env: `LLAMA_URL` (default `http://embedding-llama:8080`), `ONNX_DIR` (default `/app/onnx`).

- [ ] **Step 1: Failing tests** (append to `tests/unit/test_embedding_shim.py`)

```python
from starlette.testclient import TestClient  # noqa: E402

from shim_app import create_app  # noqa: E402


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
    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        return [float(-i) for i in range(len(candidates))]


def _client(healthy: bool = True) -> tuple[TestClient, FakeEmbedBackend]:
    backend = FakeEmbedBackend(healthy=healthy)
    app = create_app(backend, FakeRerankBackend())
    return TestClient(app), backend


def test_app_embed_batch():
    client, backend = _client()
    resp = client.post("/embed", json={"texts": ["a", "b"]})
    assert resp.status_code == 200
    assert resp.json() == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]
    assert backend.calls == [["a", "b"]]


def test_app_embed_empty_list():
    client, backend = _client()
    resp = client.post("/embed", json={"texts": []})
    assert resp.status_code == 200
    assert resp.json() == []
    assert backend.calls == []


def test_app_embed_missing_texts_is_400():
    client, _ = _client()
    assert client.post("/embed", json={}).status_code == 400


def test_app_embed_query_json_body():
    client, backend = _client()
    resp = client.post("/embed/query", json={"text": "hello"})
    assert resp.status_code == 200
    assert resp.json() == [0.1, 0.2, 0.3]
    assert backend.calls == [["hello"]]


def test_app_embed_query_legacy_query_param():
    client, backend = _client()
    resp = client.post("/embed/query", params={"text": "legacy"})
    assert resp.status_code == 200
    assert backend.calls == [["legacy"]]


def test_app_embed_query_body_wins_over_param():
    client, backend = _client()
    client.post("/embed/query", params={"text": "param"}, json={"text": "body"})
    assert backend.calls == [["body"]]


def test_app_embed_query_missing_text_is_400():
    client, _ = _client()
    assert client.post("/embed/query").status_code == 400


def test_app_embed_single_same_contract():
    client, backend = _client()
    resp = client.post("/embed/single", json={"text": "doc"})
    assert resp.status_code == 200
    assert resp.json() == [0.1, 0.2, 0.3]


def test_app_rerank():
    client, _ = _client()
    resp = client.post(
        "/rerank", json={"query": "q", "candidates": ["a", "b", "c"]}
    )
    assert resp.status_code == 200
    assert resp.json() == {"scores": [0.0, -1.0, -2.0]}


def test_app_rerank_empty_candidates():
    client, _ = _client()
    resp = client.post("/rerank", json={"query": "q", "candidates": []})
    assert resp.status_code == 200
    assert resp.json() == {"scores": []}


def test_app_rerank_bad_payload_is_400():
    client, _ = _client()
    assert client.post("/rerank", json={"query": "q"}).status_code == 400


def test_app_healthz_ok_when_upstream_healthy():
    client, _ = _client(healthy=True)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_app_healthz_503_when_upstream_down():
    client, _ = _client(healthy=False)
    assert client.get("/healthz").status_code == 503


def test_app_health_legacy_reranker_compat():
    client, _ = _client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_app_info():
    client, _ = _client()
    body = client.get("/").json()
    assert body["dims"] == 1536
    assert body["embed_model"] == "Qodo/Qodo-Embed-1-1.5B"
    assert "runtime" in body
```

- [ ] **Step 2: Verify the failure**

Run: `.venv/bin/python -m pytest tests/unit/test_embedding_shim.py -v -k app`
Expected: FAIL — `ModuleNotFoundError: No module named 'shim_app'`

- [ ] **Step 3: Implementation**

Create `services/embedding_shim/shim_app.py`:

```python
"""Shim Starlette app — brain-v42 legacy embedding contract (port 8003).

Exact parity with services/embedding/main.py v2.0.0 (PyTorch):
  POST /embed         {"texts": [...]}          -> [[float,...],...]
  POST /embed/query   {"text": "..."} | ?text=  -> [float,...]
  POST /embed/single  same as /embed/query      -> [float,...]
  POST /rerank        {"query","candidates"}    -> {"scores": [...]}
  GET  /              -> models/runtime info
  GET  /healthz       -> 200 if upstream llama healthy, else 503
  GET  /health        -> 200 (compat RerankerClient.is_available)

Deliberate difference: /healthz probes the upstream (the old /healthz
never touched the GPU — that's the false-green bug from the 2026-04-12
incident, learning 410eb227; we fix it here).
"""

from __future__ import annotations

import anyio.to_thread
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

EMBED_MODEL = "Qodo/Qodo-Embed-1-1.5B"
RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
EXPECTED_DIMS = 1536
RUNTIME = "llama.cpp-gguf-q8_0+onnx-cpu"


async def _json_or_none(request: Request) -> dict | None:
    try:
        payload = await request.json()
    except Exception:  # empty/non-JSON body = legacy case (?text=)
        return None
    return payload if isinstance(payload, dict) else None


def create_app(embed_backend, rerank_backend) -> Starlette:
    async def embed(request: Request) -> JSONResponse:
        payload = await _json_or_none(request)
        texts = payload.get("texts") if payload else None
        if not isinstance(texts, list):
            return JSONResponse(
                {"detail": "texts must be a list"}, status_code=400
            )
        if not texts:
            return JSONResponse([])
        vecs = await embed_backend.embed([str(t) for t in texts])
        return JSONResponse(vecs)

    async def embed_single(request: Request) -> JSONResponse:
        payload = await _json_or_none(request)
        text = payload.get("text") if payload else None
        if not text:
            text = request.query_params.get("text")
        if not text:
            return JSONResponse(
                {
                    "detail": (
                        "Missing 'text' — provide via JSON body "
                        '{"text": "..."} or ?text= query param'
                    )
                },
                status_code=400,
            )
        vecs = await embed_backend.embed([str(text)])
        return JSONResponse(vecs[0])

    async def rerank(request: Request) -> JSONResponse:
        payload = await _json_or_none(request)
        query = payload.get("query") if payload else None
        candidates = payload.get("candidates") if payload else None
        if not isinstance(query, str) or not isinstance(candidates, list):
            return JSONResponse(
                {"detail": "query (str) and candidates (list) required"},
                status_code=400,
            )
        scores = await anyio.to_thread.run_sync(
            rerank_backend.rerank, query, [str(c) for c in candidates]
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
        return JSONResponse(
            {"status": "degraded", "upstream": "unreachable"}, status_code=503
        )

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "model": RERANK_MODEL})

    return Starlette(
        routes=[
            Route("/embed", embed, methods=["POST"]),
            Route("/embed/query", embed_single, methods=["POST"]),
            Route("/embed/single", embed_single, methods=["POST"]),
            Route("/rerank", rerank, methods=["POST"]),
            Route("/", info, methods=["GET"]),
            Route("/healthz", healthz, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
        ]
    )
```

Create `services/embedding_shim/main.py`:

```python
"""Shim entrypoint — env wiring + uvicorn.

Env:
  LLAMA_URL  llama.cpp server URL (default http://embedding-llama:8080)
  ONNX_DIR   directory holding model.onnx + tokenizer.json (default /app/onnx)
"""

from __future__ import annotations

import os

from shim_app import create_app
from shim_backends import LlamaEmbedBackend, OnnxRerankBackend


def build_app():
    llama_url = os.environ.get("LLAMA_URL", "http://embedding-llama:8080")
    onnx_dir = os.environ.get("ONNX_DIR", "/app/onnx")
    return create_app(
        LlamaEmbedBackend(llama_url),
        OnnxRerankBackend(
            f"{onnx_dir}/model.onnx", f"{onnx_dir}/tokenizer.json"
        ),
    )


app = build_app()

if __name__ == "__main__":
    import uvicorn  # container only (lazy)

    uvicorn.run(app, host="0.0.0.0", port=8003, workers=1)
```

- [ ] **Step 4: Verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_embedding_shim.py -v`
Expected: 23 PASS (or 22 + 1 SKIP numpy)

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check services/embedding_shim/ tests/unit/test_embedding_shim.py
.venv/bin/ruff format services/embedding_shim/ tests/unit/test_embedding_shim.py
.venv/bin/python -m pytest tests/unit/ -q
git add services/embedding_shim/ tests/unit/test_embedding_shim.py
git commit -m "feat(shim): app Starlette contrat legacy 8003 + wiring env"
```

---

### Task 4: Shim Dockerfile + compose services + GGUF reproducibility script

**Files:**
- Create: `services/embedding_shim/Dockerfile`
- Create: `scripts/embedding_gguf_build.sh`
- Modify: `docker-compose.yml` (service `embedding` lines 32-62; add 2 services)

**Interfaces:**
- Consumes: `main:app` (Task 3), GGUF `/home/hawixs/models/qodo-gguf/qodo-embed-1.5b-q8_0.gguf`.
- Produces: compose services `embedding-llama` (internal, `http://embedding-llama:8080`) and `embedding-shim` (host `8003:8003`); `legacy` profile for the old service. Consumed by Tasks 5-6.

- [ ] **Step 1: Dockerfile**

Create `services/embedding_shim/Dockerfile`:

```dockerfile
# ============================================================
# Dockerfile — Brain v42 Embedding Shim
#
# Translates the legacy contract (:8003 /embed /embed/query /rerank)
# to the llama.cpp server (/v1/embeddings) + runs the cross-encoder
# reranker in ONNX CPU (no PyTorch: image ~600 MB instead of
# ~8 GB, RAM ~300 MB instead of 12.8 GB).
#
# The pre-exported ONNX comes from Xenova/ms-marco-MiniLM-L-6-v2
# (same model as cross-encoder/ms-marco-MiniLM-L-6-v2) — score
# parity is validated by scripts/embedding_cutover_check.py
# (Pearson gate >= 0.995 vs the PyTorch service).
# ============================================================

FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    starlette \
    "uvicorn[standard]" \
    httpx \
    anyio \
    onnxruntime \
    tokenizers \
    numpy \
    huggingface_hub

# ONNX model + tokenizer downloaded at build time (network-free startup).
RUN python -c "\
from huggingface_hub import hf_hub_download; \
p1 = hf_hub_download('Xenova/ms-marco-MiniLM-L-6-v2', 'onnx/model.onnx'); \
p2 = hf_hub_download('Xenova/ms-marco-MiniLM-L-6-v2', 'tokenizer.json'); \
import shutil, os; os.makedirs('/app/onnx', exist_ok=True); \
shutil.copy(p1, '/app/onnx/model.onnx'); \
shutil.copy(p2, '/app/onnx/tokenizer.json'); \
print('ONNX assets ready')"

COPY shim_backends.py shim_app.py main.py ./

EXPOSE 8003

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003", "--workers", "1"]
```

- [ ] **Step 2: GGUF reproducibility script**

Create `scripts/embedding_gguf_build.sh` (chmod +x):

```bash
#!/usr/bin/env bash
# Reproduces the Q8_0 GGUF for Qodo-Embed-1-1.5B (originally produced
# 2026-07-06). Output: $OUT_DIR/qodo-embed-1.5b-{f16,q8_0}.gguf
#
# Usage: scripts/embedding_gguf_build.sh [OUT_DIR]
set -euo pipefail

OUT_DIR="${1:-/home/hawixs/models/qodo-gguf}"
HF_REPO="Qodo/Qodo-Embed-1-1.5B"
SNAP_DIR="$OUT_DIR/hf_snapshot"

mkdir -p "$SNAP_DIR"

echo "→ download HF snapshot ($HF_REPO)"
docker run --rm -v "$OUT_DIR":/w python:3.12-slim bash -c "
  pip install --quiet --no-cache-dir huggingface_hub &&
  python -c \"
from huggingface_hub import snapshot_download
snapshot_download('$HF_REPO', local_dir='/w/hf_snapshot',
                  allow_patterns=['*.json', '*.safetensors', '*.txt'])
print('snapshot ok')\"
"

echo "→ convert F16"
docker run --rm -v "$OUT_DIR":/w ghcr.io/ggml-org/llama.cpp:full \
  --convert /w/hf_snapshot --outfile /w/qodo-embed-1.5b-f16.gguf --outtype f16

echo "→ quantize Q8_0"
docker run --rm -v "$OUT_DIR":/w ghcr.io/ggml-org/llama.cpp:full \
  --quantize /w/qodo-embed-1.5b-f16.gguf /w/qodo-embed-1.5b-q8_0.gguf Q8_0

ls -lh "$OUT_DIR"/*.gguf
echo "✓ done"
```

- [ ] **Step 3: Compose — legacy under a profile + 2 new services**

In `docker-compose.yml`, modify the `embedding` service (add `profiles` right under `container_name`):

```yaml
  embedding:
    build: ./services/embedding
    container_name: brain_v42_embedding
    # Legacy PyTorch fp16 — replaced by embedding-llama + embedding-shim
    # (cutover 2026-07-06, structural VRAM drift — learning 410eb227).
    # ROLLBACK: docker compose stop embedding-shim embedding-llama
    #           && docker compose --profile legacy up -d embedding
    profiles: ["legacy"]
    restart: unless-stopped
```

(the rest of the `embedding` service is unchanged)

Add after the `embedding` service:

```yaml
  embedding-llama:
    image: ghcr.io/ggml-org/llama.cpp:server-cuda
    container_name: brain_v42_embedding_llama
    restart: unless-stopped
    # --pooling last: the model's sentence-transformers config (lasttoken).
    # -ub 1024: VRAM lever #1 (2.9 GiB vs 4.7 at ub=4096, identical
    # latencies — sweep 2026-07-06, learning 906722df). ub < n_tokens
    # chunking is correct for a causal embedder (Qwen2).
    # -c 8192: covers the client cap of 15000 chars (~4-5k tokens).
    # -np 1: locked in — the KV cache is PER SLOT; -np N on this 6 GiB
    # GPU means re-measuring VRAM (N × ctx 8192).
    # --cont-batching + --embd-normalize 2 (L2): explicit so we don't
    # depend on the image's defaults (the shim also re-normalizes,
    # defense in depth).
    # Static VRAM allocation at startup → no drift (the durable fix
    # for the 2026-04-12 incident).
    command: >
      -m /models/qodo-embed-1.5b-q8_0.gguf
      --embedding --pooling last
      -ngl 999 -c 8192 -ub 1024 -b 8192
      -np 1 --cont-batching --embd-normalize 2
      --host 0.0.0.0 --port 8080
    volumes:
      - ${QODO_GGUF_DIR:-/home/hawixs/models/qodo-gguf}:/models:ro
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    healthcheck:
      # curl present in the image (verified 2026-07-06). llama-server's
      # /health returns 503 while the model loads, 200 afterward.
      test: ["CMD", "curl", "-sf", "http://127.0.0.1:8080/health"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 60s

  embedding-shim:
    build: ./services/embedding_shim
    container_name: brain_v42_embedding_shim
    restart: unless-stopped
    ports:
      - "8003:8003"
    networks:
      - default
      - hawkixs-infra
    environment:
      - LLAMA_URL=http://embedding-llama:8080
    depends_on:
      embedding-llama:
        condition: service_healthy
    healthcheck:
      # Real end-to-end healthcheck: POST /embed traverses the shim
      # AND the llama server (same philosophy as the post-incident
      # 410eb227 legacy healthcheck — never a /healthz-only false-green).
      test: ["CMD", "python3", "-c", "import urllib.request,json; r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8003/embed',data=json.dumps({'texts':['healthcheck']}).encode(),headers={'Content-Type':'application/json'}),timeout=15); assert r.read(2)==b'[['"]
      interval: 60s
      timeout: 20s
      retries: 5
      start_period: 60s
```

Note: `embedding-llama` stays on the `default` network only (no host port); `embedding-shim` joins `hawkixs-infra` like the legacy service (parity for inter-container consumers).

- [ ] **Step 4: Static checks + GGUF permissions**

```bash
sudo chown -R hawixs:hawixs /home/hawixs/models/qodo-gguf 2>/dev/null || chown -R hawixs:hawixs /home/hawixs/models/qodo-gguf || true
ls -lh /home/hawixs/models/qodo-gguf/qodo-embed-1.5b-q8_0.gguf   # must exist, 1.6G
docker compose config embedding-llama embedding-shim > /dev/null && echo "compose OK"
docker compose --profile legacy config embedding > /dev/null && echo "profile legacy OK"
```

Expected: `compose OK` + `profile legacy OK`

- [ ] **Step 5: Build the shim image (no startup)**

```bash
docker compose build embedding-shim
```

Expected: green build, ONNX download ~90 MB at build time.

- [ ] **Step 6: Commit**

```bash
chmod +x scripts/embedding_gguf_build.sh
git add services/embedding_shim/Dockerfile scripts/embedding_gguf_build.sh docker-compose.yml
git commit -m "feat(deploy): embedding-llama (GGUF Q8) + embedding-shim, PyTorch legacy en profile"
```

---

### Task 5: Cutover validation script + PyTorch baseline

**Files:**
- Create: `scripts/embedding_cutover_check.py`
- Modify: `bench/embedding_v1/.gitignore` (add the `cutover/` line)

**Interfaces:**
- Consumes: `bench/embedding_v1/gen_gold.py` (QUERIES, SAMPLE_SIZES, SEED), `bench/embedding_v1/run_bench.py` (`load_corpus`, `compute_metrics`, `cosine_rank_all`, `QueryResult`, `_norm`), gold `bench/embedding_v1/gold_v1.jsonl`, PG `localhost:5433`.
- Produces: CLI `python scripts/embedding_cutover_check.py --url URL --output FILE [--baseline FILE] [--limit-queries N]` — exit 0 if gates PASS (or no baseline), exit 1 if FAIL. Output JSON: `{url, self: {...metrics}, cross: {...}, rerank_scores: [...], n_gold_kept, n_cross_corpus}`.

- [ ] **Step 1: Write the script**

Create `scripts/embedding_cutover_check.py`:

```python
"""Embedding cutover validation — gold bench v1 against a baseline.

Three measurements against the target endpoint (native /embed, /rerank contract):

  self   — corpus AND queries embedded by the target (quality of the
           served model, same harness as bench/embedding_v1/run_bench.py).
  cross  — corpus = vectors STORED in PG (embedded by the historical
           PyTorch fp16), queries embedded by the target. This is the
           real post-cutover scenario: GGUF queries against a corpus
           that hasn't been re-embedded in fp16.
  rerank — /rerank scores on deterministic pairs (parity ONNX vs
           PyTorch CrossEncoder).

Usage:
  # baseline (PyTorch still in prod)
  python scripts/embedding_cutover_check.py \
      --url http://localhost:8003 \
      --output bench/embedding_v1/cutover/baseline_pytorch.json

  # post-cutover (shim in place) + gates
  python scripts/embedding_cutover_check.py \
      --url http://localhost:8003 \
      --output bench/embedding_v1/cutover/candidate_gguf.json \
      --baseline bench/embedding_v1/cutover/baseline_pytorch.json

Gates (candidate vs baseline): dMRR_self >= -0.01,
drecall@10_self >= -0.005, dMRR_cross >= -0.01, pearson_rerank >= 0.995.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import urllib.request
from pathlib import Path

import asyncpg

REPO = Path(__file__).resolve().parents[1]
BENCH = REPO / "bench" / "embedding_v1"
sys.path.insert(0, str(BENCH))

from run_bench import (  # noqa: E402
    PG_DSN,
    QueryResult,
    compute_metrics,
    cosine_rank_all,
    load_corpus,
)

GOLD_PATH = BENCH / "gold_v1.jsonl"

# etype (gen_gold) -> PG table. Tables without an embedding column are
# excluded from cross mode at run time (explicit log, no silent cap).
TABLES = {
    "learning": "learnings",
    "feature": "features",
    "plan_chunk": "indexed_plan_chunks",
    "decision": "decisions",
    "snippet": "snippets",
    "plan": "indexed_plans",
    "runbook": "runbooks",
    "adr": "adrs",
}

RERANK_PAIRS = 20
EMBED_BATCH = 16
# Below this number of resolvable gold queries in cross mode, the
# dMRR_cross gate isn't statistically significant → exit 2 (neither
# PASS nor FAIL: sample too small, investigate before cutover).
MIN_CROSS_GOLD = 100

GATES = {
    "d_mrr_self": -0.01,
    "d_recall10_self": -0.005,
    "d_mrr_cross": -0.01,
    "pearson_rerank": 0.995,
}


def _post(url: str, payload: dict, timeout: float = 300.0) -> object:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def embed_texts(base_url: str, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        chunk = texts[i : i + EMBED_BATCH]
        vecs = _post(f"{base_url}/embed", {"texts": chunk})
        out.extend(vecs)  # type: ignore[arg-type]
    return out


def load_gold() -> list[dict]:
    gold = []
    with GOLD_PATH.open() as f:
        for line in f:
            if line.strip():
                gold.append(json.loads(line))
    return gold


async def load_stored_vectors(
    ids_by_type: dict[str, list[str]],
) -> dict[str, list[float]]:
    """PG vectors stored (historical fp16) for the corpus ids."""
    conn = await asyncpg.connect(PG_DSN)
    stored: dict[str, list[float]] = {}
    try:
        for etype, ids in ids_by_type.items():
            table = TABLES[etype]
            try:
                rows = await conn.fetch(
                    f"SELECT id::text AS id, embedding::text AS emb "
                    f"FROM {table} "  # table from the closed TABLES dict
                    f"WHERE id::text = ANY($1) AND embedding IS NOT NULL",
                    ids,
                )
            except asyncpg.PostgresError as exc:
                print(f"  ! {table}: exclu du mode cross ({exc})")
                continue
            for row in rows:
                stored[row["id"]] = json.loads(row["emb"])
            missing = len(ids) - len(rows)
            if missing:
                print(f"  ! {table}: {missing}/{len(ids)} sans embedding stocké")
    finally:
        await conn.close()
    return stored


def evaluate(
    gold: list[dict],
    query_vecs: dict[str, list[float]],
    corpus_ids: list[str],
    corpus_vecs: list[list[float]],
) -> dict[str, float]:
    results: list[QueryResult] = []
    for q in gold:
        qvec = query_vecs[q["query_id"]]
        ranked = cosine_rank_all(qvec, corpus_vecs, corpus_ids, top_k=50)
        rank = 0
        for idx, (cid, _score) in enumerate(ranked, 1):
            if cid == q["gold_id"]:
                rank = idx
                break
        results.append(
            QueryResult(q["query_id"], q["variant"], q["gold_type"], rank)
        )
    return compute_metrics(results)


def build_rerank_pairs(
    gold: list[dict], corpus: list[tuple[str, str, str]]
) -> list[tuple[str, list[str]]]:
    """Deterministic pairs: (query, [gold text, fixed distractor])."""
    if not corpus:
        return []
    text_by_id = {cid: text for _etype, cid, text in corpus}
    pairs: list[tuple[str, list[str]]] = []
    for i, q in enumerate(gold):
        if len(pairs) >= RERANK_PAIRS:
            break
        gold_text = text_by_id.get(q["gold_id"])
        if not gold_text:
            continue
        distractor = corpus[(i * 7) % len(corpus)][2]
        pairs.append((q["query"], [gold_text[:1000], distractor[:1000]]))
    return pairs


def rerank_scores(base_url: str, pairs: list[tuple[str, list[str]]]) -> list[float]:
    scores: list[float] = []
    for query, candidates in pairs:
        out = _post(
            f"{base_url}/rerank",
            {"query": query, "candidates": candidates},
            timeout=60.0,
        )
        scores.extend(out["scores"])  # type: ignore[index]
    return scores


def pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n == 0 or n != len(b):
        return 0.0
    ma = sum(a) / n
    mb = sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if va == 0.0 or vb == 0.0:
        return 0.0
    return cov / (va * vb)


def compare_with_baseline(candidate: dict, baseline: dict) -> int:
    d_mrr_self = candidate["self"]["mrr"] - baseline["self"]["mrr"]
    d_r10_self = candidate["self"]["recall@10"] - baseline["self"]["recall@10"]
    d_mrr_cross = candidate["cross"]["mrr"] - baseline["cross"]["mrr"]
    corr = pearson(candidate["rerank_scores"], baseline["rerank_scores"])

    checks = [
        ("dMRR_self", d_mrr_self, GATES["d_mrr_self"]),
        ("dRecall@10_self", d_r10_self, GATES["d_recall10_self"]),
        ("dMRR_cross", d_mrr_cross, GATES["d_mrr_cross"]),
        ("pearson_rerank", corr, GATES["pearson_rerank"]),
    ]
    failed = False
    print("\n━━━ Gates vs baseline ━━━")
    for name, value, floor in checks:
        ok = value >= floor
        failed = failed or not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}={value:+.4f} (gate >= {floor})")
    print("VERDICT:", "FAIL — rollback requis" if failed else "PASS — cutover validé")
    return 1 if failed else 0


async def run(args: argparse.Namespace) -> int:
    print(f"Cible : {args.url}")
    corpus = await load_corpus()
    print(f"Corpus stratifié : {len(corpus)} entités")

    present = {cid for _etype, cid, _text in corpus}
    gold_all = load_gold()
    gold = [q for q in gold_all if q["gold_id"] in present]
    print(f"Gold : {len(gold)}/{len(gold_all)} queries gardées (gold_id présent)")
    if args.limit_queries:
        gold = gold[: args.limit_queries]

    print(f"Embedding de {len(gold)} queries via la cible …")
    qvec_list = embed_texts(args.url, [q["query"] for q in gold])
    query_vecs = {
        q["query_id"]: v for q, v in zip(gold, qvec_list, strict=True)
    }

    print("Mode SELF : embedding du corpus via la cible …")
    corpus_ids = [cid for _e, cid, _t in corpus]
    corpus_vecs = embed_texts(args.url, [t for _e, _c, t in corpus])
    self_metrics = evaluate(gold, query_vecs, corpus_ids, corpus_vecs)
    print(f"  self : mrr={self_metrics['mrr']:.4f} r@10={self_metrics['recall@10']:.4f}")

    print("Mode CROSS : vecteurs stockés PG …")
    ids_by_type: dict[str, list[str]] = {}
    for etype, cid, _text in corpus:
        ids_by_type.setdefault(etype, []).append(cid)
    stored = await load_stored_vectors(ids_by_type)
    cross_ids = [cid for cid in corpus_ids if cid in stored]
    cross_vecs = [stored[cid] for cid in cross_ids]
    gold_cross = [q for q in gold if q["gold_id"] in stored]
    print(f"  corpus cross : {len(cross_ids)}/{len(corpus_ids)} — gold : {len(gold_cross)}")
    if len(gold_cross) < MIN_CROSS_GOLD:
        print(
            f"ÉCHANTILLON CROSS INSUFFISANT ({len(gold_cross)} < {MIN_CROSS_GOLD}) "
            "— gate non significatif, investiguer avant cutover"
        )
        return 2
    cross_metrics = evaluate(gold_cross, query_vecs, cross_ids, cross_vecs)
    print(f"  cross : mrr={cross_metrics['mrr']:.4f} r@10={cross_metrics['recall@10']:.4f}")

    print("Parity RERANK …")
    pairs = build_rerank_pairs(gold, corpus)
    scores = rerank_scores(args.url, pairs)
    print(f"  {len(scores)} scores collectés")

    out = {
        "url": args.url,
        "self": self_metrics,
        "cross": cross_metrics,
        "rerank_scores": scores,
        "n_gold_kept": len(gold),
        "n_cross_corpus": len(cross_ids),
    }
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(f"Résultats → {out_path}")

    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text())
        return compare_with_baseline(out, baseline)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--baseline", default=None)
    ap.add_argument("--limit-queries", type=int, default=None)
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Gitignore the artifacts**

Add the following line to `bench/embedding_v1/.gitignore`:

```
cutover/
```

- [ ] **Step 3: Lint gates**

```bash
.venv/bin/ruff check scripts/embedding_cutover_check.py
.venv/bin/ruff format scripts/embedding_cutover_check.py
```

Expected: clean (fix any imports/formatting, NOT the CI gates).

- [ ] **Step 4: Quick smoke test of the script (20 queries, PyTorch prod up)**

```bash
.venv/bin/python scripts/embedding_cutover_check.py \
  --url http://localhost:8003 \
  --output bench/embedding_v1/cutover/smoke.json \
  --limit-queries 20
```

Expected: exit 0, self/cross metrics displayed non-null, 40 rerank scores.

- [ ] **Step 5: Full PyTorch baseline (~915 queries, ~5-10 min)**

```bash
.venv/bin/python scripts/embedding_cutover_check.py \
  --url http://localhost:8003 \
  --output bench/embedding_v1/cutover/baseline_pytorch.json
```

Expected: exit 0. Note `self.mrr` (expected ≈ 0.88-0.93, consistent with report_v1) and `cross.mrr` (≈ self: same fp16 vectors on both sides).

- [ ] **Step 6: Commit**

```bash
git add scripts/embedding_cutover_check.py bench/embedding_v1/.gitignore
git commit -m "feat(bench): script cutover_check — self/cross/rerank-parity + gates vs baseline"
```

---

### Task 6: Production cutover + validation + (rollback if FAIL) — INLINE, no subagent

**Files:** no code changes — docker/validation operations.

**Interfaces:**
- Consumes: images/services from Task 4, baseline from Task 5.
- Produces: GGUF stack in production on :8003, `candidate_gguf.json`, gate verdict.

**ROLLBACK (keep this in view for the whole task):**
```bash
docker compose stop embedding-shim embedding-llama
docker compose --profile legacy up -d embedding
curl -s http://localhost:8003/healthz   # → {"status":"ok"} in ~25s
```

- [ ] **Step 1: Preflight**

```bash
docker ps --filter name=brain_v42_embedding --format '{{.Names}} {{.Status}}'  # legacy healthy
ls -lh /home/hawixs/models/qodo-gguf/qodo-embed-1.5b-q8_0.gguf
test -s bench/embedding_v1/cutover/baseline_pytorch.json && echo baseline-ok
```

- [ ] **Step 2: Cutover**

```bash
docker compose stop embedding          # stop PyTorch (frees the VRAM)
docker compose up -d embedding-llama embedding-shim
# wait for healthy (llama ~10s to load, shim starts after)
for i in $(seq 1 30); do
  curl -s -m 3 http://localhost:8003/healthz | grep -q ok && break; sleep 3
done
curl -s http://localhost:8003/ | python3 -m json.tool   # runtime = llama.cpp-gguf...
```

Expected: `/healthz` 200 in <90s, info `runtime: llama.cpp-gguf-q8_0+onnx-cpu`.

- [ ] **Step 3: Contract smoke test**

```bash
curl -s -X POST http://localhost:8003/embed -H 'Content-Type: application/json' \
  -d '{"texts":["a","b"]}' | python3 -c "import json,sys; v=json.load(sys.stdin); assert len(v)==2 and len(v[0])==1536; print('embed ok')"
curl -s -X POST http://localhost:8003/embed/query -H 'Content-Type: application/json' \
  -d '{"text":"restore"}' | python3 -c "import json,sys; v=json.load(sys.stdin); assert len(v)==1536; print('query ok')"
curl -s -X POST 'http://localhost:8003/embed/single?text=legacy' \
  | python3 -c "import json,sys; assert len(json.load(sys.stdin))==1536; print('legacy param ok')"
curl -s -X POST http://localhost:8003/rerank -H 'Content-Type: application/json' \
  -d '{"query":"restore embedding","candidates":["restaurer le service embedding","tarte aux pommes"]}' \
  | python3 -c "import json,sys; s=json.load(sys.stdin)['scores']; assert s[0]>s[1]; print('rerank ok', s)"
```

Expected: 4× ok. If a smoke test fails → immediate ROLLBACK + report.

- [ ] **Step 4: Full validation + gates**

```bash
.venv/bin/python scripts/embedding_cutover_check.py \
  --url http://localhost:8003 \
  --output bench/embedding_v1/cutover/candidate_gguf.json \
  --baseline bench/embedding_v1/cutover/baseline_pytorch.json
echo "exit=$?"
```

Expected: `VERDICT: PASS`, exit 0. If FAIL → ROLLBACK + report the deltas (do NOT force through).

- [ ] **Step 5: Ecosystem check**

```bash
nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader  # ~2.9-3.2 GiB used expected
docker ps --filter name=brain_v42_embedding --format '{{.Names}} {{.Status}}'
curl -s http://127.0.0.1:8765/health   # MCP HTTP intact (no restart required — URL unchanged)
```

Then via MCP: `brain_search(query="cutover gguf", project_key="brain-v42")` → results with scores (no `degraded` prefix); and a real write (`brain_learn` from Task 7 serves as the write test).

---

### Task 7: Runbook + brain persistence + docs

**Files:**
- Modify: `CLAUDE.md` (Architecture section, "GPU embedding service" line)

**Interfaces:**
- Consumes: results from Task 6.

- [ ] **Step 1: CLAUDE.md** — replace the architecture line:

```markdown
- GPU embedding service (Qodo-Embed-1-1.5B GGUF Q8_0 via llama.cpp + Starlette shim :8003, ONNX CPU reranker — static VRAM ~3 GiB, cutover 2026-07-06)
```

- [ ] **Step 2: Brain** (MCP, executed by the coordinator):
  - `brain_log_decision`: PyTorch→GGUF cutover (context: VRAM drift, measured gates, alternatives: stay on PyTorch + weekly restart / F16 / re-embed the corpus), linked to f0faecfe + 906722df + 410eb227. Explicitly document the SEMANTIC CHANGE: the dev-pc lazy-supervisor's `503 {"error":"gpu_busy"}` path no longer exists — `EmbeddingUnavailable(kind="gpu_busy")` and the client's 5/10/20s long-backoff become harmless dead code, and the metrics sidecar's `gpu_busy_errors` will stay at 0 (not a bug).
  - `brain_create_runbook`: "Operate the GGUF embedding stack (startup, healthcheck, legacy rollback, GGUF rebuild)" — steps: preflight, cutover, smoke, validation, rollback, reproducibility (`scripts/embedding_gguf_build.sh`). Include: (a) during llama's 60s `start_period` at machine boot, the shim's `/healthz` returns 503 → intermittent `healthcheck() = False` on the client/metrics side is EXPECTED noise, not an outage; (b) rollback window: if `scripts/regen_embeddings.py` is running (it reads EMBEDDING_SERVICE_URL, default localhost:8003), interrupt it BEFORE the cutover in either direction.
  - `brain_update_project_focus`: cutover delivered + gate verdict + final VRAM + next step (red-llm can come back; handoff 296dd28f to be settled).

- [ ] **Step 3: Commit + gitnexus**

```bash
git add CLAUDE.md docs/superpowers/plans/2026-07-06-embedding-gguf-cutover.md
git commit -m "docs: architecture GGUF + plan cutover embedding"
npx gitnexus analyze --embeddings   # reindex post-merge (can run in the background)
```

---

## Self-Review (done at write time)

1. **Spec coverage**: legacy contract 7 routes ✓ (Tasks 1-3), GGUF serving ✓ (Task 4), ONNX reranker ✓ (Tasks 2+4), gold validation + gates ✓ (Tasks 5-6), one-command rollback ✓ (Task 4 comment + Task 6 header), GGUF reproducibility ✓ (Task 4), docs/brain ✓ (Task 7).
2. **Placeholders**: no TBD/TODO; all code is complete.
3. **Type consistency**: `LlamaEmbedBackend.embed(list[str]) -> list[list[float]]` consumed as-is by `shim_app`; `OnnxRerankBackend.rerank` sync run via `anyio.to_thread.run_sync`; `main:app` module-level for the Dockerfile's uvicorn CMD; file names `shim_app.py`/`shim_backends.py` consistent across tests, Dockerfile COPY, and imports.
4. **Known watch points** (for reviewers): (a) the gold corpus is truncated to 2000 chars — the 0.989 long-text divergence is mostly covered by cross mode; (b) the Xenova ONNX is a third-party conversion — the Pearson ≥ 0.995 gate validates it against the live PyTorch; (c) `/healthz` changes semantics (probes the upstream) — this is intentional, documented in `shim_app.py`'s docstring.
