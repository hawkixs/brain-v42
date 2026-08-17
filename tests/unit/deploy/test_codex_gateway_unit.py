"""Static deployment contract for the private Docker Codex gateway."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[3]
COMPOSE = ROOT / "docker-compose.yml"
DOCKERIGNORE = ROOT / ".dockerignore"
PRIVATE_ENV = ROOT / "deploy" / "codex-gateway.env.example"
INSTALLER = ROOT / "deploy" / "systemd" / "install.sh"


def _gateway() -> dict:
    document = yaml.safe_load(COMPOSE.read_text())
    return document["services"]["brain-codex-gateway"]


def test_gateway_runs_in_the_brain_private_docker_network_without_published_ports() -> None:
    service = _gateway()

    assert service["build"] == {"context": ".", "target": "production"}
    assert service["command"] == ["python", "-m", "brain_v42.codex_gateway.launcher"]
    assert service["profiles"] == ["codex-gateway"]
    assert service["networks"] == ["default"]
    assert "ports" not in service
    assert "expose" not in service


def test_docker_build_context_excludes_local_secrets_and_runtime_data() -> None:
    patterns = set(DOCKERIGNORE.read_text().splitlines())

    assert {".env", ".env.*", ".secrets", "data", ".git", ".venv"} <= patterns


def test_gateway_container_bind_is_explicit_and_guarded_by_compose_isolation() -> None:
    environment = _gateway()["environment"]

    assert environment["BRAIN_CODEX_GATEWAY_HOST"] == "0.0.0.0"
    assert environment["BRAIN_CODEX_GATEWAY_ALLOW_ALL_INTERFACES"] == "true"
    assert environment["BRAIN_CODEX_GATEWAY_PORT"] == "9211"


def test_gateway_mounts_a_private_token_file_without_putting_it_in_environment() -> None:
    service = _gateway()
    token_path = "/run/secrets/codex-gateway.env"

    assert service["user"] == ("${BRAIN_CODEX_GATEWAY_UID:-1001}:${BRAIN_CODEX_GATEWAY_GID:-1001}")
    assert "env_file" not in service
    assert "BRAIN_CODEX_GATEWAY_TOKEN" not in service["environment"]
    assert service["environment"]["BRAIN_CODEX_GATEWAY_TOKEN_FILE"] == token_path
    assert service["environment"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert (
        f"${{BRAIN_CODEX_GATEWAY_ENV_FILE:-./.secrets/codex-gateway.env}}:{token_path}:ro"
    ) in service["volumes"]


def test_gateway_mounts_the_real_dream_killswitch_drop_in_read_only() -> None:
    service = _gateway()
    target = "/run/brain-v42/killswitches.conf"

    assert service["environment"]["BRAIN_CODEX_GATEWAY_KILLSWITCHES_PATH"] == target
    assert (
        f"${{BRAIN_DREAM_KILLSWITCHES_FILE:-./.secrets/killswitches.conf}}:{target}:ro"
    ) in service["volumes"]


def test_gateway_has_a_real_healthcheck_and_waits_for_postgres() -> None:
    service = _gateway()

    assert service["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "http://127.0.0.1:9211/ready" in " ".join(service["healthcheck"]["test"])


def test_gateway_private_environment_example_contains_no_real_secret() -> None:
    content = PRIVATE_ENV.read_text()

    assert "BRAIN_CODEX_GATEWAY_TOKEN=REPLACE_WITH_RANDOM_TOKEN" in content
    assert "POSTGRES_URL=" not in content


def test_systemd_installer_does_not_install_the_docker_owned_gateway() -> None:
    content = INSTALLER.read_text()

    assert "brain-codex-gateway" not in content
