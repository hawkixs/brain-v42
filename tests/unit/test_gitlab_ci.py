"""
TDD tests for Feature #639: CI/CD GitLab — ajout stage intégration + polish

These tests validate the .gitlab-ci.yml structure without running GitLab CI.
They parse and validate the YAML configuration to ensure correctness.

Red phase: all tests MUST fail before implementation (for new jobs).
Green phase: implement .gitlab-ci.yml changes.
"""

import re
import shlex
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent
CI_FILE = REPO_ROOT / ".gitlab-ci.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
LOCK_FILE = REPO_ROOT / "uv.lock"
EMBEDDING_SUPERVISOR_DIR = REPO_ROOT / "services" / "embedding_supervisor"
EMBEDDING_SUPERVISOR_REQUIREMENTS = EMBEDDING_SUPERVISOR_DIR / "requirements.txt"
EMBEDDING_SUPERVISOR_LOCK = EMBEDDING_SUPERVISOR_DIR / "requirements.lock"
EMBEDDING_SUPERVISOR_DOCKERFILE = EMBEDDING_SUPERVISOR_DIR / "Dockerfile"
LOCKED_DEV_SYNC_COMMAND = ["uv", "sync", "--locked", "--no-dev", "--extra", "dev"]
DOCKER_SMOKE_PROGRAM = (
    'import os, pathlib, sys; import brain_v42.mcp.server; venv = pathlib.Path("/app/.venv"); '
    "entrypoint = pathlib.Path(brain_v42.mcp.server.__file__); "
    'assert sys.executable == "/app/.venv/bin/python"; assert os.geteuid() == 1000; '
    "assert venv.stat().st_uid == 0; assert entrypoint.stat().st_uid == 0; "
    "assert not os.access(venv, os.W_OK); assert not os.access(entrypoint, os.W_OK)"
)
CI_SMOKE_IMAGE = "brain-v42-ci-smoke:${CI_COMMIT_SHA}"


def _shell_commands(value: object) -> list[list[str]]:
    """Tokenize top-level CI commands without matching comments or echoed text."""
    if not isinstance(value, list):
        return []
    return [shlex.split(command) for command in value if isinstance(command, str)]


def _docker_instructions() -> list[str]:
    """Return normalized Dockerfile instructions with continuations joined."""
    instructions: list[str] = []
    pending = ""
    for raw_line in DOCKERFILE.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        instructions.append(pending)
        pending = ""
    assert not pending, "Dockerfile must not end with a dangling continuation"
    return instructions


def _embedding_supervisor_docker_instructions() -> list[str]:
    """Return normalized supervisor Dockerfile instructions."""
    instructions: list[str] = []
    pending = ""
    for raw_line in EMBEDDING_SUPERVISOR_DOCKERFILE.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        instructions.append(pending)
        pending = ""
    assert not pending, "embedding supervisor Dockerfile must not end with a dangling continuation"
    return instructions


def _has_locked_dev_sync(commands: list[list[str]]) -> bool:
    """Return whether commands contain a freshness-checked dev dependency sync."""
    sync_commands = [command for command in commands if command[:2] == ["uv", "sync"]]
    return sync_commands == [LOCKED_DEV_SYNC_COMMAND]


@pytest.fixture(scope="module")
def ci_config() -> dict:  # type: ignore[type-arg]
    """Load and return the parsed .gitlab-ci.yml."""
    assert CI_FILE.exists(), f".gitlab-ci.yml not found at {CI_FILE}"
    with CI_FILE.open() as f:
        config = yaml.safe_load(f)
    assert config is not None, ".gitlab-ci.yml is empty"
    return config  # type: ignore[return-value]


class TestGitlabCiFileValid:
    """Verify the .gitlab-ci.yml file exists and is valid YAML."""

    def test_gitlab_ci_file_exists(self) -> None:
        """gitlab-ci.yml must exist at the repository root."""
        assert CI_FILE.exists(), f".gitlab-ci.yml not found at {CI_FILE}"

    def test_gitlab_ci_is_valid_yaml(self) -> None:
        """gitlab-ci.yml must be parseable YAML without errors."""
        with CI_FILE.open() as f:
            config = yaml.safe_load(f)
        assert config is not None, ".gitlab-ci.yml is empty"
        assert isinstance(config, dict), ".gitlab-ci.yml must be a YAML mapping"

    def test_python_yaml_validation(self) -> None:
        """Simulate the validation command from acceptance criteria."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c", f"import yaml; yaml.safe_load(open('{CI_FILE}'))"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"YAML validation failed: {result.stderr}"


class TestGitlabCiStages:
    """Validate the stages configuration."""

    def test_stages_key_exists(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """stages key must be defined."""
        assert "stages" in ci_config, ".gitlab-ci.yml must define 'stages'"

    def test_lint_stage_present(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """lint stage must be in stages."""
        assert "lint" in ci_config["stages"], "stages must include 'lint'"

    def test_test_stage_present(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """test stage must be in stages."""
        assert "test" in ci_config["stages"], "stages must include 'test'"

    def test_build_stage_present(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """build stage must be in stages."""
        assert "build" in ci_config["stages"], "stages must include 'build'"

    def test_stages_order(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """Stages must be in order: lint, test, build."""
        stages = ci_config["stages"]
        lint_idx = stages.index("lint")
        test_idx = stages.index("test")
        build_idx = stages.index("build")
        assert lint_idx < test_idx < build_idx, (
            f"Stages must be in order lint < test < build, got: {stages}"
        )


class TestLintRuffJob:
    """Validate the lint:ruff job is not broken."""

    def test_lint_ruff_job_exists(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """lint:ruff job must still exist."""
        assert "lint:ruff" in ci_config, "lint:ruff job must exist"

    def test_lint_ruff_stage(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """lint:ruff must be in the lint stage."""
        job = ci_config["lint:ruff"]
        assert job.get("stage") == "lint", (
            f"lint:ruff must be in 'lint' stage, got: {job.get('stage')}"
        )

    def test_lint_ruff_has_rules(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """lint:ruff must have rules."""
        job = ci_config["lint:ruff"]
        assert "rules" in job, "lint:ruff must have 'rules'"

    def test_lint_ruff_pinned(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """lint:ruff must come from the freshness-checked lock at an exact version.

        A bare `pip install ruff` floats at pipeline time — the exact
        invisible-layer drift that turns a green commit red with no code change.
        """
        job = ci_config["lint:ruff"]
        before = job.get("before_script", [])
        with LOCK_FILE.open("rb") as f:
            lock = tomllib.load(f)
        ruff_versions = {
            package["version"] for package in lock["package"] if package["name"] == "ruff"
        }

        assert len(ruff_versions) == 1
        assert re.fullmatch(r"\d+\.\d+\.\d+", ruff_versions.pop())
        assert _has_locked_dev_sync(_shell_commands(before)), (
            f"lint:ruff must freshness-check and sync the locked graph, got: {before}"
        )

    def test_lint_ruff_covers_scripts(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """lint:ruff must lint the whole repository, including operational code.

        A root target covers scripts/ plus services/, bench/ and migrations that
        previously shipped outside the CI lint boundary.
        """
        job = ci_config["lint:ruff"]
        commands = _shell_commands(job.get("script", []))
        assert commands == [
            [".venv/bin/ruff", "check", ".", "--output-format=gitlab"],
            [".venv/bin/ruff", "format", "--check", "."],
        ]


class TestLintMypyJob:
    """Validate the new lint:mypy job."""

    def test_lint_mypy_job_exists(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """lint:mypy job must exist."""
        assert "lint:mypy" in ci_config, "lint:mypy job must exist"

    def test_lint_mypy_stage(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """lint:mypy must be in the lint stage."""
        job = ci_config["lint:mypy"]
        assert job.get("stage") == "lint", (
            f"lint:mypy must be in 'lint' stage, got: {job.get('stage')}"
        )

    def test_lint_mypy_is_blocking(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """lint:mypy must be blocking (no allow_failure).

        Flipped from allow_failure:true on 2026-05-29: a non-blocking gate let
        type errors silently drift 0→20 over two months (audit finding).
        """
        job = ci_config["lint:mypy"]
        assert job.get("allow_failure") in (None, False), (
            f"lint:mypy must be blocking (allow_failure unset/false), "
            f"got: {job.get('allow_failure')}"
        )

    def test_lint_mypy_runs_mypy(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """lint:mypy script must run mypy src/."""
        job = ci_config["lint:mypy"]
        script = job.get("script", [])
        script_str = " ".join(script) if isinstance(script, list) else str(script)
        assert "mypy" in script_str, f"lint:mypy script must run mypy, got: {script}"
        assert "src/" in script_str or "src" in script_str, (
            f"lint:mypy must target src/, got: {script}"
        )

    def test_lint_mypy_has_rules(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """lint:mypy must have rules."""
        job = ci_config["lint:mypy"]
        assert "rules" in job, "lint:mypy must have 'rules'"

    def test_lint_mypy_rules_include_mr(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """lint:mypy rules must trigger on merge_request_event."""
        job = ci_config["lint:mypy"]
        rules = job.get("rules", [])
        rules_str = str(rules)
        assert "merge_request_event" in rules_str, (
            "lint:mypy rules must include merge_request_event"
        )

    def test_lint_mypy_rules_include_main(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """lint:mypy rules must trigger on main branch."""
        job = ci_config["lint:mypy"]
        rules = job.get("rules", [])
        rules_str = str(rules)
        assert "main" in rules_str, "lint:mypy rules must include main branch"


class TestTestUnitJob:
    """Validate the test:unit job is not broken."""

    def test_test_unit_job_exists(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """test:unit job must still exist."""
        assert "test:unit" in ci_config, "test:unit job must exist"

    def test_test_unit_stage(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """test:unit must be in the test stage."""
        job = ci_config["test:unit"]
        assert job.get("stage") == "test", (
            f"test:unit must be in 'test' stage, got: {job.get('stage')}"
        )

    def test_test_unit_runs_layering_preflight_before_pytest(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """test:unit must reject a module cycle before running pytest."""
        job = ci_config["test:unit"]
        script = job.get("script", [])
        assert isinstance(script, list), f"test:unit script must be a list, got: {script}"
        assert script[:3] == [
            ".venv/bin/python scripts/check_module_layering.py --package src/brain_v42",
            ".venv/bin/python scripts/check_container_image_pins.py",
            ".venv/bin/pytest tests/unit/ -v --tb=short --junitxml=report.xml",
        ]

    def test_test_unit_runs_catalog_gate_with_canonical_ci_python(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """The offline image catalogue gate must run before the unit suite."""
        job = ci_config["test:unit"]
        script = job.get("script", [])
        assert isinstance(script, list), f"test:unit script must be a list, got: {script}"
        assert script[:3] == [
            ".venv/bin/python scripts/check_module_layering.py --package src/brain_v42",
            ".venv/bin/python scripts/check_container_image_pins.py",
            ".venv/bin/pytest tests/unit/ -v --tb=short --junitxml=report.xml",
        ]

    def test_test_unit_publishes_junit(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """test:unit must publish junit artifacts."""
        job = ci_config["test:unit"]
        artifacts = job.get("artifacts", {})
        reports = artifacts.get("reports", {})
        assert "junit" in reports, "test:unit must publish junit artifacts"

    def test_test_unit_opts_into_the_database_backed_tests(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """Without this variable, 51 unit tests SKIP — and a skip is green.

        That is how they went unrun for weeks: the gate was honest and incomplete at
        the same time. Removing the variable from .gitlab-ci.yml leaves the whole
        suite green, so this assertion is the only thing standing between the hole
        and its return.
        """
        variables = ci_config["test:unit"].get("variables", {})
        url = variables.get("BRAIN_V42_TEST_DB_URL")
        assert url, (
            "test:unit must set BRAIN_V42_TEST_DB_URL, otherwise the DB-backed unit "
            "tests skip and the job is green without having run them"
        )
        assert url.endswith("/brain_test"), (
            f"the unit gate must point at brain_test, never a production database: {url}"
        )

    def test_coverage_can_build_settings_without_a_database(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """POSTGRES_URL doit être POSÉ, et BRAIN_V42_TEST_DB_URL doit rester ABSENT.

        Deux exigences opposées, et il faut les deux. `Settings()` refuse de se
        construire sans POSTGRES_URL — c'est ce qui faisait tomber
        test_the_setting_ships_closed, et une simple variable suffit puisque le
        test lit un réglage sans se connecter.

        Mais poser BRAIN_V42_TEST_DB_URL réveillerait ici les 51 tests adossés à
        une base, qui ne passent pas dans le Postgres de la CI : il persiste entre
        pipelines et ignore donc POSTGRES_DB. Mesuré au pipeline 4392 — 2 échecs
        deviennent 31. La couverture reste donc mesurée sur la suite sans base,
        et c'est un écart TRACÉ, pas un écart ignoré.
        """
        coverage = ci_config["test:coverage"].get("variables", {})

        assert coverage.get("POSTGRES_URL"), (
            "test:coverage doit poser POSTGRES_URL, sinon Settings() lève et la "
            "couverture échoue sur un défaut d'environnement, pas sur le code"
        )
        assert "BRAIN_V42_TEST_DB_URL" not in coverage, (
            "test:coverage ne doit PAS réveiller les tests adossés à une base tant "
            "que le Postgres de la CI ne les supporte pas — 2 échecs deviendraient 31"
        )

    def test_test_unit_applies_the_schema_before_pytest(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """tests/unit has no run_migrations fixture, unlike tests/integration.

        Against a bare pgvector database the DB-backed tests fail on missing
        relations instead of skipping, so the upgrade is part of the contract.
        """
        before_script = ci_config["test:unit"].get("before_script", [])
        assert any("alembic upgrade head" in str(step) for step in before_script), (
            "test:unit must apply the schema before pytest; without it the DB-backed "
            f"tests fail on missing relations. before_script={before_script}"
        )


class TestBuildDockerJob:
    """Validate the build:docker job is not broken."""

    def test_build_docker_job_exists(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """build:docker job must still exist."""
        assert "build:docker" in ci_config, "build:docker job must exist"

    def test_build_docker_stage(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """build:docker must be in the build stage."""
        job = ci_config["build:docker"]
        assert job.get("stage") == "build", (
            f"build:docker must be in 'build' stage, got: {job.get('stage')}"
        )

    def test_build_docker_has_tags(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """build:docker must have docker tag for runner selection."""
        job = ci_config["build:docker"]
        tags = job.get("tags", [])
        assert "docker" in tags, f"build:docker must have 'docker' tag, got: {tags}"


class TestSecurityStage:
    """Validate the security scanning stage (pip-audit, bandit, gitleaks).

    The audit of 2026-07-23 found no dependency, SAST or secret scanning in the
    pipeline. The manual checks were clean, but nothing stopped a regression.

    These jobs start non-blocking for a burn-in, which is exactly the shape that
    rotted mypy from 0 to 20 errors (brain learning b5256bc1). So the burn-in
    carries a hard deadline: once SECURITY_BURN_IN_UNTIL passes, this test suite
    fails until every security job is flipped to blocking. The escalation cannot
    be forgotten, because test:unit is itself blocking.
    """

    SECURITY_JOBS = (
        "security:pip-audit",
        "security:pip-audit:embedding-supervisor",
        "security:bandit",
        "security:gitleaks",
    )

    def test_security_stage_runs_between_test_and_build(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        stages = ci_config["stages"]
        assert "security" in stages, f"stages must include 'security', got: {stages}"
        assert stages.index("test") < stages.index("security") < stages.index("build"), (
            f"security must run after test and before build, got: {stages}"
        )

    @pytest.mark.parametrize("job_name", SECURITY_JOBS)
    def test_security_job_exists_in_its_stage(self, ci_config: dict, job_name: str) -> None:  # type: ignore[type-arg]
        assert job_name in ci_config, f"{job_name} job must exist"
        assert ci_config[job_name].get("stage") == "security", (
            f"{job_name} must be in the 'security' stage, got: {ci_config[job_name].get('stage')}"
        )

    @pytest.mark.parametrize("job_name", SECURITY_JOBS)
    def test_security_job_starts_without_waiting_for_earlier_stages(
        self,
        ci_config: dict,  # type: ignore[type-arg]
        job_name: str,
    ) -> None:
        """`needs: []` keeps feedback fast despite the late stage position."""
        assert ci_config[job_name].get("needs") == [], (
            f"{job_name} must declare 'needs: []' so it starts immediately, "
            f"got: {ci_config[job_name].get('needs')}"
        )

    @pytest.mark.parametrize("job_name", SECURITY_JOBS)
    def test_security_job_runs_on_merge_requests_and_both_branches(
        self,
        ci_config: dict,  # type: ignore[type-arg]
        job_name: str,
    ) -> None:
        rules = str(ci_config[job_name].get("rules", []))
        assert "merge_request_event" in rules, f"{job_name} must run on merge requests"
        assert "main" in rules, f"{job_name} must run on main"
        assert "dev" in rules, f"{job_name} must run on dev"

    def test_burn_in_deadline_is_an_explicit_iso_date(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        deadline = ci_config.get("variables", {}).get("SECURITY_BURN_IN_UNTIL")
        assert isinstance(deadline, str), (
            "variables must declare SECURITY_BURN_IN_UNTIL — a non-blocking gate "
            "without a dated escalation is the silent-failure pattern"
        )
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline), (
            f"SECURITY_BURN_IN_UNTIL must be an ISO date, got: {deadline!r}"
        )

    def test_non_blocking_security_jobs_expire_at_the_burn_in_deadline(
        self,
        ci_config: dict,  # type: ignore[type-arg]
    ) -> None:
        """The tripwire: allow_failure is legal only until the deadline.

        Failing here means the burn-in is over. Flip the offending jobs to
        blocking (drop `allow_failure: true`), or make a dated decision to
        extend SECURITY_BURN_IN_UNTIL — but do not leave it drifting.
        """
        deadline = date.fromisoformat(ci_config["variables"]["SECURITY_BURN_IN_UNTIL"])
        non_blocking = [
            name for name in self.SECURITY_JOBS if ci_config[name].get("allow_failure") is True
        ]
        today = datetime.now(tz=UTC).date()

        assert not (today > deadline and non_blocking), (
            f"security burn-in ended on {deadline} ({(today - deadline).days} days ago): "
            f"{non_blocking} must now be blocking, or the deadline must be moved "
            f"with an explicit decision"
        )

    def test_pip_audit_scans_the_locked_dependency_graph(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """Auditing the lock export, not the ambient venv, keeps the scan exact."""
        commands = _shell_commands(ci_config["security:pip-audit"].get("script", []))
        export = next(
            (command for command in commands if command[:2] == ["uv", "export"]),
            None,
        )
        audit = next(
            (command for command in commands if command[0].endswith("pip-audit")),
            None,
        )

        assert export is not None, f"security:pip-audit must export the lock, got: {commands}"
        assert "--locked" in export, f"the export must be freshness-checked, got: {export}"
        assert audit is not None, f"security:pip-audit must run pip-audit, got: {commands}"
        assert "-r" in audit, f"pip-audit must audit the exported requirements, got: {audit}"
        assert commands.index(export) < commands.index(audit)

    def test_pip_audit_consumes_hashes_without_re_resolving(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """The exported lock is authoritative; pip-audit must not invoke pip."""
        commands = _shell_commands(ci_config["security:pip-audit"].get("script", []))
        audit = next(
            (command for command in commands if command and command[0].endswith("pip-audit")),
            None,
        )

        assert audit is not None, f"security:pip-audit must run pip-audit, got: {commands}"
        assert "--require-hashes" in audit, (
            f"pip-audit must verify every exported lock hash, got: {audit}"
        )
        assert "--disable-pip" in audit, (
            f"pip-audit must not bootstrap or re-resolve through pip, got: {audit}"
        )

    def test_pip_audit_uses_a_bounded_online_advisory_policy(
        self,
        ci_config: dict,  # type: ignore[type-arg]
    ) -> None:
        """Query fresh PyPI advisories and fail closed quickly if unavailable."""
        commands = _shell_commands(ci_config["security:pip-audit"].get("script", []))
        audit = next(
            (command for command in commands if command and command[0].endswith("pip-audit")),
            None,
        )

        assert audit is not None, f"security:pip-audit must run pip-audit, got: {commands}"
        assert "--vulnerability-service" in audit, (
            f"pip-audit must name its online advisory source, got: {audit}"
        )
        service = audit[audit.index("--vulnerability-service") + 1]
        assert service == "pypi", f"pip-audit must query fresh PyPI advisories, got: {service}"
        assert "--timeout" in audit, f"pip-audit must bound advisory network waits, got: {audit}"
        timeout = int(audit[audit.index("--timeout") + 1])
        assert 1 <= timeout <= 60, f"pip-audit timeout must be within one minute, got: {timeout}"
        assert "--strict" in audit, f"online advisory failures must fail closed, got: {audit}"

    def test_embedding_supervisor_lock_is_hash_complete(self) -> None:
        """The standalone supervisor graph must be frozen with artifact hashes."""
        assert EMBEDDING_SUPERVISOR_REQUIREMENTS.exists(), (
            "supervisor source requirements must remain declared"
        )
        assert EMBEDDING_SUPERVISOR_LOCK.exists(), "supervisor must commit its generated lock"

        lines = EMBEDDING_SUPERVISOR_LOCK.read_text().splitlines()
        package_starts = [
            index for index, line in enumerate(lines) if re.match(r"^[a-z0-9][a-z0-9._-]*==", line)
        ]
        assert package_starts, "supervisor lock must contain resolved distributions"

        hash_complete = []
        for start, end in zip(package_starts, [*package_starts[1:], len(lines)], strict=True):
            block = "\n".join(lines[start:end])
            hash_complete.append("--hash=sha256:" in block)
        assert all(hash_complete), "every supervisor distribution must be hash pinned"

    def test_embedding_supervisor_aiohttp_pin_meets_the_remediated_audit_floor(self) -> None:
        """The direct supervisor input must not regress to the freshly audited vulnerable pin."""
        direct_requirements = dict(
            line.split("==", maxsplit=1)
            for line in EMBEDDING_SUPERVISOR_REQUIREMENTS.read_text().splitlines()
            if line and not line.startswith("#")
        )
        lock = EMBEDDING_SUPERVISOR_LOCK.read_text()

        aiohttp_version = tuple(int(part) for part in direct_requirements["aiohttp"].split("."))
        assert aiohttp_version >= (3, 14, 1)
        assert f"aiohttp=={direct_requirements['aiohttp']}" in lock

    def test_embedding_supervisor_image_installs_only_the_hash_locked_graph(self) -> None:
        """Docker must not resolve the supervisor requirements during image build."""
        instructions = _embedding_supervisor_docker_instructions()
        copies = [instruction for instruction in instructions if instruction.startswith("COPY ")]
        installs = [
            instruction for instruction in instructions if instruction.startswith("RUN pip install")
        ]

        assert any("requirements.lock" in instruction for instruction in copies), (
            f"Dockerfile must copy the supervisor lock, got: {copies}"
        )
        assert installs == [
            "RUN pip install --no-cache-dir --require-hashes -r requirements.lock"
        ], f"Dockerfile must install only the hash-locked graph, got: {installs}"

    def test_embedding_supervisor_gate_regenerates_and_compares_its_lock(
        self, ci_config: dict
    ) -> None:  # type: ignore[type-arg]
        """CI must fail when the lock no longer matches the declared direct requirements."""
        commands = _shell_commands(
            ci_config["security:pip-audit:embedding-supervisor"].get("script", [])
        )
        compile_command = next(
            (command for command in commands if command[:3] == ["uv", "pip", "compile"]),
            None,
        )
        compare_command = next((command for command in commands if command[:1] == ["cmp"]), None)

        assert compile_command is not None, (
            f"supervisor gate must compile its declared graph, got: {commands}"
        )
        assert "services/embedding_supervisor/requirements.txt" in compile_command
        assert "--python-version" in compile_command and "3.12" in compile_command
        assert "--python-platform" in compile_command and "x86_64-manylinux_2_28" in compile_command
        assert "--resolution" in compile_command and "highest" in compile_command
        assert "--generate-hashes" in compile_command
        assert "--default-index" in compile_command and "https://pypi.org/simple" in compile_command
        assert compare_command == [
            "cmp",
            "--silent",
            "services/embedding_supervisor/requirements.lock",
            "requirements-embedding-supervisor.generated.lock",
        ], f"supervisor gate must compare the generated lock exactly, got: {compare_command}"

    def test_embedding_supervisor_gate_audits_the_same_hash_locked_graph(
        self, ci_config: dict
    ) -> None:  # type: ignore[type-arg]
        """The dedicated online audit must target the lock consumed by Docker."""
        commands = _shell_commands(
            ci_config["security:pip-audit:embedding-supervisor"].get("script", [])
        )
        audit = next(
            (command for command in commands if command and command[0].endswith("pip-audit")),
            None,
        )

        assert audit is not None, f"supervisor gate must run pip-audit, got: {commands}"
        assert audit[audit.index("-r") + 1] == "services/embedding_supervisor/requirements.lock"
        assert {"--require-hashes", "--disable-pip", "--strict", "--progress-spinner=off"} <= set(
            audit
        )
        assert audit[audit.index("--vulnerability-service") + 1] == "pypi"
        assert audit[audit.index("--timeout") + 1] == "15"
        assert "--ignore-vuln" not in audit

    def test_embedding_supervisor_gate_is_blocking(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """The new dedicated audit cannot inherit the root gate's temporary burn-in waiver."""
        job = ci_config["security:pip-audit:embedding-supervisor"]
        assert job.get("allow_failure") is not True, (
            "embedding-supervisor audit must be blocking and must not have an allow_failure waiver"
        )

    def test_bandit_scans_the_source_tree(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        commands = _shell_commands(ci_config["security:bandit"].get("script", []))
        bandit = next((command for command in commands if command[0].endswith("bandit")), None)

        assert bandit is not None, f"security:bandit must run bandit, got: {commands}"
        assert "-ll" in bandit, f"bandit must report medium severity and up, got: {bandit}"
        assert "src/" in bandit or "src" in bandit, f"bandit must target src/, got: {bandit}"

    def test_gitleaks_scans_the_repository_without_leaking_findings(
        self,
        ci_config: dict,  # type: ignore[type-arg]
    ) -> None:
        """`--redact` keeps a matched secret out of the job log.

        `dir` scans the working tree rather than history: gitleaks 8.30 dropped
        the old `detect` subcommand, and a filesystem scan stays correct under
        the shallow clone CI checks out by default.
        """
        commands = _shell_commands(ci_config["security:gitleaks"].get("script", []))
        gitleaks = next((command for command in commands if command[0] == "gitleaks"), None)

        assert gitleaks is not None, f"security:gitleaks must run gitleaks, got: {commands}"
        assert "dir" in gitleaks, f"gitleaks must scan the working tree with 'dir', got: {gitleaks}"
        assert "--redact" in gitleaks, (
            f"gitleaks must redact findings so the job log never carries the "
            f"secret it just found, got: {gitleaks}"
        )

    def test_gitleaks_image_clears_the_tool_entrypoint(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """GitLab Runner needs a shell entrypoint to execute the job script."""
        image = ci_config["security:gitleaks"].get("image")

        assert isinstance(image, dict), (
            f"security:gitleaks image must be a mapping that clears ENTRYPOINT, got: {image}"
        )
        assert "gitleaks" in str(image.get("name", "")), (
            f"security:gitleaks must retain the pinned Gitleaks image, got: {image}"
        )
        assert image.get("entrypoint") == [""], (
            f"security:gitleaks must set image.entrypoint to [''], got: {image}"
        )

    def test_gitleaks_scans_the_tracked_dependency_lock(self) -> None:
        """A tracked lockfile is supply-chain input, never generated scan noise."""
        with (REPO_ROOT / ".gitleaks.toml").open("rb") as f:
            config = tomllib.load(f)
        allowlisted_paths = config.get("allowlist", {}).get("paths", [])
        matching_paths = [pattern for pattern in allowlisted_paths if re.search(pattern, "uv.lock")]

        assert not matching_paths, (
            f"uv.lock must remain inside the secret scan boundary, got: {matching_paths}"
        )

    @pytest.mark.parametrize("tool", ["pip-audit", "bandit"])
    def test_python_security_tools_come_from_the_lockfile(self, tool: str) -> None:
        """A floating `pip install bandit` drifts the gate at pipeline time.

        The python-image jobs are already constrained to the pinned uv bootstrap
        by TestLockedDependencyInstallation, so the tools must be in uv.lock.
        """
        with LOCK_FILE.open("rb") as f:
            lock = tomllib.load(f)
        versions = {package["version"] for package in lock["package"] if package["name"] == tool}

        assert len(versions) == 1, f"{tool} must be locked exactly once, got: {versions}"
        assert re.fullmatch(r"\d+\.\d+(\.\d+)?", versions.pop())


class TestCacheConfiguration:
    """Validate the cache configuration."""

    def test_cache_key_exists(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """cache must be defined."""
        assert "cache" in ci_config, ".gitlab-ci.yml must define 'cache'"

    def test_cache_paths_include_uv(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        """cache paths must include uv cache directory."""
        cache = ci_config["cache"]
        # cache can be a list or dict
        if isinstance(cache, list):
            paths = []
            for c in cache:
                paths.extend(c.get("paths", []))
        else:
            paths = cache.get("paths", [])
        assert any("uv" in str(p) for p in paths), (
            f"cache paths must include uv cache dir, got: {paths}"
        )


class TestLockedDependencyInstallation:
    """Keep CI and the production image on the committed dependency graph."""

    _REQUIRED_PYTHON_JOBS = {
        "lint:ruff",
        "lint:mypy",
        "test:unit",
        "test:coverage",
        "test:integration",
    }

    def test_uv_version_is_pinned(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        uv_version = ci_config.get("variables", {}).get("UV_VERSION")

        assert isinstance(uv_version, str)
        assert re.fullmatch(r"\d+\.\d+\.\d+", uv_version), (
            f"UV_VERSION must be an exact semantic version, got: {uv_version!r}"
        )

    def test_python_jobs_sync_the_locked_graph(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        python_jobs = {
            name: value
            for name, value in ci_config.items()
            if isinstance(value, dict) and str(value.get("image", "")).startswith("python:")
        }
        assert self._REQUIRED_PYTHON_JOBS <= python_jobs.keys()

        for job_name, job in python_jobs.items():
            commands = _shell_commands(job.get("before_script", []))
            pip_installs = [command for command in commands if command[:2] == ["pip", "install"]]
            assert pip_installs == [["pip", "install", "uv==$UV_VERSION"]], (
                f"{job_name} must only install the pinned uv bootstrap, got: {commands}"
            )
            assert _has_locked_dev_sync(commands), (
                f"{job_name} must freshness-check and install uv.lock, got: {commands}"
            )

    def test_cache_key_includes_lockfile(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        key_files = ci_config["cache"].get("key", {}).get("files", [])

        assert "uv.lock" in key_files, f"cache key must include uv.lock, got: {key_files}"

    def test_dockerfile_installs_from_the_locked_graph(self, ci_config: dict) -> None:  # type: ignore[type-arg]
        instructions = _docker_instructions()
        docker_arg = next(
            (
                instruction
                for instruction in instructions
                if instruction.startswith("ARG UV_VERSION=")
            ),
            "",
        )
        match = re.fullmatch(r"ARG UV_VERSION=(\d+\.\d+\.\d+)", docker_arg)
        assert match is not None
        assert match.group(1) == ci_config["variables"]["UV_VERSION"]

        tokenized = [shlex.split(instruction) for instruction in instructions]
        assert ["RUN", "pip", "install", "--no-cache-dir", "uv==${UV_VERSION}"] in tokenized
        assert ["COPY", "pyproject.toml", "uv.lock", "README.md", "./"] in tokenized

        prod_sync = ["RUN", "uv", "sync", "--locked", "--no-dev", "--no-cache"]
        dev_sync = [
            "RUN",
            "uv",
            "sync",
            "--locked",
            "--no-dev",
            "--extra",
            "dev",
            "--no-cache",
        ]
        assert tokenized.count(prod_sync) == 1
        assert tokenized.count(dev_sync) == 1
        assert [command for command in tokenized if command[:3] == ["RUN", "uv", "sync"]] == [
            prod_sync,
            dev_sync,
        ]
        assert [command for command in tokenized if command[:3] == ["RUN", "pip", "install"]] == [
            ["RUN", "pip", "install", "--no-cache-dir", "uv==${UV_VERSION}"]
        ]
        assert tokenized.index(["FROM", "base", "AS", "deps"]) < tokenized.index(prod_sync)
        assert tokenized.index(prod_sync) < tokenized.index(["FROM", "deps", "AS", "deps-dev"])
        assert tokenized.index(["FROM", "deps", "AS", "deps-dev"]) < tokenized.index(dev_sync)
        assert tokenized.index(dev_sync) < tokenized.index(["FROM", "deps-dev", "AS", "test"])
        assert ["FROM", "deps", "AS", "production"] in tokenized
        assert all(command[:3] != ["RUN", "uv", "pip"] for command in tokenized)

    def test_docker_build_uses_ci_uv_version_and_smoke_tests_runtime(
        self,
        ci_config: dict,  # type: ignore[type-arg]
    ) -> None:
        commands = _shell_commands(ci_config["build:docker"].get("script", []))
        build_command = next(command for command in commands if command[:2] == ["docker", "build"])
        run_commands = [command for command in commands if command[:2] == ["docker", "run"]]

        assert "--build-arg" in build_command
        build_arg_index = build_command.index("--build-arg")
        assert build_command[build_arg_index + 1] == "UV_VERSION=$UV_VERSION"
        assert [
            build_command[index + 1]
            for index, token in enumerate(build_command[:-1])
            if token in {"-t", "--tag"}
        ] == [
            "$CI_REGISTRY_IMAGE:${ENV_PREFIX}-latest",
            "$CI_REGISTRY_IMAGE:${ENV_PREFIX}-${SHORT_SHA}",
            CI_SMOKE_IMAGE,
        ]
        assert run_commands == [
            [
                "docker",
                "run",
                "--pull=never",
                "--rm",
                CI_SMOKE_IMAGE,
                "python",
                "-c",
                DOCKER_SMOKE_PROGRAM,
            ]
        ]
        assert commands.index(build_command) < commands.index(run_commands[0])
        compile(DOCKER_SMOKE_PROGRAM, "<docker-smoke>", "exec")

    def test_production_runtime_uses_root_owned_venv(self) -> None:
        instructions = _docker_instructions()
        tokenized = [shlex.split(instruction) for instruction in instructions]

        assert 'ENV PATH="/app/.venv/bin:$PATH"' in instructions
        assert "USER appuser" in instructions
        assert any(
            ["chmod", "-R", "u=rwX,go=rX", "/app"] == instruction[1:5]
            for instruction in tokenized
            if instruction[:1] == ["RUN"]
        )
        assert not any(
            "--chown" in token
            for instruction in tokenized
            if instruction[:1] == ["COPY"]
            for token in instruction
        )
        assert not any(
            "chown" in instruction and "/app" in instruction for instruction in tokenized
        )
