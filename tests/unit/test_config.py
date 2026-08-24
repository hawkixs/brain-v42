"""Tests for brain_v42 configuration (TDD Red phase)."""

import pytest
from pydantic import SecretStr, ValidationError


def test_settings_default_values(monkeypatch):
    """Settings has sensible defaults for all optional fields.

    Uses monkeypatch.delenv so the test doesn't depend on a developer's
    local .env (env vars override defaults in pydantic-settings).
    """
    monkeypatch.delenv("EMBEDDING_SERVICE_URL", raising=False)
    monkeypatch.delenv("RERANKER_URL", raising=False)
    monkeypatch.delenv("EMBEDDING_DIMENSION", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    # BRAIN_-prefixed aliases win over the bare legacy name (see _brain_alias
    # in brain_v42.config); the CI workflows set BRAIN_LOG_LEVEL globally, so
    # this test must clear it too or it silently reads the CI value instead
    # of the field default.
    monkeypatch.delenv("BRAIN_EMBEDDING_SERVICE_URL", raising=False)
    monkeypatch.delenv("BRAIN_RERANKER_URL", raising=False)
    monkeypatch.delenv("BRAIN_EMBEDDING_DIMENSION", raising=False)
    monkeypatch.delenv("BRAIN_LOG_LEVEL", raising=False)
    from brain_v42.config import Settings

    s = Settings(postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain")
    assert s.log_level == "INFO"
    assert s.embedding_service_url == "http://localhost:8003"
    assert s.reranker_url == "http://localhost:8003"
    assert s.embedding_dimension == 1536


def test_dream_capability_settings_are_dormant_and_secret_safe_by_default(
    monkeypatch,
) -> None:
    """The capability firewall is opt-in and its raw registry is never rendered."""
    monkeypatch.delenv("BRAIN_DREAM_CAPABILITY_ENFORCEMENT", raising=False)
    monkeypatch.delenv("MCP_HTTP_DREAM_TOKENS", raising=False)
    from brain_v42.config import Settings

    assert "brain_dream_capability_enforcement" in Settings.model_fields
    assert "mcp_http_dream_tokens" in Settings.model_fields

    settings = Settings(
        postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
        _env_file=None,  # type: ignore[call-arg]
    )

    assert settings.brain_dream_capability_enforcement is False
    assert isinstance(settings.mcp_http_dream_tokens, SecretStr)
    assert settings.mcp_http_dream_tokens.get_secret_value() == ""


def test_dream_capability_settings_load_from_environment_without_rendering_registry(
    monkeypatch,
) -> None:
    """The shared systemd environment contract loads as a redacted SecretStr."""
    raw_registry = '{"brain-v42:scan":{"active":"registry-super-secret"}}'
    monkeypatch.setenv("BRAIN_DREAM_CAPABILITY_ENFORCEMENT", "true")
    monkeypatch.setenv("MCP_HTTP_DREAM_TOKENS", raw_registry)
    from brain_v42.config import Settings

    settings = Settings(
        postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
        _env_file=None,  # type: ignore[call-arg]
    )

    assert settings.brain_dream_capability_enforcement is True
    assert settings.mcp_http_dream_tokens.get_secret_value() == raw_registry
    assert "registry-super-secret" not in repr(settings)
    assert "registry-super-secret" not in str(settings)


def test_settings_postgres_url_required():
    """postgres_url must be provided (no default)."""
    from brain_v42.config import Settings

    with pytest.raises(ValidationError):
        Settings(postgres_url=None)  # type: ignore


def test_settings_postgres_url_asyncpg_scheme():
    """postgres_url must use postgresql+asyncpg scheme."""
    from brain_v42.config import Settings

    s = Settings(postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain")
    assert s.postgres_url.startswith("postgresql+asyncpg://")


def test_settings_postgres_url_rejects_sync_scheme():
    """postgres_url must not accept plain postgresql:// (sync driver incompatible)."""
    from brain_v42.config import Settings

    with pytest.raises(ValidationError):
        Settings(postgres_url="postgresql://brain:brain@localhost:5433/brain")


def test_invalid_postgres_scheme_does_not_render_credentials() -> None:
    """Startup validation must not echo a malformed secret-bearing DSN."""
    from brain_v42.config import Settings

    secret = "postgres-validation-secret-canary"
    invalid_url = f"postgresql://brain:{secret}@localhost:5433/brain"

    with pytest.raises(ValidationError) as exc_info:
        Settings(postgres_url=invalid_url, _env_file=None)  # type: ignore[call-arg]

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert secret not in rendered
    assert invalid_url not in rendered


def test_settings_log_level_validation():
    """log_level must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL."""
    from brain_v42.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
            log_level="INVALID",  # type: ignore
        )


def test_settings_loaded_from_env(monkeypatch):
    """Settings are loaded from environment variables."""
    # BRAIN_LOG_LEVEL (set globally by CI) takes alias priority over the bare
    # LOG_LEVEL this test exercises; clear it so the assertion below tests
    # the legacy bare-name path this test is actually about.
    monkeypatch.delenv("BRAIN_LOG_LEVEL", raising=False)
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://user:pass@host:5433/db")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("EMBEDDING_SERVICE_URL", "http://gpu-server:8003")
    from importlib import reload

    import brain_v42.config as cfg

    reload(cfg)
    s = cfg.Settings()
    assert s.postgres_url == "postgresql+asyncpg://user:pass@host:5433/db"
    assert s.log_level == "DEBUG"
    assert s.embedding_service_url == "http://gpu-server:8003"


def test_settings_no_redis_config():
    """Settings must NOT have redis_url attribute (legacy removed)."""
    from brain_v42.config import Settings

    s = Settings(postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain")
    assert not hasattr(s, "redis_url")


def test_settings_no_neo4j_uri():
    """Settings must NOT have neo4j_uri (legacy name — replaced by neo4j_url)."""
    from brain_v42.config import Settings

    s = Settings(postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain")
    assert not hasattr(s, "neo4j_uri")


def test_graph_ledger_is_dormant_and_bounded_by_default(monkeypatch) -> None:
    """The canonical ledger can be deployed before its projection cutover."""
    monkeypatch.delenv("GRAPH_LEDGER_WRITE_ENABLED", raising=False)
    monkeypatch.delenv("GRAPH_OUTBOX_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("GRAPH_OUTBOX_BATCH_SIZE", raising=False)
    monkeypatch.delenv("GRAPH_OUTBOX_MAX_ATTEMPTS", raising=False)
    from brain_v42.config import Settings

    settings = Settings(
        postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
        _env_file=None,  # type: ignore[call-arg]
    )

    assert settings.graph_ledger_write_enabled is False
    assert settings.graph_outbox_interval_seconds == 5.0
    assert settings.graph_outbox_batch_size == 100
    assert settings.graph_outbox_max_attempts == 10


def test_graph_ledger_cutover_settings_load_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("GRAPH_LEDGER_WRITE_ENABLED", "true")
    monkeypatch.setenv("GRAPH_PROJECTOR_ENABLED", "true")
    monkeypatch.setenv("GRAPH_ENABLED", "true")
    monkeypatch.setenv("GRAPH_PROJECTOR_NEO4J_URL", "bolt://127.0.0.1:7687")
    monkeypatch.setenv("GRAPH_PROJECTOR_NEO4J_USER", "projector")
    monkeypatch.setenv("GRAPH_PROJECTOR_NEO4J_PASSWORD", "projector-secret")
    monkeypatch.setenv("GRAPH_OUTBOX_INTERVAL_SECONDS", "2.5")
    monkeypatch.setenv("GRAPH_OUTBOX_BATCH_SIZE", "40")
    monkeypatch.setenv("GRAPH_OUTBOX_MAX_ATTEMPTS", "7")
    from brain_v42.config import Settings

    settings = Settings(
        postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
        _env_file=None,  # type: ignore[call-arg]
    )

    assert settings.graph_ledger_write_enabled is True
    assert settings.graph_projector_enabled is True
    assert settings.graph_projector_neo4j_url == "bolt://127.0.0.1:7687"
    assert settings.graph_projector_neo4j_user == "projector"
    assert settings.graph_projector_neo4j_password.get_secret_value() == "projector-secret"
    assert settings.graph_outbox_interval_seconds == 2.5
    assert settings.graph_outbox_batch_size == 40
    assert settings.graph_outbox_max_attempts == 7


def test_graph_ledger_rejects_cutover_without_graph_projection() -> None:
    from brain_v42.config import Settings

    with pytest.raises(ValidationError, match="requires graph_enabled"):
        Settings(
            postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
            graph_ledger_write_enabled=True,
            graph_enabled=False,
            _env_file=None,  # type: ignore[call-arg]
        )


def test_graph_projector_rejects_cutover_without_isolated_neo4j_url() -> None:
    from brain_v42.config import Settings

    with pytest.raises(ValidationError, match="requires isolated Neo4j credentials"):
        Settings(
            postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
            graph_ledger_write_enabled=True,
            graph_projector_enabled=True,
            graph_projector_neo4j_password="projector-secret",
            graph_enabled=True,
            _env_file=None,  # type: ignore[call-arg]
        )


def test_graph_projector_credentials_are_isolated_and_redacted() -> None:
    from brain_v42.config import Settings

    settings = Settings(
        postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
        graph_enabled=True,
        graph_ledger_write_enabled=True,
        graph_projector_enabled=True,
        graph_projector_neo4j_url="bolt://projector-only:7687",
        graph_projector_neo4j_user="projector",
        graph_projector_neo4j_password="projector-secret",
        _env_file=None,  # type: ignore[call-arg]
    )

    assert settings.graph_projector_enabled is True
    assert settings.graph_projector_neo4j_url == "bolt://projector-only:7687"
    assert settings.graph_projector_neo4j_user == "projector"
    assert isinstance(settings.graph_projector_neo4j_password, SecretStr)
    assert settings.graph_projector_neo4j_password.get_secret_value() == "projector-secret"
    assert "projector-secret" not in repr(settings)


def test_graph_ledger_flag_remains_visible_without_projector_role() -> None:
    from brain_v42.config import Settings

    settings = Settings(
        postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
        graph_enabled=True,
        graph_ledger_write_enabled=True,
        graph_projector_enabled=False,
        _env_file=None,  # type: ignore[call-arg]
    )

    assert settings.graph_ledger_write_enabled is True
    assert settings.graph_projector_enabled is False


def test_graph_projector_rejects_missing_isolated_credentials() -> None:
    from brain_v42.config import Settings

    with pytest.raises(ValidationError, match="requires isolated Neo4j credentials"):
        Settings(
            postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
            graph_enabled=True,
            graph_ledger_write_enabled=True,
            graph_projector_enabled=True,
            graph_projector_neo4j_url=None,
            graph_projector_neo4j_password="",
            neo4j_url="bolt://legacy-shared:7687",
            _env_file=None,  # type: ignore[call-arg]
        )


def test_graph_projector_rejects_legacy_neo4j_credentials_in_shared_settings() -> None:
    from brain_v42.config import Settings

    with pytest.raises(ValidationError, match="legacy NEO4J credentials must be absent"):
        Settings(
            postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
            graph_enabled=True,
            graph_ledger_write_enabled=True,
            graph_projector_enabled=True,
            graph_projector_neo4j_url="bolt://projector-only:7687",
            graph_projector_neo4j_user="projector",
            graph_projector_neo4j_password="private-secret",
            neo4j_url="bolt://legacy-shared:7687",
            neo4j_password="legacy-secret",
            _env_file=None,  # type: ignore[call-arg]
        )


@pytest.mark.parametrize(
    "invalid_url",
    [
        "http://neo4j.internal:7474",
        "bolt://projector:uri-secret@neo4j.internal:7687",
        "neo4j://neo4j.internal:7687?password=query-secret",
    ],
)
def test_graph_projector_url_rejects_unsafe_or_secret_bearing_forms(
    invalid_url: str,
) -> None:
    from brain_v42.config import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
            graph_enabled=True,
            graph_ledger_write_enabled=True,
            graph_projector_enabled=True,
            graph_projector_neo4j_url=invalid_url,
            graph_projector_neo4j_user="projector",
            graph_projector_neo4j_password="field-secret",
            _env_file=None,  # type: ignore[call-arg]
        )

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    for canary in ("uri-secret", "query-secret", "field-secret"):
        assert canary not in rendered


def test_graph_projector_validation_errors_hide_all_secret_inputs() -> None:
    from brain_v42.config import Settings

    private_secret = "validation-canary-private-secret"
    legacy_secret = "validation-canary-legacy-secret"
    http_secret = "validation-canary-http-secret"
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
            graph_enabled=True,
            graph_ledger_write_enabled=True,
            graph_projector_enabled=True,
            graph_projector_neo4j_url="bolt://projector-only:7687",
            graph_projector_neo4j_user="projector",
            graph_projector_neo4j_password=private_secret,
            neo4j_password=legacy_secret,
            mcp_http_token=http_secret,
            _env_file=None,  # type: ignore[call-arg]
        )

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert private_secret not in rendered
    assert legacy_secret not in rendered
    assert http_secret not in rendered


def test_settings_no_http_config():
    """Settings must NOT have host, port, or api_key (no REST API)."""
    from brain_v42.config import Settings

    s = Settings(postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain")
    assert not hasattr(s, "host")
    assert not hasattr(s, "port")


def test_get_settings_singleton(monkeypatch):
    """get_settings() returns a cached singleton instance."""
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://brain:brain@localhost:5433/brain")
    from brain_v42.config import get_settings

    # Clear cache to ensure clean state
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
    # Clean up cache after test
    get_settings.cache_clear()


def test_brain_code_mode_field_exists() -> None:
    """brain_code_mode config field must exist with default False."""
    from brain_v42.config import Settings

    assert "brain_code_mode" in Settings.model_fields
    s = Settings(
        postgres_url="postgresql+asyncpg://x@localhost/x",
        embedding_service_url="http://localhost:8003",
    )
    assert s.brain_code_mode is False


def test_brain_mcp_profile_defaults_to_compact(monkeypatch) -> None:
    """The bounded MCP catalog is the default exposure profile."""
    monkeypatch.delenv("BRAIN_MCP_PROFILE", raising=False)
    from brain_v42.config import Settings

    s = Settings(
        postgres_url="postgresql+asyncpg://x@localhost/x",
        _env_file=None,  # type: ignore[call-arg]
    )

    assert s.brain_mcp_profile == "compact"


def test_brain_mcp_profile_accepts_native_from_env(monkeypatch) -> None:
    """Operators can restore the direct catalog without changing code."""
    monkeypatch.setenv("BRAIN_MCP_PROFILE", "native")
    from brain_v42.config import Settings

    s = Settings(
        postgres_url="postgresql+asyncpg://x@localhost/x",
        _env_file=None,  # type: ignore[call-arg]
    )

    assert s.brain_mcp_profile == "native"


def test_settings_log_level_accepts_valid_values():
    """log_level accepts all valid log levels."""
    from brain_v42.config import Settings

    for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        s = Settings(
            postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
            log_level=level,  # type: ignore
        )
        assert s.log_level == level


class TestCrossProjectSettings:
    """Spec C MVP β — killswitch + briefing tuning (closed by default).

    All default-value tests pass ``_env_file=None`` so pydantic-settings does NOT
    read the repo .env (which locally sets BRAIN_DREAM_CROSS_PROJECT_ENABLED=true,
    causing a spurious RED). The hermetic constructor form is the canonical pattern
    for unit tests that assert defaults.
    """

    def test_cross_project_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("BRAIN_DREAM_CROSS_PROJECT_ENABLED", raising=False)
        from brain_v42.config import Settings

        s = Settings(
            postgres_url="postgresql+asyncpg://u:p@h:5432/db",
            _env_file=None,  # type: ignore[call-arg]
        )
        assert s.brain_dream_cross_project_enabled is False

    def test_briefing_top_n_default_2(self, monkeypatch):
        monkeypatch.delenv("BRAIN_CROSS_PROJECT_BRIEFING_DOMAINS_TOP_N", raising=False)
        from brain_v42.config import Settings

        s = Settings(
            postgres_url="postgresql+asyncpg://u:p@h:5432/db",
            _env_file=None,  # type: ignore[call-arg]
        )
        assert s.brain_cross_project_briefing_domains_top_n == 2

    def test_briefing_entries_max_default_5(self, monkeypatch):
        monkeypatch.delenv("BRAIN_CROSS_PROJECT_BRIEFING_ENTRIES_MAX", raising=False)
        from brain_v42.config import Settings

        s = Settings(
            postgres_url="postgresql+asyncpg://u:p@h:5432/db",
            _env_file=None,  # type: ignore[call-arg]
        )
        assert s.brain_cross_project_briefing_entries_max == 5

    def test_cross_project_enabled_via_env(self, monkeypatch):
        from brain_v42.config import Settings

        monkeypatch.setenv("BRAIN_DREAM_CROSS_PROJECT_ENABLED", "true")
        s = Settings(
            postgres_url="postgresql+asyncpg://u:p@h:5432/db",
            _env_file=None,  # type: ignore[call-arg]
        )
        assert s.brain_dream_cross_project_enabled is True


class TestDecayConfig:
    def test_decay_defaults(self) -> None:
        """Decay config has sensible defaults."""
        from brain_v42.config import Settings

        s = Settings(postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain")
        assert s.decay_enabled is True
        assert s.decay_floor == 0.3
        assert s.stale_threshold == 0.5
        assert s.archive_threshold == 0.2

    def test_decay_flush_interval(self) -> None:
        """Decay flush interval defaults to 300 seconds."""
        from brain_v42.config import Settings

        s = Settings(postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain")
        assert s.decay_flush_interval_seconds == 300

    def test_consolidation_defaults(self) -> None:
        """Consolidation config has sensible defaults."""
        from brain_v42.config import Settings

        s = Settings(postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain")
        assert s.consolidation_interval_seconds == 21600
        assert s.consolidation_similarity_threshold == 0.92
        assert s.forgetting_archive_days == 180


class TestAutomationRuntimeConfig:
    """Configuration additive for the isolated automation runtime."""

    @staticmethod
    def _settings(**overrides: object):
        from brain_v42.config import Settings

        return Settings(
            postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
            _env_file=None,  # type: ignore[call-arg]
            **overrides,
        )

    def test_automation_defaults_preserve_legacy_owner(self, monkeypatch) -> None:
        for name in (
            "AUTOMATION_HOST",
            "AUTOMATION_PORT",
            "AUTOMATION_DEDUP_INTERVAL_SECONDS",
            "METRICS_LEGACY_AUTOMATION_ENABLED",
        ):
            monkeypatch.delenv(name, raising=False)

        settings = self._settings()

        assert settings.automation_host == "127.0.0.1"
        assert settings.automation_port == 9201
        assert settings.automation_dedup_interval_seconds == 21600
        assert settings.metrics_legacy_automation_enabled is True

    def test_automation_values_load_from_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("AUTOMATION_HOST", "::1")
        monkeypatch.setenv("AUTOMATION_PORT", "9301")
        monkeypatch.setenv("AUTOMATION_DEDUP_INTERVAL_SECONDS", "17")
        monkeypatch.setenv("METRICS_LEGACY_AUTOMATION_ENABLED", "false")

        settings = self._settings()

        assert settings.automation_host == "::1"
        assert settings.automation_port == 9301
        assert settings.automation_dedup_interval_seconds == 17
        assert settings.metrics_legacy_automation_enabled is False

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.12", "example.com"])
    def test_automation_host_rejects_non_loopback(self, host: str) -> None:
        with pytest.raises(ValidationError, match="automation_host must be loopback"):
            self._settings(automation_host=host)

    @pytest.mark.parametrize("port", [0, 65536])
    def test_automation_port_is_bounded(self, port: int) -> None:
        with pytest.raises(ValidationError):
            self._settings(automation_port=port)

    @pytest.mark.parametrize("interval", [0, -1])
    def test_automation_dedup_interval_is_strictly_positive(self, interval: int) -> None:
        with pytest.raises(ValidationError):
            self._settings(automation_dedup_interval_seconds=interval)


class TestClientActivityReportingConfig:
    """L'émetteur d'activité est livré killswitch FERMÉ.

    Doctrine du projet : tout commit touchant ``src/`` doit être sûr à exécuter
    la nuit même, sans CI ni revue. Le processus MCP est redémarré tout seul
    (``Restart=always``), donc un défaut ouvert armerait l'émetteur dès la
    fusion — vers une route que le sidecar n'exposera qu'à la tâche 9. Chaque
    appel de tool paierait alors une tâche asyncio et un aller-retour httpx
    pour rien, sans que rien ne le signale : le feu-et-oubli avale l'échec.
    """

    @staticmethod
    def _settings(**overrides: object):
        from brain_v42.config import Settings

        return Settings(
            postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
            _env_file=None,  # type: ignore[call-arg]
            **overrides,
        )

    def test_reporting_is_closed_by_default(self, monkeypatch) -> None:
        for name in ("CLIENT_ACTIVITY_REPORTING_ENABLED", "CLIENT_ACTIVITY_URL"):
            monkeypatch.delenv(name, raising=False)

        settings = self._settings()

        assert settings.client_activity_reporting_enabled is False
        assert settings.client_activity_url == "http://127.0.0.1:9200/v1/client-activity"

    def test_reporting_is_armed_from_the_environment(self, monkeypatch) -> None:
        # Cible loopback délibérée : armer l'émetteur ne doit jamais servir de
        # prétexte à sortir de la machine (cf. la garde ci-dessous).
        monkeypatch.setenv("CLIENT_ACTIVITY_REPORTING_ENABLED", "true")
        monkeypatch.setenv("CLIENT_ACTIVITY_URL", "http://127.0.0.1:9999/v1/probe")

        settings = self._settings()

        assert settings.client_activity_reporting_enabled is True
        assert settings.client_activity_url == "http://127.0.0.1:9999/v1/probe"


class TestClientActivityUrlIsLoopbackOnly:
    """``client_activity_url`` est une sortie réseau : elle se valide comme un bind.

    ``mcp_http_host`` et ``automation_host`` ont chacun leur garde loopback ;
    cette URL SORTANTE n'en avait aucune. Le scénario mesuré est un
    ``CLIENT_ACTIVITY_URL`` LAN posé dans le ``.env`` PARTAGÉ — celui que
    ``brain-mcp-http.service`` charge par ``EnvironmentFile`` : un
    ``{"actor": ..., "session": ..., "calls": 1}`` quitterait alors la machine
    à CHAQUE appel de tool, et le feu-et-oubli de l'émetteur avalerait
    jusqu'à l'échec. La frontière réseau que ce dépôt suit (bloc « Tracked
    network boundary ») se garde ici, pas dans une note.
    """

    @staticmethod
    def _settings(**overrides: object):
        from brain_v42.config import Settings

        return Settings(
            postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
            _env_file=None,  # type: ignore[call-arg]
            **overrides,
        )

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:9200/v1/client-activity",
            "http://localhost:9200/v1/client-activity",
            "http://[::1]:9200/v1/client-activity",
        ],
    )
    def test_the_three_loopback_forms_are_accepted(self, url: str) -> None:
        assert self._settings(client_activity_url=url).client_activity_url == url

    def test_the_shipped_default_passes_the_validator(self) -> None:
        """La valeur livrée doit franchir sa propre garde, pas la contourner.

        Elle est lue depuis le champ pour que déplacer le défaut hors du
        loopback fasse tomber ce test au lieu de passer inaperçu.
        """
        from brain_v42.config import Settings

        default = Settings.model_fields["client_activity_url"].default

        assert self._settings(client_activity_url=default).client_activity_url == default

    @pytest.mark.parametrize(
        "url",
        [
            "http://192.168.1.11:9200/v1/client-activity",  # PC Dev GPU, scénario mesuré
            "http://192.168.1.12:9200/v1/client-activity",  # PC Serveur par son IP LAN
            "http://10.0.0.5:9200/v1/client-activity",
            "http://collector.example.com/v1/client-activity",
            "https://collector.example.com/v1/client-activity",
            "http://127.0.0.1.example.com/v1/client-activity",  # préfixe trompeur
            "http://127.0.0.1@example.com/v1/client-activity",  # hôte réel = example.com
        ],
    )
    def test_lan_and_external_targets_are_refused(self, url: str) -> None:
        with pytest.raises(ValidationError, match="client_activity_url must be loopback"):
            self._settings(client_activity_url=url)

    @pytest.mark.parametrize(
        "url",
        [
            "file:///tmp/exfil",
            "ftp://127.0.0.1:9200/v1/client-activity",
            "//127.0.0.1:9200/v1/client-activity",  # schéma absent
            "not a url",
            "",
            "http://[::1:9200/v1/client-activity",  # IPv6 non refermé : illisible
            "http://127.0.0.1:notaport/v1/client-activity",
            "http://127.0.0.1:99999/v1/client-activity",
        ],
    )
    def test_unreadable_or_absurd_urls_are_refused(self, url: str) -> None:
        """Fail-closed : ce qu'on ne sait pas lire, on le refuse.

        Une URL illisible acceptée puis passée telle quelle à httpx serait un
        garde-fou qui ne garde rien.
        """
        with pytest.raises(ValidationError, match="client_activity_url must be"):
            self._settings(client_activity_url=url)

    def test_a_lan_url_in_the_shared_env_is_refused(self, monkeypatch) -> None:
        """Le scénario mesuré, par le chemin réel : l'environnement du service.

        Passer par ``CLIENT_ACTIVITY_URL`` et non par un kwarg prouve que la
        garde mord là où la fuite se produirait — le ``.env`` partagé.
        """
        monkeypatch.setenv("CLIENT_ACTIVITY_REPORTING_ENABLED", "true")
        monkeypatch.setenv("CLIENT_ACTIVITY_URL", "http://192.168.1.11:9200/v1/client-activity")

        with pytest.raises(ValidationError, match="client_activity_url must be loopback"):
            self._settings()


class TestOtelTracingConfig:
    """Le tracing OTel est livré killswitch FERMÉ, et son endpoint est une SORTIE.

    Même raisonnement que ``client_activity_url``, et même garde : ce qui décide
    de ce que la machine ÉMET se valide comme un bind. Un endpoint LAN posé dans
    le ``.env`` PARTAGÉ de ``brain-mcp-http.service`` ferait sortir un span par
    appel de tool — et un span porte l'acteur et le nom du tool.
    """

    @staticmethod
    def _settings(**overrides: object):
        from brain_v42.config import Settings

        return Settings(
            postgres_url="postgresql+asyncpg://u:p@localhost:5433/brain_test",
            _env_file=None,  # type: ignore[call-arg]
            **overrides,
        )

    def test_tracing_is_disabled_by_default(self) -> None:
        """Un défaut ouvert armerait le tracing dès la fusion : le processus MCP
        redémarre tout seul (``Restart=always``)."""
        assert self._settings().otel_tracing_enabled is False

    def test_the_default_endpoint_is_loopback(self) -> None:
        settings = self._settings()
        assert "127.0.0.1" in settings.otel_endpoint

    def test_a_lan_endpoint_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="loopback"):
            self._settings(otel_endpoint="http://192.168.1.11:4318/v1/traces")

    def test_a_non_http_scheme_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="http"):
            self._settings(otel_endpoint="file:///etc/passwd")

    def test_an_unreadable_url_is_refused_not_ignored(self) -> None:
        """Fail-closed : ce qui n'est pas lisible est refusé."""
        with pytest.raises(ValidationError):
            self._settings(otel_endpoint="http://127.0.0.1:notaport/v1/traces")

    def test_the_error_never_echoes_the_whole_url(self) -> None:
        """Une URL peut porter des identifiants : seul l'hôte est recopié."""
        with pytest.raises(ValidationError) as excinfo:
            self._settings(otel_endpoint="http://user:motdepasse@10.0.0.5:4318/v1/traces")
        assert "motdepasse" not in str(excinfo.value)


# --- BRAIN_ prefix standardisation (open-source prep, ticket bdc4db73) ---
#
# Every field below carries `validation_alias=_brain_alias(LEGACY_ENV)`:
# BRAIN_<LEGACY_ENV> is the preferred env var, and LEGACY_ENV keeps working
# as a fallback so no existing .env file or systemd unit needs to change.
# This table is exhaustive over the fields wired that way -- extend it
# alongside config.py, don't let it drift into a "representative sample".
_ALIASED_FIELDS: list[tuple[str, str]] = [
    ("postgres_url", "POSTGRES_URL"),
    ("log_level", "LOG_LEVEL"),
    ("embedding_service_url", "EMBEDDING_SERVICE_URL"),
    ("embedding_dimension", "EMBEDDING_DIMENSION"),
    ("claude_md_paths", "CLAUDE_MD_PATHS"),
    ("mcp_http_host", "MCP_HTTP_HOST"),
    ("mcp_http_port", "MCP_HTTP_PORT"),
    ("mcp_http_token", "MCP_HTTP_TOKEN"),
    ("mcp_http_dream_tokens", "MCP_HTTP_DREAM_TOKENS"),
    ("metrics_enabled", "METRICS_ENABLED"),
    ("metrics_port", "METRICS_PORT"),
    ("metrics_host", "METRICS_HOST"),
    ("mcp_http_stateless", "MCP_HTTP_STATELESS"),
    ("mcp_http_session_idle_seconds", "MCP_HTTP_SESSION_IDLE_SECONDS"),
    ("client_activity_reporting_enabled", "CLIENT_ACTIVITY_REPORTING_ENABLED"),
    ("client_activity_url", "CLIENT_ACTIVITY_URL"),
    ("otel_tracing_enabled", "OTEL_TRACING_ENABLED"),
    ("otel_endpoint", "OTEL_ENDPOINT"),
    ("automation_host", "AUTOMATION_HOST"),
    ("automation_port", "AUTOMATION_PORT"),
    ("automation_dedup_interval_seconds", "AUTOMATION_DEDUP_INTERVAL_SECONDS"),
    ("metrics_legacy_automation_enabled", "METRICS_LEGACY_AUTOMATION_ENABLED"),
    ("decay_enabled", "DECAY_ENABLED"),
    ("decay_floor", "DECAY_FLOOR"),
    ("decay_flush_interval_seconds", "DECAY_FLUSH_INTERVAL_SECONDS"),
    ("decay_human_signal_enabled", "DECAY_HUMAN_SIGNAL_ENABLED"),
    ("stale_threshold", "STALE_THRESHOLD"),
    ("archive_threshold", "ARCHIVE_THRESHOLD"),
    ("forgetting_archive_days", "FORGETTING_ARCHIVE_DAYS"),
    ("consolidation_interval_seconds", "CONSOLIDATION_INTERVAL_SECONDS"),
    ("consolidation_similarity_threshold", "CONSOLIDATION_SIMILARITY_THRESHOLD"),
    ("reranker_url", "RERANKER_URL"),
    ("reranker_timeout", "RERANKER_TIMEOUT"),
    ("neo4j_url", "NEO4J_URL"),
    ("neo4j_user", "NEO4J_USER"),
    ("neo4j_password", "NEO4J_PASSWORD"),
    ("neo4j_timeout", "NEO4J_TIMEOUT"),
    ("graph_enabled", "GRAPH_ENABLED"),
    ("graph_ledger_write_enabled", "GRAPH_LEDGER_WRITE_ENABLED"),
    ("graph_outbox_interval_seconds", "GRAPH_OUTBOX_INTERVAL_SECONDS"),
    ("graph_outbox_batch_size", "GRAPH_OUTBOX_BATCH_SIZE"),
    ("graph_outbox_max_attempts", "GRAPH_OUTBOX_MAX_ATTEMPTS"),
    ("graph_projector_enabled", "GRAPH_PROJECTOR_ENABLED"),
    ("graph_projector_neo4j_url", "GRAPH_PROJECTOR_NEO4J_URL"),
    ("graph_projector_neo4j_user", "GRAPH_PROJECTOR_NEO4J_USER"),
    ("graph_projector_neo4j_password", "GRAPH_PROJECTOR_NEO4J_PASSWORD"),
    ("gitlab_webhook_secret", "GITLAB_WEBHOOK_SECRET"),
    ("brain_project_hierarchy_path", "PROJECT_HIERARCHY_PATH"),
]


@pytest.mark.parametrize("field_name,legacy_env", _ALIASED_FIELDS)
def test_every_aliased_field_prefers_brain_prefix_over_the_legacy_bare_name(
    field_name: str, legacy_env: str
) -> None:
    """Structural contract: BRAIN_<X> is tried before the legacy bare <X>.

    Checked against AliasChoices directly rather than by instantiating
    Settings for every field: several fields (graph_ledger_write_enabled,
    graph_projector_*, ...) are cross-validated against sibling fields, so a
    real end-to-end round trip per field would need to fabricate a
    consistent whole-Settings environment for each row. The mechanism this
    step adds is `_brain_alias()` wiring -- that's what this asserts,
    exhaustively; a handful of full round trips below cover the actual
    env-var-to-value pipeline plus priority ordering.
    """
    from brain_v42.config import Settings

    field = Settings.model_fields[field_name]
    choices = field.validation_alias
    assert choices is not None, f"{field_name} has no validation_alias"
    aliases = list(choices.choices)  # type: ignore[union-attr]
    assert aliases[0] == f"BRAIN_{legacy_env}", (
        f"{field_name}: BRAIN_{legacy_env} must be tried first, got {aliases}"
    )
    assert legacy_env in aliases, f"{field_name}: legacy env {legacy_env} must stay a fallback"


class TestBrainPrefixEndToEnd:
    """A handful of full round trips through real env vars, not just the alias table."""

    def _settings(self, monkeypatch: pytest.MonkeyPatch, **env: str):
        from brain_v42.config import Settings

        monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://brain:brain@localhost:5433/brain")
        for key in (
            "BRAIN_LOG_LEVEL",
            "LOG_LEVEL",
            "BRAIN_METRICS_PORT",
            "METRICS_PORT",
            "BRAIN_RERANKER_URL",
            "RERANKER_URL",
        ):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return Settings()  # type: ignore[call-arg]

    def test_the_brain_prefixed_name_populates_the_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = self._settings(monkeypatch, BRAIN_LOG_LEVEL="DEBUG")
        assert settings.log_level == "DEBUG"

    def test_the_legacy_bare_name_still_populates_the_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = self._settings(monkeypatch, LOG_LEVEL="WARNING")
        assert settings.log_level == "WARNING"

    def test_brain_prefixed_wins_when_both_are_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = self._settings(monkeypatch, BRAIN_METRICS_PORT="9301", METRICS_PORT="9200")
        assert settings.metrics_port == 9301

    def test_direct_keyword_construction_by_python_field_name_still_works(self) -> None:
        """populate_by_name=True: dozens of tests build Settings(field=...) directly."""
        from brain_v42.config import Settings

        settings = Settings(
            postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
            reranker_url="http://localhost:9999",
            _env_file=None,  # type: ignore[call-arg]
        )
        assert settings.reranker_url == "http://localhost:9999"


class TestDerivedCaptureFlag:
    """La dérivation de capture est livrée FERMÉE, comme toute capacité neuve ici.

    Le défaut fermé n'est pas de la prudence de forme : c'est lui qui garde vert
    l'ensemble du contrat de capture explicite sans qu'on y touche, et c'est lui
    qui rend ce lot livrable alors que la fermeture (`end`) n'a pas encore appris
    à accepter un ledger dérivé.
    """

    def test_flag_exists_and_defaults_to_false(self) -> None:
        from brain_v42.config import Settings

        assert Settings.model_fields["brain_session_derived_capture_enabled"].default is False

    def test_the_flag_can_be_opened_explicitly(self) -> None:
        from brain_v42.config import Settings

        settings = Settings(
            postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain_test",
            brain_session_derived_capture_enabled=True,
            _env_file=None,  # type: ignore[call-arg]
        )
        assert settings.brain_session_derived_capture_enabled is True
