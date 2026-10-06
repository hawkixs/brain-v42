"""One construction path for the embedding client (TDD Red phase)."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from brain_v42.config import Settings
from brain_v42.services.embedding_factory import build_embedding_service, build_reranker_client
from brain_v42.services.embedding_wire import OpenAIWire, ShimWire
from brain_v42.services.rerank_wire import CohereRerankWire, ShimRerankWire


def test_reranker_budget_and_observer_are_wired_through() -> None:
    from unittest.mock import MagicMock

    observer = MagicMock()
    client = build_reranker_client(_settings(rerank_budget_seconds=0.75), observer=observer)
    assert client._budget_seconds == 0.75
    assert client._observer is observer


def test_reranker_budget_setting_alias_and_positive_validation(monkeypatch) -> None:
    from pydantic import ValidationError

    monkeypatch.setenv("BRAIN_RERANK_BUDGET_SECONDS", "0.75")
    assert _settings().rerank_budget_seconds == 0.75
    monkeypatch.setenv("BRAIN_RERANK_BUDGET_SECONDS", "0")
    with pytest.raises(ValidationError):
        _settings()


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


# ---------------------------------------------------------------------------
# Hosted reranker: key file, routing, probe path
# ---------------------------------------------------------------------------

_RERANK_KEY = "sk-or-SENTINEL-DO-NOT-LEAK"


def _key_file(tmp_path, content: str = _RERANK_KEY, mode: int = 0o600):
    path = tmp_path / "rerank.key"
    path.write_text(content + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _hosted(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "rerank_backend": "cohere",
        "rerank_model": "voyageai/rerank-3-lite",
        "reranker_url": "https://openrouter.ai/api",
        "rerank_health_path": "/v1/key",
        "rerank_provider": {"only": ["voyageai"]},
    }
    values.update(overrides)
    return _settings(**values)


class TestHostedRerankWiring:
    def test_probe_path_and_routing_reach_the_wire(self) -> None:
        wire = build_reranker_client(_hosted())._wire
        assert wire.health_path == "/v1/key"
        _, body = wire.request("q", ["a"])
        assert body["provider"] == {
            "only": ["voyageai"],
            "allow_fallbacks": False,
            "data_collection": "deny",
        }

    def test_no_routing_means_no_provider_key(self) -> None:
        wire = build_reranker_client(
            _settings(rerank_backend="cohere", rerank_model="bge", reranker_url="http://tei.test")
        )._wire
        _, body = wire.request("q", ["a"])
        assert "provider" not in body
        assert wire.health_path == "/health"


class TestRerankBearer:
    def test_the_key_file_wins_over_a_shim_bearer_file(self, tmp_path) -> None:
        shim_token = tmp_path / "shim.token"
        shim_token.write_text("shim-bearer\n", encoding="utf-8")
        client = build_reranker_client(
            _hosted(brain_embedding_token_file=shim_token, rerank_api_key_file=_key_file(tmp_path))
        )
        assert client._get_client().headers["authorization"] == f"Bearer {_RERANK_KEY}"

    def test_an_inline_key_never_collides_with_the_shim_bearer_file(self, tmp_path) -> None:
        shim_token = tmp_path / "shim.token"
        shim_token.write_text("shim-bearer\n", encoding="utf-8")
        client = build_reranker_client(
            _hosted(
                reranker_url="http://tei.test:8080",
                rerank_provider=None,
                brain_embedding_token_file=shim_token,
                rerank_api_key=SecretStr("inline"),
            )
        )
        assert client._get_client().headers["authorization"] == "Bearer inline"

    def test_a_group_readable_key_file_is_refused_without_leaking_it(self, tmp_path) -> None:
        from brain_v42.services.embedding_factory import RerankKeyError

        path = _key_file(tmp_path, mode=0o644)
        with pytest.raises(RerankKeyError) as excinfo:
            build_reranker_client(_hosted(rerank_api_key_file=path))
        assert str(path) in str(excinfo.value)
        assert "0600" in str(excinfo.value)
        assert _RERANK_KEY not in str(excinfo.value)

    def test_an_empty_key_file_is_refused(self, tmp_path) -> None:
        from brain_v42.services.embedding_factory import RerankKeyError

        with pytest.raises(RerankKeyError, match="empty"):
            build_reranker_client(_hosted(rerank_api_key_file=_key_file(tmp_path, content="  ")))

    def test_a_missing_key_file_is_refused(self, tmp_path) -> None:
        from brain_v42.services.embedding_factory import RerankKeyError

        missing = tmp_path / "absent.key"
        with pytest.raises(RerankKeyError, match=str(missing)):
            build_reranker_client(_hosted(rerank_api_key_file=missing))

    def test_the_key_stays_out_of_repr_and_logs(self, tmp_path) -> None:
        from structlog.testing import capture_logs

        settings = _hosted(rerank_api_key_file=_key_file(tmp_path))
        with capture_logs() as records:
            client = build_reranker_client(settings)
            client._get_client()
        assert _RERANK_KEY not in repr(settings)
        assert _RERANK_KEY not in repr(records)

    def test_the_shim_bearer_resolution_is_unchanged(self, tmp_path) -> None:
        from brain_v42.services.embedding_factory import EmbeddingBearerError

        shim_token = tmp_path / "shim.token"
        shim_token.write_text("shim-bearer\n", encoding="utf-8")
        client = build_reranker_client(_settings(brain_embedding_token_file=shim_token))
        assert client._get_client().headers["authorization"] == "Bearer shim-bearer"
        with pytest.raises(EmbeddingBearerError):
            build_reranker_client(
                _settings(brain_embedding_token_file=shim_token, rerank_api_key=SecretStr("k"))
            )


class TestKeyFileHoldsOnlyTheKey:
    """The whole stripped content becomes a bearer sent to a third party: a file
    that is really an env fragment must never be forwarded wholesale."""

    @pytest.mark.parametrize(
        "content",
        [
            f"{_RERANK_KEY}\nOTHER_SECRET=hunter2",
            f"{_RERANK_KEY} trailing words",
            "k" * 513,
        ],
    )
    def test_anything_but_a_single_token_is_refused_without_leaking_it(
        self, tmp_path, content: str
    ) -> None:
        from brain_v42.services.embedding_factory import RerankKeyError

        path = _key_file(tmp_path, content=content)
        with pytest.raises(RerankKeyError) as excinfo:
            build_reranker_client(_hosted(rerank_api_key_file=path))
        assert str(path) in str(excinfo.value)
        assert "must hold only the key value" in str(excinfo.value)
        for leaked in (_RERANK_KEY, "hunter2", content):
            assert leaked not in str(excinfo.value)

    def test_a_key_at_the_length_limit_is_accepted(self, tmp_path) -> None:
        client = build_reranker_client(
            _hosted(rerank_api_key_file=_key_file(tmp_path, content="k" * 512))
        )
        assert client._get_client().headers["authorization"] == "Bearer " + "k" * 512


class TestKeyFileMayBeADotenvFile:
    """Ticket 1b9ba559: the key lives in a private file read BY NAME. Every other
    line of such a file is somebody else's secret and goes nowhere."""

    @staticmethod
    def _bearer(tmp_path, content: str) -> str:
        client = build_reranker_client(
            _hosted(rerank_api_key_file=_key_file(tmp_path, content=content))
        )
        return client._get_client().headers["authorization"]

    def test_a_value_only_file_still_works(self, tmp_path) -> None:
        assert self._bearer(tmp_path, _RERANK_KEY) == f"Bearer {_RERANK_KEY}"

    def test_only_the_named_line_is_used(self, tmp_path) -> None:
        from structlog.testing import capture_logs

        content = f"# private\nOTHER_SECRET=sentinel-other\n\nBRAIN_RERANK_API_KEY={_RERANK_KEY}\n"
        with capture_logs() as records:
            header = self._bearer(tmp_path, content)
        assert header == f"Bearer {_RERANK_KEY}"
        assert "sentinel-other" not in header
        assert "sentinel-other" not in repr(records)

    @pytest.mark.parametrize(
        "line",
        [
            f'BRAIN_RERANK_API_KEY="{_RERANK_KEY}"',
            f"BRAIN_RERANK_API_KEY='{_RERANK_KEY}'",
            f"export BRAIN_RERANK_API_KEY={_RERANK_KEY}",
        ],
    )
    def test_quotes_and_export_prefix_are_accepted(self, tmp_path, line: str) -> None:
        assert self._bearer(tmp_path, f"OTHER=x\n{line}") == f"Bearer {_RERANK_KEY}"

    @pytest.mark.parametrize(
        "content",
        [
            "OTHER_SECRET=sentinel-other",
            f"BRAIN_RERANK_API_KEY={_RERANK_KEY}\nBRAIN_RERANK_API_KEY=second-sentinel",
        ],
    )
    def test_zero_or_two_named_lines_are_refused(self, tmp_path, content: str) -> None:
        from brain_v42.services.embedding_factory import RerankKeyError

        path = _key_file(tmp_path, content=content)
        with pytest.raises(RerankKeyError) as excinfo:
            build_reranker_client(_hosted(rerank_api_key_file=path))
        message = str(excinfo.value)
        assert str(path) in message
        assert "BRAIN_RERANK_API_KEY" in message
        for leaked in (_RERANK_KEY, "sentinel-other", "second-sentinel"):
            assert leaked not in message

    @pytest.mark.parametrize(
        "content",
        [
            f"BRAIN_RERANK_API_KEY={_RERANK_KEY} extra words",
            "BRAIN_RERANK_API_KEY=",
            "BRAIN_RERANK_API_KEY=a=b\nOTHER=sentinel-other",
            f"BRAIN_RERANK_API_KEY={_RERANK_KEY}\nbare words sentinel-other",
        ],
    )
    def test_a_malformed_file_is_refused_without_leaking(self, tmp_path, content: str) -> None:
        from brain_v42.services.embedding_factory import RerankKeyError

        with pytest.raises(RerankKeyError) as excinfo:
            build_reranker_client(_hosted(rerank_api_key_file=_key_file(tmp_path, content=content)))
        message = str(excinfo.value)
        assert "BRAIN_RERANK_API_KEY=<value>" in message or "key value" in message
        for leaked in (_RERANK_KEY, "sentinel-other"):
            assert leaked not in message
