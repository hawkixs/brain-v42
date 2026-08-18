"""Standalone scripts resolve their own DSN and must not require POSTGRES_URL.

``get_settings()`` treats POSTGRES_URL as required and insists on the
``postgresql+asyncpg://`` driver form. ``scripts/regen_embeddings.py`` takes
``--postgres-url``, documents a plain ``postgresql://`` default, and is meant to
run from any directory with no ``.env`` in reach — so routing it through
``get_settings()`` turned "no env var" into a crash before the banner, on the
exact script the README tells an operator to run after changing a prefix.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from brain_v42.services.embedding_factory import as_asyncpg_dsn, settings_for_standalone_script

PLAIN = "postgresql://brain:brain@localhost:5433/brain"
DRIVER = "postgresql+asyncpg://brain:brain@localhost:5433/brain"


@pytest.fixture(autouse=True)
def _get_settings_cannot_resolve(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Force the exact condition the fallback exists for.

    Simulating it by clearing the environment and chdir-ing away from the
    repository ``.env`` is not reliable inside the full suite — whether
    ``get_settings()`` succeeds then depends on the working directory and on
    the lru_cache state left by earlier tests, so the fallback under test
    silently stops being exercised. Making ``get_settings`` raise states the
    precondition outright instead of arranging for it.
    """
    import brain_v42.services.embedding_factory as factory

    def _refuse():  # type: ignore[no-untyped-def]
        raise ValidationError.from_exception_data("Settings", [])

    monkeypatch.setattr(factory, "get_settings", _refuse)
    yield


class TestDsnNormalisation:
    def test_a_plain_dsn_is_given_the_driver_the_settings_require(self) -> None:
        assert as_asyncpg_dsn(PLAIN) == DRIVER

    def test_an_already_qualified_dsn_is_left_alone(self) -> None:
        assert as_asyncpg_dsn(DRIVER) == DRIVER


class TestSettingsResolutionWithoutTheEnvironment:
    def test_a_plain_dsn_still_yields_settings(self) -> None:
        """The form the script's own --help documents must work."""
        settings = settings_for_standalone_script(PLAIN)
        assert settings.postgres_url == DRIVER

    def test_the_driver_form_also_works(self) -> None:
        assert settings_for_standalone_script(DRIVER).postgres_url == DRIVER

    def test_embedding_configuration_still_comes_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the database URL is supplied; the backend must not be reset."""
        monkeypatch.setenv("BRAIN_EMBEDDING_BACKEND", "openai")
        monkeypatch.setenv("BRAIN_EMBEDDING_MODEL", "nomic-embed-text")

        settings = settings_for_standalone_script(PLAIN)

        assert settings.embedding_backend == "openai"
        assert settings.embedding_model == "nomic-embed-text"


class TestRegenKeepsItsBatchTimeout:
    def test_batch_timeout_does_not_shrink_to_the_interactive_default(self) -> None:
        """The pre-façade code posted every batch with timeout=60."""
        from scripts import regen_embeddings

        service = regen_embeddings.build_service(
            service_url=None, settings=settings_for_standalone_script(PLAIN)
        )
        assert service._timeout >= 60.0
