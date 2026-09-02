"""
TDD tests for Feature #607: set up pyproject.toml with its dependencies.

These tests verify that pyproject.toml contains all required dependencies
and does NOT contain any forbidden packages.
"""

import importlib.metadata as importlib_metadata
import re
import tomllib
from pathlib import Path
from typing import cast

import pytest
from packaging.version import Version

PYPROJECT_PATH = Path(__file__).parent.parent.parent / "pyproject.toml"


@pytest.fixture
def pyproject_content() -> str:
    """Read the pyproject.toml content."""
    return PYPROJECT_PATH.read_text(encoding="utf-8")


@pytest.fixture
def dependencies_section(pyproject_content: str) -> str:
    """Extract the [project] dependencies block as a full string.

    We locate the 'dependencies = [' marker inside the [project] section
    and collect lines until the closing ']' at the start of a line.
    """
    lines = pyproject_content.splitlines()
    in_project = False
    in_deps = False
    depth = 0
    collected: list[str] = []

    for line in lines:
        stripped = line.strip()

        if stripped == "[project]":
            in_project = True
            continue
        if in_project and stripped.startswith("[") and stripped != "[project]":
            # Entering a new section — stop if we were collecting
            if in_deps:
                break
            in_project = False
            continue

        if in_project and re.match(r"^dependencies\s*=\s*\[", stripped):
            in_deps = True
            depth = stripped.count("[") - stripped.count("]")
            collected.append(line)
            if depth == 0:
                break
            continue

        if in_deps:
            collected.append(line)
            depth += line.count("[") - line.count("]")
            if depth <= 0:
                break

    assert collected, "Could not find dependencies block in pyproject.toml"
    return "\n".join(collected)


@pytest.fixture
def dev_dependencies_section(pyproject_content: str) -> str:
    """Extract the [project.optional-dependencies] dev block."""
    lines = pyproject_content.splitlines()
    in_optional = False
    in_dev = False
    depth = 0
    collected: list[str] = []

    for line in lines:
        stripped = line.strip()

        if stripped == "[project.optional-dependencies]":
            in_optional = True
            continue
        if (
            in_optional
            and stripped.startswith("[")
            and stripped != "[project.optional-dependencies]"
        ):
            if in_dev:
                break
            in_optional = False
            continue

        if in_optional and re.match(r"^dev\s*=\s*\[", stripped):
            in_dev = True
            depth = stripped.count("[") - stripped.count("]")
            collected.append(line)
            if depth == 0:
                break
            continue

        if in_dev:
            collected.append(line)
            depth += line.count("[") - line.count("]")
            if depth <= 0:
                break

    assert collected, "Could not find dev dependencies block in pyproject.toml"
    return "\n".join(collected)


# --- Production dependencies ---


class TestProductionDependencies:
    def test_sqlalchemy_asyncio_present(self, dependencies_section: str) -> None:
        """sqlalchemy[asyncio]>=2.0 must be present for async support."""
        assert re.search(r"sqlalchemy\[asyncio\]>=2\.0", dependencies_section, re.IGNORECASE), (
            "sqlalchemy[asyncio]>=2.0 must be in dependencies"
        )

    def test_asyncpg_present(self, dependencies_section: str) -> None:
        """asyncpg>=0.30 must be present as async PG driver."""
        assert re.search(r"asyncpg>=0\.30", dependencies_section), (
            "asyncpg>=0.30 must be in dependencies"
        )

    def test_alembic_present(self, dependencies_section: str) -> None:
        """alembic>=1.13 must be present for DB migrations."""
        assert re.search(r"alembic>=1\.13", dependencies_section), (
            "alembic>=1.13 must be in dependencies"
        )

    def test_pgvector_present(self, dependencies_section: str) -> None:
        """pgvector>=0.3 must be present for vector type support."""
        assert re.search(r"pgvector>=0\.3", dependencies_section), (
            "pgvector>=0.3 must be in dependencies"
        )

    def test_httpx_in_prod_deps(self, dependencies_section: str) -> None:
        """httpx>=0.27 must be in production dependencies (GPU embedding client)."""
        assert re.search(r"httpx>=0\.27", dependencies_section), (
            "httpx>=0.27 must be in dependencies"
        )

    def test_fastmcp_present(self, dependencies_section: str) -> None:
        """fastmcp>=3.4.1 must be present for MCP server framework."""
        assert re.search(r"fastmcp>=3\.4\.1", dependencies_section), (
            "fastmcp>=3.4.1 must be in dependencies"
        )

    def test_pydantic_present(self, dependencies_section: str) -> None:
        """pydantic>=2.0 must be present for data validation."""
        assert re.search(r"pydantic>=2\.0", dependencies_section), (
            "pydantic>=2.0 must be in dependencies"
        )

    def test_pydantic_settings_present(self, dependencies_section: str) -> None:
        """pydantic-settings>=2.0 must be present for config management."""
        assert re.search(r"pydantic-settings>=2\.0", dependencies_section), (
            "pydantic-settings>=2.0 must be in dependencies"
        )

    def test_structlog_present(self, dependencies_section: str) -> None:
        """structlog>=24.1 must be present for structured logging."""
        assert re.search(r"structlog>=24\.1", dependencies_section), (
            "structlog>=24.1 must be in dependencies"
        )

    def test_neo4j_present(self, dependencies_section: str) -> None:
        """neo4j>=5.0 must be in production dependencies (optional graph index)."""
        assert re.search(r"neo4j>=5\.0", dependencies_section), (
            "neo4j>=5.0 must be in dependencies (optional Neo4j relationship index)"
        )

    def test_neo4j_upper_bound(self, dependencies_section: str) -> None:
        """neo4j must be capped (<7) — the codebase targets Neo4j 5.x/6.x APIs.

        An unbounded floor let neo4j float to a 6.x major already; without a cap
        a future neo4j 7 could land untested. Pin the major boundary.
        """
        assert re.search(r"neo4j>=5\.0,<7", dependencies_section), (
            "neo4j must be capped: neo4j>=5.0,<7"
        )


# --- Forbidden packages ---


class TestForbiddenPackages:
    def test_fastapi_gateway_range(self, dependencies_section: str) -> None:
        """The dedicated Codex gateway uses the reviewed FastAPI 0.139 line."""
        assert re.search(r"fastapi>=0\.139\.2,<0\.140", dependencies_section)

    def test_uvicorn_gateway_range(self, dependencies_section: str) -> None:
        """The dedicated Codex gateway has a bounded direct ASGI server dependency."""
        assert re.search(r"uvicorn>=0\.41,<1", dependencies_section)

    def test_no_redis(self, dependencies_section: str) -> None:
        """redis must NOT be in production dependencies."""
        assert "redis" not in dependencies_section.lower(), (
            "redis must NOT be in dependencies (removed from architecture)"
        )

    def test_no_torch(self, dependencies_section: str) -> None:
        """torch must NOT be in production dependencies (as a package, not in comments)."""
        non_comment_lines = [
            line for line in dependencies_section.splitlines() if not line.strip().startswith("#")
        ]
        non_comment_text = "\n".join(non_comment_lines)
        assert "torch" not in non_comment_text.lower(), (
            "torch must NOT be listed as a package dependency (replaced by GPU embedding service)"
        )

    def test_no_sentence_transformers(self, dependencies_section: str) -> None:
        """sentence-transformers must NOT be in production dependencies (as a package)."""
        non_comment_lines = [
            line for line in dependencies_section.splitlines() if not line.strip().startswith("#")
        ]
        non_comment_text = "\n".join(non_comment_lines)
        assert "sentence-transformers" not in non_comment_text.lower(), (
            "sentence-transformers must NOT be listed as a package dependency"
        )

    def test_onnxruntime_removed(self, dependencies_section: str) -> None:
        """onnxruntime must NOT be in deps (reranker uses shared HTTP service)."""
        assert "onnxruntime" not in dependencies_section.lower(), (
            "onnxruntime should be removed — reranker uses HTTP service on :8004"
        )

    def test_tokenizers_removed(self, dependencies_section: str) -> None:
        """tokenizers must NOT be in deps (reranker uses shared HTTP service)."""
        assert "tokenizers" not in dependencies_section.lower(), (
            "tokenizers should be removed — reranker uses HTTP service on :8004"
        )


# --- Dev dependencies ---


class TestDevDependencies:
    """Whole CI toolchain pinned EXACT (learning c43ed8b8, decision of 2026-07-10).

    A floating spec (tool>=...) lets CI silently float to whatever is latest
    at pipeline time, so a new upstream release can turn a green commit red
    with zero code change (mypy 1.19.1 local vs 2.2.0 CI, 2026-07-10). Pin
    deliberately; bump via explicit commit, verified green.
    """

    def test_pytest_pinned(self, dev_dependencies_section: str) -> None:
        """pytest must be pinned exact (pytest==X.Y.Z) in dev dependencies."""
        assert re.search(r'"pytest==\d+\.\d+\.\d+"', dev_dependencies_section), (
            "pytest must be pinned to an exact version (pytest==X.Y.Z) in dev deps"
        )

    def test_pytest_asyncio_pinned(self, dev_dependencies_section: str) -> None:
        """pytest-asyncio must be pinned exact in dev dependencies."""
        assert re.search(r"pytest-asyncio==\d+\.\d+\.\d+", dev_dependencies_section), (
            "pytest-asyncio must be pinned to an exact version in dev deps"
        )

    def test_pytest_cov_pinned(self, dev_dependencies_section: str) -> None:
        """pytest-cov must be pinned exact in dev dependencies."""
        assert re.search(r"pytest-cov==\d+\.\d+\.\d+", dev_dependencies_section), (
            "pytest-cov must be pinned to an exact version in dev deps"
        )

    def test_ruff_pinned(self, dev_dependencies_section: str) -> None:
        """ruff must be pinned to an exact version (==X.Y.Z) to kill CI linter drift."""
        assert re.search(r"ruff==\d+\.\d+\.\d+", dev_dependencies_section), (
            "ruff must be pinned to an exact version (ruff==X.Y.Z) in dev deps"
        )

    def test_mypy_pinned(self, dev_dependencies_section: str) -> None:
        """mypy must be pinned exact (mypy==X.Y.Z) in dev dependencies."""
        assert re.search(r"mypy==\d+\.\d+\.\d+", dev_dependencies_section), (
            "mypy must be pinned to an exact version (mypy==X.Y.Z) in dev deps"
        )


# --- No placeholder comments ---


class TestNoPlaceholders:
    def test_no_placeholder_comment(self, pyproject_content: str) -> None:
        """The placeholder comment must be removed."""
        assert "# Add project-specific deps here" not in pyproject_content, (
            "Placeholder comment '# Add project-specific deps here' must be removed"
        )


# --- Tool config sections must remain intact ---


class TestToolConfigsUntouched:
    def test_pytest_config_present(self, pyproject_content: str) -> None:
        """[tool.pytest.ini_options] section must still be present."""
        assert "[tool.pytest.ini_options]" in pyproject_content, (
            "[tool.pytest.ini_options] section must not be removed"
        )

    def test_ruff_config_present(self, pyproject_content: str) -> None:
        """[tool.ruff] section must still be present."""
        assert "[tool.ruff]" in pyproject_content, "[tool.ruff] section must not be removed"

    def test_mypy_config_present(self, pyproject_content: str) -> None:
        """[tool.mypy] section must still be present."""
        assert "[tool.mypy]" in pyproject_content, "[tool.mypy] section must not be removed"

    def test_coverage_config_present(self, pyproject_content: str) -> None:
        """[tool.coverage.run] section must still be present."""
        assert "[tool.coverage.run]" in pyproject_content, (
            "[tool.coverage.run] section must not be removed"
        )

    def test_asyncio_mode_auto(self, pyproject_content: str) -> None:
        """asyncio_mode = 'auto' must remain in pytest config."""
        assert 'asyncio_mode = "auto"' in pyproject_content, (
            "asyncio_mode must remain set to 'auto'"
        )

    def test_requires_python(self, pyproject_content: str) -> None:
        """requires-python = '>=3.12' must remain."""
        assert 'requires-python = ">=3.12"' in pyproject_content, (
            "requires-python = '>=3.12' must remain"
        )


# --- Security floor specifiers (task 0.1: CVE-2026-48710 / HTTP migration) ---


def _parse_project_deps_via_tomllib() -> list[str]:
    """Return the raw dep strings from [project].dependencies via tomllib."""
    with PYPROJECT_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    return cast("list[str]", data["project"]["dependencies"])


class TestSecurityFloorSpecifiers:
    """Assert that pyproject.toml specifiers contain the required security floors.

    RED: These tests MUST fail until pyproject.toml is updated.
    The venv already has the target versions installed — the risk is that the
    LOOSE specifiers permit a future CI/Docker resolve to drop below the floors.
    """

    def test_fastmcp_floor_in_specifier(self) -> None:
        """fastmcp>=3.4.1 must be present in pyproject dependencies specifier."""
        deps = _parse_project_deps_via_tomllib()
        fastmcp_specs = [d for d in deps if d.lower().startswith("fastmcp")]
        assert fastmcp_specs, "fastmcp must be listed in [project].dependencies"
        combined = " ".join(fastmcp_specs)
        assert ">=3.4.1" in combined, (
            f"fastmcp specifier must contain >=3.4.1 (CVE-2026-48710 floor); found: {fastmcp_specs}"
        )

    def test_fastmcp_upper_bound_in_specifier(self) -> None:
        """fastmcp must remain capped at <4 to avoid major-version drift."""
        deps = _parse_project_deps_via_tomllib()
        fastmcp_specs = [d for d in deps if d.lower().startswith("fastmcp")]
        combined = " ".join(fastmcp_specs)
        assert "<4" in combined, f"fastmcp specifier must contain <4; found: {fastmcp_specs}"

    def test_starlette_floor_in_specifier(self) -> None:
        """starlette>=1.0.1 must be present (CVE-2026-48710: BadHost header injection)."""
        deps = _parse_project_deps_via_tomllib()
        starlette_specs = [d for d in deps if d.lower().startswith("starlette")]
        assert starlette_specs, "starlette must be listed explicitly in [project].dependencies"
        combined = " ".join(starlette_specs)
        assert ">=1.0.1" in combined, (
            f"starlette specifier must contain >=1.0.1; found: {starlette_specs}"
        )

    def test_mcp_floor_in_specifier(self) -> None:
        """mcp>=1.27 must be present in pyproject dependencies specifier."""
        deps = _parse_project_deps_via_tomllib()
        mcp_specs = [d for d in deps if re.match(r"^mcp[^-]", d.lower())]
        assert mcp_specs, "mcp must be listed explicitly in [project].dependencies"
        combined = " ".join(mcp_specs)
        assert ">=1.27" in combined, f"mcp specifier must contain >=1.27; found: {mcp_specs}"

    def test_mcp_upper_bound_in_specifier(self) -> None:
        """mcp must be capped at <2 to avoid major-version drift."""
        deps = _parse_project_deps_via_tomllib()
        mcp_specs = [d for d in deps if re.match(r"^mcp[^-]", d.lower())]
        combined = " ".join(mcp_specs)
        assert "<2" in combined, f"mcp specifier must contain <2; found: {mcp_specs}"

    def test_sqlalchemy_floor_in_specifier(self) -> None:
        """sqlalchemy[asyncio]>=2.0.44 must be present (bumped from bare >=2.0)."""
        deps = _parse_project_deps_via_tomllib()
        sa_specs = [d for d in deps if d.lower().startswith("sqlalchemy")]
        assert sa_specs, "sqlalchemy must be in [project].dependencies"
        combined = " ".join(sa_specs)
        assert ">=2.0.44" in combined, (
            f"sqlalchemy specifier must contain >=2.0.44; found: {sa_specs}"
        )

    def test_asyncpg_floor_in_specifier(self) -> None:
        """asyncpg>=0.30 must be present (bumped from >=0.29)."""
        deps = _parse_project_deps_via_tomllib()
        asyncpg_specs = [d for d in deps if d.lower().startswith("asyncpg")]
        assert asyncpg_specs, "asyncpg must be in [project].dependencies"
        combined = " ".join(asyncpg_specs)
        assert ">=0.30" in combined, (
            f"asyncpg specifier must contain >=0.30; found: {asyncpg_specs}"
        )


class TestSecurityFloorInstalledVersions:
    """Complementary: assert that INSTALLED versions satisfy the security floors.

    These tests use importlib.metadata + packaging.version — they validate the
    venv state independently of the specifiers.
    """

    @pytest.mark.parametrize(
        "package,floor",
        [
            ("fastmcp", "3.4.1"),
            ("starlette", "1.0.1"),
            ("mcp", "1.27"),
            ("sqlalchemy", "2.0.44"),
            ("asyncpg", "0.30"),
        ],
    )
    def test_installed_version_meets_floor(self, package: str, floor: str) -> None:
        """Installed {package} must be >= {floor}."""
        installed = Version(importlib_metadata.version(package))
        assert installed >= Version(floor), (
            f"Installed {package}=={installed} does not meet floor >={floor}"
        )
