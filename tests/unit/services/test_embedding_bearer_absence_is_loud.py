"""A runtime that calls the shim bare-headed says so, once, at its first build.

Measured 2026-09-03: **1724 header-less calls in 12 h** reached the shim from the
HOST, and the cause was not code — `embedding_factory` already resolves the
bearer centrally for both clients. The cause was that
`BRAIN_EMBEDDING_TOKEN_FILE` existed in exactly one place, the MCP's systemd
drop-in, so `brain-metrics` and any script launched from the repository built
their client with an empty api_key and sent no header at all. Nothing said so.

Silence is the defect. `optional` mode accepts those calls and only the shim's
own log counts them, which means the misconfiguration is invisible from inside
the process that has it — until an operator arms `required` and every one of them
becomes a 401.

So the absence is announced where it happens, once per process, naming the
runtime so the line is actionable rather than atmospheric. It is a WARNING and
not an error: header-less is still a valid deployment today, and refusing to
build would take down a runtime for a state the shim currently accepts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
from pydantic import SecretStr

from brain_v42.config import Settings
from brain_v42.services import embedding_factory
from brain_v42.services.embedding_factory import (
    build_embedding_service,
    build_reranker_client,
)

TOKEN = "a-token-long-enough-to-satisfy-the-shim-minimum-of-32-bytes"


def _settings(**kwargs: object) -> Settings:
    # `_env_file=None` is not decoration: since 2026-09-03 the repository's `.env`
    # DECLARES `BRAIN_EMBEDDING_TOKEN_FILE`, so a Settings built without it reads
    # the operator's real path and this module would test the opposite of what it
    # says. The neighbouring bearer module has isolated itself the same way from
    # the day it was written.
    return Settings(
        postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
        _env_file=None,  # type: ignore[call-arg]
        **kwargs,
    )


def _token_file(tmp_path: Path) -> Path:
    path = tmp_path / "embedding-shim-bearer"
    path.write_text(TOKEN, encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture(autouse=True)
def _forget_previous_announcements() -> None:
    """The warning is once per PROCESS; a test process builds many clients."""
    embedding_factory.reset_bearer_absence_announcement()


def _announcements(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return [event for event in events if event.get("event") == "embedding_factory.no_shim_bearer"]


class TestTheAbsenceIsAnnounced:
    def test_building_without_a_token_file_warns(self) -> None:
        with structlog.testing.capture_logs() as events:
            build_embedding_service(_settings())
        announced = _announcements(events)
        assert len(announced) == 1
        assert announced[0]["log_level"] == "warning"

    def test_the_warning_names_the_runtime_so_the_line_is_actionable(self) -> None:
        """ "Some process somewhere" is not a finding anybody can act on."""
        with structlog.testing.capture_logs() as events:
            build_embedding_service(_settings())
        announced = _announcements(events)[0]
        assert announced["setting"] == "BRAIN_EMBEDDING_TOKEN_FILE"
        assert announced["runtime"]
        assert announced["pid"]
        # `Settings` reads `.env` RELATIVE to the working directory, so where the
        # runtime was started IS the diagnosis.
        assert announced["cwd"]

    def test_the_reranker_client_announces_it_too(self) -> None:
        with structlog.testing.capture_logs() as events:
            build_reranker_client(_settings())
        assert len(_announcements(events)) == 1


class TestItIsSaidOnceAndNotOnEveryCall:
    def test_a_second_build_stays_silent(self) -> None:
        """A line repeated per request drowns the log it was meant to make readable."""
        with structlog.testing.capture_logs() as events:
            build_embedding_service(_settings())
            build_embedding_service(_settings())
            build_reranker_client(_settings())
        assert len(_announcements(events)) == 1

    def test_the_two_clients_share_one_announcement(self) -> None:
        with structlog.testing.capture_logs() as events:
            build_reranker_client(_settings())
            build_embedding_service(_settings())
        assert len(_announcements(events)) == 1


class TestAConfiguredRuntimeStaysQuiet:
    def test_a_token_file_produces_no_warning(self, tmp_path: Path) -> None:
        with structlog.testing.capture_logs() as events:
            build_embedding_service(_settings(brain_embedding_token_file=_token_file(tmp_path)))
        assert _announcements(events) == []

    def test_a_hosted_provider_key_produces_no_warning(self) -> None:
        """An api_key IS an Authorization header; the runtime is not bare-headed."""
        with structlog.testing.capture_logs() as events:
            build_embedding_service(_settings(embedding_api_key=SecretStr("a-hosted-key")))
        assert _announcements(events) == []

    def test_the_announcement_never_carries_a_token(self, tmp_path: Path) -> None:
        """Belt and braces: no event of the quiet path may carry the value."""
        with structlog.testing.capture_logs() as events:
            build_embedding_service(_settings(brain_embedding_token_file=_token_file(tmp_path)))
        assert TOKEN not in repr(events)
