"""Unit tests for brain_v42.scripts.init_graph's project-hierarchy seed step.

Scoped narrowly to create_project_hierarchy()'s path resolution: the rest of
init_graph.py is an untested, database-heavy CLI (Neo4j + PostgreSQL) that
this open-source-prep step does not extend coverage for.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from brain_v42.config import get_settings
from brain_v42.scripts.init_graph import create_project_hierarchy


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_path_is_configurable_via_settings_not_hardcoded_to_the_package_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is the bug the open-source-prep script move (step 2) introduced:
    the old `Path(__file__).parent.parent / "config" / ...` pointed at
    src/brain_v42/config/ once init_graph.py moved under src/brain_v42/scripts/
    -- a directory that has never existed. It degraded silently (the
    missing-file branch just skips), so nothing crashed; it just always
    seeded nothing.
    """
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://brain:brain@localhost:5433/brain")
    hierarchy_file = tmp_path / "project_hierarchy.yml"
    hierarchy_file.write_text(
        yaml.safe_dump(
            {"project_hierarchy": {"red": {"contains": ["brain-v42"], "depends_on": []}}}
        )
    )
    monkeypatch.setenv("BRAIN_PROJECT_HIERARCHY_PATH", str(hierarchy_file))
    get_settings.cache_clear()

    session = AsyncMock()
    session.run = AsyncMock()
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    driver = AsyncMock()
    driver.session = lambda: session_cm

    await create_project_hierarchy(driver)

    # At least one MERGE ran for the "red" project node from our fixture --
    # proof the configured path was actually read, not silently skipped.
    assert session.run.await_count > 0


async def test_a_missing_configured_path_is_a_graceful_skip_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://brain:brain@localhost:5433/brain")
    monkeypatch.setenv("BRAIN_PROJECT_HIERARCHY_PATH", str(tmp_path / "does-not-exist.yml"))
    get_settings.cache_clear()

    driver = AsyncMock()
    driver.session = AsyncMock()

    await create_project_hierarchy(driver)

    driver.session.assert_not_called()
