"""Query/document intent and instruction prefixes (TDD Red phase).

``/embed/query`` is a misnomer: it is the SINGLE-TEXT route, not the query
route. Measured on the tree, ~14 of the 20 ``embed()`` call sites write
DOCUMENTS (learning, decision, ADR, snippet, runbook, feature, dedup) and only
6 issue queries. So ``embed()`` keeps its callers and MEANS document; query
intent gets its own method.

The asymmetry is the whole argument: a document embedded with the wrong prefix
is persisted and costs a full ``regen_embeddings.py`` pass to undo, while a
query embedded with the wrong prefix is a transient retrieval bug fixed in one
line. Any call site a future contributor forgets to convert lands on the
repairable side.
"""

from __future__ import annotations

import httpx
import pytest

from brain_v42.services.gpu_embedding_service import GPUEmbeddingService

VEC = [0.5, 0.5, 0.5, 0.5]


def _service(**kwargs: object) -> tuple[GPUEmbeddingService, list[httpx.Request]]:
    """A service whose transport records every outgoing request."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        batch = '"texts"' in request.content.decode()
        return httpx.Response(200, json=[VEC] if batch else VEC)

    service = GPUEmbeddingService(base_url="http://embed.test", **kwargs)  # type: ignore[arg-type]
    service._client = httpx.AsyncClient(
        base_url="http://embed.test", transport=httpx.MockTransport(handler)
    )
    return service, seen


class TestEmptyPrefixesKeepProductionByteIdentical:
    """The split must ship as a provable no-op."""

    @pytest.mark.asyncio
    async def test_embed_posts_exactly_the_body_it_posts_today(self) -> None:
        service, seen = _service()
        await service.embed("hello")
        assert seen[0].url.path == "/embed/query"
        assert seen[0].content == b'{"text":"hello"}'

    @pytest.mark.asyncio
    async def test_embed_texts_posts_exactly_the_body_it_posts_today(self) -> None:
        service, seen = _service()
        await service.embed_texts(["a", "b"])
        assert seen[0].url.path == "/embed"
        assert seen[0].content == b'{"texts":["a","b"]}'

    @pytest.mark.asyncio
    async def test_embed_query_with_no_prefix_is_indistinguishable_from_embed(self) -> None:
        service, seen = _service()
        await service.embed_query("hello")
        assert seen[0].url.path == "/embed/query"
        assert seen[0].content == b'{"text":"hello"}'


class TestPrefixesAreAppliedInsideTheClient:
    """A call site can forget an intent; it can never forget a prefix string."""

    @pytest.mark.asyncio
    async def test_embed_query_applies_the_query_prefix(self) -> None:
        service, seen = _service(query_prefix="query: ", document_prefix="passage: ")
        await service.embed_query("wombat")
        assert seen[0].content == b'{"text":"query: wombat"}'

    @pytest.mark.asyncio
    async def test_embed_applies_the_document_prefix(self) -> None:
        service, seen = _service(query_prefix="query: ", document_prefix="passage: ")
        await service.embed("wombat")
        assert seen[0].content == b'{"text":"passage: wombat"}'

    @pytest.mark.asyncio
    async def test_embed_texts_applies_the_document_prefix_to_every_text(self) -> None:
        service, seen = _service(document_prefix="passage: ")
        await service.embed_texts(["a", "b"])
        assert seen[0].content == b'{"texts":["passage: a","passage: b"]}'

    @pytest.mark.asyncio
    async def test_prefix_is_exact_concatenation_with_no_injected_separator(self) -> None:
        """The operator owns the trailing space; the client must not add one."""
        service, seen = _service(query_prefix="QUERY:")
        await service.embed_query("wombat")
        assert seen[0].content == b'{"text":"QUERY:wombat"}'

    @pytest.mark.asyncio
    async def test_empty_batch_still_short_circuits_without_a_request(self) -> None:
        service, seen = _service(document_prefix="passage: ")
        assert await service.embed_texts([]) == []
        assert seen == []


class TestInstrumentedWrapperKeepsUpWithTheInterface:
    """metrics/instrument.py wraps the service with hand-written passthroughs.

    A method missing there raises AttributeError ONLY when metrics_enabled=true
    — that is, only in production. This test fails for any future method too,
    not just embed_query.
    """

    def test_wrapper_exposes_every_public_method_of_the_service(self) -> None:
        from brain_v42.metrics.instrument import InstrumentedEmbeddingService

        expected = {
            name
            for name in vars(GPUEmbeddingService)
            if not name.startswith("_") and callable(getattr(GPUEmbeddingService, name))
        }
        missing = expected - set(dir(InstrumentedEmbeddingService))
        assert not missing, f"InstrumentedEmbeddingService is missing {sorted(missing)}"

    @pytest.mark.asyncio
    async def test_wrapper_embed_query_delegates_and_records_one_request(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from brain_v42.metrics.instrument import InstrumentedEmbeddingService

        inner = MagicMock()
        inner.embed_query = AsyncMock(return_value=VEC)
        collector = MagicMock()

        wrapped = InstrumentedEmbeddingService(inner, collector)
        assert await wrapped.embed_query("q") == VEC

        inner.embed_query.assert_awaited_once_with("q")
        collector.record_embedding_request.assert_called_once()
