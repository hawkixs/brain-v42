"""One construction path for the embedding client (TDD Red phase)."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from brain_v42.config import Settings
from brain_v42.services.embedding_factory import build_embedding_service, build_reranker_client
from brain_v42.services.embedding_wire import OpenAIWire, ShimWire
from brain_v42.services.rerank_wire import CohereRerankWire, ShimRerankWire

DSN = "postgresql+asyncpg://brain:brain@localhost:5433/brain"


def _settings(**kwargs: object) -> Settings:
    return Settings(postgres_url=DSN, _env_file=None, **kwargs)  # type: ignore[call-arg]


class TestBackendSelection:
    def test_default_settings_build_the_shim_wire(self) -> None:
        service = build_embedding_service(_settings())
        assert isinstance(service._wire, ShimWire)

    def test_openai_backend_builds_the_openai_wire_with_the_configured_model(self) -> None:
        service = build_embedding_service(
            _settings(embedding_backend="openai", embedding_model="nomic-embed-text")
        )
        assert isinstance(service._wire, OpenAIWire)
        assert service._wire._model == "nomic-embed-text"


class TestSettingsReachTheClient:
    def test_prefixes_are_wired_through(self) -> None:
        service = build_embedding_service(
            _settings(embedding_query_prefix="query: ", embedding_document_prefix="passage: ")
        )
        assert service._query_prefix == "query: "
        assert service._document_prefix == "passage: "

    def test_url_and_timeout_are_wired_through(self) -> None:
        service = build_embedding_service(
            _settings(embedding_service_url="http://ollama.test:11434", embedding_timeout=7.5)
        )
        assert service._base_url == "http://ollama.test:11434"
        assert service._timeout == 7.5


class TestApiKeyHeader:
    def test_no_authorization_header_when_the_key_is_empty(self) -> None:
        service = build_embedding_service(_settings())
        assert "authorization" not in service._get_client().headers

    def test_bearer_header_is_set_when_a_key_is_configured(self) -> None:
        service = build_embedding_service(
            _settings(embedding_api_key=SecretStr("sk-test-key"), embedding_backend="openai")
        )
        assert service._get_client().headers["authorization"] == "Bearer sk-test-key"


class TestRerankerBackendSelection:
    def test_default_settings_build_the_shim_rerank_wire(self) -> None:
        assert isinstance(build_reranker_client(_settings())._wire, ShimRerankWire)

    def test_cohere_backend_builds_the_cohere_wire(self) -> None:
        client = build_reranker_client(
            _settings(rerank_backend="cohere", rerank_model="rerank-english-v3.0")
        )
        assert isinstance(client._wire, CohereRerankWire)
        assert client._wire._model == "rerank-english-v3.0"

    def test_reranker_url_and_timeout_are_wired_through(self) -> None:
        client = build_reranker_client(
            _settings(reranker_url="http://tei.test:8080", reranker_timeout=3.0)
        )
        assert client._base_url == "http://tei.test:8080"
        assert client._timeout == 3.0


class TestTheBuiltClientActuallySpeaksToAnOpenAIEndpoint:
    @pytest.mark.asyncio
    async def test_end_to_end_against_a_mocked_openai_endpoint(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [3.0, 4.0]}], "model": "m"},
            )

        service = build_embedding_service(
            _settings(embedding_backend="openai", embedding_query_prefix="query: ")
        )
        service._client = httpx.AsyncClient(
            base_url="http://any.test", transport=httpx.MockTransport(handler)
        )

        assert await service.embed_query("wombat") == [0.6, 0.8]
        assert seen[0].url.path == "/v1/embeddings"
        assert b'"query: wombat"' in seen[0].content
