"""Pin the private-compose contract without requiring Docker or production secrets."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

from brain_v42.config import Settings
from brain_v42.delivery_observer.config import load_observer_settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy" / "compose.yaml"
ONE_SHOTS = {"restore", "migrate", "graph-bootstrap"}
SERVERS = {"mcp", "metrics", "delivery-observer", "graph-recon"}
BRAIN_SERVICES = ONE_SHOTS | SERVERS
SERVICES = BRAIN_SERVICES | {"postgres", "neo4j"}
RAIL_VARIABLES = {
    "COMPOSE_PROJECT_NAME",
    "IMAGE_REFERENCE",
    "IMAGE_DIGEST",
    "GIT_SHA",
    "VERSION",
    "BIND_ADDRESS",
}


@pytest.fixture
def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture
def clean_settings_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """An operator's ambient configuration must not influence the parser contract."""
    for key in os.environ:
        if key.startswith(
            ("BRAIN_", "MCP_", "METRICS_", "GRAPH_", "NEO4J_", "EMBEDDING_", "RERANK")
        ):
            monkeypatch.delenv(key)
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://brain_app@postgres:5432/brain_test")
    return monkeypatch


def test_compose_exists() -> None:
    assert COMPOSE.is_file(), "The released commit must supply deploy/compose.yaml"


def test_project_and_exact_service_names(compose: dict[str, Any]) -> None:
    assert compose["name"] == "brain-v42"
    assert set(compose["services"]) == SERVICES
    for name, service in compose["services"].items():
        assert service["container_name"] == f"brain-v42-{name}"


def test_images_reuse_the_development_pins_and_released_image(compose: dict[str, Any]) -> None:
    development = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for name in {"postgres", "neo4j"}:
        assert compose["services"][name]["image"] == development["services"][name]["image"]
    for name in BRAIN_SERVICES:
        assert compose["services"][name]["image"] == "${IMAGE_REFERENCE}"
    assert all("build" not in service for service in compose["services"].values())


@pytest.mark.parametrize(
    ("name", "dependencies"),
    [
        ("restore", {"postgres": "service_healthy"}),
        ("migrate", {"restore": "service_completed_successfully", "postgres": "service_healthy"}),
        (
            "graph-bootstrap",
            {"migrate": "service_completed_successfully", "neo4j": "service_healthy"},
        ),
        *[
            (name, {"graph-bootstrap": "service_completed_successfully"})
            for name in sorted(SERVERS)
        ],
    ],
)
def test_startup_chain(compose: dict[str, Any], name: str, dependencies: dict[str, str]) -> None:
    assert compose["services"][name]["depends_on"] == {
        dependency: {"condition": condition} for dependency, condition in dependencies.items()
    }


def test_restart_and_healthchecks(compose: dict[str, Any]) -> None:
    for name in ONE_SHOTS:
        assert compose["services"][name]["restart"] == "no"
    for name in SERVERS | {"postgres", "neo4j"}:
        service = compose["services"][name]
        assert service["restart"] == "unless-stopped"
        assert service["healthcheck"]["test"][0] in {"CMD", "CMD-SHELL"}
        assert service["healthcheck"]["test"] != ["CMD", "true"]
    for name, port in [("mcp", 8765), ("metrics", 9200)]:
        probe = compose["services"][name]["healthcheck"]["test"]
        assert probe[:3] == ["CMD", "python", "-c"]
        assert "urllib.request.urlopen" in probe[3]
        assert f"http://127.0.0.1:{port}/healthz" in probe[3]
        assert "timeout=" in probe[3]


def test_only_wireguard_server_ports_are_published(compose: dict[str, Any]) -> None:
    published = {
        name: service["ports"]
        for name, service in compose["services"].items()
        if "ports" in service
    }
    assert published == {
        "mcp": ["${BIND_ADDRESS}:8765:8765"],
        "metrics": ["${BIND_ADDRESS}:9200:9200"],
    }
    assert all("network_mode" not in service for service in compose["services"].values())


def test_external_binds_are_explicit_and_parseable(
    compose: dict[str, Any], clean_settings_env: pytest.MonkeyPatch
) -> None:
    mcp = compose["services"]["mcp"]["environment"]
    metrics = compose["services"]["metrics"]["environment"]
    assert mcp["MCP_HTTP_HOST"] == metrics["METRICS_HOST"] == "0.0.0.0"
    assert mcp["MCP_HTTP_PORT"] == "8765"
    assert metrics["METRICS_PORT"] == "9200"
    assert mcp["BRAIN_MCP_TRANSPORT"] == "http"
    assert mcp["BRAIN_MCP_AUTH_MODE"] == "credentials"
    assert mcp["MCP_HTTP_ALLOW_NON_LOOPBACK"] == "true"
    assert mcp["MCP_HTTP_ALLOWED_HOSTS"] == "${BIND_ADDRESS}:8765"
    assert metrics["METRICS_ALLOW_NON_LOOPBACK"] == "yes"
    assert metrics["METRICS_RECEIVER_AUTH"] == "credentials"
    for environment in (mcp, metrics):
        for key, value in environment.items():
            clean_settings_env.setenv(key, value.replace("${BIND_ADDRESS}", "192.0.2.10"))
    clean_settings_env.setenv("GRAPH_PROJECTOR_NEO4J_PASSWORD", "test-only-password")
    settings = Settings(_env_file=None)
    assert settings.mcp_http_allow_non_loopback and settings.metrics_allow_non_loopback
    assert settings.mcp_http_allowed_hosts == frozenset({"192.0.2.10:8765"})
    assert settings.metrics_receiver_auth == "credentials"
    assert not settings.mcp_http_stateless


def test_memory_budgets_have_headroom(compose: dict[str, Any]) -> None:
    limits = {
        "postgres": "512m",
        "neo4j": "2.5g",
        "restore": "512m",
        "migrate": "512m",
        "graph-bootstrap": "512m",
        "mcp": "512m",
        "metrics": "384m",
        "delivery-observer": "256m",
        "graph-recon": "256m",
    }
    assert {name: service["mem_limit"] for name, service in compose["services"].items()} == limits
    neo4j = compose["services"]["neo4j"]["environment"]
    assert neo4j["NEO4J_server_memory_heap_initial__size"] == "1G"
    assert neo4j["NEO4J_server_memory_heap_max__size"] == "1G"
    assert neo4j["NEO4J_server_memory_pagecache_size"] == "1G"
    text = COMPOSE.read_text(encoding="utf-8")
    for measurement in ("238 MiB", "224 MiB", "191 MiB", "124 MiB"):
        assert measurement in text


def test_json_logging_uses_one_shared_anchor(compose: dict[str, Any]) -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert text.count("BRAIN_LOG_FORMAT:") == 1
    assert "&brain-env" in text and "*brain-env" in text
    assert compose["x-brain-env"] == {"BRAIN_LOG_FORMAT": "json"}
    for name in BRAIN_SERVICES:
        assert compose["services"][name]["environment"]["BRAIN_LOG_FORMAT"] == "json"


def test_release_identity_uses_the_real_parser(
    compose: dict[str, Any], clean_settings_env: pytest.MonkeyPatch
) -> None:
    identity = compose["services"]["mcp"]["environment"]["BRAIN_FACTS_LIVE_RELEASE_IDENTITY"]
    assert identity == '{"release_sha":"${GIT_SHA}","package_version":"${VERSION}"}'
    sha, version = "a" * 40, "0.6.9"
    clean_settings_env.setenv(
        "BRAIN_FACTS_LIVE_RELEASE_IDENTITY",
        identity.replace("${GIT_SHA}", sha).replace("${VERSION}", version),
    )
    assert Settings(_env_file=None).facts_live_release_identity() == {
        "release_sha": sha,
        "package_version": version,
    }
    # The metrics runtime exposes collectors, not the measured-facts registry.
    assert "BRAIN_FACTS_LIVE_RELEASE_IDENTITY" not in compose["services"]["metrics"]["environment"]


def test_owner_credentials_never_reach_runtime_services(compose: dict[str, Any]) -> None:
    for name in {"restore", "migrate"}:
        assert compose["services"][name]["env_file"] == ["/etc/brain-v42/postgres-owner.env"]
    for name in BRAIN_SERVICES - {"restore", "migrate", "delivery-observer"}:
        files = compose["services"][name]["env_file"]
        assert "/etc/brain-v42/postgres-app.env" in files
        assert "/etc/brain-v42/postgres-owner.env" not in files
    observer = compose["services"]["delivery-observer"]
    assert "env_file" not in observer
    assert compose["secrets"]["observer_config"]["file"] == "/etc/brain-v42/delivery-observer.env"
    assert set(compose["services"]["mcp"]["env_file"]) == {
        "/etc/brain-v42/postgres-app.env",
        "/etc/brain-v42/graph-projector.env",
        "/etc/brain-v42/facts-identity.env",
        "/etc/brain-v42/delivery-mcp.env",
    }
    for service in compose["services"].values():
        environment = service.get("environment", {})
        assert not any(
            key in environment
            for key in {
                "POSTGRES_URL",
                "BRAIN_POSTGRES_URL",
                "BRAIN_DELIVERY_POSTGRES_URL",
                "MCP_HTTP_TOKEN",
                "BRAIN_MCP_HTTP_TOKEN",
                "GRAPH_PROJECTOR_NEO4J_PASSWORD",
                "NEO4J_PASSWORD",
                "POSTGRES_PASSWORD",
                "NEO4J_AUTH",
                "EMBEDDING_API_KEY",
                "BRAIN_EMBEDDING_API_KEY",
            }
        )


def test_absolute_secret_sources_have_one_reader_uid_and_operator_instructions(
    compose: dict[str, Any],
) -> None:
    readers: dict[str, set[int]] = {}
    header = COMPOSE.read_text(encoding="utf-8").split("name:", 1)[0]
    for name, service in compose["services"].items():
        uid = {"postgres": 999, "neo4j": 7474}.get(name, 1000)
        if name in BRAIN_SERVICES:
            assert service["user"] == "1000:1000"
        for path in service.get("env_file", []):
            assert path.startswith("/etc/brain-v42/") and path.endswith(".env")
            assert "${" not in path
            # The rail runs `docker compose` over ssh as its dedicated deploy user
            # (red-rail, uid 1001 on red-base): the compose CLI, not the daemon,
            # reads env_files.
            assert f"{path}: uid 1001, mode 0600" in header
        for secret in service.get("secrets", []):
            source = compose["secrets"][secret]["file"]
            assert source.startswith("/etc/brain-v42/") and "${" not in source
            readers.setdefault(source, set()).add(uid)
            assert f"{source}: uid {uid}, mode 0600" in header
    assert all(len(uids) == 1 for uids in readers.values())
    assert "uid 0" not in header
    assert "the compose CLI reads env_files as the rail deploy user" in header
    assert set(readers) == {secret["file"] for secret in compose["secrets"].values()}


def test_file_credentials_and_hosted_embeddings(
    compose: dict[str, Any], clean_settings_env: pytest.MonkeyPatch
) -> None:
    bindings = {
        "postgres": ("POSTGRES_PASSWORD_FILE", "postgres_password"),
        "neo4j": ("NEO4J_AUTH_FILE", "neo4j_auth"),
        "migrate": ("BRAIN_APP_PASSWORD_FILE", "brain_app_password"),
        "mcp": ("BRAIN_EMBEDDING_TOKEN_FILE", "embedding_token"),
        "metrics": ("BRAIN_EMBEDDING_TOKEN_FILE", "embedding_token"),
    }
    for name, (variable, secret) in bindings.items():
        service = compose["services"][name]
        assert secret in service["secrets"]
        assert service["environment"][variable] == f"/run/secrets/{secret}"
    assert compose["services"]["migrate"]["environment"]["BRAIN_ALEMBIC_ALLOW_PROD"] == "true"
    assert "github_app_key" in compose["services"]["delivery-observer"]["secrets"]
    header = COMPOSE.read_text(encoding="utf-8").split("name:", 1)[0]
    assert "BRAIN_DELIVERY_GITHUB_PRIVATE_KEY_PATH=/run/secrets/github_app_key" in header
    for name in {"mcp", "metrics"}:
        environment = compose["services"][name]["environment"]
        assert environment["EMBEDDING_BACKEND"] == "openai"
        assert environment["EMBEDDING_SERVICE_URL"] == "https://api.mistral.ai"
        assert environment["EMBEDDING_MODEL"] == "codestral-embed-2505"
        assert (
            environment["EMBEDDING_DIMENSION"]
            == str(Settings(_env_file=None).embedding_dimension)
            == "1536"
        )


def test_activity_directory_allows_a_token_created_after_deployment(
    compose: dict[str, Any],
) -> None:
    mcp = compose["services"]["mcp"]
    assert mcp["volumes"] == ["/etc/brain-v42/activity:/etc/brain-v42/activity:ro"]
    assert mcp["environment"]["BRAIN_CLIENT_ACTIVITY_TOKEN_FILE"] == "/etc/brain-v42/activity/token"
    environment = mcp["environment"]
    assert environment["BRAIN_CLIENT_ACTIVITY_REPORTING_ENABLED"] == "true"
    url = urlsplit(environment["BRAIN_CLIENT_ACTIVITY_URL"])
    assert url.scheme == "http" and url.port == 9200 and url.path == "/v1/client-activity"
    assert url.hostname == environment["BRAIN_CLIENT_ACTIVITY_ALLOWED_HOST"] == "metrics"
    assert url.hostname in compose["services"]
    assert "metrics" not in mcp["depends_on"]
    header = COMPOSE.read_text(encoding="utf-8").split("name:", 1)[0]
    assert "BRAIN_CLIENT_ACTIVITY_ALLOWED_HOST=metrics" in header
    assert "brain-v42-mcp" in header and "telemetry" in header


def test_restore_input_and_bootstrap_marker_source(compose: dict[str, Any]) -> None:
    restore = compose["services"]["restore"]
    assert restore["volumes"] == ["/srv/brain-v42/restore:/restore:ro"]
    assert restore["command"] == ["brain-v42-restore", "--dump", "/restore/brain.dump"]
    assert compose["services"]["migrate"]["command"] == ["brain-v42-migrate"]
    assert compose["services"]["graph-bootstrap"]["command"] == ["brain-v42-graph-bootstrap"]
    assert "volumes" not in compose["services"]["graph-bootstrap"]
    header = COMPOSE.read_text(encoding="utf-8").split("name:", 1)[0]
    for path in ("/srv/brain-v42/restore/brain.dump", "/srv/brain-v42/restore/brain.dump.sha256"):
        assert f"{path}: root:1000, mode 0640" in header
    assert "/srv/brain-v42/restore: root:1000, mode 0750" in header


def test_rerank_is_a_named_replaceable_block(compose: dict[str, Any]) -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "x-rerank-env: &rerank-env" in text
    assert "lot 0.6.10 replaces this block with the hosted rerank settings" in text
    assert compose["x-rerank-env"] == {"BRAIN_RERANK_BACKEND": "none"}
    assert "*rerank-env" in text
    assert compose["services"]["mcp"]["environment"]["BRAIN_RERANK_BACKEND"] == "none"
    assert ":8003" not in text and "RERANKER_URL" not in text


def test_graph_recon_sleeps_before_each_weekly_inventory(compose: dict[str, Any]) -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "workstation's brain-v42-graph-recon unit" in text
    command = compose["services"]["graph-recon"]["command"]
    assert command[:2] == ["/bin/sh", "-c"]
    assert re.fullmatch(
        r"while sleep 604800; do\s+python -m brain_v42\.scripts\.rebuild_graph_projection\s*\|\| exit 1;\s*done\s*",
        command[2],
    )
    assert (
        compose["services"]["graph-recon"]["environment"]
        == compose["services"]["graph-bootstrap"]["environment"]
    )


def test_projector_is_private_to_the_graph_processes(compose: dict[str, Any]) -> None:
    for name in {"mcp", "graph-bootstrap", "graph-recon"}:
        service = compose["services"][name]
        assert "/etc/brain-v42/graph-projector.env" in service["env_file"]
        environment = service["environment"]
        for key in {"GRAPH_ENABLED", "GRAPH_LEDGER_WRITE_ENABLED", "GRAPH_PROJECTOR_ENABLED"}:
            assert environment[key] == "true"
        assert environment["GRAPH_PROJECTOR_NEO4J_URL"] == "bolt://neo4j:7687"
        assert environment["GRAPH_PROJECTOR_NEO4J_USER"] == "neo4j"
    metrics = compose["services"]["metrics"]
    assert metrics["environment"]["GRAPH_PROJECTOR_ENABLED"] == "false"
    assert metrics["environment"]["METRICS_LEGACY_AUTOMATION_ENABLED"] == "false"
    assert "/etc/brain-v42/graph-projector.env" not in metrics["env_file"]


def test_persistent_volume_names_are_stable_for_empty_volume_retries(
    compose: dict[str, Any],
) -> None:
    volumes = {"postgres-data": "brain-v42-postgres-data", "neo4j-data": "brain-v42-neo4j-data"}
    assert compose["volumes"] == {key: {"name": name} for key, name in volumes.items()}
    assert compose["services"]["postgres"]["volumes"] == ["postgres-data:/var/lib/postgresql/data"]
    assert compose["services"]["neo4j"]["volumes"] == ["neo4j-data:/data"]
    header = COMPOSE.read_text(encoding="utf-8").split("name:", 1)[0]
    assert all(name in header for name in volumes.values())


def test_observer_configuration_is_expressible_without_a_root_owned_file_in_the_image(
    compose: dict[str, Any], clean_settings_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observer = compose["services"]["delivery-observer"]
    assert observer["command"] == [
        "python",
        "-m",
        "brain_v42.delivery_observer",
        "--env-file",
        "/run/secrets/observer_config",
    ]
    assert "env_file" not in observer
    assert compose["secrets"]["observer_config"]["file"] == "/etc/brain-v42/delivery-observer.env"
    assert observer["environment"] == {"BRAIN_LOG_FORMAT": "json"}
    assert "observer_config" in observer["secrets"]
    assert observer["user"] == "1000:1000"
    header = COMPOSE.read_text(encoding="utf-8").split("name:", 1)[0]
    assert "/etc/brain-v42/delivery-observer.env: uid 1000, mode 0600" in header
    assert "BRAIN_DELIVERY_POSTGRES_URL for brain_app" in header
    for key, value in observer["environment"].items():
        clean_settings_env.setenv(key, value)
    key_path = tmp_path / "app-key"
    key_path.write_text("test-only-key", encoding="utf-8")
    key_path.chmod(0o600)
    config_path = tmp_path / "observer-config.env"
    config_path.write_text(
        "BRAIN_DELIVERY_ENABLED=true\n"
        "BRAIN_DELIVERY_POSTGRES_URL=postgresql+asyncpg://brain_app@postgres:5432/brain_test\n"
        "BRAIN_DELIVERY_GITHUB_APP_ID=1\n"
        "BRAIN_DELIVERY_GITHUB_INSTALLATION_ID=1\n"
        f"BRAIN_DELIVERY_GITHUB_PRIVATE_KEY_PATH={key_path}\n",
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    settings = load_observer_settings(config_path)
    assert settings.enabled and settings.postgres_url is not None
    assert settings.postgres_url.get_secret_value() == (
        "postgresql+asyncpg://brain_app@postgres:5432/brain_test"
    )
    assert settings.github_app_id == settings.github_installation_id == 1
    assert settings.github_private_key_path == key_path


def test_only_the_six_rail_variables_are_interpolated(compose: dict[str, Any]) -> None:
    # YAML loading first catches malformed syntax before inspecting interpolation.
    assert isinstance(compose, dict)
    references = re.findall(r"\$\{([^}]+)\}", COMPOSE.read_text(encoding="utf-8"))
    assert references
    assert set(references) <= RAIL_VARIABLES
