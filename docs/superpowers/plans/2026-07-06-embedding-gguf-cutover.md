# Embedding GGUF Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remplacer le serving embedding PyTorch fp16 (drift VRAM structurel, 4.9/6 GiB) par llama.cpp GGUF Q8_0 (allocation statique, 2.9 GiB, 2× plus rapide) + shim Starlette conservant le contrat legacy port 8003 + reranker ONNX CPU, validé par le gold bench v1 contre une baseline PyTorch re-mesurée, avec rollback en une commande.

**Architecture:** Deux nouveaux services compose : `embedding-llama` (image `ghcr.io/ggml-org/llama.cpp:server-cuda`, GGUF Q8_0 bind-mounté, `--embedding --pooling last`, API OpenAI `/v1/embeddings`, réseau interne) et `embedding-shim` (Starlette + uvicorn, publie 8003, traduit le contrat legacy `/embed` `/embed/query` `/embed/single` vers llama, exécute `/rerank` localement via onnxruntime CPU sur ms-marco-MiniLM-L-6-v2 pré-exporté par Xenova). Le service legacy `embedding` (PyTorch) passe sous `profiles: ["legacy"]` — rollback instantané. Les clients (`GPUEmbeddingService`, `RerankerClient`, MCP, metrics sidecar) ne changent PAS : même URL `http://localhost:8003`, mêmes shapes de réponse.

**Tech Stack:** Starlette (dep directe du repo — PAS FastAPI), httpx, anyio, uvicorn, onnxruntime + tokenizers (container uniquement, lazy-imports), llama.cpp server-cuda, gold bench v1 existant (`bench/embedding_v1/`).

## Global Constraints

- ruff `==0.15.18` : `ruff check src/ tests/ scripts/` ET `ruff format --check src/ tests/ scripts/` doivent passer (le CI lance les deux ; `services/` n'est PAS linté par le CI mais on écrit propre quand même).
- `mypy src/` doit rester clean (le shim vit dans `services/`, hors scope mypy — ne rien ajouter dans `src/`).
- `pytest tests/unit/ ` 100% vert ; coverage CI ≥ 60% (le shim testé via `tests/unit/test_embedding_shim.py` compte dans le run mais pas dans `--cov=brain_v42`).
- AUCUNE nouvelle dépendance dans `pyproject.toml` : les tests du shim n'utilisent que starlette/httpx/anyio/pytest (déjà présents). `onnxruntime`/`tokenizers`/`numpy` sont lazy-importés (container only) — le test qui exerce le chemin numpy utilise `pytest.importorskip`.
- Contrat API legacy à préserver À L'IDENTIQUE (source de vérité : `services/embedding/main.py` v2.0.0) :
  - `POST /embed {"texts": [...]}` → `[[float,...],...]` brut (liste vide → `[]`)
  - `POST /embed/query` et `POST /embed/single` : body JSON `{"text": "..."}` prioritaire, `?text=` legacy accepté, ni l'un ni l'autre → 400 ; réponse `[float,...]` brut
  - `POST /rerank {"query": str, "candidates": [...]}` → `{"scores": [float,...]}` — **logits bruts** (pas de sigmoid ; parity avec `CrossEncoder.predict`, vérifié en prod : scores -1.37/-11.34)
  - `GET /healthz` → `{"status":"ok"}` (200) ; `GET /health` → `{"status":"ok","model":...}` (compat `RerankerClient.is_available`) ; `GET /` → info JSON
- Vecteurs L2-normalisés côté serveur (les clients n'en font pas).
- llama.cpp : `--pooling last` OBLIGATOIRE (config sentence-transformers du modèle : `pooling_mode_lasttoken: true`), `-ub 1024` (levier VRAM n°1, sweep 2026-07-06 : 2841 MiB vs 4683 à ub=4096, latences identiques ; le chunking ub<n_tokens est correct pour un embedder CAUSAL Qwen2), `-c 8192` (couvre le cap client 15000 chars ≈ 4-5k tokens).
- GGUF canonique : `/home/hawixs/models/qodo-gguf/qodo-embed-1.5b-q8_0.gguf` (1.6 GiB, produit le 2026-07-06, cosine 0.9998 vs PyTorch).
- Gates de validation (vs baseline PyTorch re-mesurée le jour même, même harnais) : ΔMRR_self ≥ −0.01, Δrecall@10_self ≥ −0.005, ΔMRR_cross ≥ −0.01, Pearson rerank ≥ 0.995. FAIL → rollback, pas de forçage.
- Commits atomiques Conventional Commits ; jamais de commit sur main (branche `feat/embedding-gguf-cutover`).
- Le cutover (Task 6) est une opération prod : exécutée INLINE par le coordinateur (pas par un subagent), avec le rollback affiché avant d'agir.

## File Structure

| Fichier | Rôle |
|---|---|
| `services/embedding_shim/shim_backends.py` | Créer — `LlamaEmbedBackend` (proxy /v1/embeddings, tri par index, L2-norm, garde de troncature + retry), `OnnxRerankBackend` (cross-encoder onnxruntime CPU, lazy-load), `UpstreamError` |
| `services/embedding_shim/shim_app.py` | Créer — factory Starlette `create_app(embed_backend, rerank_backend)`, 7 routes legacy |
| `services/embedding_shim/main.py` | Créer — wiring env (`LLAMA_URL`, `ONNX_DIR`) + entrypoint uvicorn |
| `services/embedding_shim/Dockerfile` | Créer — python:3.12-slim + deps + download ONNX Xenova au build |
| `tests/unit/test_embedding_shim.py` | Créer — tests TDD backends + app (fakes + MockTransport) |
| `docker-compose.yml` | Modifier — `embedding` → `profiles: ["legacy"]` ; ajouter `embedding-llama` + `embedding-shim` |
| `scripts/embedding_cutover_check.py` | Créer — bench self/cross/rerank-parity vs baseline, gates PASS/FAIL |
| `scripts/embedding_gguf_build.sh` | Créer — reproductibilité de la conversion GGUF (download → convert F16 → quantize Q8_0) |
| `bench/embedding_v1/.gitignore` | Modifier — ignorer `cutover/` (artefacts de runs) |

---

### Task 1: LlamaEmbedBackend (proxy /v1/embeddings)

**Files:**
- Create: `services/embedding_shim/shim_backends.py`
- Test: `tests/unit/test_embedding_shim.py`

**Interfaces:**
- Produces: `LlamaEmbedBackend(base_url: str, timeout: float = 60.0, transport: httpx.AsyncBaseTransport | None = None)` avec `async embed(texts: list[str]) -> list[list[float]]`, `async healthy() -> bool`, `async close() -> None` ; exception `UpstreamError` ; constantes `MAX_TEXT_CHARS = 20_000`, `RETRY_TEXT_CHARS = 8_000`. Consommé par Task 3 (app) et Task 4 (container).

- [ ] **Step 1: Écrire les tests qui échouent**

Créer `tests/unit/test_embedding_shim.py` :

```python
"""Tests du shim embedding (services/embedding_shim/) — backends + app.

Le shim n'est pas un package installé : on l'importe via sys.path.
Contrat de référence : services/embedding/main.py v2.0.0 (PyTorch legacy).
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
    """Réponse /v1/embeddings au format OpenAI (index volontairement mélangés)."""
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
        # Réponse dans le désordre + vecteurs non normalisés
        return httpx.Response(
            200, json=_openai_payload({1: [0.0, 2.0], 0: [3.0, 4.0]})
        )

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
```

- [ ] **Step 2: Vérifier l'échec**

Run: `.venv/bin/python -m pytest tests/unit/test_embedding_shim.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'shim_backends'`

- [ ] **Step 3: Implémentation minimale**

Créer `services/embedding_shim/shim_backends.py` :

```python
"""Backends du shim embedding brain-v42.

LlamaEmbedBackend — proxy vers llama.cpp server (/v1/embeddings, shape
OpenAI). Trie par index, L2-normalise (défensif — llama normalise déjà
avec --embd-normalize par défaut), borne la taille des textes et retente
une fois plus court si l'upstream 500 (dépassement de contexte tokens).

OnnxRerankBackend — cross-encoder ms-marco-MiniLM-L-6-v2 via onnxruntime
CPU (Task 2). Lazy-load : onnxruntime/tokenizers/numpy ne sont importés
qu'au premier appel — absents du venv de dev, présents dans le container.
"""

from __future__ import annotations

import math
import threading

import httpx

# ~5-7k tokens : garde sous n_ctx=8192 du serveur llama. Le cap client
# historique est 15000 chars (ADR #7) ; 20000 laisse de la marge aux
# appels directs hors MCP.
MAX_TEXT_CHARS = 20_000
# Retry après un 500 upstream (texte token-dense qui déborde malgré la
# garde en chars) : à 8000 chars on est toujours < 4096 tokens.
RETRY_TEXT_CHARS = 8_000


class UpstreamError(Exception):
    """Le serveur llama a répondu 5xx (après retry de troncature)."""


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


class LlamaEmbedBackend:
    """Client async du /v1/embeddings de llama.cpp server."""

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

- [ ] **Step 4: Vérifier que ça passe**

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

### Task 2: OnnxRerankBackend (cross-encoder CPU)

**Files:**
- Modify: `services/embedding_shim/shim_backends.py` (append)
- Test: `tests/unit/test_embedding_shim.py` (append)

**Interfaces:**
- Produces: `OnnxRerankBackend(model_path: str, tokenizer_path: str, max_length: int = 512)` avec `rerank(query: str, candidates: list[str]) -> list[float]` (SYNC — l'app l'exécute via `anyio.to_thread.run_sync`). Consommé par Task 3.

- [ ] **Step 1: Tests qui échouent** (append à `tests/unit/test_embedding_shim.py`)

```python
from shim_backends import OnnxRerankBackend  # noqa: E402


def test_rerank_empty_candidates_short_circuits(tmp_path):
    # Chemins volontairement inexistants : si le lazy-load se déclenche
    # sur candidates=[], le test explose — c'est le comportement testé.
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
```

- [ ] **Step 2: Vérifier l'échec**

Run: `.venv/bin/python -m pytest tests/unit/test_embedding_shim.py -v -k rerank`
Expected: FAIL — `ImportError: cannot import name 'OnnxRerankBackend'`

- [ ] **Step 3: Implémentation** (append à `shim_backends.py`)

```python
class OnnxRerankBackend:
    """Cross-encoder ms-marco-MiniLM-L-6-v2 via onnxruntime (CPU).

    Retourne les logits BRUTS (pas de sigmoid) — parity exacte avec
    CrossEncoder.predict du service PyTorch legacy (scores observés en
    prod : -1.37 pertinent / -11.34 non pertinent).
    """

    def __init__(
        self, model_path: str, tokenizer_path: str, max_length: int = 512
    ) -> None:
        self._model_path = model_path
        self._tokenizer_path = tokenizer_path
        self._max_length = max_length
        self._session = None
        self._tokenizer = None
        # rerank() tourne dans le threadpool anyio : deux premiers appels
        # concurrents peuvent croiser le lazy-load sans ce lock.
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

- [ ] **Step 4: Vérifier que ça passe**

Run: `.venv/bin/python -m pytest tests/unit/test_embedding_shim.py -v`
Expected: 8 PASS (ou 7 PASS + 1 SKIP si numpy absent du venv)

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check services/embedding_shim/ tests/unit/test_embedding_shim.py
.venv/bin/ruff format services/embedding_shim/ tests/unit/test_embedding_shim.py
git add -u && git commit -m "feat(shim): OnnxRerankBackend — cross-encoder onnxruntime CPU (TDD)"
```

---

### Task 3: App Starlette (contrat legacy) + wiring

**Files:**
- Create: `services/embedding_shim/shim_app.py`
- Create: `services/embedding_shim/main.py`
- Test: `tests/unit/test_embedding_shim.py` (append)

**Interfaces:**
- Consumes: `LlamaEmbedBackend.embed/healthy` (Task 1), `OnnxRerankBackend.rerank` (Task 2).
- Produces: `create_app(embed_backend, rerank_backend) -> Starlette` ; `main.py` expose `app` module-level pour `uvicorn main:app` (Task 4). Env : `LLAMA_URL` (défaut `http://embedding-llama:8080`), `ONNX_DIR` (défaut `/app/onnx`).

- [ ] **Step 1: Tests qui échouent** (append à `tests/unit/test_embedding_shim.py`)

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

- [ ] **Step 2: Vérifier l'échec**

Run: `.venv/bin/python -m pytest tests/unit/test_embedding_shim.py -v -k app`
Expected: FAIL — `ModuleNotFoundError: No module named 'shim_app'`

- [ ] **Step 3: Implémentation**

Créer `services/embedding_shim/shim_app.py` :

```python
"""App Starlette du shim — contrat legacy embedding brain-v42 (port 8003).

Parity exacte avec services/embedding/main.py v2.0.0 (PyTorch) :
  POST /embed         {"texts": [...]}          -> [[float,...],...]
  POST /embed/query   {"text": "..."} | ?text=  -> [float,...]
  POST /embed/single  idem /embed/query         -> [float,...]
  POST /rerank        {"query","candidates"}    -> {"scores": [...]}
  GET  /              -> info modèles/runtime
  GET  /healthz       -> 200 si upstream llama healthy, sinon 503
  GET  /health        -> 200 (compat RerankerClient.is_available)

Différence assumée : /healthz sonde l'upstream (l'ancien /healthz ne
touchait jamais le GPU — c'est le bug du false-green de l'incident
2026-04-12, learning 410eb227 ; ici on corrige).
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
    except Exception:  # body vide/non-JSON = cas legacy (?text=)
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

Créer `services/embedding_shim/main.py` :

```python
"""Entrypoint du shim — wiring env + uvicorn.

Env:
  LLAMA_URL  URL du serveur llama.cpp (défaut http://embedding-llama:8080)
  ONNX_DIR   dossier contenant model.onnx + tokenizer.json (défaut /app/onnx)
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

- [ ] **Step 4: Vérifier que ça passe**

Run: `.venv/bin/python -m pytest tests/unit/test_embedding_shim.py -v`
Expected: 23 PASS (ou 22 + 1 SKIP numpy)

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check services/embedding_shim/ tests/unit/test_embedding_shim.py
.venv/bin/ruff format services/embedding_shim/ tests/unit/test_embedding_shim.py
.venv/bin/python -m pytest tests/unit/ -q
git add services/embedding_shim/ tests/unit/test_embedding_shim.py
git commit -m "feat(shim): app Starlette contrat legacy 8003 + wiring env"
```

---

### Task 4: Dockerfile shim + services compose + script de reproductibilité GGUF

**Files:**
- Create: `services/embedding_shim/Dockerfile`
- Create: `scripts/embedding_gguf_build.sh`
- Modify: `docker-compose.yml` (service `embedding` lignes 32-62 ; ajouter 2 services)

**Interfaces:**
- Consumes: `main:app` (Task 3), GGUF `/home/hawixs/models/qodo-gguf/qodo-embed-1.5b-q8_0.gguf`.
- Produces: services compose `embedding-llama` (interne, `http://embedding-llama:8080`) et `embedding-shim` (host `8003:8003`) ; profil `legacy` pour l'ancien service. Consommé par Tasks 5-6.

- [ ] **Step 1: Dockerfile**

Créer `services/embedding_shim/Dockerfile` :

```dockerfile
# ============================================================
# Dockerfile — Brain v42 Embedding Shim
#
# Traduit le contrat legacy (:8003 /embed /embed/query /rerank)
# vers llama.cpp server (/v1/embeddings) + exécute le reranker
# cross-encoder en ONNX CPU (pas de PyTorch : image ~600 MB au
# lieu de ~8 GB, RAM ~300 MB au lieu de 12.8 GB).
#
# L'ONNX pré-exporté vient de Xenova/ms-marco-MiniLM-L-6-v2
# (même modèle que cross-encoder/ms-marco-MiniLM-L-6-v2) — la
# parity des scores est validée par scripts/embedding_cutover_check.py
# (gate Pearson >= 0.995 vs le service PyTorch).
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

# Modèle ONNX + tokenizer téléchargés au build (démarrage sans réseau).
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

- [ ] **Step 2: Script de reproductibilité GGUF**

Créer `scripts/embedding_gguf_build.sh` (chmod +x) :

```bash
#!/usr/bin/env bash
# Reproduit le GGUF Q8_0 de Qodo-Embed-1-1.5B (produit initialement le
# 2026-07-06). Sortie : $OUT_DIR/qodo-embed-1.5b-{f16,q8_0}.gguf
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

- [ ] **Step 3: Compose — legacy en profile + 2 nouveaux services**

Dans `docker-compose.yml`, modifier le service `embedding` (ajouter `profiles` juste sous `container_name`) :

```yaml
  embedding:
    build: ./services/embedding
    container_name: brain_v42_embedding
    # PyTorch fp16 legacy — remplacé par embedding-llama + embedding-shim
    # (cutover 2026-07-06, drift VRAM structurel — learning 410eb227).
    # ROLLBACK : docker compose stop embedding-shim embedding-llama
    #            && docker compose --profile legacy up -d embedding
    profiles: ["legacy"]
    restart: unless-stopped
```

(le reste du service `embedding` est inchangé)

Ajouter après le service `embedding` :

```yaml
  embedding-llama:
    image: ghcr.io/ggml-org/llama.cpp:server-cuda
    container_name: brain_v42_embedding_llama
    restart: unless-stopped
    # --pooling last : config sentence-transformers du modèle (lasttoken).
    # -ub 1024 : levier VRAM n°1 (2.9 GiB vs 4.7 à ub=4096, latences
    # identiques — sweep 2026-07-06, learning 906722df). Le chunking
    # ub < n_tokens est correct pour un embedder causal (Qwen2).
    # -c 8192 : couvre le cap client 15000 chars (~4-5k tokens).
    # -np 1 : verrouillé — le KV cache est PAR SLOT ; -np N sur ce GPU
    # 6 GiB implique de re-mesurer la VRAM (N × ctx 8192).
    # --cont-batching + --embd-normalize 2 (L2) : explicites pour ne pas
    # dépendre des défauts de l'image (le shim re-normalise aussi,
    # défense en profondeur).
    # Allocation VRAM statique au démarrage → pas de drift (le fix
    # durable de l'incident 2026-04-12).
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
      # curl présent dans l'image (vérifié 2026-07-06). /health de
      # llama-server retourne 503 tant que le modèle charge, 200 ensuite.
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
      # Healthcheck réel de bout en bout : POST /embed traverse le shim
      # ET le serveur llama (même philosophie que le healthcheck legacy
      # post-incident 410eb227 — jamais de /healthz-only false-green).
      test: ["CMD", "python3", "-c", "import urllib.request,json; r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8003/embed',data=json.dumps({'texts':['healthcheck']}).encode(),headers={'Content-Type':'application/json'}),timeout=15); assert r.read(2)==b'[['"]
      interval: 60s
      timeout: 20s
      retries: 5
      start_period: 60s
```

Note : `embedding-llama` reste sur le réseau `default` uniquement (pas de port host) ; `embedding-shim` rejoint `hawkixs-infra` comme le legacy (parity pour les consommateurs inter-containers).

- [ ] **Step 4: Vérifications statiques + droits GGUF**

```bash
sudo chown -R hawixs:hawixs /home/hawixs/models/qodo-gguf 2>/dev/null || chown -R hawixs:hawixs /home/hawixs/models/qodo-gguf || true
ls -lh /home/hawixs/models/qodo-gguf/qodo-embed-1.5b-q8_0.gguf   # doit exister, 1.6G
docker compose config embedding-llama embedding-shim > /dev/null && echo "compose OK"
docker compose --profile legacy config embedding > /dev/null && echo "profile legacy OK"
```

Expected: `compose OK` + `profile legacy OK`

- [ ] **Step 5: Build de l'image shim (pas de démarrage)**

```bash
docker compose build embedding-shim
```

Expected: build vert, download ONNX ~90 MB au build.

- [ ] **Step 6: Commit**

```bash
chmod +x scripts/embedding_gguf_build.sh
git add services/embedding_shim/Dockerfile scripts/embedding_gguf_build.sh docker-compose.yml
git commit -m "feat(deploy): embedding-llama (GGUF Q8) + embedding-shim, PyTorch legacy en profile"
```

---

### Task 5: Script de validation cutover + baseline PyTorch

**Files:**
- Create: `scripts/embedding_cutover_check.py`
- Modify: `bench/embedding_v1/.gitignore` (ajouter la ligne `cutover/`)

**Interfaces:**
- Consumes: `bench/embedding_v1/gen_gold.py` (QUERIES, SAMPLE_SIZES, SEED), `bench/embedding_v1/run_bench.py` (`load_corpus`, `compute_metrics`, `cosine_rank_all`, `QueryResult`, `_norm`), gold `bench/embedding_v1/gold_v1.jsonl`, PG `localhost:5433`.
- Produces: CLI `python scripts/embedding_cutover_check.py --url URL --output FILE [--baseline FILE] [--limit-queries N]` — exit 0 si gates PASS (ou pas de baseline), exit 1 si FAIL. JSON de sortie : `{url, self: {...métriques}, cross: {...}, rerank_scores: [...], n_gold_kept, n_cross_corpus}`.

- [ ] **Step 1: Écrire le script**

Créer `scripts/embedding_cutover_check.py` :

```python
"""Validation du cutover embedding — gold bench v1 contre une baseline.

Trois mesures sur l'endpoint cible (contrat natif /embed, /rerank) :

  self   — corpus ET queries embeddés par la cible (qualité du modèle
           servi, harnais identique à bench/embedding_v1/run_bench.py).
  cross  — corpus = vecteurs STOCKÉS en PG (embeddés par le PyTorch
           fp16 historique), queries embeddées par la cible. C'est le
           scénario réel post-cutover : requêtes GGUF contre un corpus
           fp16 non ré-embeddé.
  rerank — scores /rerank sur des paires déterministes (parity ONNX vs
           CrossEncoder PyTorch).

Usage :
  # baseline (PyTorch encore en prod)
  python scripts/embedding_cutover_check.py \
      --url http://localhost:8003 \
      --output bench/embedding_v1/cutover/baseline_pytorch.json

  # post-cutover (shim en place) + gates
  python scripts/embedding_cutover_check.py \
      --url http://localhost:8003 \
      --output bench/embedding_v1/cutover/candidate_gguf.json \
      --baseline bench/embedding_v1/cutover/baseline_pytorch.json

Gates (candidate vs baseline) : dMRR_self >= -0.01,
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

# etype (gen_gold) -> table PG. Les tables sans colonne embedding sont
# exclues du mode cross à l'exécution (log explicite, pas de cap silencieux).
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
# En dessous de ce nombre de queries gold résolvables en mode cross, le
# gate dMRR_cross n'est pas statistiquement significatif → exit 2 (ni
# PASS ni FAIL : échantillon insuffisant, à investiguer avant cutover).
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
    """Vecteurs PG stockés (fp16 historique) pour les ids du corpus."""
    conn = await asyncpg.connect(PG_DSN)
    stored: dict[str, list[float]] = {}
    try:
        for etype, ids in ids_by_type.items():
            table = TABLES[etype]
            try:
                rows = await conn.fetch(
                    f"SELECT id::text AS id, embedding::text AS emb "
                    f"FROM {table} "  # table depuis le dict fermé TABLES
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
    """Paires déterministes : (query, [texte gold, distracteur fixe])."""
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

- [ ] **Step 2: Gitignore des artefacts**

Ajouter à `bench/embedding_v1/.gitignore` la ligne :

```
cutover/
```

- [ ] **Step 3: Gates lint**

```bash
.venv/bin/ruff check scripts/embedding_cutover_check.py
.venv/bin/ruff format scripts/embedding_cutover_check.py
```

Expected: clean (corriger les éventuels imports/format, PAS les gates du CI).

- [ ] **Step 4: Smoke rapide du script (20 queries, prod PyTorch up)**

```bash
.venv/bin/python scripts/embedding_cutover_check.py \
  --url http://localhost:8003 \
  --output bench/embedding_v1/cutover/smoke.json \
  --limit-queries 20
```

Expected: exit 0, métriques self/cross affichées non nulles, 40 scores rerank.

- [ ] **Step 5: Baseline complète PyTorch (~915 queries, ~5-10 min)**

```bash
.venv/bin/python scripts/embedding_cutover_check.py \
  --url http://localhost:8003 \
  --output bench/embedding_v1/cutover/baseline_pytorch.json
```

Expected: exit 0. Noter `self.mrr` (attendu ≈ 0.88-0.93, cohérent report_v1) et `cross.mrr` (≈ self : mêmes vecteurs fp16 des deux côtés).

- [ ] **Step 6: Commit**

```bash
git add scripts/embedding_cutover_check.py bench/embedding_v1/.gitignore
git commit -m "feat(bench): script cutover_check — self/cross/rerank-parity + gates vs baseline"
```

---

### Task 6: Cutover prod + validation + (rollback si FAIL) — INLINE, pas de subagent

**Files:** aucun changement de code — opérations docker/validation.

**Interfaces:**
- Consumes: images/services de Task 4, baseline de Task 5.
- Produces: stack GGUF en prod sur :8003, `candidate_gguf.json`, verdict gates.

**ROLLBACK (à garder sous les yeux pendant toute la task) :**
```bash
docker compose stop embedding-shim embedding-llama
docker compose --profile legacy up -d embedding
curl -s http://localhost:8003/healthz   # → {"status":"ok"} en ~25s
```

- [ ] **Step 1: Prévol**

```bash
docker ps --filter name=brain_v42_embedding --format '{{.Names}} {{.Status}}'  # legacy healthy
ls -lh /home/hawixs/models/qodo-gguf/qodo-embed-1.5b-q8_0.gguf
test -s bench/embedding_v1/cutover/baseline_pytorch.json && echo baseline-ok
```

- [ ] **Step 2: Bascule**

```bash
docker compose stop embedding          # stoppe le PyTorch (libère la VRAM)
docker compose up -d embedding-llama embedding-shim
# attendre le healthy (llama ~10s de load, shim démarre après)
for i in $(seq 1 30); do
  curl -s -m 3 http://localhost:8003/healthz | grep -q ok && break; sleep 3
done
curl -s http://localhost:8003/ | python3 -m json.tool   # runtime = llama.cpp-gguf...
```

Expected: `/healthz` 200 en <90s, info `runtime: llama.cpp-gguf-q8_0+onnx-cpu`.

- [ ] **Step 3: Smoke contrat**

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

Expected: 4× ok. Si un smoke échoue → ROLLBACK immédiat + rapport.

- [ ] **Step 4: Validation complète + gates**

```bash
.venv/bin/python scripts/embedding_cutover_check.py \
  --url http://localhost:8003 \
  --output bench/embedding_v1/cutover/candidate_gguf.json \
  --baseline bench/embedding_v1/cutover/baseline_pytorch.json
echo "exit=$?"
```

Expected: `VERDICT: PASS`, exit 0. Si FAIL → ROLLBACK + rapport des deltas (ne PAS forcer).

- [ ] **Step 5: Vérification écosystème**

```bash
nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader  # ~2.9-3.2 GiB used attendu
docker ps --filter name=brain_v42_embedding --format '{{.Names}} {{.Status}}'
curl -s http://127.0.0.1:8765/health   # MCP HTTP intact (aucun restart requis — URL inchangée)
```

Puis via MCP : `brain_search(query="cutover gguf", project_key="brain-v42")` → résultats avec scores (pas de préfixe `degraded`) ; et une écriture réelle (`brain_learn` de la Task 7 fait office de test write).

---

### Task 7: Runbook + persistance brain + docs

**Files:**
- Modify: `CLAUDE.md` (section Architecture, ligne « GPU embedding service »)

**Interfaces:**
- Consumes: résultats Task 6.

- [ ] **Step 1: CLAUDE.md** — remplacer la ligne architecture :

```markdown
- GPU embedding service (Qodo-Embed-1-1.5B GGUF Q8_0 via llama.cpp + shim Starlette :8003, reranker ONNX CPU — VRAM statique ~3 GiB, cutover 2026-07-06)
```

- [ ] **Step 2: Brain** (MCP, exécuté par le coordinateur) :
  - `brain_log_decision` : cutover PyTorch→GGUF (contexte drift VRAM, gates mesurés, alternatives : rester PyTorch + restart hebdo / F16 / re-embed corpus), lié à f0faecfe + 906722df + 410eb227. Documenter explicitement le CHANGEMENT DE SÉMANTIQUE : la voie `503 {"error":"gpu_busy"}` du lazy-supervisor dev-pc n'existe plus — `EmbeddingUnavailable(kind="gpu_busy")` et le long-backoff 5/10/20s du client deviennent du dead code inoffensif, et `gpu_busy_errors` du metrics sidecar restera à 0 (pas un bug).
  - `brain_create_runbook` : « Opérer le stack embedding GGUF (démarrage, healthcheck, rollback legacy, re-build GGUF) » — étapes : prévol, bascule, smoke, validation, rollback, reproductibilité (`scripts/embedding_gguf_build.sh`). Inclure : (a) pendant le `start_period` 60s de llama au boot machine, `/healthz` du shim rend 503 → `healthcheck() = False` intermittents côté client/metrics = bruit ATTENDU, pas une panne ; (b) fenêtre rollback : si `scripts/regen_embeddings.py` tourne (il lit EMBEDDING_SERVICE_URL, défaut localhost:8003), l'interrompre AVANT la bascule dans un sens ou dans l'autre.
  - `brain_update_project_focus` : cutover livré + verdict gates + VRAM finale + prochaine étape (red-llm peut revenir ; handoff 296dd28f à trancher).

- [ ] **Step 3: Commit + gitnexus**

```bash
git add CLAUDE.md docs/superpowers/plans/2026-07-06-embedding-gguf-cutover.md
git commit -m "docs: architecture GGUF + plan cutover embedding"
npx gitnexus analyze --embeddings   # reindex post-merge (peut tourner en fond)
```

---

## Self-Review (fait à l'écriture)

1. **Couverture spec** : contrat legacy 7 routes ✓ (Tasks 1-3), serving GGUF ✓ (Task 4), reranker ONNX ✓ (Tasks 2+4), validation gold + gates ✓ (Tasks 5-6), rollback une commande ✓ (Task 4 comment + Task 6 header), reproductibilité GGUF ✓ (Task 4), docs/brain ✓ (Task 7).
2. **Placeholders** : aucun TBD/TODO ; tout le code est complet.
3. **Cohérence de types** : `LlamaEmbedBackend.embed(list[str]) -> list[list[float]]` consommé tel quel par `shim_app` ; `OnnxRerankBackend.rerank` sync exécuté via `anyio.to_thread.run_sync` ; `main:app` module-level pour le CMD uvicorn du Dockerfile ; noms de fichiers `shim_app.py`/`shim_backends.py` cohérents entre tests, Dockerfile COPY et imports.
4. **Points de vigilance connus** (pour les reviewers) : (a) le corpus gold est tronqué à 2000 chars — la divergence long-texte 0.989 est surtout couverte par le mode cross ; (b) l'ONNX Xenova est une conversion tierce — le gate Pearson ≥ 0.995 la valide contre le PyTorch vivant ; (c) `/healthz` change de sémantique (sonde l'upstream) — c'est voulu, documenté dans le docstring de `shim_app.py`.
