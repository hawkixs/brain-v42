"""Unit tests for MCP server initialization — no real DB or GPU service needed."""

from unittest.mock import MagicMock, patch

import pytest


def test_mcp_module_importable() -> None:
    """src/brain_v42/mcp/__init__.py exists and is importable."""
    import brain_v42.mcp  # noqa: F401


def test_server_module_importable() -> None:
    """src/brain_v42/mcp/server.py exists and is importable."""
    import brain_v42.mcp.server  # noqa: F401


def test_build_brain_session_service_wires_transactional_repository() -> None:
    from brain_v42.mcp.server import build_brain_session_service
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo
    from brain_v42.services.brain_session_service import BrainSessionService

    session_factory = MagicMock()

    service = build_brain_session_service(session_factory)

    assert isinstance(service, BrainSessionService)
    assert isinstance(service.repo, PgBrainSessionRepo)
    assert service.repo._session_factory is session_factory


def test_mcp_instance_is_fastmcp() -> None:
    """The module-level `mcp` object is a FastMCP instance."""
    from fastmcp import FastMCP

    from brain_v42.mcp import server

    assert isinstance(server.mcp, FastMCP)


def test_mcp_server_name() -> None:
    """The FastMCP instance is named 'brain'."""
    from brain_v42.mcp import server

    assert server.mcp.name == "brain"


def test_build_services_returns_all_services() -> None:
    """build_services() returns a dict with all service keys including decay components."""
    session_factory = MagicMock()
    embedding_svc = MagicMock()
    mock_settings = MagicMock(
        embedding_service_url="http://localhost:8003",
        embedding_dimension=1536,
        metrics_enabled=False,
        decay_enabled=False,
        graph_enabled=False,
        graph_projector_enabled=False,
        neo4j_url=None,
        neo4j_user="neo4j",
        neo4j_password="",
        neo4j_timeout=5.0,
    )
    with (
        patch("brain_v42.mcp.server.get_session_factory", return_value=session_factory),
        patch("brain_v42.mcp.server.get_settings", return_value=mock_settings),
        patch("brain_v42.mcp.server.GPUEmbeddingService", return_value=embedding_svc),
        patch("brain_v42.mcp.server.FeatureCreationService") as feature_creation_cls,
        patch("brain_v42.db.engine.get_engine", return_value=MagicMock()),
        patch("brain_v42.metrics.collector.get_settings", return_value=mock_settings),
        patch("brain_v42.mcp.server.create_neo4j_driver", return_value=None),
    ):
        from brain_v42.mcp.server import build_services

        services = build_services()
        feature_creation_cls.assert_called_once_with(
            session_factory=session_factory,
            embedding_svc=embedding_svc,
            embedding_dimension=1536,
        )
        assert set(services.keys()) == {
            "decision_svc",
            "learning_svc",
            "snippet_svc",
            "runbook_svc",
            "adr_svc",
            "project_context_svc",
            "brain_svc",
            "metrics_collector",
            "embedding_svc",
            "feature_linker",
            "feature_creation_svc",
            "roadmap_svc",
            "decay_calculator",
            "access_logger",
            "access_log_repo",
            "consolidation_log_repo",
            "consolidation_job",
            "reranker_client",
            "status_engine",
            "cluster_guard",
            "plan_indexer",
            "graph_service",
            "graph_ledger_repo",
            "graph_outbox_projector",
            "neo4j_driver",
            "auto_linker",
            "ticket_svc",
        }


def test_main_module_importable() -> None:
    """src/brain_v42/__main__.py exists and is importable."""
    import brain_v42.__main__  # noqa: F401


def test_tools_module_importable() -> None:
    """src/brain_v42/mcp/tools/__init__.py exists and is importable."""
    import brain_v42.mcp.tools  # noqa: F401


def test_brain_tools_module_importable() -> None:
    """src/brain_v42/mcp/tools/brain_tools.py exists and is importable."""
    import brain_v42.mcp.tools.brain_tools  # noqa: F401


def test_register_tools_is_callable() -> None:
    """register_tools() stub function exists and is callable."""
    from brain_v42.mcp.tools.brain_tools import register_tools

    assert callable(register_tools)


def test_register_tools_accepts_all_service_kwargs() -> None:
    """register_tools() accepts mcp + all 7 service kwargs (keyword-only)."""
    from unittest.mock import MagicMock

    from brain_v42.mcp.tools.brain_tools import register_tools

    mock_mcp = MagicMock()
    # Should not raise — all kwargs are accepted
    register_tools(
        mock_mcp,
        decision_svc=MagicMock(),
        learning_svc=MagicMock(),
        snippet_svc=MagicMock(),
        runbook_svc=MagicMock(),
        adr_svc=MagicMock(),
        project_context_svc=MagicMock(),
        brain_svc=MagicMock(),
    )


def test_register_tools_forwards_the_same_optional_access_logger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composition shares one internal logger across snippet and runbook tools."""
    import inspect
    from unittest.mock import MagicMock

    from brain_v42.mcp.tools import brain_tools

    assert "access_logger" in inspect.signature(brain_tools.register_tools).parameters
    snippet_register = MagicMock()
    runbook_register = MagicMock()
    monkeypatch.setattr(brain_tools, "register_snippet_tools", snippet_register)
    monkeypatch.setattr(brain_tools, "register_runbook_tools", runbook_register)
    mcp = MagicMock()
    snippet_svc = MagicMock()
    runbook_svc = MagicMock()
    access_logger = MagicMock()

    brain_tools.register_tools(
        mcp,
        decision_svc=MagicMock(),
        learning_svc=MagicMock(),
        snippet_svc=snippet_svc,
        runbook_svc=runbook_svc,
        adr_svc=MagicMock(),
        project_context_svc=MagicMock(),
        brain_svc=MagicMock(),
        access_logger=access_logger,
    )

    snippet_register.assert_called_once_with(
        mcp,
        snippet_svc,
        metrics_collector=None,
        access_logger=access_logger,
    )
    runbook_register.assert_called_once_with(
        mcp,
        runbook_svc,
        access_logger=access_logger,
    )


def test_usage_access_logger_is_enabled_once_and_disabled_cleanly() -> None:
    """All tool registration paths select the exact same built logger instance."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from brain_v42.mcp import server

    assert hasattr(server, "_select_usage_access_logger")
    access_logger = MagicMock()
    services = {"access_logger": access_logger}

    assert (
        server._select_usage_access_logger(  # type: ignore[attr-defined]
            SimpleNamespace(decay_enabled=True), services
        )
        is access_logger
    )
    assert (
        server._select_usage_access_logger(  # type: ignore[attr-defined]
            SimpleNamespace(decay_enabled=False), services
        )
        is None
    )


def test_build_services_includes_graph_service_when_enabled() -> None:
    """build_services() returns graph_service (not None) when graph_enabled=True and neo4j_url is set."""
    from unittest.mock import MagicMock, patch

    mock_settings = MagicMock(
        embedding_service_url="http://localhost:8003",
        embedding_dimension=1536,
        metrics_enabled=False,
        decay_enabled=False,
        graph_enabled=True,
        graph_ledger_write_enabled=False,
        graph_projector_enabled=False,
        neo4j_url="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="",
        neo4j_timeout=5.0,
    )
    mock_driver = MagicMock()
    with (
        patch("brain_v42.mcp.server.get_session_factory", return_value=MagicMock()),
        patch("brain_v42.mcp.server.get_settings", return_value=mock_settings),
        patch("brain_v42.mcp.server.GPUEmbeddingService", return_value=MagicMock()),
        patch("brain_v42.db.engine.get_engine", return_value=MagicMock()),
        patch("brain_v42.metrics.collector.get_settings", return_value=mock_settings),
        patch("brain_v42.mcp.server.create_neo4j_driver", return_value=mock_driver),
    ):
        from brain_v42.mcp.server import build_services

        services = build_services()
        assert "graph_service" in services
        assert services["graph_service"] is not None


def test_projector_uses_service_private_neo4j_credentials() -> None:
    from types import SimpleNamespace

    from pydantic import SecretStr

    from brain_v42.mcp.server import _neo4j_connection_settings

    settings = SimpleNamespace(
        graph_projector_enabled=True,
        graph_projector_neo4j_url="bolt://projector-only:7687",
        graph_projector_neo4j_user="projector",
        graph_projector_neo4j_password=SecretStr("private-secret"),
        neo4j_url="bolt://legacy-shared:7687",
        neo4j_user="legacy",
        neo4j_password="legacy-secret",
    )

    assert _neo4j_connection_settings(settings) == (
        "bolt://projector-only:7687",
        "projector",
        "private-secret",
    )


def test_legacy_graph_uses_legacy_neo4j_credentials() -> None:
    from types import SimpleNamespace

    from brain_v42.mcp.server import _neo4j_connection_settings

    settings = SimpleNamespace(
        graph_projector_enabled=False,
        neo4j_url="bolt://legacy-shared:7687",
        neo4j_user="legacy",
        neo4j_password="legacy-secret",
    )

    assert _neo4j_connection_settings(settings) == (
        "bolt://legacy-shared:7687",
        "legacy",
        "legacy-secret",
    )


def test_build_services_uses_private_projector_credentials_from_real_settings() -> None:
    from unittest.mock import MagicMock, patch

    from brain_v42.config import Settings
    from brain_v42.mcp.server import build_services

    settings = Settings(
        postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
        graph_enabled=True,
        graph_ledger_write_enabled=True,
        graph_projector_enabled=True,
        graph_projector_neo4j_url="bolt://projector-only:7687",
        graph_projector_neo4j_user="projector",
        graph_projector_neo4j_password="private-secret",
        _env_file=None,  # type: ignore[call-arg]
    )
    driver = MagicMock()
    with (
        patch("brain_v42.mcp.server.get_session_factory", return_value=MagicMock()),
        patch("brain_v42.mcp.server.get_settings", return_value=settings),
        patch("brain_v42.mcp.server.GPUEmbeddingService", return_value=MagicMock()),
        patch("brain_v42.db.engine.get_engine", return_value=MagicMock()),
        patch("brain_v42.metrics.collector.get_settings", return_value=settings),
        patch(
            "brain_v42.mcp.server.create_neo4j_driver",
            return_value=driver,
        ) as create_driver,
    ):
        services = build_services()

    create_driver.assert_called_once_with(
        url="bolt://projector-only:7687",
        user="projector",
        password="private-secret",
        enabled=True,
    )
    assert services["neo4j_driver"] is driver
    assert services["graph_outbox_projector"] is not None


def test_build_services_fails_closed_when_ledger_lacks_private_projector_role() -> None:
    from unittest.mock import MagicMock, patch

    from brain_v42.config import Settings
    from brain_v42.mcp.server import build_services

    settings = Settings(
        postgres_url="postgresql+asyncpg://brain:brain@localhost:5433/brain",
        graph_enabled=True,
        graph_ledger_write_enabled=True,
        graph_projector_enabled=False,
        neo4j_url="bolt://legacy-shared:7687",
        _env_file=None,  # type: ignore[call-arg]
    )
    with (
        patch("brain_v42.mcp.server.get_session_factory", return_value=MagicMock()),
        patch("brain_v42.mcp.server.get_settings", return_value=settings),
        patch("brain_v42.mcp.server.GPUEmbeddingService", return_value=MagicMock()),
        patch("brain_v42.db.engine.get_engine", return_value=MagicMock()),
        patch("brain_v42.metrics.collector.get_settings", return_value=settings),
        patch("brain_v42.mcp.server.create_neo4j_driver", return_value=MagicMock()),
    ):
        with pytest.raises(RuntimeError, match="private projector role"):
            build_services()


def test_build_services_graph_service_none_when_disabled() -> None:
    """build_services() returns graph_service=None when graph_enabled=False."""
    from unittest.mock import MagicMock, patch

    mock_settings = MagicMock(
        embedding_service_url="http://localhost:8003",
        embedding_dimension=1536,
        metrics_enabled=False,
        decay_enabled=False,
        graph_enabled=False,
        graph_projector_enabled=False,
        neo4j_url=None,
        neo4j_user="neo4j",
        neo4j_password="",
        neo4j_timeout=5.0,
    )
    with (
        patch("brain_v42.mcp.server.get_session_factory", return_value=MagicMock()),
        patch("brain_v42.mcp.server.get_settings", return_value=mock_settings),
        patch("brain_v42.mcp.server.GPUEmbeddingService", return_value=MagicMock()),
        patch("brain_v42.db.engine.get_engine", return_value=MagicMock()),
        patch("brain_v42.metrics.collector.get_settings", return_value=mock_settings),
        patch("brain_v42.mcp.server.create_neo4j_driver", return_value=None),
    ):
        from brain_v42.mcp.server import build_services

        services = build_services()
        assert "graph_service" in services
        assert services["graph_service"] is None


def test_build_services_still_returns_all_existing_services_with_graph() -> None:
    """build_services() still returns all original service keys after graph wiring."""
    from unittest.mock import MagicMock, patch

    mock_settings = MagicMock(
        embedding_service_url="http://localhost:8003",
        embedding_dimension=1536,
        metrics_enabled=False,
        decay_enabled=False,
        graph_enabled=False,
        graph_projector_enabled=False,
        neo4j_url=None,
        neo4j_user="neo4j",
        neo4j_password="",
        neo4j_timeout=5.0,
    )
    with (
        patch("brain_v42.mcp.server.get_session_factory", return_value=MagicMock()),
        patch("brain_v42.mcp.server.get_settings", return_value=mock_settings),
        patch("brain_v42.mcp.server.GPUEmbeddingService", return_value=MagicMock()),
        patch("brain_v42.db.engine.get_engine", return_value=MagicMock()),
        patch("brain_v42.metrics.collector.get_settings", return_value=mock_settings),
        patch("brain_v42.mcp.server.create_neo4j_driver", return_value=None),
    ):
        from brain_v42.mcp.server import build_services

        services = build_services()
        expected_keys = {
            "decision_svc",
            "learning_svc",
            "snippet_svc",
            "runbook_svc",
            "adr_svc",
            "project_context_svc",
            "brain_svc",
            "metrics_collector",
            "embedding_svc",
            "feature_linker",
            "roadmap_svc",
            "decay_calculator",
            "access_logger",
            "access_log_repo",
            "consolidation_log_repo",
            "consolidation_job",
            "reranker_client",
            "status_engine",
            "cluster_guard",
            "plan_indexer",
            "graph_service",
            "neo4j_driver",
        }
        assert expected_keys.issubset(set(services.keys()))


@pytest.mark.asyncio
async def test_stdio_transport_never_constructs_dream_project_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastmcp import FastMCP
    from pydantic import SecretStr

    from brain_v42.config import Settings
    from brain_v42.mcp import server as production_server
    from brain_v42.mcp.dream_capabilities import DreamCapabilityMiddleware

    mcp = FastMCP("stdio-project-authorization-compatibility")
    transports: list[str] = []

    async def fake_run_async(*, transport: str) -> None:
        transports.append(transport)

    def forbidden_factory() -> object:
        raise AssertionError("STDIO must not construct a Dream project resolver")

    monkeypatch.setattr(mcp, "run_async", fake_run_async)
    monkeypatch.setattr(production_server, "get_session_factory", forbidden_factory)
    monkeypatch.setattr(
        production_server,
        "_install_signal_handlers",
        lambda _loop, _shutdown_event: None,
    )
    settings = Settings(
        postgres_url="postgresql+asyncpg://unused:unused@127.0.0.1:1/unreachable_sec1b",
        brain_mcp_transport="stdio",
        brain_dream_capability_enforcement=True,
        mcp_http_token="admin-token",
        mcp_http_dream_tokens=SecretStr("unused-in-stdio"),
        _env_file=None,  # type: ignore[call-arg]
    )

    await production_server._run_mcp(mcp, settings)

    assert transports == ["stdio"]
    assert mcp.auth is None
    assert not any(isinstance(entry, DreamCapabilityMiddleware) for entry in mcp.middleware)


def test_build_services_wires_the_project_guard_into_every_knowledge_service() -> None:
    """La garde projet-inconnu est FAIL-OPEN par défaut : c'est le câblage qui la crée.

    ``require_known_project`` rend immédiatement quand ``project_context_repo`` est
    ``None`` — un défaut assumé, pour que les nombreux doubles de test qui
    construisent ces services sans lui continuent d'exercer le reste. La
    conséquence est que retirer ``project_context_repo=`` d'un site de câblage ne
    casse RIEN : la garde disparaît en silence et les écritures sous un projet
    inexistant recommencent à rendre ``ok``.

    Livrée le 2026-08-06 (87389e6d), elle n'avait aucun témoin. Ce test est un
    épinglage : il ne passe pas du rouge au vert, il empêche le vert de mentir.
    Vérifié en retirant l'argument de learning_svc — il tombe, et lui seul.
    """
    session_factory = MagicMock()
    embedding_svc = MagicMock()
    mock_settings = MagicMock(
        embedding_service_url="http://localhost:8003",
        embedding_dimension=1536,
        metrics_enabled=False,
        decay_enabled=False,
        graph_enabled=False,
        graph_projector_enabled=False,
        neo4j_url=None,
        neo4j_user="neo4j",
        neo4j_password="",
        neo4j_timeout=5.0,
    )
    with (
        patch("brain_v42.mcp.server.get_session_factory", return_value=session_factory),
        patch("brain_v42.mcp.server.get_settings", return_value=mock_settings),
        patch("brain_v42.mcp.server.GPUEmbeddingService", return_value=embedding_svc),
        patch("brain_v42.mcp.server.FeatureCreationService"),
        patch("brain_v42.db.engine.get_engine", return_value=MagicMock()),
        patch("brain_v42.metrics.collector.get_settings", return_value=mock_settings),
        patch("brain_v42.mcp.server.create_neo4j_driver", return_value=None),
    ):
        from brain_v42.mcp.server import build_services

        services = build_services()

    for key in ("learning_svc", "decision_svc", "snippet_svc", "runbook_svc", "adr_svc"):
        repo = getattr(services[key], "_project_context_repo", None)
        assert repo is not None, (
            f"{key} est construit sans project_context_repo : la garde projet-inconnu "
            f"est désarmée sur ce chemin d'écriture et l'échec sera SILENCIEUX"
        )
