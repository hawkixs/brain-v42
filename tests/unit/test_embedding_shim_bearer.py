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


#: Un client RÉSEAU par défaut : ASGITransport présente ("127.0.0.1", 123) si on
#: ne dit rien, et l'exemption loopback ferait passer tous les tests du garde
#: sans jamais l'exercer — un faux vert structurel.
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


class TestLoopbackExemption:
    """L'intérieur du netns est DANS la frontière de confiance du processus.

    Hypothèse de confiance, explicite : seul un processus du namespace réseau
    du conteneur peut sourcer 127.0.0.1 — le noyau refuse les paquets à source
    loopback arrivant par une interface non-lo (filtrage martien,
    route_localnet=0), et les connexions publiées par Docker arrivent avec
    l'adresse de la passerelle bridge, jamais 127.0.0.1. Quiconque peut déjà
    exécuter dans le conteneur possède le processus : lui demander un bearer
    ne protège rien. Le healthcheck compose (POST /embed sans Authorization,
    exécuté dans le conteneur) vit exactement là.
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
        """Le healthcheck tire toutes les 60 s : le compter noierait le recensement."""
        async with _client(_guard(required=False), peer=LOOPBACK_CLIENT) as client:
            with caplog.at_level(logging.WARNING, logger="shim_app"):
                await client.post("/embed", json={"texts": ["a"]})

        assert not [r for r in caplog.records if "bearer" in r.getMessage().lower()]


class TestComposeHealthcheckContract:
    """Le SEUL prober de prod est le healthcheck compose — épinglé depuis le YAML.

    La review de PR 43 a reproduit le mode de panne : en mode armé, ce POST
    /embed sans Authorization sortait en 401 → conteneur unhealthy à vie,
    pendant que le canari /healthz restait vert. Le test rejoue la requête
    RÉELLE (URL, corps et absence d'Authorization extraits du compose, jamais
    recopiés) contre l'app en mode armé ET en mode sans-secret.
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
        # Le script du compose vérifie littéralement r.read(2) == b'[['.
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
    """Un recensement qui ne nomme personne ne recense rien.

    Reproduit par la review : deux clients distincts rendaient deux lignes
    octet pour octet identiques — impossible de savoir QUI migrer avant
    d'armer.
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
        """Le chemin est contrôlé par l'appelant : un chemin inconnu journalisé
        brut est un canal d'injection (%0A percent-décodé = vraie nouvelle
        ligne, reproduit par la review). Les chemins inconnus rendent leur 404
        (ou 401 en mode armé) sans UNE ligne de recensement — le recensement ne
        connaît que les routes réelles du shim, qui ne contiennent ni retour
        chariot ni caractère de contrôle.
        """
        async with _client(_guard(required=False)) as client:
            with caplog.at_level(logging.WARNING, logger="shim_app"):
                await client.post("/embed%0AFAKE-LOG-LINE", content=b"{}")

        assert not [r for r in caplog.records if "bearer" in r.getMessage().lower()]
        assert "FAKE-LOG-LINE" not in caplog.text


class TestBuildAppWiring:
    """`bearer=bearer_from_env(...)` dans build_app peut disparaître suite verte :
    ces tests importent le VRAI build_app et prouvent le câblage."""

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
        """Témoin négatif du câblage : 401 partout serait aussi un garde cassé.

        /embed vide court-circuite avant tout appel backend — le vrai
        LlamaEmbedBackend n'est jamais touché.
        """
        app = self._built_app(monkeypatch, tmp_path)

        async with _client(None, app=app) as client:
            response = await client.post("/embed", json={"texts": []}, headers=_auth(TOKEN))

        assert response.status_code == 200
