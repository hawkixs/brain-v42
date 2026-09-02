"""
TDD tests for Feature #606: Docker Compose PostgreSQL + pgvector

These tests validate the docker-compose.yml structure and supporting files
without requiring Docker to be running. They parse and validate the YAML
configuration to ensure correctness.

Red phase: all tests MUST fail before implementation.
Green phase: implement docker-compose.yml + data/postgres/.gitkeep + .gitignore
"""

from pathlib import Path

import pytest
import yaml

# Repository root is two levels up from tests/unit/
REPO_ROOT = Path(__file__).parent.parent.parent


class TestDockerComposeFileExists:
    """Verify the docker-compose.yml file exists at the repo root."""

    def test_docker_compose_file_exists(self) -> None:
        """docker-compose.yml must exist at the repository root."""
        compose_file = REPO_ROOT / "docker-compose.yml"
        assert compose_file.exists(), (
            f"docker-compose.yml not found at {compose_file}. Create it at the repo root."
        )

    def test_docker_compose_is_valid_yaml(self) -> None:
        """docker-compose.yml must be parseable YAML."""
        compose_file = REPO_ROOT / "docker-compose.yml"
        with compose_file.open() as f:
            config = yaml.safe_load(f)
        assert config is not None, "docker-compose.yml is empty"
        assert isinstance(config, dict), "docker-compose.yml must be a YAML mapping"


class TestDockerComposeServices:
    """Validate the services section of docker-compose.yml."""

    @pytest.fixture
    def compose_config(self) -> dict:  # type: ignore[type-arg]
        """Load and return the parsed docker-compose.yml."""
        compose_file = REPO_ROOT / "docker-compose.yml"
        with compose_file.open() as f:
            return yaml.safe_load(f)  # type: ignore[return-value]

    def test_services_key_exists(self, compose_config: dict) -> None:  # type: ignore[type-arg]
        """docker-compose.yml must have a 'services' key."""
        assert "services" in compose_config, "docker-compose.yml must have a 'services' section"

    def test_postgres_service_exists(self, compose_config: dict) -> None:  # type: ignore[type-arg]
        """A 'postgres' service must be defined."""
        services = compose_config.get("services", {})
        assert "postgres" in services, "A 'postgres' service must be defined under 'services'"

    def test_postgres_uses_pgvector_image(self, compose_config: dict) -> None:  # type: ignore[type-arg]
        """postgres service must use pgvector/pgvector:pg16 image."""
        postgres = compose_config["services"]["postgres"]
        assert postgres.get("image") == (
            "pgvector/pgvector:pg16@"
            "sha256:1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb"
        ), f"Expected the digest-pinned pgvector/pgvector:pg16 image, got '{postgres.get('image')}'"

    def test_postgres_container_name(self, compose_config: dict) -> None:  # type: ignore[type-arg]
        """postgres service must have container_name brain_v42_postgres."""
        postgres = compose_config["services"]["postgres"]
        assert postgres.get("container_name") == "brain_v42_postgres", (
            f"Expected container_name 'brain_v42_postgres', got '{postgres.get('container_name')}'"
        )

    def test_postgres_restart_policy(self, compose_config: dict) -> None:  # type: ignore[type-arg]
        """postgres service must have restart: unless-stopped."""
        postgres = compose_config["services"]["postgres"]
        assert postgres.get("restart") == "unless-stopped", (
            f"Expected restart 'unless-stopped', got '{postgres.get('restart')}'"
        )

    def test_no_version_key(self, compose_config: dict) -> None:  # type: ignore[type-arg]
        """Modern Docker Compose v2+ does not require the 'version' key.

        Including it triggers a deprecation warning. It must be absent.
        """
        assert "version" not in compose_config, (
            "Remove the 'version' key — it is deprecated in Docker Compose v2+"
        )


class TestDockerComposePortMapping:
    """Validate port configuration: host 5433 -> container 5432."""

    @pytest.fixture
    def postgres_service(self) -> dict:  # type: ignore[type-arg]
        """Load the postgres service config."""
        compose_file = REPO_ROOT / "docker-compose.yml"
        with compose_file.open() as f:
            config = yaml.safe_load(f)
        return config["services"]["postgres"]  # type: ignore[return-value]

    def test_ports_key_exists(self, postgres_service: dict) -> None:  # type: ignore[type-arg]
        """postgres service must define port mappings."""
        assert "ports" in postgres_service, "postgres service must define 'ports'"

    def test_host_port_is_5433(self, postgres_service: dict) -> None:  # type: ignore[type-arg]
        """Host port must be 5433 (not 5432) to avoid conflict with existing PG."""
        ports = postgres_service["ports"]
        # Ports can be strings like "5433:5432" or dicts
        port_strings = [str(p) for p in ports]
        assert any("5433" in p for p in port_strings), (
            f"Expected host port 5433 in ports, got: {ports}"
        )

    def test_container_port_is_5432(self, postgres_service: dict) -> None:  # type: ignore[type-arg]
        """Container port must be 5432 (standard PostgreSQL port)."""
        ports = postgres_service["ports"]
        port_strings = [str(p) for p in ports]
        # Standard mapping is "5433:5432"
        assert any("5432" in p for p in port_strings), (
            f"Expected container port 5432 in ports, got: {ports}"
        )

    def test_port_mapping_format(self, postgres_service: dict) -> None:  # type: ignore[type-arg]
        """Port mapping must be loopback-bound '127.0.0.1:5433:5432'.

        Audit sécu 2026-07-03 : le port publié sans IP de bind écoutait sur
        0.0.0.0 (LAN entier) avec les creds par défaut. Les clients hôte
        passent par localhost:5433, les containers par le réseau docker
        brain_v42_default — rien ne justifie une exposition LAN.
        """
        ports = postgres_service["ports"]
        port_strings = [str(p) for p in ports]
        assert any(p == "127.0.0.1:5433:5432" for p in port_strings), (
            f"Expected loopback-bound mapping '127.0.0.1:5433:5432', got: {ports}"
        )


class TestDockerComposeEnvironment:
    """Validate environment variables for the postgres service."""

    @pytest.fixture
    def postgres_env(self) -> dict:  # type: ignore[type-arg]
        """Load environment variables from the postgres service."""
        compose_file = REPO_ROOT / "docker-compose.yml"
        with compose_file.open() as f:
            config = yaml.safe_load(f)
        postgres = config["services"]["postgres"]
        env = postgres.get("environment", {})
        # environment can be a dict or a list of "KEY=VALUE" strings
        if isinstance(env, list):
            result = {}
            for item in env:
                key, _, value = item.partition("=")
                result[key] = value
            return result  # type: ignore[return-value]
        return env  # type: ignore[return-value]

    def test_postgres_user_defaults_to_brain(self, postgres_env: dict) -> None:  # type: ignore[type-arg]
        """POSTGRES_USER must default to 'brain' (override-capable via .env)."""
        assert postgres_env.get("POSTGRES_USER") in ("brain", "${POSTGRES_USER:-brain}"), (
            f"Expected POSTGRES_USER default brain, got '{postgres_env.get('POSTGRES_USER')}'"
        )

    def test_postgres_password_defaults_to_brain(self, postgres_env: dict) -> None:  # type: ignore[type-arg]
        """POSTGRES_PASSWORD must default to 'brain' (override-capable via .env)."""
        assert postgres_env.get("POSTGRES_PASSWORD") in (
            "brain",
            "${POSTGRES_PASSWORD:-brain}",
        ), (
            f"Expected POSTGRES_PASSWORD default brain, got '{postgres_env.get('POSTGRES_PASSWORD')}'"
        )

    def test_postgres_db_defaults_to_brain(self, postgres_env: dict) -> None:  # type: ignore[type-arg]
        """POSTGRES_DB must default to 'brain' (override-capable via .env)."""
        assert postgres_env.get("POSTGRES_DB") in ("brain", "${POSTGRES_DB:-brain}"), (
            f"Expected POSTGRES_DB default brain, got '{postgres_env.get('POSTGRES_DB')}'"
        )


class TestDockerComposeVolumes:
    """Validate volume configuration for the postgres service."""

    @pytest.fixture
    def postgres_service(self) -> dict:  # type: ignore[type-arg]
        """Load the postgres service config."""
        compose_file = REPO_ROOT / "docker-compose.yml"
        with compose_file.open() as f:
            config = yaml.safe_load(f)
        return config["services"]["postgres"]  # type: ignore[return-value]

    def test_volumes_key_exists(self, postgres_service: dict) -> None:  # type: ignore[type-arg]
        """postgres service must define volumes."""
        assert "volumes" in postgres_service, "postgres service must define 'volumes'"

    def test_volume_uses_bind_mount(self, postgres_service: dict) -> None:  # type: ignore[type-arg]
        """Volume must use bind-mount to ./data/postgres."""
        volumes = postgres_service["volumes"]
        volume_strings = [str(v) for v in volumes]
        assert any("data/postgres" in v for v in volume_strings), (
            f"Expected volume with './data/postgres', got: {volumes}"
        )

    def test_volume_maps_to_pg_data(self, postgres_service: dict) -> None:  # type: ignore[type-arg]
        """Volume must map to /var/lib/postgresql/data inside the container."""
        volumes = postgres_service["volumes"]
        volume_strings = [str(v) for v in volumes]
        assert any("/var/lib/postgresql/data" in v for v in volume_strings), (
            f"Expected '/var/lib/postgresql/data' in volumes, got: {volumes}"
        )


class TestDockerComposeHealthcheck:
    """Validate healthcheck configuration for the postgres service."""

    @pytest.fixture
    def healthcheck(self) -> dict:  # type: ignore[type-arg]
        """Load healthcheck config from the postgres service."""
        compose_file = REPO_ROOT / "docker-compose.yml"
        with compose_file.open() as f:
            config = yaml.safe_load(f)
        postgres = config["services"]["postgres"]
        assert "healthcheck" in postgres, "postgres service must define 'healthcheck'"
        return postgres["healthcheck"]  # type: ignore[return-value]

    def test_healthcheck_uses_pg_isready(self, healthcheck: dict) -> None:  # type: ignore[type-arg]
        """Healthcheck must use pg_isready."""
        test_cmd = str(healthcheck.get("test", ""))
        assert "pg_isready" in test_cmd, f"Healthcheck test must use pg_isready, got: {test_cmd}"

    def test_healthcheck_checks_brain_user(self, healthcheck: dict) -> None:  # type: ignore[type-arg]
        """Healthcheck must check the 'brain' user."""
        test_cmd = str(healthcheck.get("test", ""))
        assert "-U brain" in test_cmd or "brain" in test_cmd, (
            f"Healthcheck must check -U brain, got: {test_cmd}"
        )

    def test_healthcheck_checks_brain_db(self, healthcheck: dict) -> None:  # type: ignore[type-arg]
        """Healthcheck must check the 'brain' database (default, override-capable)."""
        test_cmd = str(healthcheck.get("test", ""))
        assert "-d brain" in test_cmd or "-d ${POSTGRES_DB:-brain}" in test_cmd, (
            f"Healthcheck must check -d brain, got: {test_cmd}"
        )

    def test_healthcheck_interval(self, healthcheck: dict) -> None:  # type: ignore[type-arg]
        """Healthcheck interval must be 5s."""
        assert healthcheck.get("interval") == "5s", (
            f"Expected interval '5s', got '{healthcheck.get('interval')}'"
        )

    def test_healthcheck_retries(self, healthcheck: dict) -> None:  # type: ignore[type-arg]
        """Healthcheck retries must be 5."""
        assert healthcheck.get("retries") == 5, (
            f"Expected retries 5, got '{healthcheck.get('retries')}'"
        )

    def test_healthcheck_start_period(self, healthcheck: dict) -> None:  # type: ignore[type-arg]
        """Healthcheck start_period must be 10s to allow PG initialization."""
        assert healthcheck.get("start_period") == "10s", (
            f"Expected start_period '10s', got '{healthcheck.get('start_period')}'"
        )

    def test_healthcheck_timeout(self, healthcheck: dict) -> None:  # type: ignore[type-arg]
        """Healthcheck timeout must be defined."""
        assert "timeout" in healthcheck, "Healthcheck must define 'timeout'"


class TestNeo4jPrivateAuthentication:
    """Keep Neo4j credentials out of Compose metadata and process arguments."""

    @pytest.fixture
    def compose_config(self) -> dict:  # type: ignore[type-arg]
        compose_file = REPO_ROOT / "docker-compose.yml"
        with compose_file.open() as f:
            return yaml.safe_load(f)  # type: ignore[return-value]

    def test_neo4j_reads_auth_from_a_compose_secret(self, compose_config: dict) -> None:  # type: ignore[type-arg]
        neo4j = compose_config["services"]["neo4j"]
        environment = neo4j.get("environment", {})

        assert "NEO4J_AUTH" not in environment
        assert environment.get("NEO4J_AUTH_FILE") == "/run/secrets/neo4j_auth"
        assert "neo4j_auth" in neo4j.get("secrets", [])

    def test_neo4j_image_is_pinned_to_the_validated_digest(self, compose_config: dict) -> None:  # type: ignore[type-arg]
        assert compose_config["services"]["neo4j"]["image"] == (
            "neo4j:5.26.21@sha256:409728716bc239f9fa046368ac6ce6ef280f9e5f0bcb7cdd75031a4465cc192d"
        )

    def test_neo4j_auth_secret_comes_from_a_private_host_file(self, compose_config: dict) -> None:  # type: ignore[type-arg]
        secret = compose_config.get("secrets", {}).get("neo4j_auth", {})
        assert secret.get("file") == ("${BRAIN_NEO4J_AUTH_FILE:-./.secrets/neo4j-auth}")

    def test_neo4j_healthcheck_never_receives_credentials(self, compose_config: dict) -> None:  # type: ignore[type-arg]
        healthcheck = compose_config["services"]["neo4j"]["healthcheck"]
        command = " ".join(str(part) for part in healthcheck.get("test", []))
        lowered = command.lower()

        assert "wget" in lowered
        assert "http://127.0.0.1:7474/" in lowered
        for forbidden in (
            "cypher-shell",
            "neo4j_password",
            "password",
            " -p ",
        ):
            assert forbidden not in lowered


class TestEmbeddingShimBearerWiring:
    """Pin how the shim receives its static bearer: by file, in optional mode.

    The value itself must never reach Compose metadata — `docker inspect` on
    the container prints `Config.Env` verbatim, so a token wired as a plain
    variable would be readable by anyone who can talk to the daemon.
    """

    @pytest.fixture
    def compose_config(self) -> dict:  # type: ignore[type-arg]
        compose_file = REPO_ROOT / "docker-compose.yml"
        with compose_file.open() as f:
            return yaml.safe_load(f)  # type: ignore[return-value]

    @staticmethod
    def _environment(service: dict) -> dict:  # type: ignore[type-arg]
        """Compose accepts both forms; the shim block uses the list form."""
        raw = service.get("environment", {})
        if isinstance(raw, dict):
            return {str(key): str(value) for key, value in raw.items()}
        return dict(str(item).split("=", 1) for item in raw)

    def test_shim_reads_its_bearer_from_a_compose_secret(self, compose_config: dict) -> None:  # type: ignore[type-arg]
        shim = compose_config["services"]["embedding-shim"]
        environment = self._environment(shim)

        assert environment.get("SHIM_BEARER_TOKEN_FILE") == "/run/secrets/embedding_shim_bearer"
        assert "embedding_shim_bearer" in shim.get("secrets", [])

    def test_shim_bearer_mode_is_optional_and_never_required(self, compose_config: dict) -> None:  # type: ignore[type-arg]
        """`optional` accepts and logs; `required` would 401 every client.

        Six auto-discord containers reach :8003 on brain-net carrying no bearer
        at all. Arming this to `required` is a separate operator gesture that
        waits on the client-side ticket, so the versioned target pins the
        census mode and nothing else.
        """
        environment = self._environment(compose_config["services"]["embedding-shim"])

        assert environment.get("SHIM_BEARER_MODE") == "optional"

    def test_shim_bearer_secret_comes_from_a_private_host_file(self, compose_config: dict) -> None:  # type: ignore[type-arg]
        secret = compose_config.get("secrets", {}).get("embedding_shim_bearer", {})

        assert secret.get("file") == ("${BRAIN_SHIM_BEARER_FILE:-./.secrets/embedding-shim-bearer}")

    def test_the_bearer_value_never_lands_in_compose_metadata(self, compose_config: dict) -> None:  # type: ignore[type-arg]
        """Only the PATH is wired; a `SHIM_BEARER_TOKEN` variable would leak."""
        environment = self._environment(compose_config["services"]["embedding-shim"])

        assert "SHIM_BEARER_TOKEN" not in environment
        for name, value in environment.items():
            assert "BEARER" not in name or value.startswith(("/run/secrets/", "optional"))


class TestGitignore:
    """Validate .gitignore entries for data/postgres."""

    @pytest.fixture
    def gitignore_content(self) -> str:
        """Read .gitignore content."""
        gitignore = REPO_ROOT / ".gitignore"
        assert gitignore.exists(), ".gitignore must exist"
        return gitignore.read_text()

    def test_gitignore_ignores_postgres_data(self, gitignore_content: str) -> None:
        """data/postgres/ must be ignored to prevent committing PG data files.

        The whole container-owned dir is ignored (not `data/postgres/*` + a
        keepfile negation), so git never descends into the 0700 dir.
        """
        assert "data/postgres/" in gitignore_content, (
            "'.gitignore' must ignore data/postgres/ (PG data files)"
        )

    def test_gitignore_ignores_local_secret_directory(self, gitignore_content: str) -> None:
        """Compose's local secret source must never be committed."""
        assert ".secrets/" in gitignore_content

    def test_gitignore_does_not_force_track_container_owned_keepfile(
        self, gitignore_content: str
    ) -> None:
        """The container-owned data/postgres/.gitkeep must NOT be force-tracked.

        Postgres owns data/postgres as mode 0700; a `!data/postgres/.gitkeep`
        negation forces git to descend into the unreadable dir and fail with
        exit-128 on status/diff/clean. The keep-file is anchored at the
        host-owned data/.gitkeep level instead.
        """
        assert "!data/postgres/.gitkeep" not in gitignore_content, (
            "must NOT force-track data/postgres/.gitkeep (causes git exit-128); "
            "anchor the keep-file at data/.gitkeep instead"
        )
        assert (REPO_ROOT / "data" / ".gitkeep").exists(), (
            "data/.gitkeep must exist as the host-owned directory anchor"
        )

    def test_gitignore_has_postgres_data_section(self, gitignore_content: str) -> None:
        """gitignore should have a comment explaining the PostgreSQL data section."""
        # Check for either the comment or the entries being present
        has_comment = "PostgreSQL" in gitignore_content or "postgres" in gitignore_content.lower()
        assert has_comment, "'.gitignore' should have a section for PostgreSQL data"
