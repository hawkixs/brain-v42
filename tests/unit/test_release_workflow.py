"""The release rail must be able to run with the RUNNER OFF.

Measured on 2026-08-14: 19 runs of the delivery rail, 19 `cancelled`, 0 successes.
The `[self-hosted, Linux, X64, red-ci]` runner is started on demand and its
`offline` state is NORMAL, documented. A release job placed there would never have
run: it would have waited indefinitely for an operator to start a VM, that is,
exactly the manual gesture the release is meant to replace.

This rail attaches NO image. The registry is private and on internal DNS
(`registry.hawkixs.local`), so its address means nothing to a release consumer; and
an image would bake in the reranker's ONNX, whose upstream licence NOTICE declares
indeterminate.

The check that the wheel does carry the migrations is REUSED, never copied:
`tests/unit/test_wheel_ships_migrations.py` already builds a real wheel and reads
the expected set from disk. A second implementation in YAML would drift from the
first without anything reporting it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW_PATH = REPO_ROOT / ".github/workflows/release.yml"
WHEEL_MIGRATION_TEST = "tests/unit/test_wheel_ships_migrations.py"

HOSTED_RUNNER = "ubuntu-24.04"
ON_DEMAND_RUNNER_LABEL = "red-ci"

# `uses: owner/repo@<40 hex characters>` — a mobile tag (`@v4`) is rewritten under
# the same name, a SHA is not.
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


@pytest.fixture(scope="module")
def release_workflow() -> dict[Any, Any]:
    assert RELEASE_WORKFLOW_PATH.is_file(), (
        f"{RELEASE_WORKFLOW_PATH.relative_to(REPO_ROOT)} est absent : sans lui, publier "
        "une release reste un geste manuel non reproductible"
    )
    loaded = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module")
def release_body() -> str:
    return RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def release_job(release_workflow: dict[Any, Any]) -> dict[Any, Any]:
    jobs = release_workflow["jobs"]
    assert len(jobs) == 1, f"un seul job attendu sur ce rail, obtenu {sorted(jobs)}"
    job = next(iter(jobs.values()))
    assert isinstance(job, dict)
    return job


def _run_scripts(job: dict[Any, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def test_release_triggers_only_on_a_version_tag(release_workflow: dict[Any, Any]) -> None:
    """A push on a branch publishes nothing: only a `v*` tag triggers."""
    # PyYAML 1.1 reads a bare `on:` key as the boolean True.
    assert release_workflow[True] == {"push": {"tags": ["v*"]}}


def test_release_runs_on_a_hosted_runner(release_job: dict[Any, Any]) -> None:
    """The heart of the batch: this rail must run with the runner OFF.

    Measurement: 19 delivery runs, 19 `cancelled`, 0 successes, because the
    on-demand runner is offline by design.
    """
    runner = release_job["runs-on"]
    assert runner == HOSTED_RUNNER, (
        f"la release doit tourner sur le runner hébergé {HOSTED_RUNNER}, pas sur {runner!r}"
    )
    assert ON_DEMAND_RUNNER_LABEL not in str(runner)


def test_release_asks_for_nothing_beyond_writing_the_release(
    release_workflow: dict[Any, Any],
) -> None:
    """`contents: write` and NOTHING else — the default token is too broad."""
    assert release_workflow["permissions"] == {"contents": "write"}


def test_release_job_does_not_widen_the_workflow_permissions(
    release_job: dict[Any, Any],
) -> None:
    """A permission set at job level REPLACES the workflow's."""
    assert "permissions" not in release_job


def test_every_action_is_pinned_by_sha(release_body: str) -> None:
    """A mobile tag is rewritten under the same name; a SHA is not."""
    referenced = re.findall(r"^\s*uses:\s*(\S+)", release_body, flags=re.MULTILINE)
    assert referenced, "le rail doit référencer au moins une action (checkout)"
    unpinned = [action for action in referenced if not PINNED_ACTION.match(action)]
    assert not unpinned, f"action(s) non épinglée(s) par SHA : {unpinned}"


def test_release_builds_both_distributions(release_job: dict[Any, Any]) -> None:
    """A wheel alone is not enough: the sdist carries the corresponding source."""
    scripts = _run_scripts(release_job)
    assert "uv build" in scripts, "le rail doit construire les distributions"
    assert ".whl" in scripts and ".tar.gz" in scripts, (
        "le rail doit nommer les deux artefacts, wheel ET sdist"
    )


def test_release_reuses_the_wheel_migration_contract(release_job: dict[Any, Any]) -> None:
    """The migration check is REUSED, never reimplemented in YAML.

    The referenced test builds a real wheel and reads the expected set from disk:
    copying its logic here would make it drift in silence.
    """
    assert (REPO_ROOT / WHEEL_MIGRATION_TEST).is_file(), (
        f"{WHEEL_MIGRATION_TEST} est le contrat réutilisé ; il doit exister"
    )
    scripts = _run_scripts(release_job)
    assert WHEEL_MIGRATION_TEST in scripts, (
        "le rail doit lancer le contrat existant, pas en écrire un second dans du YAML"
    )
    assert "alembic/versions" not in scripts, (
        "réimplémentation détectée : la vérification des migrations appartient à "
        f"{WHEEL_MIGRATION_TEST}"
    )


def test_release_refuses_a_tag_that_does_not_name_the_built_version(
    release_job: dict[Any, Any],
) -> None:
    """Without this guard, `v9.9.9` would publish a 0.2.0 wheel.

    That is the feature's whole promise: the release's number is the one `/health`
    announces. A tag that does not name the version built breaks it silently, and the
    release would stay published.
    """
    scripts = _run_scripts(release_job)
    assert "github.ref_name" in scripts or "GITHUB_REF_NAME" in scripts, (
        "le rail doit lire le tag pour le confronter à la version construite"
    )
    assert "brain_v42-${" in scripts, (
        "le rail doit exiger sur le disque les artefacts nommés par le tag, "
        "sinon rien ne lie la release à la version qu'elle publie"
    )


def test_release_attaches_the_two_artifacts_with_generated_notes(
    release_job: dict[Any, Any],
) -> None:
    """191 of the 199 subjects of the last 200 commits are conventional: the
    generated notes say something."""
    scripts = _run_scripts(release_job)
    assert "gh release create" in scripts
    assert "--generate-notes" in scripts
    assert "--verify-tag" in scripts, (
        "sans --verify-tag, `gh` créerait le tag manquant au lieu de refuser"
    )


def test_release_attaches_no_container_image(release_body: str) -> None:
    """The registry is private and on internal DNS; and an image would bake in the
    reranker's ONNX, whose upstream licence NOTICE declares indeterminate."""
    body = "\n".join(
        line for line in release_body.splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in ("docker", "CI_REGISTRY", "registry.hawkixs.local", "REGISTRY_PASSWORD"):
        assert forbidden not in body, (
            f"le rail de release ne doit pas toucher au registre d'images : {forbidden!r}"
        )


def test_release_never_invokes_a_module_entrypoint(release_job: dict[Any, Any]) -> None:
    """`scripts/check_container_image_pins.py` is fail-closed on `python -m <module>`
    and would refuse the workflow — which would redden the WHOLE unit gate."""
    assert "python -m " not in _run_scripts(release_job)
