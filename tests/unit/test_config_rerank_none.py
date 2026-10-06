"""An intentional rollback must reject leftover hosted reranker settings."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from brain_v42.config import Settings


def test_none_accepts_defaults() -> None:
    settings = Settings(
        postgres_url="postgresql+asyncpg://test:test@localhost/brain_unit",
        rerank_backend="none",
        _env_file=None,
    )
    assert settings.rerank_backend == "none"


@pytest.mark.parametrize(
    "field,value",
    [
        ("rerank_model", "sentinel-secret"),
        ("rerank_api_key", "sentinel-secret"),
        ("rerank_api_key_file", Path("sentinel-secret")),
        ("rerank_provider", {"only": ["sentinel-secret"]}),
        ("reranker_url", "http://sentinel-secret.invalid"),
        ("rerank_health_path", "/sentinel-secret"),
        ("reranker_url", "http://localhost:8003"),
        ("rerank_health_path", "/health"),
    ],
)
def test_none_rejects_leftover_settings_without_disclosing_values(field: str, value: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(
            postgres_url="postgresql+asyncpg://test:test@localhost/brain_unit",
            rerank_backend="none",
            **{field: value},
            _env_file=None,
        )
    assert field in str(caught.value)
    assert "sentinel-secret" not in str(caught.value)


def test_none_reports_every_conflicting_field() -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(
            postgres_url="postgresql+asyncpg://test:test@localhost/brain_unit",
            rerank_backend="none",
            rerank_model="sentinel-secret",
            rerank_api_key="sentinel-secret",
            rerank_api_key_file=Path("sentinel-secret"),
            _env_file=None,
        )
    for field in ("rerank_model", "rerank_api_key", "rerank_api_key_file"):
        assert field in str(caught.value)
    assert "sentinel-secret" not in str(caught.value)
