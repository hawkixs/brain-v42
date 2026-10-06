"""Hosted reranker settings: defaults, key sources and the enforced routing policy.

The policy is enforced and not merely checked for presence: a routing object
that allows fallbacks or another provider would let a request leave for a
vendor nobody approved, with every field "set".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

DSN = "postgresql+asyncpg://brain:brain@localhost:5433/brain"
OPENROUTER = "https://openrouter.ai/api"
MODEL = "voyageai/rerank-3-lite"
SENTINEL = "sk-or-SENTINEL-DO-NOT-LEAK"

_RERANK_ENV = (
    "RERANK_BACKEND",
    "RERANK_MODEL",
    "RERANK_API_KEY",
    "RERANK_API_KEY_FILE",
    "RERANK_HEALTH_PATH",
    "RERANK_PROVIDER",
    "RERANK_PROBE_INTERVAL_SECONDS",
    "RERANKER_URL",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _RERANK_ENV:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"BRAIN_{name}", raising=False)


def _settings(**kwargs: object):  # type: ignore[no-untyped-def]
    from brain_v42.config import Settings

    return Settings(postgres_url=DSN, _env_file=None, **kwargs)  # type: ignore[call-arg]


def _openrouter(**overrides: object):  # type: ignore[no-untyped-def]
    values: dict[str, object] = {
        "rerank_backend": "cohere",
        "rerank_model": MODEL,
        "reranker_url": OPENROUTER,
        "rerank_provider": {"only": ["voyageai"]},
    }
    values.update(overrides)
    return _settings(**values)


class TestDefaultsChangeNothing:
    def test_new_settings_default_to_the_shim_deployment(self) -> None:
        settings = _settings()
        assert settings.rerank_health_path == "/health"
        assert settings.rerank_api_key_file is None
        assert settings.rerank_provider is None
        assert settings.rerank_probe_interval_seconds == 300.0
        assert settings.reranker_url == "http://localhost:8003"

    def test_probe_interval_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            _settings(rerank_probe_interval_seconds=0)

    def test_provider_reads_json_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRAIN_RERANK_PROVIDER", '{"only": ["voyageai"]}')
        provider = _settings().rerank_provider
        assert provider is not None
        assert provider.only == ["voyageai"]
        assert provider.allow_fallbacks is False
        assert provider.data_collection == "deny"
        assert provider.zdr is None

    def test_provider_forbids_unknown_keys_and_an_empty_only(self) -> None:
        with pytest.raises(ValidationError):
            _settings(rerank_provider={"only": ["voyageai"], "order": ["x"]})
        with pytest.raises(ValidationError):
            _settings(rerank_provider={"only": []})


class TestOpenRouterPolicyIsEnforced:
    def test_the_approved_routing_is_accepted(self) -> None:
        provider = _openrouter().rerank_provider
        assert provider is not None
        assert provider.model_dump(exclude_none=True) == {
            "only": ["voyageai"],
            "allow_fallbacks": False,
            "data_collection": "deny",
        }

    def test_zdr_alone_permits_data_collection_allow(self) -> None:
        settings = _openrouter(
            rerank_provider={"only": ["voyageai"], "data_collection": "allow", "zdr": True}
        )
        assert settings.rerank_provider is not None

    def test_a_subdomain_of_openrouter_is_covered(self) -> None:
        with pytest.raises(ValidationError, match="rerank_provider"):
            _openrouter(reranker_url="https://eu.openrouter.ai/api", rerank_provider=None)

    def test_a_lookalike_host_is_not_openrouter(self) -> None:
        settings = _openrouter(reranker_url="https://notopenrouter.ai", rerank_provider=None)
        assert settings.rerank_provider is None

    def test_missing_routing_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="rerank_provider"):
            _openrouter(rerank_provider=None)

    def test_only_must_name_the_models_author(self) -> None:
        with pytest.raises(ValidationError, match="rerank_provider.only"):
            _openrouter(rerank_provider={"only": ["cohere"]})

    def test_only_must_hold_exactly_one_slug(self) -> None:
        with pytest.raises(ValidationError, match="rerank_provider.only"):
            _openrouter(rerank_provider={"only": ["voyageai", "x"]})

    def test_a_model_without_an_author_prefix_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="rerank_model"):
            _openrouter(rerank_model="rerank-3-lite")

    def test_fallbacks_are_refused(self) -> None:
        with pytest.raises(ValidationError, match="allow_fallbacks"):
            _openrouter(rerank_provider={"only": ["voyageai"], "allow_fallbacks": True})

    def test_data_collection_allow_without_zdr_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="data_collection"):
            _openrouter(rerank_provider={"only": ["voyageai"], "data_collection": "allow"})

    def test_a_self_hosted_cohere_endpoint_needs_no_routing(self) -> None:
        settings = _settings(
            rerank_backend="cohere", rerank_model="bge", reranker_url="http://tei.test:8080"
        )
        assert settings.rerank_provider is None

    def test_the_shim_backend_is_not_subject_to_the_policy(self) -> None:
        assert _settings(reranker_url=OPENROUTER).rerank_provider is None


class TestKeySources:
    def test_both_sources_are_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="rerank_api_key_file"):
            _settings(
                rerank_backend="cohere",
                rerank_api_key=SENTINEL,
                rerank_api_key_file=tmp_path / "key",
            )

    def test_the_error_and_repr_never_carry_the_key(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _settings(
                rerank_backend="cohere",
                rerank_api_key=SENTINEL,
                rerank_api_key_file=tmp_path / "key",
            )
        assert SENTINEL not in str(excinfo.value)
        assert SENTINEL not in repr(_settings(rerank_api_key=SENTINEL))

    def test_a_key_file_with_the_shim_backend_is_refused(self, tmp_path: Path) -> None:
        """The shim authenticates with brain_embedding_token_file; a key file
        there would be silently ignored, and the operator would think it armed."""
        with pytest.raises(ValidationError, match="rerank_api_key_file"):
            _settings(rerank_api_key_file=tmp_path / "key")

    def test_a_key_file_alone_is_accepted(self, tmp_path: Path) -> None:
        settings = _settings(rerank_backend="cohere", rerank_api_key_file=tmp_path / "key")
        assert settings.rerank_api_key_file == tmp_path / "key"
