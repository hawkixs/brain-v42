"""Static bearer for the embedding shim — ticket 530d796a, reduced to point (a).

The `:8003` shim carried NO application authentication: what closes it is the
loopback bind, not a token, and a client placed on `brain-net` reaches it without
presenting anything (measured on 2026-08-23, report ca-verite-doc-securite). The
two `auto-discord` containers (7 hourly Dagster pipelines) are its live clients:
breaking them is forbidden.

Hence the TWO modes, and the deployment order pinned in the ticket:
- OPTIONAL (shipped by default as soon as a secret is configured): an absent or
  wrong header is ACCEPTED but LOGGED — the observation phase that surveys the
  clients without a token without breaking a single one;
- ARMED (`required`): 401 except on the health endpoints — a SEPARATE operator
  gesture, to be taken only after the auto-discord client (ticket 9ef5c69d)
  carries its bearer.

With no secret configured, `create_app` keeps exactly the current contract: the
rest of the suite (test_embedding_shim.py) is the witness.

Template: src/brain_v42/codex_gateway/auth.py — 0600 secret file, constant-time
comparison, never the token in a log.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "embedding_shim"))

from shim_app import (  # noqa: E402
    BearerGuard,
    bearer_from_env,
    create_app,
    load_bearer_token,
)

TOKEN = "s" * 32


class _FakeEmbedBackend:
    def __init__(self, healthy: bool = True) -> None:
        self._healthy = healthy

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]

    async def healthy(self) -> bool:
        return self._healthy


class _FakeRerankBackend:
    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        return [0.0 for _ in candidates]


#: A NETWORK client by default: ASGITransport presents ("127.0.0.1", 123) if
#: nothing is said, and the loopback exemption would pass every guard test without
#: ever exercising it — a structural false green.
NETWORK_CLIENT = ("192.168.80.9", 51000)
LOOPBACK_CLIENT = ("127.0.0.1", 40001)


@asynccontextmanager
async def _client(
    guard: BearerGuard | None,
    *,
    healthy: bool = True,
    peer: tuple[str, int] = NETWORK_CLIENT,
    app: Any | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    if app is None:
        app = create_app(_FakeEmbedBackend(healthy=healthy), _FakeRerankBackend(), bearer=guard)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False, client=peer)
    async with httpx.AsyncClient(transport=transport, base_url="http://shim") as client:
        yield client


def _guard(*, required: bool) -> BearerGuard:
    return BearerGuard(token=TOKEN.encode(), required=required)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestOptionalMode:
    @pytest.mark.asyncio
    async def test_a_missing_bearer_is_accepted_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The optional mode observes, it never breaks a live client."""
        async with _client(_guard(required=False)) as client:
            with caplog.at_level(logging.WARNING, logger="shim_app"):
                response = await client.post("/embed", json={"texts": ["a"]})

        assert response.status_code == 200
        records = [r for r in caplog.records if "bearer" in r.getMessage().lower()]
        assert records, "un header absent doit laisser une trace en mode optionnel"
        assert any("missing" in r.getMessage() for r in records)

    @pytest.mark.asyncio
    async def test_a_wrong_bearer_is_accepted_and_logged_as_invalid(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with _client(_guard(required=False)) as client:
            with caplog.at_level(logging.WARNING, logger="shim_app"):
                response = await client.post(
                    "/embed", json={"texts": ["a"]}, headers=_auth("wrong-token")
                )

        assert response.status_code == 200
        assert any("invalid" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_valid_bearer_is_accepted_and_silent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An already-migrated client must not pollute the observation log."""
        async with _client(_guard(required=False)) as client:
            with caplog.at_level(logging.WARNING, logger="shim_app"):
                response = await client.post("/embed", json={"texts": ["a"]}, headers=_auth(TOKEN))

        assert response.status_code == 200
        assert not [r for r in caplog.records if "bearer" in r.getMessage().lower()]

    @pytest.mark.asyncio
    async def test_the_log_never_carries_the_presented_value(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Logging the presented token would make the log an exfiltration channel."""
        secret_attempt = "nvapi-SENTINEL-DO-NOT-LEAK"
        async with _client(_guard(required=False)) as client:
            with caplog.at_level(logging.WARNING, logger="shim_app"):
                await client.post("/embed", json={"texts": ["a"]}, headers=_auth(secret_attempt))

        assert secret_attempt not in caplog.text
        assert TOKEN not in caplog.text


class TestRequiredMode:
    @pytest.mark.asyncio
    async def test_a_missing_bearer_is_refused_401(self) -> None:
        async with _client(_guard(required=True)) as client:
            response = await client.post("/embed", json={"texts": ["a"]})

        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    @pytest.mark.asyncio
    async def test_a_wrong_bearer_is_refused_401(self) -> None:
        async with _client(_guard(required=True)) as client:
            response = await client.post(
                "/embed", json={"texts": ["a"]}, headers=_auth("wrong-token")
            )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_a_valid_bearer_passes(self) -> None:
        async with _client(_guard(required=True)) as client:
            response = await client.post(
                "/rerank",
                json={"query": "q", "candidates": ["a", "b"]},
                headers=_auth(TOKEN),
            )

        assert response.status_code == 200
        assert response.json() == {"scores": [0.0, 0.0]}

    @pytest.mark.asyncio
    async def test_health_endpoints_stay_open(self) -> None:
        """Arming must break neither the systemd watchdog nor RerankerClient.is_available."""
        async with _client(_guard(required=True)) as client:
            healthz = await client.get("/healthz")
            health = await client.get("/health")

        assert healthz.status_code == 200
        assert health.status_code == 200

    @pytest.mark.asyncio
    async def test_healthz_still_reports_degraded_upstream(self) -> None:
        """The exemption lets the request through, it does not invent a green."""
        async with _client(_guard(required=True), healthy=False) as client:
            response = await client.get("/healthz")

        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_info_route_is_guarded_too(self) -> None:
        """Only the health endpoints are exempt — GET / describes the runtime."""
        async with _client(_guard(required=True)) as client:
            response = await client.get("/")

        assert response.status_code == 401


class TestTokenFile:
    def _write(self, tmp_path: Path, content: str, mode: int = 0o600) -> Path:
        token_file = tmp_path / "shim-bearer.token"
        token_file.write_text(content)
        token_file.chmod(mode)
        return token_file

    def test_a_valid_file_yields_the_stripped_token(self, tmp_path: Path) -> None:
        token_file = self._write(tmp_path, f"{TOKEN}\n")

        assert load_bearer_token(token_file) == TOKEN.encode()

    def test_a_group_readable_file_is_refused(self, tmp_path: Path) -> None:
        """A secret readable beyond its owner is not a secret (0600)."""
        token_file = self._write(tmp_path, TOKEN, mode=0o640)

        with pytest.raises(ValueError, match="0600"):
            load_bearer_token(token_file)

    def test_a_short_token_is_refused(self, tmp_path: Path) -> None:
        token_file = self._write(tmp_path, "short")

        with pytest.raises(ValueError, match="32"):
            load_bearer_token(token_file)

    def test_a_placeholder_token_is_refused(self, tmp_path: Path) -> None:
        """Same guard as codex_gateway: a copy-pasted REPLACE_ME does not count."""
        token_file = self._write(tmp_path, "REPLACE_WITH_A_REAL_SECRET_OF_32_BYTES_OK")

        with pytest.raises(ValueError, match="REPLACE_"):
            load_bearer_token(token_file)


class TestEnvWiring:
    def test_no_env_means_no_guard(self) -> None:
        """Shipped closed: with no configuration, the current contract does not move."""
        assert bearer_from_env({}) is None

    def test_token_file_alone_yields_the_optional_mode(self, tmp_path: Path) -> None:
        """The default is OBSERVE, never refuse: arming is one word to change."""
        token_file = tmp_path / "t"
        token_file.write_text(TOKEN)
        token_file.chmod(0o600)

        guard = bearer_from_env({"SHIM_BEARER_TOKEN_FILE": str(token_file)})

        assert guard is not None
        assert guard.required is False

    def test_required_mode_is_an_explicit_word(self, tmp_path: Path) -> None:
        token_file = tmp_path / "t"
        token_file.write_text(TOKEN)
        token_file.chmod(0o600)

        guard = bearer_from_env(
            {"SHIM_BEARER_TOKEN_FILE": str(token_file), "SHIM_BEARER_MODE": "required"}
        )

        assert guard is not None
        assert guard.required is True

    def test_an_unknown_mode_fails_closed_at_startup(self, tmp_path: Path) -> None:
        """A typo in the mode must kill the startup, not open the door."""
        token_file = tmp_path / "t"
        token_file.write_text(TOKEN)
        token_file.chmod(0o600)

        with pytest.raises(ValueError, match="SHIM_BEARER_MODE"):
            bearer_from_env(
                {"SHIM_BEARER_TOKEN_FILE": str(token_file), "SHIM_BEARER_MODE": "optionnal"}
            )

    def test_a_mode_without_token_file_fails_closed(self) -> None:
        """A mode set without a secret is a lying configuration, not a default."""
        with pytest.raises(ValueError, match="SHIM_BEARER_TOKEN_FILE"):
            bearer_from_env({"SHIM_BEARER_MODE": "required"})


class TestLoopbackExemption:
    """The inside of the netns is WITHIN the process's trust boundary.

    An explicit trust assumption: only a process in the container's network
    namespace can source 127.0.0.1 — the kernel refuses packets with a loopback
    source arriving through a non-lo interface (martian filtering,
    route_localnet=0), and connections published by Docker arrive with the bridge
    gateway's address, never 127.0.0.1. Anyone who can already execute inside the
    container owns the process: asking them for a bearer protects nothing. The
    compose healthcheck (POST /embed with no Authorization, run inside the
    container) lives exactly there.
    """

    @pytest.mark.asyncio
    async def test_a_loopback_client_passes_without_bearer_in_required_mode(self) -> None:
        async with _client(_guard(required=True), peer=LOOPBACK_CLIENT) as client:
            response = await client.post("/embed", json={"texts": ["a"]})

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_a_loopback_client_is_not_counted_by_the_census(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The healthcheck fires every 60 s: counting it would drown the survey."""
        async with _client(_guard(required=False), peer=LOOPBACK_CLIENT) as client:
            with caplog.at_level(logging.WARNING, logger="shim_app"):
                await client.post("/embed", json={"texts": ["a"]})

        assert not [r for r in caplog.records if "bearer" in r.getMessage().lower()]


class TestComposeHealthcheckContract:
    """The ONLY production prober is the compose healthcheck — pinned from the YAML.

    The PR 43 review reproduced the failure mode: in armed mode, this POST /embed
    with no Authorization returned 401 → a container unhealthy for life, while the
    /healthz canary stayed green. The test replays the REAL request (URL, body and
    absence of Authorization extracted from the compose, never retyped) against the
    app in armed mode AND in no-secret mode.
    """

    @staticmethod
    def _healthcheck_request() -> tuple[str, dict[str, Any]]:
        import ast
        import re

        import yaml

        compose = yaml.safe_load(
            (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
        )
        command = compose["services"]["embedding-shim"]["healthcheck"]["test"]
        script = command[-1]

        assert "Authorization" not in script, (
            "le healthcheck présente désormais un bearer : ce contrat d'exemption "
            "loopback ne le couvre plus tel quel, le mettre à jour"
        )
        url_match = re.search(r"http://127\.0\.0\.1:8003(/[^']*)", script)
        body_match = re.search(r"json\.dumps\((\{[^)]*\})\)", script)
        assert url_match and body_match, "healthcheck compose illisible — contrat à réviser"
        return url_match.group(1), ast.literal_eval(body_match.group(1))

    @pytest.mark.asyncio
    async def test_the_real_healthcheck_stays_green_in_required_mode(self) -> None:
        path, body = self._healthcheck_request()

        async with _client(_guard(required=True), peer=LOOPBACK_CLIENT) as client:
            response = await client.post(path, json=body)

        assert response.status_code == 200
        # The compose's script literally checks r.read(2) == b'[['.
        assert response.content[:2] == b"[["

    @pytest.mark.asyncio
    async def test_the_real_healthcheck_stays_green_without_any_secret(self) -> None:
        path, body = self._healthcheck_request()

        async with _client(None, peer=LOOPBACK_CLIENT) as client:
            response = await client.post(path, json=body)

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_the_same_call_from_the_network_stays_401_when_armed(self) -> None:
        path, body = self._healthcheck_request()

        async with _client(_guard(required=True), peer=NETWORK_CLIENT) as client:
            response = await client.post(path, json=body)

        assert response.status_code == 401


class TestCensusIdentification:
    """A survey that names nobody surveys nothing.

    Reproduced by the review: two distinct clients returned two byte-identical
    lines — impossible to know WHO to migrate before arming.
    """

    @pytest.mark.asyncio
    async def test_two_distinct_clients_yield_two_distinct_lines(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        guard = _guard(required=False)
        with caplog.at_level(logging.WARNING, logger="shim_app"):
            async with _client(guard, peer=("192.168.80.4", 40001)) as client:
                await client.post("/embed", json={"texts": ["a"]})
            async with _client(guard, peer=("192.168.80.7", 40002)) as client:
                await client.post("/embed", json={"texts": ["a"]})

        lines = [r.getMessage() for r in caplog.records if "bearer" in r.getMessage().lower()]
        assert len(lines) == 2
        assert lines[0] != lines[1]
        assert "192.168.80.4:40001" in lines[0]
        assert "192.168.80.7:40002" in lines[1]

    @pytest.mark.asyncio
    async def test_the_user_agent_names_the_client_software(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with _client(_guard(required=False)) as client:
            with caplog.at_level(logging.WARNING, logger="shim_app"):
                await client.post(
                    "/embed",
                    json={"texts": ["a"]},
                    headers={"User-Agent": "auto-discord-bot/1.0"},
                )

        assert any("auto-discord-bot/1.0" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_an_unknown_path_is_never_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """The path is controlled by the caller: an unknown path logged raw is an
        injection channel (a percent-decoded %0A = a real newline, reproduced by the
        review). Unknown paths return their 404 (or 401 in armed mode) without a
        SINGLE survey line — the survey only knows the shim's real routes, which
        contain neither carriage return nor control character.
        """
        async with _client(_guard(required=False)) as client:
            with caplog.at_level(logging.WARNING, logger="shim_app"):
                await client.post("/embed%0AFAKE-LOG-LINE", content=b"{}")

        assert not [r for r in caplog.records if "bearer" in r.getMessage().lower()]
        assert "FAKE-LOG-LINE" not in caplog.text


class TestBuildAppWiring:
    """`bearer=bearer_from_env(...)` in build_app can disappear with the suite green:
    these tests import the REAL build_app and prove the wiring."""

    @staticmethod
    def _built_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
        import main as shim_main

        token_file = tmp_path / "t"
        token_file.write_text(TOKEN)
        token_file.chmod(0o600)
        monkeypatch.setenv("SHIM_BEARER_TOKEN_FILE", str(token_file))
        monkeypatch.setenv("SHIM_BEARER_MODE", "required")
        return shim_main.build_app()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/embed", "/embed/query", "/embed/single", "/rerank"])
    async def test_every_compute_route_of_the_real_app_is_guarded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str
    ) -> None:
        app = self._built_app(monkeypatch, tmp_path)

        async with _client(None, app=app) as client:
            response = await client.post(path, json={})

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_the_real_app_still_passes_with_the_bearer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Negative witness for the wiring: 401 everywhere would also be a broken guard.

        An empty /embed short-circuits before any backend call — the real
        LlamaEmbedBackend is never touched.
        """
        app = self._built_app(monkeypatch, tmp_path)

        async with _client(None, app=app) as client:
            response = await client.post("/embed", json={"texts": []}, headers=_auth(TOKEN))

        assert response.status_code == 200
