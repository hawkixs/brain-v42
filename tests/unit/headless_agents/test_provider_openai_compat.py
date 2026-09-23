"""The ``openai-compat`` provider and its presets (spec 3.2, lot 3).

Every run goes through a real child process (the killable worker) and a real
HTTP server on the loopback: the classification of failures, the deadline and
the non-disclosure of the key are properties of that process boundary, which a
mocked transport would not exercise. No network beyond 127.0.0.1, no quota.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from headless_agents.capability import (
    INVALID_USAGE_EXIT_CODE,
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
)
from headless_agents.context import ContextBundle, ContextFile
from headless_agents.profile import CapabilityProfile, McpServer, Workspace
from headless_agents.providers import openai_compat
from headless_agents.providers.openai_compat import PRESETS, OpenAICompatProvider
from headless_agents.spec import RunSpec

# A fresh marker per test session: a fake key must not look like a committed
# secret to the scanner, and a random one proves nothing reused it by chance.
SECRET = f"fake-{uuid.uuid4().hex}"
KEY_ENV = "HA_TEST_OPENAI_KEY"


class _Server:
    """A loopback server whose reply each test chooses; records every request."""

    def __init__(self, reply: Callable[[dict[str, object]], tuple[int, object, float]]) -> None:
        self.requests: list[dict[str, object]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                server.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body}
                )
                status, payload, delay = reply(body)
                if delay:
                    time.sleep(delay)
                data = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args: object) -> None:
                return None

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _completion(text: str = "hello", **usage: object) -> dict[str, object]:
    return {
        "id": "cmpl-1",
        "model": "served-model-v2",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, **usage},
    }


@pytest.fixture
def serve() -> Iterator[Callable[..., _Server]]:
    servers: list[_Server] = []

    def start(reply: Callable[[dict[str, object]], tuple[int, object, float]]) -> _Server:
        server = _Server(reply)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


def _spec(tmp_path: Path, url: str, **overrides: object) -> RunSpec:
    fields: dict[str, object] = {
        "prompt": "Say hello",
        "model": "some-model",
        "timeout_seconds": 20.0,
        "run_dir": tmp_path / "run-1",
        "environment": {"PATH": "/usr/bin:/bin", KEY_ENV: SECRET},
        "extra": {"base_url": url, "key_env": KEY_ENV},
    }
    fields.update(overrides)
    return RunSpec(**fields)  # type: ignore[arg-type]


def _everything_written(run_dir: Path) -> str:
    return "\n".join(p.read_text(errors="replace") for p in run_dir.rglob("*") if p.is_file())


# ── Success ────────────────────────────────────────────────────────────────


def test_a_completion_returns_its_text_tokens_and_reported_model(tmp_path, serve) -> None:
    server = serve(
        lambda _b: (
            200,
            _completion(
                "hello there",
                prompt_tokens_details={"cached_tokens": 4},
                completion_tokens_details={"reasoning_tokens": 3},
            ),
            0,
        )
    )
    result = OpenAICompatProvider().run(_spec(tmp_path, server.url))

    assert result.exit_code == 0
    assert result.text == "hello there"
    assert result.provider == "openai-compat"
    assert result.model == "some-model"
    assert result.model_reported == "served-model-v2"
    assert result.tokens is not None
    assert (result.tokens.input, result.tokens.output) == (11, 7)
    assert (result.tokens.cached, result.tokens.fresh, result.tokens.thinking) == (4, 7, 3)
    assert result.cost_usd is None
    assert result.tool_call_completed is False
    assert result.run_id == "run-1"
    recorded = json.loads((tmp_path / "run-1" / "result.json").read_text())
    assert recorded["text"] == "hello there" and recorded["schema"] == 1
    assert (tmp_path / "run-1" / "report.log").read_text() == "hello there"


def test_unmeasured_counts_stay_none_not_zero(tmp_path, serve) -> None:
    server = serve(lambda _b: (200, _completion("x"), 0))
    result = OpenAICompatProvider().run(_spec(tmp_path, server.url))
    assert result.tokens is not None
    assert result.tokens.cached is None
    assert result.tokens.fresh is None
    assert result.tokens.thinking is None


def test_the_request_carries_prompt_preamble_options_and_bearer(tmp_path, serve) -> None:
    server = serve(lambda _b: (200, _completion(), 0))
    bundle = ContextBundle(
        level="global",
        files=(
            ContextFile(
                source=Path("/home/op/.claude/CLAUDE.md"),
                scope="user",
                content="Answer in English.",
                size_bytes=18,
                sha256="0" * 64,
            ),
        ),
    )
    spec = _spec(
        tmp_path,
        server.url,
        context=bundle,
        extra={
            "base_url": server.url,
            "key_env": KEY_ENV,
            "temperature": 0.2,
            "max_tokens": 64,
            "response_format": {"type": "json_object"},
        },
    )
    OpenAICompatProvider().run(spec)

    (request,) = server.requests
    assert request["path"] == "/v1/chat/completions"
    headers = request["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == f"Bearer {SECRET}"
    body = request["body"]
    assert isinstance(body, dict)
    assert body["model"] == "some-model"
    assert body["messages"] == [
        {"role": "system", "content": bundle.preamble()},
        {"role": "user", "content": "Say hello"},
    ]
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 64
    assert body["response_format"] == {"type": "json_object"}
    assert "stream" not in body and "tools" not in body


def test_an_empty_answer_is_no_text(tmp_path, serve) -> None:
    server = serve(lambda _b: (200, _completion(""), 0))
    result = OpenAICompatProvider().run(_spec(tmp_path, server.url))
    assert result.exit_code == 0
    assert result.text is None


# ── Failure classification ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, PROVIDER_FALLBACK_EXIT_CODE),
        (500, PROVIDER_FALLBACK_EXIT_CODE),
        (503, PROVIDER_FALLBACK_EXIT_CODE),
        (401, 1),
        (403, 1),
        (400, 1),
        (404, 1),
    ],
)
def test_http_errors_are_classified(tmp_path, serve, status: int, expected: int) -> None:
    server = serve(lambda _b: (status, {"error": {"message": "nope"}}, 0))
    result = OpenAICompatProvider().run(_spec(tmp_path, server.url))
    assert result.exit_code == expected
    assert result.text is None


def test_a_refused_connection_lets_the_chain_advance(tmp_path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    result = OpenAICompatProvider().run(_spec(tmp_path, f"http://127.0.0.1:{port}/v1"))
    assert result.exit_code == PROVIDER_FALLBACK_EXIT_CODE


def test_a_malformed_success_is_an_ordinary_failure(tmp_path, serve) -> None:
    server = serve(lambda _b: (200, {"unexpected": True}, 0))
    result = OpenAICompatProvider().run(_spec(tmp_path, server.url))
    assert result.exit_code == 1
    assert result.text is None


def test_the_own_deadline_kills_the_worker_and_returns_124(tmp_path, serve) -> None:
    server = serve(lambda _b: (200, _completion(), 5.0))
    start = time.monotonic()
    result = OpenAICompatProvider().run(_spec(tmp_path, server.url, timeout_seconds=0.8))
    assert result.exit_code == TIMEOUT_EXIT_CODE
    assert time.monotonic() - start < 4.0


def test_a_passed_deadline_caps_the_timeout(tmp_path, serve) -> None:
    server = serve(lambda _b: (200, _completion(), 5.0))
    spec = _spec(tmp_path, server.url, timeout_seconds=60.0, deadline=time.monotonic() + 0.8)
    start = time.monotonic()
    result = OpenAICompatProvider().run(spec)
    assert result.exit_code == TIMEOUT_EXIT_CODE
    assert time.monotonic() - start < 4.0


# ── The key never leaks ────────────────────────────────────────────────────


def test_the_key_never_appears_in_any_file_even_when_the_error_echoes_it(tmp_path, serve) -> None:
    server = serve(lambda _b: (401, {"error": {"message": f"invalid key {SECRET}"}}, 0))
    result = OpenAICompatProvider().run(_spec(tmp_path, server.url))
    assert result.exit_code == 1
    assert SECRET not in _everything_written(tmp_path)


def test_the_key_is_neither_in_argv_nor_in_the_child_environment(tmp_path) -> None:
    provider = OpenAICompatProvider()
    spec = _spec(tmp_path, "http://127.0.0.1:9/v1")
    assert all(SECRET not in part for part in provider.build_command(spec))
    child = provider.child_environment(spec, spec.environment or {})
    assert child is not None
    assert KEY_ENV not in child
    assert SECRET not in child.values()


def test_a_missing_key_fails_without_a_request(tmp_path, serve) -> None:
    server = serve(lambda _b: (200, _completion(), 0))
    spec = _spec(tmp_path, server.url, environment={"PATH": "/usr/bin:/bin"})
    result = OpenAICompatProvider().run(spec)
    assert result.exit_code == 1
    assert server.requests == []
    assert KEY_ENV in (tmp_path / "run-1" / "stderr.log").read_text()


# ── Usage limits ────────────────────────────────────────────────────────────


def test_a_profile_with_mcp_is_rejected(tmp_path) -> None:
    profile = CapabilityProfile(
        mcp=McpServer(name="brain", url="http://127.0.0.1:8765/mcp", bearer_env_var="T")
    )
    with pytest.raises(ValueError, match="mcp"):
        OpenAICompatProvider().run(_spec(tmp_path, "http://127.0.0.1:9/v1", profile=profile))


def test_a_profile_with_a_workspace_is_rejected(tmp_path) -> None:
    profile = CapabilityProfile(workspace=Workspace(path=tmp_path))
    with pytest.raises(ValueError, match="workspace"):
        OpenAICompatProvider().run(_spec(tmp_path, "http://127.0.0.1:9/v1", profile=profile))


@pytest.mark.parametrize(
    "extra",
    [
        {"key_env": KEY_ENV},
        {"base_url": "http://127.0.0.1:9/v1"},
        {"base_url": "ftp://127.0.0.1/v1", "key_env": KEY_ENV},
        {"base_url": "http://user:pw@127.0.0.1/v1", "key_env": KEY_ENV},
        {"base_url": "http://127.0.0.1:9/v1", "key_env": KEY_ENV, "stream": True},
    ],
)
def test_invalid_generic_configuration_is_a_usage_error(tmp_path, extra) -> None:
    result = OpenAICompatProvider().run(_spec(tmp_path, "unused", extra=extra))
    assert result.exit_code == INVALID_USAGE_EXIT_CODE


def test_an_empty_model_is_a_usage_error(tmp_path) -> None:
    result = OpenAICompatProvider().run(_spec(tmp_path, "http://127.0.0.1:9/v1", model=""))
    assert result.exit_code == INVALID_USAGE_EXIT_CODE


@pytest.mark.parametrize("name", ["openrouter", "mistral", "nvidia"])
def test_a_preset_refuses_a_caller_url_or_key_variable(tmp_path, name) -> None:
    result = OpenAICompatProvider(name).run(_spec(tmp_path, "http://127.0.0.1:9/v1"))
    assert result.exit_code == INVALID_USAGE_EXIT_CODE


# ── Presets ────────────────────────────────────────────────────────────────


def test_the_presets_fix_their_url_and_key_variable() -> None:
    assert {name: (p.base_url, p.key_env) for name, p in PRESETS.items()} == {
        "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
        "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
        "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
    }


def test_only_openrouter_asks_for_the_cost_in_usage() -> None:
    body = openai_compat.request_body("openrouter", model="m", messages=[], options={})
    assert body["usage"] == {"include": True}
    for name in ("mistral", "nvidia", "openai-compat"):
        assert "usage" not in openai_compat.request_body(name, model="m", messages=[], options={})


def test_a_reported_cost_is_read_from_usage() -> None:
    parsed = openai_compat.parse_completion(_completion("x", cost=0.00042))
    assert parsed.cost_usd == 0.00042
    assert openai_compat.parse_completion(_completion("x")).cost_usd is None


def test_an_unknown_preset_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="gpt"):
        OpenAICompatProvider("gpt")


# ── The worker itself ─────────────────────────────────────────────────────


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://127.0.0.1/x", "data:,hi"])
def test_the_worker_refuses_a_non_http_url_without_opening_it(url: str) -> None:
    from headless_agents.providers import _openai_worker

    outcome = _openai_worker.perform({"url": url, "api_key": "k", "body": {}, "timeout": 1.0})
    assert outcome == {"ok": False, "category": "error", "status": None}
