"""Every failure a caller can meet arrives as EmbeddingUnavailable.

That single exception type is the whole degradation contract: brain_service
catches it and falls back to FTS, writes persist with a NULL embedding, and
embedding_backfill catches up later. Anything else escaping the client takes
down the search call itself.

Adding an OpenAI-compatible backend widened the ways an endpoint can answer
badly — a hosted provider rejects a key with 401, rate-limits with 429, and a
local server can answer 200 with an error envelope while a model loads. None
of those existed on the private shim path, and none of them may crash a search.
"""

from __future__ import annotations

import httpx
import pytest

from brain_v42.services.embedding_wire import OpenAIWire
from brain_v42.services.gpu_embedding_service import EmbeddingUnavailable, GPUEmbeddingService


def _service(handler, **kwargs: object) -> GPUEmbeddingService:  # type: ignore[no-untyped-def]
    service = GPUEmbeddingService(base_url="http://embed.test", max_retries=0, **kwargs)  # type: ignore[arg-type]
    service._client = httpx.AsyncClient(
        base_url="http://embed.test", transport=httpx.MockTransport(handler)
    )
    return service


class TestClientErrorsDegradeInsteadOfCrashing:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
    @pytest.mark.asyncio
    async def test_a_4xx_is_reported_as_unavailable(self, status: int) -> None:
        service = _service(lambda request: httpx.Response(status, json={"error": "nope"}))

        with pytest.raises(EmbeddingUnavailable) as caught:
            await service.embed_query("q")
        assert caught.value.kind == "other"

    @pytest.mark.asyncio
    async def test_the_shim_path_degrades_too(self) -> None:
        """Not an openai-only guarantee — the default backend answers the same."""
        service = _service(lambda request: httpx.Response(404))

        with pytest.raises(EmbeddingUnavailable):
            await service.embed("a document")


class TestUnparseablePayloadsDegradeInsteadOfCrashing:
    """A 200 whose body the wire rejects used to escape as a bare ValueError."""

    @pytest.mark.asyncio
    async def test_error_envelope_behind_http_200(self) -> None:
        service = _service(
            lambda request: httpx.Response(200, json={"error": {"message": "loading model"}}),
            wire=OpenAIWire(model="m"),
        )

        with pytest.raises(EmbeddingUnavailable):
            await service.embed_query("q")

    @pytest.mark.asyncio
    async def test_base64_embeddings_instead_of_floats(self) -> None:
        service = _service(
            lambda request: httpx.Response(
                200, json={"data": [{"index": 0, "embedding": "eyJhIjogMX0="}]}
            ),
            wire=OpenAIWire(model="m"),
        )

        with pytest.raises(EmbeddingUnavailable):
            await service.embed_query("q")

    @pytest.mark.asyncio
    async def test_short_batch_result_set(self) -> None:
        service = _service(
            lambda request: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]}),
            wire=OpenAIWire(model="m"),
        )

        with pytest.raises(EmbeddingUnavailable):
            await service.embed_texts(["a", "b"])

    @pytest.mark.asyncio
    async def test_an_empty_vector_is_refused(self) -> None:
        service = _service(
            lambda request: httpx.Response(200, json={"data": [{"index": 0, "embedding": []}]}),
            wire=OpenAIWire(model="m"),
        )

        with pytest.raises(EmbeddingUnavailable):
            await service.embed_query("q")


class TestHealthyResponsesStillWork:
    @pytest.mark.asyncio
    async def test_a_well_formed_openai_response_is_returned(self) -> None:
        service = _service(
            lambda request: httpx.Response(
                200, json={"data": [{"index": 0, "embedding": [3.0, 4.0]}]}
            ),
            wire=OpenAIWire(model="m"),
        )

        assert await service.embed_query("q") == [0.6, 0.8]
