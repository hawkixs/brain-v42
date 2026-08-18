"""Wire shapes for reranker backends.

Same split as ``embedding_wire``: the wire shapes the request and parses the
response, the client owns the HTTP.

Both downstream consumers constrain what a wire may return:

``BatchingRerankerClient``
    Coalesces candidates from up to six concurrent callers into one list and
    slices the returned scores back by offset. Scores MUST come back in input
    order. Its length check would not catch a reordering.

``HybridReranker``
    Applies ``1/(1+exp(-s))`` to every score, because the reference
    cross-encoder answers raw logits. A wire whose provider already returns a
    normalised probability must hand back the logit, so that sigmoid returns
    the provider's own number rather than squashing it into [0.5, 0.73].
"""

from __future__ import annotations

import math
from typing import Any, Protocol

# logit(p) is infinite at the extremes. Clamping at this epsilon keeps a
# saturated provider score finite (about ±16 in logit space) so sorting and
# min_score comparisons stay total.
_PROBABILITY_EPSILON = 1e-7


class RerankWire(Protocol):
    """Request shaping and response parsing for one rerank wire format."""

    def request(self, query: str, candidates: list[str]) -> tuple[str, dict[str, Any]]:
        """Return the (path, json body) that scores ``candidates``."""
        ...

    def parse(self, payload: Any, expected: int) -> list[float]:
        """Parse a response into one score per candidate, in input order."""
        ...


class ShimRerankWire:
    """The private contract: POST /rerank -> {"scores": [...]}.

    The reference cross-encoder answers raw logits in candidate order, which
    is exactly what HybridReranker expects, so parsing is a passthrough.
    """

    def request(self, query: str, candidates: list[str]) -> tuple[str, dict[str, Any]]:
        return "/rerank", {"query": query, "candidates": candidates}

    def parse(self, payload: Any, expected: int) -> list[float]:
        return payload["scores"]  # type: ignore[no-any-return]


class CohereRerankWire:
    """POST /v1/rerank, the Cohere-style shape implemented by TEI, Jina and vLLM.

    Results arrive sorted by relevance and are remapped to input order here —
    never by position, always by the reported ``index``.
    """

    def __init__(self, model: str) -> None:
        self._model = model

    def request(self, query: str, candidates: list[str]) -> tuple[str, dict[str, Any]]:
        return "/v1/rerank", {
            "model": self._model,
            "query": query,
            "documents": candidates,
            # Without an explicit top_n most providers truncate to a default,
            # and the caller would be handed fewer scores than candidates.
            "top_n": len(candidates),
        }

    def parse(self, payload: Any, expected: int) -> list[float]:
        try:
            results = payload["results"]
        except (TypeError, KeyError) as exc:
            raise ValueError("rerank response has no 'results' array") from exc

        if len(results) != expected:
            raise ValueError(f"rerank response returned {len(results)} scores, expected {expected}")

        by_index: dict[int, float] = {}
        for item in results:
            try:
                index = int(item["index"])
                score = float(item["relevance_score"])
            except (TypeError, KeyError, ValueError) as exc:
                raise ValueError("rerank response item is malformed") from exc
            if not math.isfinite(score):
                raise ValueError("rerank response carries a non-finite score")
            by_index[index] = score

        if set(by_index) != set(range(expected)):
            raise ValueError(
                f"rerank response carries indices {sorted(by_index)}, expected 0..{expected - 1}"
            )
        return [_to_logit(by_index[i]) for i in range(expected)]


def _to_logit(probability: float) -> float:
    """Invert the sigmoid HybridReranker is about to apply."""
    clamped = min(max(probability, _PROBABILITY_EPSILON), 1.0 - _PROBABILITY_EPSILON)
    return math.log(clamped / (1.0 - clamped))
