"""Contract tests for the GitHub Actions rails — the repository's only CI/CD authority.

The dual-rail era of ADR #4 ended when the GitLab rail was retired (decision
218028c7, 2026-08-18): `.gitlab-ci.yml` left the tree and these tests stopped
asserting parity against it. What they hold instead is the boundary that made the
split worthwhile in the first place: pull requests gate on GitHub-hosted runners so
they stay verifiable with no runner of ours online.

Since 2026-09-23 (ticket 03846021) NO rail reaches the self-hosted ``red-ci``
runner: GitHub deleted it after fourteen days offline, and a public repository has
no business running Docker as root on a VM it shares with private ones. Delivery
publishes to GHCR from a hosted runner with the workflow's own token.
"""

from __future__ import annotations

import re
import shlex
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIRECTORY = REPO_ROOT / ".github/workflows"
CI_WORKFLOW_PATH = WORKFLOW_DIRECTORY / "continuous-integration.yml"
CD_WORKFLOW_PATH = WORKFLOW_DIRECTORY / "continuous-delivery.yml"
RELEASE_WORKFLOW_PATH = WORKFLOW_DIRECTORY / "release.yml"

# This file only loaded the two rails it names, so a third workflow file was
# invisible to all of its assertions — including the runner boundary it exists to
# hold. The list is therefore pinned.
KNOWN_WORKFLOWS = {
    "continuous-integration.yml",
    "continuous-delivery.yml",
    "release.yml",
}

HOSTED_RUNNER = "ubuntu-24.04"
DELIVERY_IMAGE = "ghcr.io/hawkixs/brain-v42"
ON_DEMAND_RUNNER_LABEL = "red-ci"

# The full pull-request gate, pinned as a literal. This roster used to be derived
# from the GitLab job list; with that rail gone, deriving it from anything would
# let a silently dropped job read as "still covered". A job added or removed must
# pass through here by name.
EXPECTED_CI_JOBS = {
    "lint-ruff",
    "lint-mypy",
    "repo-checks",
    "unit-shard",
    "integration-shard",
    "test-unit",
    "test-coverage",
    "test-integration",
    "security-pip-audit",
    "security-pip-audit-embedding-supervisor",
    "security-bandit",
    "security-gitleaks",
    "build-docker",
}

#: The pull-request actions that ship or fetch the shard reports, pinned by commit
#: SHA like every other action in the workflow.
UPLOAD_ARTIFACT_PIN = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
DOWNLOAD_ARTIFACT_PIN = "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"


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
    """A fourth file has to come through here before it can exist.

    This module's assertions only load the paths they name: a workflow added
    alongside was constrained by nothing, and could therefore target the
    on-demand runner without any test flinching.
    """
    present = {
        path.name
        for path in WORKFLOW_DIRECTORY.iterdir()
        if path.is_file() and path.suffix in {".yml", ".yaml"}
    }
    assert present == KNOWN_WORKFLOWS


def test_no_rail_reaches_the_on_demand_runner() -> None:
    """The runner boundary held over the WHOLE directory, not over two files.

    Measured on 2026-08-14: 19 delivery runs, 19 `cancelled`, 0 successes. A job
    placed on this runner does not have "a latency" — it does not run. Since
    2026-09-23 the runner does not exist at all (ticket 03846021).
    """
    for path in sorted(WORKFLOW_DIRECTORY.iterdir()):
        if not path.is_file() or path.suffix not in {".yml", ".yaml"}:
            continue
        for name, job in _load(path)["jobs"].items():
            assert ON_DEMAND_RUNNER_LABEL not in str(job["runs-on"]), (
                f"{path.name}:{name} vise le runner à la demande, hors ligne par conception"
            )


def test_the_release_rail_is_hosted_and_tag_driven() -> None:
    """The release runs with the runner OFF, and that is its whole point.

    The internal registry is private and on internal DNS: it publishes nothing a
    release consumer can reach, so the rail attaches only the wheel and the sdist,
    from a hosted runner no operator has to start.
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


def test_delivery_rail_only_builds_and_pushes_on_a_hosted_runner(
    cd_workflow: dict[Any, Any],
) -> None:
    assert set(cd_workflow["jobs"]) == {"build-docker"}
    assert cd_workflow["jobs"]["build-docker"]["runs-on"] == HOSTED_RUNNER


def test_delivery_rail_publishes_to_ghcr_with_the_workflow_token(
    cd_workflow: dict[Any, Any],
) -> None:
    """GHCR through `github.token`, scoped to this job: no credential to rotate.

    The internal registry this rail pushed to has been off since 2026-09-21.
    """
    job = cd_workflow["jobs"]["build-docker"]
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    assert cd_workflow["permissions"] == {"contents": "read"}
    body = CD_WORKFLOW_PATH.read_text(encoding="utf-8")
    assert f'"{DELIVERY_IMAGE}"' in body
    assert "${{ github.token }}" in body
    assert "registry.hawkixs.local" not in body


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


def test_delivery_rail_consumes_no_repository_secret(
    cd_workflow: dict[Any, Any],
) -> None:
    body = CD_WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "${{ secrets." not in body


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


def test_github_unit_shard_opts_into_the_database_backed_tests(
    ci_workflow: dict[Any, Any],
) -> None:
    """The unit shard must opt into the DB-backed tests explicitly.

    51 unit tests skip without this variable, and a skip is green: a shard that
    dropped it would pass without having run them.
    """
    job = ci_workflow["jobs"]["unit-shard"]
    url = job.get("env", {}).get("BRAIN_V42_TEST_DB_URL")
    assert url, (
        "unit-shard must set BRAIN_V42_TEST_DB_URL, otherwise the DB-backed unit "
        "tests skip and the shard is green without having run them"
    )
    assert url.endswith("/brain_test"), (
        f"the unit shard must point at brain_test, never a production database: {url}"
    )
    assert "postgres" in job.get("services", {}), (
        "unit-shard declares a database URL but no postgres service to serve it"
    )


def test_github_unit_shard_applies_the_schema_before_pytest(
    ci_workflow: dict[Any, Any],
) -> None:
    """tests/unit has no run_migrations fixture; the shard must apply the schema."""
    steps = ci_workflow["jobs"]["unit-shard"].get("steps", [])
    assert any("alembic upgrade head" in str(step.get("run", "")) for step in steps), (
        "unit-shard must apply the schema before pytest; against a bare pgvector "
        "database the DB-backed tests fail on missing relations instead of skipping"
    )


def test_github_unit_shard_measures_coverage_over_the_full_world(
    ci_workflow: dict[Any, Any],
) -> None:
    """Coverage must measure the code the suite REALLY tests.

    The shard that measures coverage is the same shard that runs the DB-backed
    tests: `POSTGRES_URL` must be set (otherwise `Settings()` raises), the DB URL
    must target `brain_test`, the postgres service must serve it, and the schema
    must be applied. A measurement made anywhere narrower would publish a
    percentage over a subset (ticket `f779092b`).
    """
    job = ci_workflow["jobs"]["unit-shard"]
    env = job.get("env", {})

    assert env.get("POSTGRES_URL"), "unit-shard doit poser POSTGRES_URL, sinon Settings() lève"
    url = env.get("BRAIN_V42_TEST_DB_URL")
    assert url, (
        "unit-shard doit poser BRAIN_V42_TEST_DB_URL, sinon les tests adossés à "
        "une base SAUTENT et la couverture publiée décrit un sous-ensemble du code testé"
    )
    assert url.endswith("/brain_test"), (
        f"la mesure de couverture doit viser brain_test, jamais une base de production : {url}"
    )
    assert "postgres" in job.get("services", {}), (
        "unit-shard déclare une URL de base sans service postgres pour la servir"
    )
    runs = [str(step.get("run", "")) for step in job.get("steps", [])]
    assert any("--cov=brain_v42" in run for run in runs), (
        "le shard doit mesurer la couverture ; sans --cov la couverture agrégée est vide"
    )


def test_github_integration_shard_opts_into_the_disposable_cluster(
    ci_workflow: dict[Any, Any],
) -> None:
    """The integration shard runs its own throwaway Postgres cluster."""
    job = ci_workflow["jobs"]["integration-shard"]
    env = job.get("env", {})
    assert env.get("BRAIN_V42_TEST_DB_URL"), "integration-shard must opt into the DB-backed tests"
    assert env.get("BRAIN_V42_TEST_DISPOSABLE_CLUSTER") == "1", (
        "integration-shard runs a throwaway cluster; dropping the opt-in lets the "
        "role-mutation benches run against a shared database"
    )
    assert "postgres" in job.get("services", {}), (
        "integration-shard declares a database URL but no postgres service to serve it"
    )


def test_github_shard_matrices_run_every_instance_and_tolerate_failures(
    ci_workflow: dict[Any, Any],
) -> None:
    """Every shard runs to completion even when a sibling fails, on its own Postgres."""
    unit = ci_workflow["jobs"]["unit-shard"]
    integration = ci_workflow["jobs"]["integration-shard"]

    assert unit["strategy"]["fail-fast"] is False, "a failing unit shard must not cancel the rest"
    assert unit["strategy"]["matrix"]["shard"] == [1, 2, 3, 4]
    assert "postgres" in unit.get("services", {})

    assert integration["strategy"]["fail-fast"] is False, (
        "a failing integration shard must not cancel the rest"
    )
    assert integration["strategy"]["matrix"]["shard"] == [1, 2, 3]
    assert "postgres" in integration.get("services", {})


def test_github_shards_read_n_only_from_the_matrix(
    ci_workflow: dict[Any, Any],
) -> None:
    """`--shard` takes `strategy.job-total`, so N lives only in the matrix list."""
    for job_name in ("unit-shard", "integration-shard"):
        steps = ci_workflow["jobs"][job_name].get("steps", [])
        runs = [str(step.get("run", "")) for step in steps]
        shard_runs = [run for run in runs if "--shard=" in run]
        assert shard_runs, f"{job_name} must pass --shard to pytest"
        for run in shard_runs:
            assert "${{ strategy.job-total }}" in run, (
                f"{job_name} must read N from strategy.job-total, never hardcode it"
            )
            assert "${{ matrix.shard }}" in run, f"{job_name} must read its index from matrix.shard"


def test_github_aggregators_gate_their_shard_matrices(
    ci_workflow: dict[Any, Any],
) -> None:
    """The three historical names are aggregators that fail unless every shard passed."""
    for aggregator, shard in (
        ("test-unit", "unit-shard"),
        ("test-integration", "integration-shard"),
        ("test-coverage", "unit-shard"),
    ):
        job = ci_workflow["jobs"][aggregator]
        needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
        assert shard in needs, f"{aggregator} must need {shard}"
        assert job["if"] == "always()", f"{aggregator} must run even when {shard} fails"
        runs = [str(step.get("run", "")) for step in job.get("steps", [])]
        assert any(f"needs.{shard}.result" in run for run in runs), (
            f"{aggregator} must fail unless {shard}.result is success"
        )


def test_github_test_unit_carries_the_static_repository_checks(
    ci_workflow: dict[Any, Any],
) -> None:
    """`repo-checks` is not a required status check of the protected branch; `test-unit` is.

    The module layering and image pin gates used to run inside `test-unit`. They now
    run in `repo-checks` (in parallel with the shards), so the `test-unit` aggregator
    must fail unless `repo-checks` passed too: otherwise those two gates would lose their
    merge-blocking force without any branch protection change.
    """
    job = ci_workflow["jobs"]["test-unit"]
    assert "repo-checks" in job["needs"]
    assert job["if"] == "always()"
    runs = " ".join(str(step.get("run", "")) for step in job.get("steps", []))
    assert "needs.repo-checks.result" in runs, "test-unit must fail unless repo-checks passed"
    assert "needs.unit-shard.result" in runs


def test_github_shard_reports_are_pinned_by_sha(
    ci_workflow: dict[Any, Any],
) -> None:
    """The artifact actions that move shard reports are pinned by commit SHA."""
    unit_steps = ci_workflow["jobs"]["unit-shard"]["steps"]
    uses = [step.get("uses") for step in unit_steps]
    assert UPLOAD_ARTIFACT_PIN in uses, (
        "unit-shard must upload its reports with the pinned upload-artifact action"
    )

    coverage_steps = ci_workflow["jobs"]["test-coverage"]["steps"]
    uses = [step.get("uses") for step in coverage_steps]
    assert DOWNLOAD_ARTIFACT_PIN in uses, (
        "test-coverage must download the shard reports with the pinned download-artifact action"
    )


def test_github_coverage_threshold_is_passed_to_the_report(
    ci_workflow: dict[Any, Any],
) -> None:
    """The unchanged 60 threshold must reach `coverage report`, not be dropped."""
    steps = ci_workflow["jobs"]["test-coverage"].get("steps", [])
    runs = [str(step.get("run", "")) for step in steps]
    reports = [run for run in runs if "coverage report" in run]
    assert reports, "test-coverage must report the combined coverage"
    assert all("--fail-under=60" in run for run in reports), (
        "the coverage threshold must stay 60 on the combined report"
    )


def test_github_coverage_refuses_a_measurement_made_on_a_subset(
    ci_workflow: dict[Any, Any],
) -> None:
    """The inverted witness — IT is the deliverable, not the percentage.

    A test that SKIPS does not redden a job: without this guardrail, the shard's
    recipe could drift and CI would stay green on partial coverage. The witness
    now reads every shard's JUnit report, not a single job's.
    """
    unit_steps = ci_workflow["jobs"]["unit-shard"].get("steps", [])
    unit_runs = [str(step.get("run", "")) for step in unit_steps]
    assert any("--junitxml=" in run for run in unit_runs), (
        "chaque shard doit produire un rapport JUnit, seule source machine-lisible des sauts"
    )

    coverage_steps = ci_workflow["jobs"]["test-coverage"].get("steps", [])
    coverage_runs = [str(step.get("run", "")) for step in coverage_steps]
    witness = [run for run in coverage_runs if "check_coverage_saw_the_db_tests.py" in run]
    assert witness, (
        "le job doit refuser une couverture mesurée sur un sous-ensemble ; sans ce "
        "garde-fou le chiffre redérive au prochain écart de recette"
    )
    assert all("unit-junit-" in run for run in witness), (
        "le témoin doit lire CHAQUE rapport JUnit des shards, pas un seul"
    )


def _image_step_script(workflow: dict[Any, Any]) -> str:
    """The `run` script of the one step that builds the image, from the parsed workflow."""
    scripts = [
        str(step["run"])
        for step in workflow["jobs"]["build-docker"]["steps"]
        if "docker build" in str(step.get("run", ""))
    ]
    assert len(scripts) == 1, "exactly one step must build the image"
    return scripts[0]


def _command(script: str, prefix: str) -> list[str]:
    lines = [line.strip() for line in script.splitlines() if line.strip().startswith(prefix)]
    assert len(lines) == 1, f"expected exactly one `{prefix}` command"
    return shlex.split(lines[0])


def _assert_uv_version_parity(workflows: dict[str, dict[Any, Any]]) -> None:
    """Pin the resolved uv version across rails and all narrower env scopes."""
    names = (
        "continuous-integration.yml",
        "continuous-delivery.yml",
        "release.yml",
    )
    versions: list[str] = []
    for name in names:
        workflow = workflows[name]
        version = (workflow.get("env") or {}).get("UV_VERSION")
        assert isinstance(version, str) and version.strip(), (
            f"{name} must set a non-empty top-level env.UV_VERSION"
        )
        versions.append(version)
    assert len(set(versions)) == 1, (
        f"workflow uv versions differ: {dict(zip(names, versions, strict=True))}"
    )

    expected = versions[0]
    for name in names:
        for job_name, job in workflows[name]["jobs"].items():
            assert (job.get("env") or {}).get("UV_VERSION", expected) == expected, (
                f"{name}:{job_name} overrides UV_VERSION"
            )
            for step_index, step in enumerate(job.get("steps", [])):
                assert (step.get("env") or {}).get("UV_VERSION", expected) == expected, (
                    f"{name}:{job_name} step {step_index} overrides UV_VERSION"
                )


def test_pull_request_rail_builds_the_image_without_publishing_anything(
    ci_workflow: dict[Any, Any],
) -> None:
    """The Dockerfile must be exercised BEFORE the merge, with nothing leaving the runner.

    Delivery is the only other place the image is built, and it runs on push to main:
    a Dockerfile broken by a PR (the workspace member `uv sync --locked` could not
    find, 2026-09-14 to 2026-09-23) was first seen after the merge. The job carries
    the read-only default token, no registry login, no secret and no artifact.
    """
    job = ci_workflow["jobs"]["build-docker"]
    assert job["permissions"] == {"contents": "read"}
    body = yaml.safe_dump(job)
    for forbidden in ("docker login", "docker push", "secrets.", "github.token", "upload-artifact"):
        assert forbidden not in body


def test_pull_request_image_build_cannot_drift_from_the_delivery_build(
    ci_workflow: dict[Any, Any], cd_workflow: dict[Any, Any]
) -> None:
    """Both rails build the same Dockerfile with the same build arguments.

    The registry-facing options (cache source, publication tags, OCI label) are the
    only ones delivery may carry alone; the smoke command is the same line.
    """
    ci_script, cd_script = _image_step_script(ci_workflow), _image_step_script(cd_workflow)

    def shared_options(script: str) -> list[str]:
        words = _command(script, "docker build")
        options: list[str] = []
        index = 0
        while index < len(words):
            if words[index] in {"--cache-from", "--label", "-t", "--build-arg", "-f"}:
                flag, value = words[index], words[index + 1]
                index += 2
                registry_only = flag in {"--cache-from", "--label", "-t"} or (
                    flag == "--build-arg" and value.startswith("BUILDKIT_INLINE_CACHE")
                )
                if not registry_only:
                    options.append(f"{flag} {value}")
            else:
                options.append(words[index])
                index += 1
        return options

    assert shared_options(ci_script) == shared_options(cd_script)
    assert _command(ci_script, "docker run") == _command(cd_script, "docker run")


def test_all_workflows_use_the_same_uv_version() -> None:
    """The three rails must resolve the same uv version at every scope."""
    workflows = {
        path.name: _load(path)
        for path in (CI_WORKFLOW_PATH, CD_WORKFLOW_PATH, RELEASE_WORKFLOW_PATH)
    }

    _assert_uv_version_parity(workflows)

    modified = {name: dict(workflow) for name, workflow in workflows.items()}
    modified["continuous-delivery.yml"]["env"] = {
        **workflows["continuous-delivery.yml"]["env"],
        "UV_VERSION": "0.10.8",
    }
    with pytest.raises(AssertionError, match="workflow uv versions differ"):
        _assert_uv_version_parity(modified)


def test_uv_version_parity_rejects_job_override() -> None:
    workflows = {
        path.name: _load(path)
        for path in (CI_WORKFLOW_PATH, CD_WORKFLOW_PATH, RELEASE_WORKFLOW_PATH)
    }
    modified = deepcopy(workflows)
    modified["continuous-delivery.yml"]["jobs"]["build-docker"].setdefault("env", {})[
        "UV_VERSION"
    ] = "0.10.8"
    with pytest.raises(
        AssertionError,
        match="continuous-delivery.yml:build-docker overrides UV_VERSION",
    ):
        _assert_uv_version_parity(modified)


def test_uv_version_parity_rejects_step_override() -> None:
    workflows = {
        path.name: _load(path)
        for path in (CI_WORKFLOW_PATH, CD_WORKFLOW_PATH, RELEASE_WORKFLOW_PATH)
    }
    modified = deepcopy(workflows)
    modified["continuous-delivery.yml"]["jobs"]["build-docker"]["steps"][0].setdefault("env", {})[
        "UV_VERSION"
    ] = "0.10.8"
    with pytest.raises(
        AssertionError,
        match="continuous-delivery.yml:build-docker step 0 overrides UV_VERSION",
    ):
        _assert_uv_version_parity(modified)


def test_uv_version_parity_accepts_null_job_and_step_env() -> None:
    workflows = {
        path.name: _load(path)
        for path in (CI_WORKFLOW_PATH, CD_WORKFLOW_PATH, RELEASE_WORKFLOW_PATH)
    }
    modified = deepcopy(workflows)
    null_job = modified["continuous-delivery.yml"]["jobs"]["build-docker"]
    null_job["env"] = None
    null_job["steps"][0]["env"] = None
    _assert_uv_version_parity(modified)


def test_the_default_dockerfile_stage_is_the_production_one() -> None:
    """Neither rail passes `--target`: the image they build is the LAST stage."""
    stages = re.findall(
        r"^FROM\s+\S+\s+AS\s+(\S+)\s*$",
        (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    assert stages[-1] == "production"
    for path in (CI_WORKFLOW_PATH, CD_WORKFLOW_PATH):
        assert "--target" not in path.read_text(encoding="utf-8")
