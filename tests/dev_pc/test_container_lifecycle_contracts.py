"""Static contracts for deterministic local-image lifecycle commands.

The dev-pc Compose services use ``pull_policy: build``.  Every lifecycle path
that consumes an image already loaded or just built must therefore pass
``--no-build`` explicitly, otherwise ``docker compose up`` may rebuild it.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
ROLLBACK_SCRIPT = REPO_ROOT / "deploy" / "dev-pc" / "rollback.sh"
README = REPO_ROOT / "deploy" / "dev-pc" / "README.md"


def _active_lines(path: Path) -> list[str]:
    """Return non-empty, non-comment shell lines stripped of indentation."""
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_rollback_recreates_loaded_images_without_building() -> None:
    """Rollback must consume restored images and keep qodo stopped."""
    lines = _active_lines(ROLLBACK_SCRIPT)
    qodo = 'docker compose -f "$COMPOSE_FILE" up -d --no-build --no-start embedding-qodo'
    supervisor = 'docker compose -f "$COMPOSE_FILE" up -d --no-build embedding-supervisor'

    assert qodo in lines
    assert supervisor in lines
    assert lines.index(qodo) < lines.index(supervisor)


def test_native_redeploy_recreates_both_services_without_implicit_build() -> None:
    """After a build, redeploy must replace stopped qodo then supervisor."""
    readme = README.read_text(encoding="utf-8")
    section = readme.split("### 7.3 — Upgrade / redeploy (native)", maxsplit=1)[1]
    section = section.split("\n---", maxsplit=1)[0]

    build = "docker compose build"
    qodo = "docker compose up -d --no-build --no-start --force-recreate embedding-qodo"
    supervisor = "docker compose up -d --no-build --force-recreate embedding-supervisor"

    assert build in section
    assert qodo in section
    assert supervisor in section
    assert section.index(build) < section.index(qodo) < section.index(supervisor)
    assert "qodo rebuilt lazily" not in section.lower()
