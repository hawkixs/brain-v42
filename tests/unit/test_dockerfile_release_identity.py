from pathlib import Path


def test_production_stage_is_the_default_build_target() -> None:
    """red-rail's `rail release` builds without --target, so the last stage ships."""
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    stages = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]

    assert stages[-1].endswith(" AS production")


def test_production_stage_embeds_release_identity_and_recovery_binding() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    lines = dockerfile.splitlines()
    start = lines.index("FROM deps AS production")
    production = lines[start + 1 :]
    publish = next(i for i, line in enumerate(production) if "publish-image" in line)
    copy_recovery = next(
        i for i, line in enumerate(production) if line.startswith("COPY ops/recovery/")
    )
    copy_alembic = next(i for i, line in enumerate(production) if line.startswith("COPY alembic/"))
    user = next(i for i, line in enumerate(production) if line == "USER appuser")
    assert copy_alembic < publish < user
    assert copy_recovery < publish < user
    assert '--release-sha "$GIT_SHA"' in production[publish]
    assert sum(line.startswith("ARG GIT_SHA") for line in lines) == 1
    assert sum(line.startswith("ARG VERSION") for line in lines) == 1
    assert production[publish - 3].startswith("ARG VERSION")
    assert production[publish - 2].startswith("ARG GIT_SHA")
    assert production[publish - 1].startswith("ENV BRAIN_RELEASE_VERSION=")
    revision_labels = [
        i
        for i, line in enumerate(production)
        if line
        in (
            "LABEL org.opencontainers.image.revision=$GIT_SHA",
            "LABEL org.opencontainers.image.revision=${GIT_SHA}",
        )
    ]
    assert len(revision_labels) == 1
    git_sha_arg = next(i for i, line in enumerate(production) if line == "ARG GIT_SHA=")
    assert git_sha_arg < revision_labels[0]
    assert '--release-sha "$GIT_SHA"' in production[publish]
    assert lines[-1] == 'CMD ["python", "-m", "brain_v42.mcp.server"]'
    assert "FROM python:3.12-slim@sha256:" in dockerfile
