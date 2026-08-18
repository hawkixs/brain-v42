"""Wire shapes for embedding backends.

A wire owns two things and nothing else: how a request body is shaped, and how
a response payload is parsed. Retry, backoff, the 503 ``gpu_busy`` handling and
the :class:`EmbeddingUnavailable` contract live in ``GPUEmbeddingService``, so
adding a backend here inherits the graceful degradation the rest of the system
depends on — ``brain_search`` falling back to FTS, writes persisting with a
NULL embedding — without reimplementing any of it.

Two wires ship:

``ShimWire``
    The private three-route contract served by the bundled reference stack
    (``services/``). This is the default and reproduces today's bytes exactly.

``OpenAIWire``
    ``POST /v1/embeddings``, the shape spoken by Ollama, vLLM, llama.cpp
    server, LM Studio, TEI, Jina, Mistral, Voyage and OpenAI itself.
"""

from __future__ import annotations

import math
from typing import Any, Protocol

# The reference shim bounds text before it reaches the model (n_ctx=8192 on the
# llama server). Talking straight to a provider takes the shim out of the
# request path, so the bound has to travel with the wire or it silently
# disappears for exactly the third-party installs that need it most.
MAX_TEXT_CHARS = 20_000


class EmbeddingWire(Protocol):
    """Request shaping and response parsing for one embedding wire format."""

    health_path: str

    def batch_request(self, texts: list[str]) -> tuple[str, dict[str, Any]]:
        """Return the (path, json body) that embeds ``texts``."""
        ...

    def single_request(self, text: str) -> tuple[str, dict[str, Any]]:
        """Return the (path, json body) that embeds one ``text``."""
        ...

    def parse_batch(self, payload: Any, expected: int) -> list[list[float]]:
        """Parse a batch response into one vector per input, in input order."""
        ...

    def parse_single(self, payload: Any) -> list[float]:
        """Parse a single-text response into one vector."""
        ...


class ShimWire:
    """The private contract: POST /embed and POST /embed/query.

    The shim answers bare arrays and already L2-normalises server-side, so
    parsing is a passthrough.
    """

    health_path = "/healthz"

    def batch_request(self, texts: list[str]) -> tuple[str, dict[str, Any]]:
        return "/embed", {"texts": texts}

    def single_request(self, text: str) -> tuple[str, dict[str, Any]]:
        return "/embed/query", {"text": text}

    def parse_batch(self, payload: Any, expected: int) -> list[list[float]]:
        return payload  # type: ignore[no-any-return]

    def parse_single(self, payload: Any) -> list[float]:
        return payload  # type: ignore[no-any-return]


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


class OpenAIWire:
    """POST /v1/embeddings, the OpenAI-compatible embeddings shape.

    Parsing is deliberately stricter than ``sorted(data, key=index)``. Sorting
    alone accepts a short result set, a duplicated index or a missing one, and
    the caller would then zip vectors against the wrong rows — a corruption
    that looks like valid data. Every result set is checked to carry exactly
    the indices ``0..n-1`` before anything is returned.
    """

    def __init__(
        self,
        model: str,
        *,
        max_text_chars: int = MAX_TEXT_CHARS,
        health_path: str = "/v1/models",
    ) -> None:
        self._model = model
        self._max_text_chars = max_text_chars
        self.health_path = health_path

    def _body(self, texts: list[str]) -> dict[str, Any]:
        return {
            "model": self._model,
            # encoding_format is explicit because several providers default to
            # base64 and would otherwise hand pgvector a string.
            "input": [t[: self._max_text_chars] for t in texts],
            "encoding_format": "float",
        }

    def batch_request(self, texts: list[str]) -> tuple[str, dict[str, Any]]:
        return "/v1/embeddings", self._body(texts)

    def single_request(self, text: str) -> tuple[str, dict[str, Any]]:
        return "/v1/embeddings", self._body([text])

    def parse_batch(self, payload: Any, expected: int) -> list[list[float]]:
        try:
            data = payload["data"]
        except (TypeError, KeyError) as exc:
            raise ValueError("embedding response has no 'data' array") from exc

        if len(data) != expected:
            raise ValueError(
                f"embedding response returned {len(data)} vectors, expected {expected}"
            )

        by_index: dict[int, list[float]] = {}
        for item in data:
            try:
                index = int(item["index"])
                embedding = item["embedding"]
            except (TypeError, KeyError, ValueError) as exc:
                raise ValueError("embedding response item is malformed") from exc
            by_index[index] = _validate_vector(embedding)

        if set(by_index) != set(range(expected)):
            raise ValueError(
                f"embedding response carries indices {sorted(by_index)}, expected 0..{expected - 1}"
            )
        return [_l2_normalize(by_index[i]) for i in range(expected)]

    def parse_single(self, payload: Any) -> list[float]:
        return self.parse_batch(payload, expected=1)[0]


def _validate_vector(embedding: Any) -> list[float]:
    """Refuse anything pgvector could not store, at the boundary."""
    if isinstance(embedding, str) or not isinstance(embedding, list):
        raise ValueError(f"embedding is not a float array (got {type(embedding).__name__})")
    out: list[float] = []
    for value in embedding:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("embedding contains a non-numeric value")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("embedding contains a non-finite value")
        out.append(number)
    return out
