"""regen_embeddings must embed through the same client as the live path.

The script used to POST raw httpx at ``{service_url}/embed``. That was
harmless while nothing shaped a request — and becomes a silent corpus split
the moment a document prefix or a non-shim backend is configured: the live
write path would prefix its documents while a regen pass rewrote the same rows
unprefixed, leaving two incompatible vector populations in one column.
"""

from __future__ import annotations

import httpx
import pytest
from scripts import regen_embeddings

from brain_v42.config import Settings


class TestEmbedBatchGoesThroughTheClient:
    @pytest.mark.asyncio
    async def test_embed_batch_delegates_to_the_document_path(self) -> None:
        seen: list[list[str]] = []

        class FakeService:
            async def embed_texts(self, texts: list[str]) -> list[list[float]]:
                seen.append(texts)
                return [[1.0] for _ in texts]

        result = await regen_embeddings.embed_batch(FakeService(), ["a", "b"])

        assert result == [[1.0], [1.0]]
        assert seen == [["a", "b"]], "regen must call embed_texts, not raw httpx"


class TestConfiguredPrefixReachesTheRegeneratedVectors:
    @pytest.mark.asyncio
    async def test_a_document_prefix_is_applied_to_regenerated_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point: a regen pass and a live write must agree."""
        monkeypatch.setenv("BRAIN_EMBEDDING_DOCUMENT_PREFIX", "passage: ")
        monkeypatch.setenv(
            "BRAIN_POSTGRES_URL", "postgresql+asyncpg://brain:brain@localhost:5433/brain"
        )
        from brain_v42.config import Settings
        from brain_v42.services.embedding_factory import build_embedding_service

        settings = Settings()  # type: ignore[call-arg]
        service = build_embedding_service(settings)

        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=[[1.0], [1.0]])

        service._client = httpx.AsyncClient(
            base_url="http://any.test", transport=httpx.MockTransport(handler)
        )

        await regen_embeddings.embed_batch(service, ["alpha", "beta"])

        assert b'"passage: alpha"' in seen[0].content
        assert b'"passage: beta"' in seen[0].content


class TestServiceUrlOverrideStillWorks:
    """Settings are injected rather than read from the ambient process.

    Reading them through ``get_settings()`` would make these tests depend on
    the developer's env, the repository ``.env`` (which does carry
    EMBEDDING_SERVICE_URL) and the lru_cache all agreeing — an ordering
    dependency that passes alone and fails in the full suite.
    """

    @staticmethod
    def _settings() -> Settings:
        return Settings(
            postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
            embedding_service_url="http://configured.test",
            _env_file=None,  # type: ignore[call-arg]
        )

    def test_cli_service_url_wins_over_the_configured_endpoint(self) -> None:
        service = regen_embeddings.build_service(
            service_url="http://from-cli.test", settings=self._settings()
        )

        assert service._base_url == "http://from-cli.test"

    def test_without_an_override_the_configured_endpoint_is_used(self) -> None:
        service = regen_embeddings.build_service(service_url=None, settings=self._settings())

        assert service._base_url == "http://configured.test"
