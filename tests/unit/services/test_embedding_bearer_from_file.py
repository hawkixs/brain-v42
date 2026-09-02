"""The shim client's bearer comes from a file, or it does not exist at all.

The shim runs in `optional` census mode, and its log named this very process as
a caller without a token (`python-httpx` on /embed/query and /rerank). Arming
the shim to `required` would therefore cut `brain_search` off from its own
embeddings. This is the half that has to land first.

The token is read from a FILE and never from a variable carrying its value:
`docker inspect` and `systemctl show` both print an environment verbatim, so a
value passed that way is readable by anyone who can reach the daemon or the
service manager.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from brain_v42.config import Settings
from brain_v42.services.embedding_factory import (
    EmbeddingBearerError,
    build_embedding_service,
    build_reranker_client,
)

DSN = "postgresql+asyncpg://brain:brain@localhost:5433/brain"
TOKEN = "b8b0f0a1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9"


def _settings(**kwargs: object) -> Settings:
    return Settings(postgres_url=DSN, _env_file=None, **kwargs)  # type: ignore[call-arg]


def _token_file(tmp_path: Path, content: str = TOKEN) -> Path:
    path = tmp_path / "embedding-bearer"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


class TestUnconfiguredKeepsTodaysContract:
    """Default is None, and the wire stays byte-identical to today's."""

    def test_the_embedding_client_sends_no_authorization(self) -> None:
        service = build_embedding_service(_settings())
        assert "Authorization" not in service._get_client().headers

    def test_the_reranker_client_sends_no_authorization(self) -> None:
        client = build_reranker_client(_settings())
        assert "Authorization" not in client._get_client().headers

    def test_the_setting_defaults_to_none(self) -> None:
        assert _settings().brain_embedding_token_file is None


class TestConfiguredSendsTheBearer:
    """One injection point per client, so every route inherits the header."""

    def test_the_embedding_client_carries_the_bearer(self, tmp_path: Path) -> None:
        service = build_embedding_service(
            _settings(brain_embedding_token_file=_token_file(tmp_path))
        )
        assert service._get_client().headers["Authorization"] == f"Bearer {TOKEN}"

    def test_the_reranker_client_carries_the_bearer(self, tmp_path: Path) -> None:
        client = build_reranker_client(_settings(brain_embedding_token_file=_token_file(tmp_path)))
        assert client._get_client().headers["Authorization"] == f"Bearer {TOKEN}"

    def test_surrounding_whitespace_is_stripped(self, tmp_path: Path) -> None:
        """`openssl rand -hex 32 > file` leaves a trailing newline."""
        service = build_embedding_service(
            _settings(brain_embedding_token_file=_token_file(tmp_path, f"  {TOKEN}\n"))
        )
        assert service._get_client().headers["Authorization"] == f"Bearer {TOKEN}"

    @pytest.mark.asyncio
    async def test_every_route_carries_it_measured_not_assumed(self, tmp_path: Path) -> None:
        """The census the shim logged names routes, so prove it on routes.

        One injection point per client means the header is set on the
        ``AsyncClient`` and inherited, rather than added at each call site where
        the next route to be written would forget it.
        """
        seen: list[tuple[str, str | None]] = []

        def _record(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, request.headers.get("Authorization")))
            if request.url.path == "/rerank":
                return httpx.Response(200, json={"scores": [0.5]})
            if request.url.path in ("/healthz", "/health"):
                return httpx.Response(200, json={"status": "ok"})
            return httpx.Response(200, json=[[0.1, 0.2]])

        transport = httpx.MockTransport(_record)
        token_file = _token_file(tmp_path)

        service = build_embedding_service(_settings(brain_embedding_token_file=token_file))
        service._client = httpx.AsyncClient(
            base_url="http://localhost:8003",
            headers=service._get_client().headers,
            transport=transport,
        )
        await service.embed_texts(["a"])
        await service.embed_query("q")
        await service.healthcheck()

        reranker = build_reranker_client(_settings(brain_embedding_token_file=token_file))
        reranker._client = httpx.AsyncClient(
            base_url="http://localhost:8003",
            headers=reranker._get_client().headers,
            transport=transport,
        )
        await reranker.rerank("q", ["c"])
        await reranker.is_available()

        assert {path for path, _ in seen} >= {"/embed", "/embed/query", "/rerank"}
        assert all(header == f"Bearer {TOKEN}" for _, header in seen), seen


class TestConfiguredButUnusableFailsClosed:
    """A configured file that cannot be read is a named startup failure.

    Never a silent call without a bearer: once the shim is armed, that would be
    an outage whose cause is invisible in this process's own logs.
    """

    def test_an_absent_file_raises_a_named_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent"
        with pytest.raises(EmbeddingBearerError) as excinfo:
            build_embedding_service(_settings(brain_embedding_token_file=missing))
        assert "absent" in str(excinfo.value)

    def test_an_empty_file_raises_a_named_error(self, tmp_path: Path) -> None:
        with pytest.raises(EmbeddingBearerError):
            build_reranker_client(_settings(brain_embedding_token_file=_token_file(tmp_path, "\n")))

    def test_a_directory_raises_a_named_error(self, tmp_path: Path) -> None:
        with pytest.raises(EmbeddingBearerError):
            build_embedding_service(_settings(brain_embedding_token_file=tmp_path))

    def test_a_file_and_an_api_key_together_raise(self, tmp_path: Path) -> None:
        """Two tokens for one header is a misconfiguration, not a precedence puzzle."""
        with pytest.raises(EmbeddingBearerError):
            build_embedding_service(
                _settings(
                    brain_embedding_token_file=_token_file(tmp_path),
                    embedding_api_key=SecretStr("a-hosted-provider-key"),
                )
            )


class TestTheTokenNeverLeaks:
    """A log is not an exfiltration channel — the same rule as the shim's."""

    def test_the_startup_log_never_carries_the_value(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("DEBUG"):
            build_embedding_service(_settings(brain_embedding_token_file=_token_file(tmp_path)))
            build_reranker_client(_settings(brain_embedding_token_file=_token_file(tmp_path)))
        assert TOKEN not in caplog.text

    def test_the_error_message_never_carries_the_value(self, tmp_path: Path) -> None:
        """The conflict error names the two sources, never what they hold."""
        with pytest.raises(EmbeddingBearerError) as excinfo:
            build_embedding_service(
                _settings(
                    brain_embedding_token_file=_token_file(tmp_path),
                    embedding_api_key=SecretStr("a-hosted-provider-key"),
                )
            )
        rendered = str(excinfo.value)
        assert TOKEN not in rendered
        assert "a-hosted-provider-key" not in rendered

    def test_no_repr_of_the_settings_carries_the_value(self, tmp_path: Path) -> None:
        """The setting holds a PATH, so a repr can only ever show the path."""
        settings = _settings(brain_embedding_token_file=_token_file(tmp_path))
        assert TOKEN not in repr(settings)

    def test_no_repr_of_the_clients_carries_the_value(self, tmp_path: Path) -> None:
        service = build_embedding_service(
            _settings(brain_embedding_token_file=_token_file(tmp_path))
        )
        client = build_reranker_client(_settings(brain_embedding_token_file=_token_file(tmp_path)))
        assert TOKEN not in repr(service)
        assert TOKEN not in repr(client)
