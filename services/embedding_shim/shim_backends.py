"""Backends of the brain-v42 embedding shim.

LlamaEmbedBackend — proxies the llama.cpp server (/v1/embeddings, OpenAI
shape). Sorts by index, L2-normalises (defensively — llama-server does
normalise /v1/embeddings by default but exposes NO --embd-normalize flag,
see crashloop fix 8280524; we do not depend on its config), bounds text
size and retries once, shorter, when upstream answers 500 (token context
overrun).

OnnxRerankBackend — ms-marco-MiniLM-L-6-v2 cross-encoder via onnxruntime on
CPU. Lazy-loaded: onnxruntime/tokenizers/numpy are imported on the first call
only — absent from the dev venv, present in the container.
"""

from __future__ import annotations

import math
import threading
from typing import Any

import httpx

# ~5-7k tokens: stays under the llama server's n_ctx=8192. The historical
# client cap is 15000 chars (ADR #7); 20000 leaves room for direct calls
# made outside MCP.
MAX_TEXT_CHARS = 20_000
# Retry after an upstream 500 (token-dense text overflowing despite the
# char guard): at 8000 chars we are still under 4096 tokens.
RETRY_TEXT_CHARS = 8_000


class UpstreamError(Exception):
    """The llama server answered 5xx (after the truncation retry)."""


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


class OnnxRerankBackend:
    """Cross-encoder ms-marco-MiniLM-L-6-v2 via onnxruntime (CPU).

    Returns the RAW logits (no sigmoid) — exact parity with the legacy
    PyTorch service's CrossEncoder.predict (scores observed in production:
    -1.37 relevant / -11.34 not relevant).
    """

    def __init__(self, model_path: str, tokenizer_path: str, max_length: int = 512) -> None:
        self._model_path = model_path
        self._tokenizer_path = tokenizer_path
        self._max_length = max_length
        self._session = None
        self._tokenizer = None
        # rerank() runs in a worker thread: two concurrent first calls can
        # cross the lazy-load without this lock.
        self._lock = threading.Lock()

    def _load(self) -> tuple[Any, Any]:
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
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encodings], dtype=np.int64),
        }
        wanted = {i.name for i in session.get_inputs()}
        feeds = {k: v for k, v in feeds.items() if k in wanted}
        logits = session.run(None, feeds)[0]
        return [float(x) for x in logits.reshape(-1)]
