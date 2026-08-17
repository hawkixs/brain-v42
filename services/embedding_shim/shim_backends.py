"""Backends du shim embedding brain-v42.

LlamaEmbedBackend — proxy vers llama.cpp server (/v1/embeddings, shape
OpenAI). Trie par index, L2-normalise (défensif — llama-server normalise
/v1/embeddings par défaut mais n'expose PAS de flag --embd-normalize,
cf. crashloop fix 8280524 ; on ne dépend pas de sa config), borne la
taille des textes et retente une fois plus court si l'upstream 500
(dépassement de contexte tokens).

OnnxRerankBackend — cross-encoder ms-marco-MiniLM-L-6-v2 via onnxruntime
CPU. Lazy-load : onnxruntime/tokenizers/numpy ne sont importés qu'au
premier appel — absents du venv de dev, présents dans le container.
"""

from __future__ import annotations

import math
import threading
from typing import Any

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


class OnnxRerankBackend:
    """Cross-encoder ms-marco-MiniLM-L-6-v2 via onnxruntime (CPU).

    Retourne les logits BRUTS (pas de sigmoid) — parity exacte avec
    CrossEncoder.predict du service PyTorch legacy (scores observés en
    prod : -1.37 pertinent / -11.34 non pertinent).
    """

    def __init__(self, model_path: str, tokenizer_path: str, max_length: int = 512) -> None:
        self._model_path = model_path
        self._tokenizer_path = tokenizer_path
        self._max_length = max_length
        self._session = None
        self._tokenizer = None
        # rerank() tourne dans un worker thread : deux premiers appels
        # concurrents peuvent croiser le lazy-load sans ce lock.
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
