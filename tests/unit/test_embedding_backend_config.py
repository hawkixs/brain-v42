"""Settings for pluggable embedding backends (TDD Red phase).

Every test here purges BOTH the bare env name and its ``BRAIN_``-prefixed twin:
``_brain_alias`` makes the prefixed name win, so clearing only one leaves the
suite sensitive to whatever the CI environment happens to export.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

DSN = "postgresql+asyncpg://brain:brain@localhost:5433/brain"

_EMBEDDING_ENV = (
    "EMBEDDING_BACKEND",
    "EMBEDDING_MODEL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_TIMEOUT",
    "EMBEDDING_QUERY_PREFIX",
    "EMBEDDING_DOCUMENT_PREFIX",
    "EMBEDDING_DIMENSION",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _EMBEDDING_ENV:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"BRAIN_{name}", raising=False)


def _settings(**kwargs: object):  # type: ignore[no-untyped-def]
    from brain_v42.config import Settings

    return Settings(postgres_url=DSN, _env_file=None, **kwargs)  # type: ignore[call-arg]


class TestDefaultsKeepProductionIdentical:
    """The whole feature ships as a no-op until an operator opts in."""

    def test_backend_defaults_to_the_private_shim_contract(self, clean_env: None) -> None:
        assert _settings().embedding_backend == "shim"

    def test_both_prefixes_default_to_empty(self, clean_env: None) -> None:
        settings = _settings()
        assert settings.embedding_query_prefix == ""
        assert settings.embedding_document_prefix == ""

    def test_api_key_defaults_to_empty_secret(self, clean_env: None) -> None:
        assert _settings().embedding_api_key.get_secret_value() == ""

    def test_unknown_backend_is_rejected(self, clean_env: None) -> None:
        with pytest.raises(ValidationError):
            _settings(embedding_backend="cohere-ish")


class TestPrefixesKeepTheirTrailingSpace:
    """``str_strip_whitespace=True`` on Settings would eat the one character
    that makes an asymmetric-model prefix work (``"query: "``, ``"passage: "``).
    """

    def test_query_prefix_keeps_its_trailing_space(self, clean_env: None) -> None:
        assert _settings(embedding_query_prefix="query: ").embedding_query_prefix == "query: "

    def test_document_prefix_keeps_its_trailing_space(self, clean_env: None) -> None:
        settings = _settings(embedding_document_prefix="passage: ")
        assert settings.embedding_document_prefix == "passage: "

    def test_prefix_from_the_environment_also_keeps_its_space(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRAIN_EMBEDDING_QUERY_PREFIX", "Represent this query: ")
        from brain_v42.config import Settings

        settings = Settings(postgres_url=DSN, _env_file=None)  # type: ignore[call-arg]
        assert settings.embedding_query_prefix == "Represent this query: "


class TestDimensionIsBoundedByPgvector:
    """pgvector 0.8.2 measured: ``CREATE INDEX ... USING hnsw`` refuses a column
    wider than 2000 dims. Without the bound, ``EMBEDDING_DIMENSION=3072``
    (text-embedding-3-large) passes every Python check and detonates inside the
    migration's CREATE INDEX instead.
    """

    def test_dimension_above_the_hnsw_ceiling_is_rejected(self, clean_env: None) -> None:
        with pytest.raises(ValidationError):
            _settings(embedding_dimension=3072)

    def test_the_ceiling_itself_is_accepted(self, clean_env: None) -> None:
        assert _settings(embedding_dimension=2000).embedding_dimension == 2000

    def test_non_positive_dimension_is_rejected(self, clean_env: None) -> None:
        with pytest.raises(ValidationError):
            _settings(embedding_dimension=0)

    def test_production_dimension_still_loads(self, clean_env: None) -> None:
        assert _settings().embedding_dimension == 1536


class TestAliasContract:
    def test_brain_prefixed_name_wins_over_the_bare_legacy_name(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EMBEDDING_BACKEND", "shim")
        monkeypatch.setenv("BRAIN_EMBEDDING_BACKEND", "openai")
        from brain_v42.config import Settings

        settings = Settings(postgres_url=DSN, _env_file=None)  # type: ignore[call-arg]
        assert settings.embedding_backend == "openai"

    def test_bare_legacy_name_still_works(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EMBEDDING_BACKEND", "openai")
        from brain_v42.config import Settings

        settings = Settings(postgres_url=DSN, _env_file=None)  # type: ignore[call-arg]
        assert settings.embedding_backend == "openai"

    def test_api_key_is_a_secret_and_never_rendered(self, clean_env: None) -> None:
        from brain_v42.config import Settings

        # Without this guard the test passes vacuously: `extra="ignore"` drops
        # an unknown kwarg, so repr() would never contain the key anyway.
        assert "embedding_api_key" in Settings.model_fields
        settings = _settings(embedding_api_key=SecretStr("sk-not-a-real-key"))
        assert "sk-not-a-real-key" not in repr(settings)
