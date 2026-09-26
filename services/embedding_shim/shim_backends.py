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

import logging
import math
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

_LOGGER = logging.getLogger(__name__)

_RERANK_DEVICES = frozenset({"auto", "cuda", "cpu"})

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


def _resolve_providers(device: str, available: list[str]) -> list[str]:
    """Ordered onnxruntime provider list for the requested device.

    ``cpu`` forces CPUExecutionProvider. ``cuda`` prefers CUDAExecutionProvider
    when the runtime reports it available, else logs a warning and falls back
    to CPU rather than failing session creation. ``auto`` (default) picks CUDA
    when available, CPU otherwise. When CUDA is requested/picked, CPU stays
    listed second so onnxruntime itself can fall back for ops with no CUDA
    kernel.
    """
    if device == "cpu":
        return ["CPUExecutionProvider"]
    if "CUDAExecutionProvider" not in available:
        if device == "cuda":
            _LOGGER.warning("rerank_cuda_requested_unavailable available_providers=%s", available)
        return ["CPUExecutionProvider"]
    return ["CUDAExecutionProvider", "CPUExecutionProvider"]


class OnnxRerankBackend:
    """Cross-encoder ms-marco-MiniLM-L-6-v2 via onnxruntime (GPU with CPU fallback).

    Returns the RAW logits (no sigmoid) — exact parity with the legacy
    PyTorch service's CrossEncoder.predict (scores observed in production:
    -1.37 relevant / -11.34 not relevant).

    Measured 2026-09-26 (85/128 real knowledge-base candidates, same model +
    tokenizer): a single CPU batch takes ~3.4 s, dominated by
    ``tokenizer.enable_padding()`` padding the whole batch to its longest
    candidate (near-always 512 tokens) plus CPU inference on a loaded host.
    Sorting candidates by token length and scoring them in micro-batches of
    ``batch_size`` caps padding within each micro-batch instead of across the
    whole request: ~1.9 s on CPU, ~0.3-0.4 s on CUDA (a single CUDA batch of
    128x512 OOMs the shared 6 GB GPU — a 1.6 GB attention buffer next to the
    llama.cpp embedder's 2.5 GB). CPU vs CUDA scores: max |diff| 0.00012,
    identical top-10.

    CUDA circuit breaker: a shared GPU can be OOM for minutes at a time. Without
    a breaker, every request would still try CUDA first and pay its failure
    latency before falling back to CPU. After a CUDA run fails, the breaker
    opens and routes every request straight to CPU for ``cuda_cooldown_seconds``
    (env ``RERANK_CUDA_COOLDOWN_SECONDS``, default 300; ``0`` disables it) —
    CUDA is not touched again until the cooldown elapses. The first request
    after that retries CUDA; success closes the breaker, failure reopens it
    (refreshing the cooldown, no duplicate log). Open/close transitions are
    each logged exactly once, structured, with no query/candidate payload.
    """

    def __init__(
        self,
        model_path: str,
        tokenizer_path: str,
        max_length: int = 512,
        device: str = "auto",
        batch_size: int = 32,
        cuda_cooldown_seconds: float = 300.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if device not in _RERANK_DEVICES:
            raise ValueError(f"RERANK_DEVICE must be one of {sorted(_RERANK_DEVICES)}: {device!r}")
        if batch_size < 1:
            raise ValueError(f"RERANK_BATCH_SIZE must be >= 1: {batch_size!r}")
        if cuda_cooldown_seconds < 0:
            raise ValueError(
                f"RERANK_CUDA_COOLDOWN_SECONDS must be >= 0: {cuda_cooldown_seconds!r}"
            )
        self._model_path = model_path
        self._tokenizer_path = tokenizer_path
        self._max_length = max_length
        self._device = device
        self._batch_size = batch_size
        self._session = None
        self._tokenizer = None
        self._cpu_session = None
        # rerank() runs in a worker thread: two concurrent first calls can
        # cross the lazy-load without this lock.
        self._lock = threading.Lock()
        self._cuda_cooldown_seconds = cuda_cooldown_seconds
        self._clock = clock or time.monotonic
        # None: breaker closed. Set to the clock reading of the failure that
        # (re)opened it; cleared back to None only on a successful CUDA run.
        self._cuda_open_since: float | None = None

    def _create_session(self, providers: list[str]) -> Any:
        import onnxruntime  # container only (lazy)

        # Needed by onnxruntime-gpu before a CUDA session on some platforms
        # (loads the bundled CUDA/cuDNN wheel DLLs); absent from CPU-only
        # onnxruntime builds. Harmless to call again for a CPU-only session.
        if hasattr(onnxruntime, "preload_dlls"):
            onnxruntime.preload_dlls()

        session = onnxruntime.InferenceSession(self._model_path, providers=providers)
        # onnxruntime's own EPFail fallback would silently retry a failed run
        # on CPU inside session.run() and return scores without ever raising —
        # the explicit retry + breaker in rerank() would then never see the
        # failure, so the breaker would never open and the session could stay
        # pinned to CPU even after the GPU recovers (MAJOR review finding, PR
        # #231). disable_fallback() is the documented onnxruntime 1.30 API for
        # this (onnxruntime.capi.onnxruntime_inference_collection.Session).
        session.disable_fallback()
        return session

    def _load(self) -> tuple[Any, Any]:
        with self._lock:
            if self._session is None:
                import onnxruntime  # container only (lazy)
                from tokenizers import Tokenizer  # container only (lazy)

                providers = _resolve_providers(self._device, onnxruntime.get_available_providers())
                try:
                    self._session = self._create_session(providers)
                except Exception as exc:
                    # CUDA reported available but the session itself failed to
                    # construct (unusable GPU/driver): the run-level retry in
                    # rerank() never gets a chance to act because it never even
                    # gets a CUDA session to try (MAJOR review finding, PR
                    # #231). Treat it exactly like a run failure: open the
                    # breaker and use a CPU session instead.
                    if "CUDAExecutionProvider" not in providers:
                        raise
                    _LOGGER.warning(
                        "rerank_cuda_session_creation_failed_using_cpu error_type=%s",
                        type(exc).__name__,
                    )
                    self._record_cuda_failure()
                    self._cpu_session = self._create_session(["CPUExecutionProvider"])
                    self._session = self._cpu_session
                # Structured, no text payloads (query/candidates never logged here).
                _LOGGER.info(
                    "rerank_provider_loaded requested_device=%s provider=%s",
                    self._device,
                    self._session.get_providers()[0],
                )

                tokenizer = Tokenizer.from_file(self._tokenizer_path)
                tokenizer.enable_truncation(max_length=self._max_length)
                tokenizer.enable_padding()
                self._tokenizer = tokenizer
        return self._session, self._tokenizer

    def _load_cpu_fallback(self) -> Any:
        if self._cpu_session is None:
            self._cpu_session = self._create_session(["CPUExecutionProvider"])
            _LOGGER.info("rerank_cpu_fallback_session_loaded")
        return self._cpu_session

    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        if not candidates:
            return []
        session, tokenizer = self._load()

        if self._cuda_breaker_is_open():
            # Cooling down after a recent CUDA failure: skip CUDA entirely,
            # do not pay its latency on a request that would likely fail too.
            cpu_session = self._load_cpu_fallback()
            return self._score(cpu_session, tokenizer, query, candidates)

        try:
            scores = self._score(session, tokenizer, query, candidates)
        except Exception as exc:
            if session.get_providers()[0] != "CUDAExecutionProvider":
                raise
            _LOGGER.warning("rerank_cuda_run_failed_retrying_cpu error_type=%s", type(exc).__name__)
            self._record_cuda_failure()
            cpu_session = self._load_cpu_fallback()
            return self._score(cpu_session, tokenizer, query, candidates)

        if (
            self._cuda_open_since is not None
            and session.get_providers()[0] == "CUDAExecutionProvider"
        ):
            self._close_cuda_breaker()
        return scores

    def _cuda_breaker_is_open(self) -> bool:
        """Whether CUDA is in cooldown after a recent failure.

        ``cuda_cooldown_seconds == 0`` disables the breaker outright: the
        elapsed-time check below is trivially satisfied, so a failure never
        routes a later request away from CUDA — the pre-breaker behaviour.
        """
        if self._cuda_cooldown_seconds <= 0 or self._cuda_open_since is None:
            return False
        return (self._clock() - self._cuda_open_since) < self._cuda_cooldown_seconds

    def _record_cuda_failure(self) -> None:
        if self._cuda_cooldown_seconds <= 0:
            return
        if self._cuda_open_since is None:
            # Structured, no payload: a cooldown value only, never query or
            # candidate text.
            _LOGGER.warning(
                "rerank_cuda_breaker_open cooldown_seconds=%s", self._cuda_cooldown_seconds
            )
        # Refresh the window even if already open (a post-cooldown retry that
        # fails again): the breaker stays open, this is not a new transition,
        # so no second "open" log.
        self._cuda_open_since = self._clock()

    def _close_cuda_breaker(self) -> None:
        _LOGGER.info("rerank_cuda_breaker_closed")
        self._cuda_open_since = None

    def _score(
        self, session: Any, tokenizer: Any, query: str, candidates: list[str]
    ) -> list[float]:
        import numpy as np  # container only (lazy)

        pairs = [(query, c) for c in candidates]
        # One padded pass to read the real (truncated) token length per
        # candidate off the attention mask. Cheap: it runs no inference, only
        # tokenization. Sorting on it below keeps each scoring micro-batch's
        # own padding close to its true max instead of the whole request's —
        # the dominant cost on CPU (see class docstring).
        length_encodings = tokenizer.encode_batch(pairs)
        lengths = [sum(e.attention_mask) for e in length_encodings]
        order = sorted(range(len(candidates)), key=lambda i: lengths[i])

        scores: list[float] = [0.0] * len(candidates)
        for start in range(0, len(order), self._batch_size):
            batch_idx = order[start : start + self._batch_size]
            batch_pairs = [pairs[i] for i in batch_idx]
            encodings = tokenizer.encode_batch(batch_pairs)
            feeds = {
                "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
                "token_type_ids": np.array([e.type_ids for e in encodings], dtype=np.int64),
            }
            wanted = {i.name for i in session.get_inputs()}
            feeds = {k: v for k, v in feeds.items() if k in wanted}
            logits = session.run(None, feeds)[0]
            for idx, value in zip(batch_idx, logits.reshape(-1), strict=True):
                scores[idx] = float(value)
        return scores
