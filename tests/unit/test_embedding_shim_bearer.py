"""Bearer statique du shim embedding — ticket 530d796a, réduit au point (a).

Le shim `:8003` ne portait AUCUNE authentification applicative : ce qui le
ferme est le bind loopback, pas un jeton, et un client posé sur `brain-net`
l'atteint sans rien présenter (mesuré le 2026-08-23, rapport
ca-verite-doc-securite). Les deux conteneurs `auto-discord` (7 pipelines
Dagster horaires) sont ses clients vivants : les casser est interdit.

D'où les DEUX modes, et l'ordre de déploiement épinglé au ticket :
- OPTIONNEL (livré par défaut dès qu'un secret est configuré) : un header
  absent ou faux est ACCEPTÉ mais JOURNALISÉ — la phase d'observation qui
  recense les clients sans jeton sans en casser un seul ;
- ARMÉ (`required`) : 401 sauf sur les endpoints de santé — un geste
  opérateur SÉPARÉ, à ne prendre qu'après que le client auto-discord
  (ticket 9ef5c69d) porte son bearer.

Sans secret configuré, `create_app` garde exactement le contrat actuel :
le reste de la suite (test_embedding_shim.py) est le témoin.

Patron : src/brain_v42/codex_gateway/auth.py — secret fichier 0600,
comparaison en temps constant, jamais le jeton dans un log.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

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


@asynccontextmanager
async def _client(
    guard: BearerGuard | None,
    *,
    healthy: bool = True,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(_FakeEmbedBackend(healthy=healthy), _FakeRerankBackend(), bearer=guard)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
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
        """Le mode optionnel observe, il ne casse jamais un client vivant."""
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
        """Un client déjà migré ne doit pas polluer le journal d'observation."""
        async with _client(_guard(required=False)) as client:
            with caplog.at_level(logging.WARNING, logger="shim_app"):
                response = await client.post("/embed", json={"texts": ["a"]}, headers=_auth(TOKEN))

        assert response.status_code == 200
        assert not [r for r in caplog.records if "bearer" in r.getMessage().lower()]

    @pytest.mark.asyncio
    async def test_the_log_never_carries_the_presented_value(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Journaliser le jeton présenté ferait du log un canal d'exfiltration."""
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
        """Armer ne doit casser ni le watchdog systemd ni RerankerClient.is_available."""
        async with _client(_guard(required=True)) as client:
            healthz = await client.get("/healthz")
            health = await client.get("/health")

        assert healthz.status_code == 200
        assert health.status_code == 200

    @pytest.mark.asyncio
    async def test_healthz_still_reports_degraded_upstream(self) -> None:
        """L'exemption laisse passer la requête, elle n'invente pas un vert."""
        async with _client(_guard(required=True), healthy=False) as client:
            response = await client.get("/healthz")

        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_info_route_is_guarded_too(self) -> None:
        """Seuls les endpoints de santé sont exemptés — GET / décrit le runtime."""
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
        """Un secret lisible au-delà du propriétaire n'est pas un secret (0600)."""
        token_file = self._write(tmp_path, TOKEN, mode=0o640)

        with pytest.raises(ValueError, match="0600"):
            load_bearer_token(token_file)

    def test_a_short_token_is_refused(self, tmp_path: Path) -> None:
        token_file = self._write(tmp_path, "short")

        with pytest.raises(ValueError, match="32"):
            load_bearer_token(token_file)

    def test_a_placeholder_token_is_refused(self, tmp_path: Path) -> None:
        """Même garde que codex_gateway : un REPLACE_ME copié-collé ne compte pas."""
        token_file = self._write(tmp_path, "REPLACE_WITH_A_REAL_SECRET_OF_32_BYTES_OK")

        with pytest.raises(ValueError, match="REPLACE_"):
            load_bearer_token(token_file)


class TestEnvWiring:
    def test_no_env_means_no_guard(self) -> None:
        """Livré fermé : sans configuration, le contrat actuel ne bouge pas."""
        assert bearer_from_env({}) is None

    def test_token_file_alone_yields_the_optional_mode(self, tmp_path: Path) -> None:
        """Le défaut est OBSERVER, jamais refuser : l'armement est un mot à changer."""
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
        """Une faute de frappe dans le mode doit tuer le démarrage, pas ouvrir."""
        token_file = tmp_path / "t"
        token_file.write_text(TOKEN)
        token_file.chmod(0o600)

        with pytest.raises(ValueError, match="SHIM_BEARER_MODE"):
            bearer_from_env(
                {"SHIM_BEARER_TOKEN_FILE": str(token_file), "SHIM_BEARER_MODE": "optionnal"}
            )

    def test_a_mode_without_token_file_fails_closed(self) -> None:
        """Un mode posé sans secret est une configuration menteuse, pas un défaut."""
        with pytest.raises(ValueError, match="SHIM_BEARER_TOKEN_FILE"):
            bearer_from_env({"SHIM_BEARER_MODE": "required"})
