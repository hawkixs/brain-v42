"""Contract tests for the GitHub Actions rails — the repository's only CI/CD authority.

The dual-rail era of ADR #4 ended when the GitLab rail was retired (decision
218028c7, 2026-08-18): `.gitlab-ci.yml` left the tree and these tests stopped
asserting parity against it. What they hold instead is the boundary that made the
split worthwhile in the first place: pull requests gate on GitHub-hosted runners so
they stay verifiable while the on-demand ``red-ci`` runner is stopped, and only
pushes to main reach that runner.
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

# The full pull-request gate, pinned as a literal. This roster used to be derived
# from the GitLab job list; with that rail gone, deriving it from anything would
# let a silently dropped job read as "still covered". A job added or removed must
# pass through here by name.
EXPECTED_CI_JOBS = {
    "lint-ruff",
    "lint-mypy",
    "test-unit",
    "test-coverage",
    "test-integration",
    "security-pip-audit",
    "security-pip-audit-embedding-supervisor",
    "security-bandit",
    "security-gitleaks",
}


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


def test_the_gitlab_rail_stays_retired() -> None:
    """The tree must not grow a second CI authority back.

    The GitLab rail was retired with its project's pipelines disabled; a
    reappearing `.gitlab-ci.yml` would be a config file no test parses and no
    gate audits — exactly the unowned-rail failure mode the retirement removed.
    """
    assert not (REPO_ROOT / ".gitlab-ci.yml").exists()


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


def test_the_release_rail_is_hosted_and_tag_driven() -> None:
    """La release tourne runner ÉTEINT, et c'est sa raison d'être.

    Le registre interne est privé et à DNS interne : il ne publie rien qu'un
    consommateur de release puisse atteindre, donc le rail n'attache que la wheel
    et le sdist, depuis un runner hébergé qu'aucun opérateur n'a à démarrer.
    """
    release = _load(RELEASE_WORKFLOW_PATH)
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


def test_pull_request_rail_carries_the_full_pinned_gate(ci_workflow: dict[Any, Any]) -> None:
    assert set(ci_workflow["jobs"]) == EXPECTED_CI_JOBS


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


def test_no_job_tolerates_failure() -> None:
    """The security burn-in is over: every job blocks on red, on every rail.

    `continue-on-error` renders a check that fails without failing the run —
    the GitLab-era `allow_failure` in different clothes. The burn-in it served
    ended with all nine jobs measured green on the public rail (PR #1,
    2026-08-18); from here, a tolerated job is just an ignored one.
    """
    for path in sorted(WORKFLOW_DIRECTORY.iterdir()):
        if not path.is_file() or path.suffix not in {".yml", ".yaml"}:
            continue
        for name, job in _load(path)["jobs"].items():
            assert "continue-on-error" not in job, f"{path.name}:{name} tolerates failure"


def test_github_test_unit_opts_into_the_database_backed_tests(
    ci_workflow: dict[Any, Any],
) -> None:
    """The unit gate must opt into the DB-backed tests explicitly.

    51 unit tests skip without this variable, and a skip is green: a job that
    dropped it would pass without having run them.
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
    """tests/unit has no run_migrations fixture; the job must apply the schema."""
    steps = ci_workflow["jobs"]["test-unit"].get("steps", [])
    assert any("alembic upgrade head" in str(step.get("run", "")) for step in steps), (
        "test-unit must apply the schema before pytest; against a bare pgvector "
        "database the DB-backed tests fail on missing relations instead of skipping"
    )


def test_github_coverage_can_build_settings_without_a_database(
    ci_workflow: dict[Any, Any],
) -> None:
    """POSTGRES_URL posé, BRAIN_V42_TEST_DB_URL absent."""
    coverage = ci_workflow["jobs"]["test-coverage"].get("env", {})

    assert coverage.get("POSTGRES_URL"), (
        "test-coverage doit poser POSTGRES_URL, sinon Settings() lève"
    )
    assert "BRAIN_V42_TEST_DB_URL" not in coverage, (
        "test-coverage ne doit pas réveiller les tests adossés à une base tant que "
        "le Postgres de la CI ne les supporte pas"
    )
