"""Contract tests for the GitHub Actions dual rail (ADR #4).

The rails are autonomous: pull requests gate on GitHub-hosted runners so they stay
verifiable while the on-demand ``red-ci`` runner is stopped, and only pushes to main
reach that runner. These tests hold that boundary and the job-for-job parity with
``.gitlab-ci.yml``, which keeps running in parallel during the migration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIRECTORY = REPO_ROOT / ".github/workflows"
CI_WORKFLOW_PATH = WORKFLOW_DIRECTORY / "continuous-integration.yml"
CD_WORKFLOW_PATH = WORKFLOW_DIRECTORY / "continuous-delivery.yml"
RELEASE_WORKFLOW_PATH = WORKFLOW_DIRECTORY / "release.yml"
GITLAB_CI_PATH = REPO_ROOT / ".gitlab-ci.yml"

# Ce fichier ne chargeait que les deux rails qu'il nomme, si bien qu'un troisième
# fichier de workflow était invisible de toutes ses assertions — y compris de la
# frontière de runner qu'il existe pour tenir. La liste est donc épinglée.
KNOWN_WORKFLOWS = {
    "continuous-integration.yml",
    "continuous-delivery.yml",
    "release.yml",
}

HOSTED_RUNNER = "ubuntu-24.04"
DELIVERY_RUNNER = ["self-hosted", "Linux", "X64", "red-ci"]
ON_DEMAND_RUNNER_LABEL = "red-ci"

# Every GitLab job and the GitHub job that carries it. build:docker is the only one
# that belongs to the delivery rail; the other nine gate pull requests.
GITLAB_TO_GITHUB = {
    "lint:ruff": "lint-ruff",
    "lint:mypy": "lint-mypy",
    "test:unit": "test-unit",
    "test:coverage": "test-coverage",
    "test:integration": "test-integration",
    "security:pip-audit": "security-pip-audit",
    "security:pip-audit:embedding-supervisor": "security-pip-audit-embedding-supervisor",
    "security:bandit": "security-bandit",
    "security:gitleaks": "security-gitleaks",
    "build:docker": "build-docker",
}
GITLAB_NON_JOB_KEYS = {"stages", "variables", "cache", "workflow", "default", "include"}


def _load(path: Path) -> dict[Any, Any]:
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing"
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module")
def ci_workflow() -> dict[Any, Any]:
    return _load(CI_WORKFLOW_PATH)


@pytest.fixture(scope="module")
def cd_workflow() -> dict[Any, Any]:
    return _load(CD_WORKFLOW_PATH)


@pytest.fixture(scope="module")
def gitlab_jobs() -> dict[str, Any]:
    config = _load(GITLAB_CI_PATH)
    return {
        str(name): value
        for name, value in config.items()
        if isinstance(name, str) and not name.startswith(".") and name not in GITLAB_NON_JOB_KEYS
    }


def test_gitlab_jobs_are_all_mapped_to_a_github_job(gitlab_jobs: dict[str, Any]) -> None:
    assert set(gitlab_jobs) == set(GITLAB_TO_GITHUB)


def test_the_workflow_directory_holds_exactly_the_known_rails() -> None:
    """Un quatrième fichier doit passer par ici avant d'exister.

    Les assertions de ce module ne chargent que les chemins qu'elles nomment :
    un workflow ajouté à côté n'était contraint par rien, et pouvait donc viser
    le runner à la demande sans qu'aucun test ne bronche.
    """
    present = {
        path.name
        for path in WORKFLOW_DIRECTORY.iterdir()
        if path.is_file() and path.suffix in {".yml", ".yaml"}
    }
    assert present == KNOWN_WORKFLOWS


def test_only_the_delivery_rail_reaches_the_on_demand_runner() -> None:
    """La frontière de runner tenue sur TOUT le répertoire, pas sur deux fichiers.

    Mesuré le 2026-08-14 : 19 runs de livraison, 19 `cancelled`, 0 succès. Un job
    posé sur ce runner n'a pas « une latence » — il ne tourne pas.
    """
    for path in sorted(WORKFLOW_DIRECTORY.iterdir()):
        if not path.is_file() or path.suffix not in {".yml", ".yaml"}:
            continue
        for name, job in _load(path)["jobs"].items():
            if path == CD_WORKFLOW_PATH:
                continue
            assert ON_DEMAND_RUNNER_LABEL not in str(job["runs-on"]), (
                f"{path.name}:{name} vise le runner à la demande, hors ligne par conception"
            )


def test_the_release_rail_is_github_only_and_hosted() -> None:
    """La release n'a pas de jumeau GitLab, et c'est délibéré.

    ``GITLAB_TO_GITHUB`` décrit la parité job pour job des rails de lint, test et
    build. Les releases de ce dépôt vivent sur GitHub seul : le registre GitLab
    est privé et à DNS interne, il ne publie rien qu'un consommateur de release
    puisse atteindre. L'asymétrie est donc nommée ici plutôt que subie.
    """
    release = _load(RELEASE_WORKFLOW_PATH)
    assert "release" not in set(GITLAB_TO_GITHUB.values())
    assert release[True] == {"push": {"tags": ["v*"]}}
    for name, job in release["jobs"].items():
        assert job["runs-on"] == HOSTED_RUNNER, name


def test_integration_rail_triggers_only_on_pull_request(ci_workflow: dict[Any, Any]) -> None:
    # PyYAML 1.1 reads a bare `on:` key as the boolean True.
    assert ci_workflow[True] == {"pull_request": None}


def test_delivery_rail_triggers_only_on_push_to_main(cd_workflow: dict[Any, Any]) -> None:
    assert cd_workflow[True] == {"push": {"branches": ["main"]}}


def test_pull_request_jobs_never_reach_the_on_demand_runner(ci_workflow: dict[Any, Any]) -> None:
    runners = {name: job["runs-on"] for name, job in ci_workflow["jobs"].items()}
    assert runners == dict.fromkeys(runners, HOSTED_RUNNER)


def test_pull_request_rail_carries_every_non_build_gitlab_job(ci_workflow: dict[Any, Any]) -> None:
    expected = {job for name, job in GITLAB_TO_GITHUB.items() if name != "build:docker"}
    assert set(ci_workflow["jobs"]) == expected


def test_delivery_rail_only_builds_and_pushes_on_the_red_ci_runner(
    cd_workflow: dict[Any, Any],
) -> None:
    assert set(cd_workflow["jobs"]) == {"build-docker"}
    assert cd_workflow["jobs"]["build-docker"]["runs-on"] == DELIVERY_RUNNER


def test_delivery_rail_carries_no_deployment_machinery(cd_workflow: dict[Any, Any]) -> None:
    # brain-v42 has no deploy stage: the rollout of a pushed digest stays a manual,
    # out-of-band operator step. Copying red-gift's VPS delivery here would be a
    # capability nobody asked for.
    job = cd_workflow["jobs"]["build-docker"]
    assert "environment" not in job
    # Comment lines are stripped: the header names the red-gift machinery precisely
    # in order to say it is absent, and the assertion is about what the rail runs.
    body = "\n".join(
        line
        for line in CD_WORKFLOW_PATH.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    for forbidden in ("SSH_PRIVATE_KEY", "VPS_SSH", "known_hosts", "rollback", "promote"):
        assert forbidden not in body


def test_delivery_rail_consumes_only_the_two_registry_secrets(
    cd_workflow: dict[Any, Any],
) -> None:
    body = CD_WORKFLOW_PATH.read_text(encoding="utf-8")
    secrets = {fragment.split("}}")[0].strip() for fragment in body.split("${{ secrets.")[1:]}
    assert secrets == {"REGISTRY_USER", "REGISTRY_PASSWORD"}


def test_security_burn_in_tolerance_matches_gitlab(
    ci_workflow: dict[Any, Any], gitlab_jobs: dict[str, Any]
) -> None:
    # Migrating and hardening at once would turn the whole rail red and unreadable:
    # a job that GitLab still tolerates must stay tolerated here, and no other may be.
    for gitlab_name, github_name in GITLAB_TO_GITHUB.items():
        if gitlab_name == "build:docker":
            continue
        allowed = bool(gitlab_jobs[gitlab_name].get("allow_failure", False))
        job = ci_workflow["jobs"][github_name]
        assert bool(job.get("continue-on-error", False)) is allowed, github_name


def test_github_test_unit_opts_into_the_database_backed_tests(
    ci_workflow: dict[Any, Any],
) -> None:
    """The GitHub rail must carry the same opt-in as GitLab, on its own.

    The two rails are autonomous by ADR #4 — neither consults the other's status —
    so GITLAB_TO_GITHUB mapping a job name proves nothing about its contents. 51
    unit tests skip without this variable, and a skip is green.
    """
    job = ci_workflow["jobs"]["test-unit"]
    url = job.get("env", {}).get("BRAIN_V42_TEST_DB_URL")
    assert url, (
        "test-unit must set BRAIN_V42_TEST_DB_URL, otherwise the DB-backed unit "
        "tests skip and the job is green without having run them"
    )
    assert url.endswith("/brain_test"), (
        f"the unit gate must point at brain_test, never a production database: {url}"
    )
    assert "postgres" in job.get("services", {}), (
        "test-unit declares a database URL but no postgres service to serve it"
    )


def test_github_test_unit_applies_the_schema_before_pytest(
    ci_workflow: dict[Any, Any],
) -> None:
    """Same contract as GitLab: tests/unit has no run_migrations fixture."""
    steps = ci_workflow["jobs"]["test-unit"].get("steps", [])
    assert any("alembic upgrade head" in str(step.get("run", "")) for step in steps), (
        "test-unit must apply the schema before pytest; against a bare pgvector "
        "database the DB-backed tests fail on missing relations instead of skipping"
    )


def test_github_coverage_can_build_settings_without_a_database(
    ci_workflow: dict[Any, Any],
) -> None:
    """Jumeau GitHub : POSTGRES_URL posé, BRAIN_V42_TEST_DB_URL absent."""
    coverage = ci_workflow["jobs"]["test-coverage"].get("env", {})

    assert coverage.get("POSTGRES_URL"), (
        "test-coverage doit poser POSTGRES_URL, sinon Settings() lève"
    )
    assert "BRAIN_V42_TEST_DB_URL" not in coverage, (
        "test-coverage ne doit pas réveiller les tests adossés à une base tant que "
        "le Postgres de la CI ne les supporte pas"
    )
