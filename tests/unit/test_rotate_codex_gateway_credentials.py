"""Contract tests for the coordinated PostgreSQL and gateway bearer rotation."""

from __future__ import annotations

import json
import stat
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import scripts.rotate_codex_gateway_credentials as rotation
from scripts.rotate_codex_gateway_credentials import (
    RotationConfig,
    RotationError,
    _atomic_write,
    _render_environment,
    parse_args,
    run_rotation,
)
from sqlalchemy.engine import make_url

from tests.unit._fixture_modes import make_directory, write_file

DEPLOYED_REVISION = "044"
OLD_BRAIN = "old-brain-canary"
OLD_CODEX = "old-codex-canary"
NEW_BRAIN = "1" * 64
NEW_CODEX = "2" * 64
NEW_BEARER = "3" * 64


class FakeDatabase:
    """In-memory authentication boundary with transactional two-role rotation."""

    def __init__(self, *, revision: str = DEPLOYED_REVISION, missing: tuple[str, ...] = ()) -> None:
        self.passwords = {"brain": OLD_BRAIN, "codex_ro": OLD_CODEX}
        self.rotations: list[tuple[str, str, str, str]] = []
        # Paramétrable, et c'est le fond du ticket 8285215c : tant qu'elle rendait
        # "037" en dur face à une constante "037" dans le code, la suite n'avait que
        # des sondes POSITIVES. Neutraliser la garde laissait 29 tests verts.
        self._revision = revision
        # Manquants déclarés par le test : "vue" ou "vue.colonne". Paramétrable
        # pour la même raison que la révision — une valeur figée à () ne
        # produirait que des sondes positives.
        self.missing = missing

    def probe(self, role: str, password: str) -> bool:
        return self.passwords[role] == password

    def rotate(
        self,
        current_brain: str,
        next_brain: str,
        current_codex: str,
        next_codex: str,
    ) -> None:
        if not self.probe("brain", current_brain) or not self.probe("codex_ro", current_codex):
            raise RuntimeError("database-secret-canary")
        self.rotations.append((current_brain, next_brain, current_codex, next_codex))
        self.passwords = {"brain": next_brain, "codex_ro": next_codex}

    def revision(self, brain_password: str) -> str:
        if not self.probe("brain", brain_password):
            raise RuntimeError("revision-secret-canary")
        return self._revision

    def codex_scope_is_bounded(self, codex_password: str) -> bool:
        return self.probe("codex_ro", codex_password)

    def missing_gateway_contract(self, codex_password: str) -> tuple[str, ...]:
        # Canary calqué sur revision() : toute confusion old_codex/new_codex
        # dans _prove_new_generation doit exploser, pas rendre un faux vert.
        if not self.probe("codex_ro", codex_password):
            raise RuntimeError("contract-secret-canary")
        return self.missing


class FakePrivilegedInstaller:
    """Install the fixed Shrik environment without needing sudo in unit tests."""

    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.preflight_calls: list[Path] = []
        self.install_calls: list[Path] = []

    def preflight(self, target: Path) -> None:
        self.preflight_calls.append(target)

    def install(self, target: Path, payload: bytes, *, mode: int, uid: int, gid: int) -> None:
        self.install_calls.append(target)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError(f"install failed {NEW_BRAIN}")
        target.write_bytes(payload)
        target.chmod(mode)


class FakeGatewayProbe:
    """Prove only status semantics while keeping tokens out of command arguments."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def prove(self, old_token: str, new_token: str) -> bool:
        self.calls.append((old_token, new_token))
        return old_token == "" and new_token == NEW_BEARER


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RotationConfig:
    brain_root = make_directory(tmp_path / "brain")
    red_root = make_directory(tmp_path / "red")
    red_data = make_directory(red_root / "projects" / "red-data", parents=True)
    red_codex = make_directory(red_root / "projects" / "red-codex")
    private_parent = make_directory(tmp_path / "private" / "brain-v42", mode=0o700, parents=True)
    private_dir = private_parent / "codex-gateway-rotation"
    shrik_dir = make_directory(tmp_path / "etc" / "shrik", parents=True)

    write_file(
        brain_root / ".env",
        "GRAPH_ENABLED=true\n"
        f"POSTGRES_URL=postgresql+asyncpg://brain:{OLD_BRAIN}@localhost:5433/brain\n",
        mode=0o600,
    )
    write_file(
        red_data / ".env",
        f"BRAIN_DB_PASSWORD={OLD_BRAIN}\nRED_DB_PASSWORD=unrelated\n",
        mode=0o664,
    )
    write_file(
        red_codex / ".env.local",
        f"CODEX_BRAIN_DSN=postgresql+asyncpg://codex_ro:{OLD_CODEX}"
        "@brain_v42_postgres:5432/brain\n"
        "CODEX_BRAIN_GATEWAY_URL=\n"
        "CODEX_BRAIN_GATEWAY_TOKEN=\n"
        "CODEX_JWT_SECRET=unrelated\n",
        mode=0o600,
    )
    shrik_env = write_file(
        shrik_dir / "env",
        f"SHRIK_BRAIN_DSN=postgresql://brain:{OLD_BRAIN}@localhost:5433/brain\n"
        "BOT_SHARED_SECRET=unrelated\n",
        mode=0o640,
    )

    monkeypatch.setattr(rotation, "_operator_private_dir", lambda: private_dir)
    return RotationConfig(
        brain_root=brain_root,
        red_root=red_root,
        private_dir=private_dir,
        shrik_env=shrik_env,
        apply=False,
        resume=False,
        rollback=False,
        consumers_stopped_confirmed=False,
        rollback_preflight_confirmed=False,
        consumers_recreated_confirmed=False,
        expected_alembic_revision=DEPLOYED_REVISION,
    )


@pytest.fixture(autouse=True)
def _use_fixture_shrik_target(
    config: RotationConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rotation, "_SHRIK_ENV", config.shrik_env)


def _values(path: Path) -> dict[str, str]:
    return {
        key: value
        for raw_line in path.read_text().splitlines()
        if (key := raw_line.partition("=")[0]) and (value := raw_line.partition("=")[2])
    }


def test_dry_run_is_read_only_and_reports_secret_free_preflight(
    config: RotationConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = FakeDatabase()
    installer = FakePrivilegedInstaller()

    def fail_generation(_size: int) -> str:
        raise AssertionError("dry-run generated a secret")

    monkeypatch.setattr(rotation.secrets, "token_hex", fail_generation)

    result = run_rotation(
        config,
        database=database,
        privileged_installer=installer,
        gateway_probe=FakeGatewayProbe(),
    )

    assert result == {
        "alembic_revision": DEPLOYED_REVISION,
        "apply": False,
        "consumer_files_valid": 5,
        "codex_scope_bounded": True,
        "gateway_port": 9211,
        "mode_hardening_required": True,
        "old_credentials_valid": True,
        "status": "preflight_ok",
    }
    assert installer.preflight_calls == [config.shrik_env]
    assert not config.private_dir.exists()


def test_preflight_accepts_operator_owned_private_group_roots(
    config: RotationConfig,
) -> None:
    config.brain_root.chmod(0o775)
    config.red_root.chmod(0o775)

    result = run_rotation(
        config,
        database=FakeDatabase(),
        privileged_installer=FakePrivilegedInstaller(),
        gateway_probe=FakeGatewayProbe(),
    )

    assert result["status"] == "preflight_ok"


def test_preflight_rejects_world_writable_project_root(config: RotationConfig) -> None:
    config.brain_root.chmod(0o777)

    with pytest.raises(RotationError, match="canonical credential paths are invalid"):
        run_rotation(
            config,
            database=FakeDatabase(),
            privileged_installer=FakePrivilegedInstaller(),
            gateway_probe=FakeGatewayProbe(),
        )


def test_preflight_rejects_a_noncanonical_shrik_target(config: RotationConfig) -> None:
    alternate = write_file(
        config.shrik_env.parent / "alternate-env",
        config.shrik_env.read_text(),
        mode=0o640,
    )

    with pytest.raises(RotationError, match="canonical credential paths are invalid"):
        run_rotation(
            replace(config, shrik_env=alternate),
            database=FakeDatabase(),
            privileged_installer=FakePrivilegedInstaller(),
            gateway_probe=FakeGatewayProbe(),
        )


def test_sudo_shrik_installer_uses_only_the_fixed_root_helper_contract(
    config: RotationConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    staged_payloads: list[bytes] = []
    stage = config.private_dir / ".shrik-env.install"
    config.private_dir.mkdir(mode=0o700)

    def run_command(args: list[str], **_kwargs: Any) -> Any:
        commands.append(args)
        if args[-1] == "--publish":
            staged_payloads.append(stage.read_bytes())
        return rotation.subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(rotation, "_SHRIK_ENV", config.shrik_env, raising=False)
    monkeypatch.setattr(rotation.subprocess, "run", run_command)
    installer = rotation.SudoShrikInstaller(config.private_dir)

    installer.preflight(config.shrik_env)
    installer.install(
        config.shrik_env,
        b"SHRIK_BRAIN_DSN=postgresql://brain:private-canary@localhost/brain\n",
        mode=0o640,
        uid=0,
        gid=config.shrik_env.stat().st_gid,
    )

    helper = "/usr/local/sbin/brain-shrik-env-control"
    assert commands == [
        ["sudo", "-n", helper, "--check"],
        ["sudo", "-n", helper, "--publish"],
    ]
    assert staged_payloads == [
        b"SHRIK_BRAIN_DSN=postgresql://brain:private-canary@localhost/brain\n"
    ]
    assert not stage.exists()


def test_preflight_accepts_only_a_dormant_legacy_gateway_endpoint(
    config: RotationConfig,
) -> None:
    codex_env = config.red_root / "projects/red-codex/.env.local"
    original = codex_env.read_text()
    codex_env.write_text(
        original.replace(
            "CODEX_BRAIN_GATEWAY_URL=\n",
            "CODEX_BRAIN_GATEWAY_URL=http://host.docker.internal:9211\n",
        )
    )

    result = run_rotation(
        config,
        database=FakeDatabase(),
        privileged_installer=FakePrivilegedInstaller(),
        gateway_probe=FakeGatewayProbe(),
    )
    assert result["status"] == "preflight_ok"

    codex_env.write_text(
        codex_env.read_text().replace(
            "CODEX_BRAIN_GATEWAY_TOKEN=\n",
            "CODEX_BRAIN_GATEWAY_TOKEN=legacy-active-canary\n",
        )
    )
    with pytest.raises(RotationError, match="gateway consumer URL is not canonical"):
        run_rotation(
            config,
            database=FakeDatabase(),
            privileged_installer=FakePrivilegedInstaller(),
            gateway_probe=FakeGatewayProbe(),
        )


def test_environment_rendering_replaces_once_adds_missing_and_rejects_duplicates() -> None:
    rendered = _render_environment(
        "A=kept\nPOSTGRES_URL=old\n",
        {"POSTGRES_URL": "new", "POSTGRES_PASSWORD": "generated"},
        add_missing={"POSTGRES_PASSWORD"},
    )

    assert rendered == "A=kept\nPOSTGRES_URL=new\nPOSTGRES_PASSWORD=generated\n"

    with pytest.raises(RotationError, match="environment contains duplicate managed keys"):
        _render_environment(
            "POSTGRES_URL=first\nPOSTGRES_URL=second\n",
            {"POSTGRES_URL": "new"},
            add_missing=set(),
        )


def test_dsn_preserves_supported_sslmode_and_rejects_other_query_parameters() -> None:
    rendered = rotation._render_dsn(
        f"postgresql://brain:{OLD_BRAIN}@localhost:5433/brain?sslmode=disable",
        expected_role="brain",
        password=NEW_BRAIN,
    )
    parsed = rotation.make_url(rendered)

    assert parsed.password == NEW_BRAIN
    assert parsed.query == {"sslmode": "disable"}

    with pytest.raises(RotationError, match="consumer PostgreSQL DSN is invalid"):
        rotation._render_dsn(
            f"postgresql://brain:{OLD_BRAIN}@localhost:5433/brain?application_name=untrusted",
            expected_role="brain",
            password=NEW_BRAIN,
        )

    with pytest.raises(RotationError, match="consumer PostgreSQL DSN is invalid"):
        rotation._render_dsn(
            f"postgresql://brain:{OLD_BRAIN}@localhost:5433/brain?sslmode=",
            expected_role="brain",
            password=NEW_BRAIN,
        )


def test_atomic_write_replaces_complete_payload_with_exact_mode(tmp_path: Path) -> None:
    target = write_file(tmp_path / "credential.env", "old\n", mode=0o644)

    _atomic_write(target, b"new\n", 0o600)

    assert target.read_bytes() == b"new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".credential.env.*"))


def test_apply_then_resume_finalizes_with_new_and_old_refusal_proofs(
    config: RotationConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = FakeDatabase()
    installer = FakePrivilegedInstaller()
    gateway = FakeGatewayProbe()
    generated = iter((NEW_BRAIN, NEW_CODEX, NEW_BEARER))
    monkeypatch.setattr(rotation.secrets, "token_hex", lambda _size: next(generated))
    apply_config = replace(
        config,
        apply=True,
        consumers_stopped_confirmed=True,
        rollback_preflight_confirmed=True,
    )

    first = run_rotation(
        apply_config,
        database=database,
        privileged_installer=installer,
        gateway_probe=gateway,
    )

    assert first == {
        "alembic_revision": DEPLOYED_REVISION,
        "bearer_installed": True,
        "codex_scope_bounded": True,
        "consumer_files_installed": 5,
        "database_credentials_rotated": True,
        "new_credentials_valid": True,
        "old_credentials_refused": True,
        "status": "awaiting_consumer_recreation",
    }
    journal = config.private_dir / ".codex-gateway-rotation-state"
    assert journal.exists()
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert stat.S_IMODE((config.red_root / "projects/red-data/.env").stat().st_mode) == 0o600
    assert _values(config.brain_root / ".env")["POSTGRES_PASSWORD"] == NEW_BRAIN
    assert _values(config.red_root / "projects/red-data/.env")["BRAIN_DB_PASSWORD"] == NEW_BRAIN
    assert (
        _values(config.red_root / "projects/red-codex/.env.local")["CODEX_BRAIN_GATEWAY_URL"]
        == "http://brain-codex-gateway:9211"
    )
    assert _values(config.private_dir.parent / "codex-gateway.env") == {
        "BRAIN_CODEX_GATEWAY_TOKEN": NEW_BEARER
    }
    assert gateway.calls == []

    finalized = run_rotation(
        replace(
            apply_config,
            resume=True,
            consumers_recreated_confirmed=True,
        ),
        database=database,
        privileged_installer=installer,
        gateway_probe=gateway,
    )

    assert finalized["status"] == "rotated"
    assert finalized["new_bearer_valid"] is True
    assert finalized["old_bearer_refused"] is True
    assert gateway.calls == [("", NEW_BEARER)]
    assert not journal.exists()


@pytest.mark.parametrize(
    "runtime_name",
    ("brain", "red-data", "red-codex", "shrik", "gateway"),
)
def test_resume_refuses_each_runtime_file_drift_and_keeps_journal(
    config: RotationConfig,
    monkeypatch: pytest.MonkeyPatch,
    runtime_name: str,
) -> None:
    database = FakeDatabase()
    installer = FakePrivilegedInstaller()
    gateway = FakeGatewayProbe()
    generated = iter((NEW_BRAIN, NEW_CODEX, NEW_BEARER))
    monkeypatch.setattr(rotation.secrets, "token_hex", lambda _size: next(generated))
    apply_config = replace(
        config,
        apply=True,
        consumers_stopped_confirmed=True,
        rollback_preflight_confirmed=True,
    )
    run_rotation(
        apply_config,
        database=database,
        privileged_installer=installer,
        gateway_probe=gateway,
    )
    runtime_files = {
        "brain": config.brain_root / ".env",
        "red-data": config.red_root / "projects/red-data/.env",
        "red-codex": config.red_root / "projects/red-codex/.env.local",
        "shrik": config.shrik_env,
        "gateway": config.private_dir.parent / "codex-gateway.env",
    }
    drifted = runtime_files[runtime_name]
    drifted_content = drifted.read_text() + "OPERATOR_DRIFT=preserve-me\n"
    drifted.write_text(drifted_content)
    journal = config.private_dir / ".codex-gateway-rotation-state"

    with pytest.raises(RotationError, match="runtime credential files changed since installation"):
        run_rotation(
            replace(
                apply_config,
                resume=True,
                consumers_recreated_confirmed=True,
            ),
            database=database,
            privileged_installer=installer,
            gateway_probe=gateway,
        )

    assert drifted.read_text() == drifted_content
    assert journal.exists()


@pytest.mark.parametrize(
    ("runtime_name", "drifted_mode"),
    (("red-data", 0o640), ("shrik", 0o600)),
)
def test_resume_refuses_runtime_mode_drift_and_keeps_journal(
    config: RotationConfig,
    monkeypatch: pytest.MonkeyPatch,
    runtime_name: str,
    drifted_mode: int,
) -> None:
    database = FakeDatabase()
    installer = FakePrivilegedInstaller()
    generated = iter((NEW_BRAIN, NEW_CODEX, NEW_BEARER))
    monkeypatch.setattr(rotation.secrets, "token_hex", lambda _size: next(generated))
    apply_config = replace(
        config,
        apply=True,
        consumers_stopped_confirmed=True,
        rollback_preflight_confirmed=True,
    )
    run_rotation(
        apply_config,
        database=database,
        privileged_installer=installer,
        gateway_probe=FakeGatewayProbe(),
    )
    runtime_files = {
        "red-data": config.red_root / "projects/red-data/.env",
        "shrik": config.shrik_env,
    }
    runtime_files[runtime_name].chmod(drifted_mode)
    journal = config.private_dir / ".codex-gateway-rotation-state"

    with pytest.raises(RotationError, match="runtime credential files changed since installation"):
        run_rotation(
            replace(
                apply_config,
                resume=True,
                consumers_recreated_confirmed=True,
            ),
            database=database,
            privileged_installer=installer,
            gateway_probe=FakeGatewayProbe(),
        )

    assert stat.S_IMODE(runtime_files[runtime_name].stat().st_mode) == drifted_mode
    assert journal.exists()


def test_apply_failure_restores_database_and_files_without_leaking_secrets(
    config: RotationConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = FakeDatabase()
    installer = FakePrivilegedInstaller(fail_once=True)
    generated = iter((NEW_BRAIN, NEW_CODEX, NEW_BEARER))
    monkeypatch.setattr(rotation.secrets, "token_hex", lambda _size: next(generated))
    originals = {
        path: path.read_bytes()
        for path in (
            config.brain_root / ".env",
            config.red_root / "projects/red-data/.env",
            config.red_root / "projects/red-codex/.env.local",
            config.shrik_env,
        )
    }

    with pytest.raises(RotationError) as exc_info:
        run_rotation(
            replace(
                config,
                apply=True,
                consumers_stopped_confirmed=True,
                rollback_preflight_confirmed=True,
            ),
            database=database,
            privileged_installer=installer,
            gateway_probe=FakeGatewayProbe(),
        )

    assert str(exc_info.value) == "cutover failed; previous generation restored; resume required"
    assert exc_info.value.__context__ is None
    assert NEW_BRAIN not in str(exc_info.value)
    assert database.passwords == {"brain": OLD_BRAIN, "codex_ro": OLD_CODEX}
    assert all(path.read_bytes() == payload for path, payload in originals.items())
    assert (config.private_dir / ".codex-gateway-rotation-state").exists()


def test_explicit_rollback_restores_previous_generation_and_removes_journal(
    config: RotationConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = FakeDatabase()
    installer = FakePrivilegedInstaller()
    generated = iter((NEW_BRAIN, NEW_CODEX, NEW_BEARER))
    monkeypatch.setattr(rotation.secrets, "token_hex", lambda _size: next(generated))
    apply_config = replace(
        config,
        apply=True,
        consumers_stopped_confirmed=True,
        rollback_preflight_confirmed=True,
    )
    run_rotation(
        apply_config,
        database=database,
        privileged_installer=installer,
        gateway_probe=FakeGatewayProbe(),
    )

    result = run_rotation(
        replace(apply_config, apply=False, rollback=True),
        database=database,
        privileged_installer=installer,
        gateway_probe=FakeGatewayProbe(),
    )

    assert result == {
        "consumer_files_restored": 5,
        "database_credentials_restored": True,
        "status": "rolled_back",
    }
    assert database.passwords == {"brain": OLD_BRAIN, "codex_ro": OLD_CODEX}
    assert not (config.private_dir / ".codex-gateway-rotation-state").exists()
    assert not (config.private_dir.parent / "codex-gateway.env").exists()


def test_gateway_probe_keeps_bearers_out_of_argv_and_uses_static_compose_selection(
    config: RotationConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> Any:
        captured["args"] = args
        captured.update(kwargs)
        return rotation.subprocess.CompletedProcess(
            args,
            0,
            json.dumps({"anonymous": 401, "new": 200, "old": 401}),
            "",
        )

    monkeypatch.setattr(rotation.subprocess, "run", fake_run)

    assert rotation.DockerGatewayProbe(config.brain_root).prove(OLD_CODEX, NEW_BEARER) is True

    rendered_metadata = json.dumps({"args": captured["args"]}, sort_keys=True)
    assert OLD_CODEX not in rendered_metadata
    assert NEW_BEARER not in rendered_metadata
    assert captured["args"][:8] == [
        "docker",
        "compose",
        "--project-name",
        "brain-v42",
        "-f",
        "docker-compose.yml",
        "exec",
        "-T",
    ]
    assert captured["cwd"] == config.brain_root
    assert not any(key.startswith("COMPOSE_") for key in captured["env"])
    assert json.loads(captured["input"]) == {"new": NEW_BEARER, "old": OLD_CODEX}


def test_gateway_probe_clears_and_restores_inherited_compose_file(
    config: RotationConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_compose_files: list[str | None] = []

    def fake_run(args: list[str], **_kwargs: Any) -> Any:
        observed_compose_files.append(rotation.os.environ.get("COMPOSE_FILE"))
        return rotation.subprocess.CompletedProcess(
            args,
            0,
            json.dumps({"anonymous": 401, "new": 200, "old": 401}),
            "",
        )

    monkeypatch.setenv("COMPOSE_FILE", "/tmp/attacker-compose.yml")
    monkeypatch.setattr(rotation.subprocess, "run", fake_run)

    assert rotation.DockerGatewayProbe(config.brain_root).prove(OLD_CODEX, NEW_BEARER) is True
    assert observed_compose_files == ["/tmp/attacker-compose.yml"]
    assert rotation.os.environ["COMPOSE_FILE"] == "/tmp/attacker-compose.yml"


def test_gateway_probe_uses_local_compose_free_environment_without_parent_mutation(
    config: RotationConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> Any:
        captured.update(kwargs)
        assert rotation.os.environ["COMPOSE_PROJECT_NAME"] == "attacker-compose_project_name"
        return rotation.subprocess.CompletedProcess(
            args, 0, json.dumps({"anonymous": 401, "new": 200, "old": 401}), ""
        )

    for key in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_ENV_FILES", "COMPOSE_PROFILES"):
        monkeypatch.setenv(key, f"attacker-{key.lower()}")
    monkeypatch.setattr(rotation.subprocess, "run", fake_run)

    assert rotation.DockerGatewayProbe(config.brain_root).prove(OLD_CODEX, NEW_BEARER) is True
    assert captured["cwd"] == config.brain_root
    assert not any(key.startswith("COMPOSE_") for key in captured["env"])
    assert rotation.os.environ["COMPOSE_FILE"] == "attacker-compose_file"


def test_gateway_probe_concurrent_calls_do_not_mutate_parent_process_state(
    config: RotationConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    barrier = threading.Barrier(2)
    observed: list[tuple[Path, str, dict[str, str]]] = []

    def fake_run(args: list[str], **kwargs: Any) -> Any:
        barrier.wait(timeout=2)
        observed.append((Path.cwd(), rotation.os.environ["COMPOSE_FILE"], kwargs["env"]))
        return rotation.subprocess.CompletedProcess(
            args, 0, json.dumps({"anonymous": 401, "new": 200, "old": 401}), ""
        )

    monkeypatch.setenv("COMPOSE_FILE", "/tmp/attacker-compose.yml")
    monkeypatch.setattr(rotation.subprocess, "run", fake_run)
    original_directory = Path.cwd()
    threads = [
        threading.Thread(
            target=rotation.DockerGatewayProbe(config.brain_root).prove,
            args=(OLD_CODEX, NEW_BEARER),
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert Path.cwd() == original_directory
    assert rotation.os.environ["COMPOSE_FILE"] == "/tmp/attacker-compose.yml"
    assert all(cwd == original_directory for cwd, _compose_file, _env in observed)
    assert all("COMPOSE_FILE" not in env for _cwd, _compose_file, env in observed)


def test_postgres_probe_counts_only_authentication_failure_as_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = rotation.AsyncpgCredentialDatabase(
        rotation.make_url(f"postgresql+asyncpg://brain:{OLD_BRAIN}@localhost:5433/brain")
    )

    async def refuse(**_kwargs: Any) -> Any:
        raise rotation.asyncpg.InvalidPasswordError("auth-secret-canary")

    monkeypatch.setattr(rotation.asyncpg, "connect", refuse)
    assert database.probe("brain", OLD_BRAIN) is False

    async def unavailable(**_kwargs: Any) -> Any:
        raise RuntimeError("network-secret-canary")

    monkeypatch.setattr(rotation.asyncpg, "connect", unavailable)
    with pytest.raises(RotationError) as exc_info:
        database.probe("brain", OLD_BRAIN)
    assert str(exc_info.value) == "PostgreSQL credential verification failed"
    assert "secret-canary" not in str(exc_info.value)


def test_asyncpg_adapter_forwards_the_validated_sslmode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Connected:
        async def fetchval(self, _statement: str) -> int:
            return 1

        async def close(self) -> None:
            return None

    async def connect(**kwargs: Any) -> Connected:
        captured.update(kwargs)
        return Connected()

    monkeypatch.setattr(rotation.asyncpg, "connect", connect)
    database = rotation.AsyncpgCredentialDatabase(
        rotation.make_url(
            f"postgresql+asyncpg://brain:{OLD_BRAIN}@localhost:5433/brain?sslmode=require"
        )
    )

    assert database.probe("brain", OLD_BRAIN) is True
    assert captured["ssl"] == "require"


def test_codex_scope_proves_every_gateway_view_and_no_public_base_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class ScopedConnection:
        async def fetchrow(self, statement: str, *args: Any) -> dict[str, bool]:
            captured["statement"] = statement
            captured["args"] = args
            return {"all_views": True, "base_table_read": False}

        async def close(self) -> None:
            return None

    async def connect(**_kwargs: Any) -> ScopedConnection:
        return ScopedConnection()

    monkeypatch.setattr(rotation.asyncpg, "connect", connect)
    database = rotation.AsyncpgCredentialDatabase(
        rotation.make_url(f"postgresql+asyncpg://brain:{OLD_BRAIN}@localhost:5433/brain")
    )

    assert database.codex_scope_is_bounded(OLD_CODEX) is True
    assert set(captured["args"][0]) == set(rotation._CODEX_GATEWAY_VIEWS)
    assert len(captured["args"][0]) == 10
    assert captured["args"][1] == 10
    assert "information_schema.tables" in captured["statement"]


def test_apply_requires_both_operator_confirmations(config: RotationConfig) -> None:
    with pytest.raises(RotationError, match="operator confirmations required"):
        run_rotation(
            replace(config, apply=True),
            database=FakeDatabase(),
            privileged_installer=FakePrivilegedInstaller(),
            gateway_probe=FakeGatewayProbe(),
        )


def test_parser_has_no_secret_bearing_arguments() -> None:
    args = parse_args(
        [
            "--brain-root",
            "/srv/brain",
            "--red-root",
            "/srv/red",
            "--private-dir",
            "/srv/private",
            "--shrik-env",
            "/etc/shrik/env",
            "--expected-alembic-revision",
            DEPLOYED_REVISION,
        ]
    )

    assert args.apply is False
    assert args.resume is False
    assert args.rollback is False
    assert not any(
        fragment in action.dest
        for action in rotation._parser()._actions
        for fragment in ("password", "secret", "token", "bearer")
    )


def test_main_emits_only_sanitized_json_on_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_unexpectedly(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(f"unexpected {NEW_BEARER}")

    monkeypatch.setattr(rotation, "run_rotation", fail_unexpectedly)

    exit_code = rotation.main(
        [
            "--brain-root",
            "/srv/brain",
            "--red-root",
            "/srv/red",
            "--private-dir",
            "/srv/private",
            "--shrik-env",
            "/etc/shrik/env",
            "--expected-alembic-revision",
            DEPLOYED_REVISION,
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": "credential cutover failed",
        "status": "error",
    }


# ---------------------------------------------------------------------------
# Le head déployé est DÉCLARÉ par l'opérateur, jamais codé en dur
#
# Ticket 8285215c. La garde comparait à la constante "037". La production est
# passée à 038, puis 039, 040, 041… 044 : la procédure de rotation était donc
# morte, son dry-run échouant au préflight. Une garde qui se périme à chaque
# migration finit par ne plus garder que contre elle-même.
#
# Elle n'est PAS retirée — elle existe pour garantir que la rotation tourne
# contre le schéma attendu. Elle cesse seulement de se périmer.
# ---------------------------------------------------------------------------


def test_dry_run_succeeds_at_the_head_the_operator_declared(
    config: RotationConfig,
) -> None:
    result = run_rotation(
        config,
        database=FakeDatabase(revision="044"),
        privileged_installer=FakePrivilegedInstaller(),
        gateway_probe=FakeGatewayProbe(),
    )

    assert result["status"] == "preflight_ok"
    # La valeur MESURÉE est rendue, jamais une constante — sinon le contrat de
    # sortie rejouerait le défaut qu'on corrige.
    assert result["alembic_revision"] == "044"


def test_preflight_refuses_a_head_that_differs_from_the_declared_one(
    config: RotationConfig,
) -> None:
    """La sonde NÉGATIVE du préflight : la garde doit encore mordre.

    Retirer la garde plutôt que la paramétrer aurait rendu ce test vert, d'où son
    existence : le correctif ne doit pas être un affaiblissement déguisé.
    """
    with pytest.raises(RotationError) as excinfo:
        run_rotation(
            replace(config, expected_alembic_revision="044"),
            database=FakeDatabase(revision="041"),
            privileged_installer=FakePrivilegedInstaller(),
            gateway_probe=FakeGatewayProbe(),
        )

    message = str(excinfo.value)
    # Le message nomme les DEUX valeurs : sans le mesuré, l'opérateur ne sait pas
    # contre quoi il a échoué.
    assert "044" in message and "041" in message


def test_the_parser_requires_the_operator_to_declare_the_head() -> None:
    """Fail-closed : pas de défaut, sinon la valeur redevient une constante cachée."""
    with pytest.raises(SystemExit) as excinfo:
        parse_args(
            [
                "--brain-root",
                "/srv/brain",
                "--red-root",
                "/srv/red",
                "--private-dir",
                "/srv/private",
                "--shrik-env",
                "/etc/shrik/env",
            ]
        )
    assert excinfo.value.code == 2


def test_the_parser_refuses_an_empty_or_malformed_head() -> None:
    for bad in ("", "   ", "0" * 65, "037; DROP TABLE alembic_version"):
        with pytest.raises(SystemExit) as excinfo:
            parse_args(
                [
                    "--brain-root",
                    "/srv/brain",
                    "--red-root",
                    "/srv/red",
                    "--private-dir",
                    "/srv/private",
                    "--shrik-env",
                    "/etc/shrik/env",
                    "--expected-alembic-revision",
                    bad,
                ]
            )
        assert excinfo.value.code == 2, f"head accepté à tort : {bad!r}"


def test_rollback_still_works_when_the_declared_head_no_longer_matches(
    config: RotationConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L'échappatoire ne doit JAMAIS dépendre de la garde de révision.

    `--rollback` saute le préflight et n'appelle pas `_prove_new_generation` : c'est
    délibéré, et c'est la seule voie restée vivante pendant que la constante `037`
    rendait tout le reste inexécutable. Ajouter un contrôle de révision ici « par
    cohérence » enfermerait l'opérateur dehors au pire moment — juste après une
    migration, précisément quand il a besoin de revenir en arrière.
    """
    database = FakeDatabase()
    installer = FakePrivilegedInstaller()
    generated = iter((NEW_BRAIN, NEW_CODEX, NEW_BEARER))
    monkeypatch.setattr(rotation.secrets, "token_hex", lambda _size: next(generated))
    apply_config = replace(
        config,
        apply=True,
        consumers_stopped_confirmed=True,
        rollback_preflight_confirmed=True,
    )
    run_rotation(
        apply_config,
        database=database,
        privileged_installer=installer,
        gateway_probe=FakeGatewayProbe(),
    )

    # Le schéma a bougé sous les pieds de l'opérateur entre l'apply et le rollback.
    database._revision = "045"

    result = run_rotation(
        replace(apply_config, apply=False, rollback=True),
        database=database,
        privileged_installer=installer,
        gateway_probe=FakeGatewayProbe(),
    )

    assert result["status"] == "rolled_back"
    assert database.passwords == {"brain": OLD_BRAIN, "codex_ro": OLD_CODEX}


def test_preflight_refuses_a_schema_that_lost_a_gateway_view(
    config: RotationConfig,
) -> None:
    """Le head déclaré peut être JUSTE et la passerelle cassée quand même.

    `deploy/CODEX_GATEWAY.md` dit pourquoi la révision est vérifiée : parce
    que ce head « conserve les dix vues requises par la gateway ». C'est un
    PROXY. Ici on prouve l'invariant lui-même.
    """
    database = FakeDatabase(missing=("codex_ticket_v1",))

    with pytest.raises(RotationError) as excinfo:
        run_rotation(
            config,
            database=database,
            privileged_installer=FakePrivilegedInstaller(),
            gateway_probe=FakeGatewayProbe(),
        )

    message = str(excinfo.value)
    assert "codex_ticket_v1" in message
    # Le message nomme les MANQUANTS et EUX SEULS. Sans cette assertion
    # négative, joindre le contrat complet passerait le test tout en rendant
    # le diagnostic inutile — c'est exactement le défaut qu'on corrige.
    assert "codex_feature_v1" not in message


def test_preflight_names_a_gateway_column_that_drifted(config: RotationConfig) -> None:
    """LE vrai apport : une vue présente dont la FORME a bougé.

    Une vue absente fait déjà échouer le préflight aujourd'hui — par accident,
    via un `except` générique qui accuse les privilèges. Une migration qui
    garde `codex_ticket_v1` et renomme `body` en `content` laisse en revanche
    le préflight vert et casse la passerelle.
    """
    database = FakeDatabase(missing=("codex_ticket_v1.body",))

    with pytest.raises(RotationError) as excinfo:
        run_rotation(
            config,
            database=database,
            privileged_installer=FakePrivilegedInstaller(),
            gateway_probe=FakeGatewayProbe(),
        )

    assert "codex_ticket_v1.body" in str(excinfo.value)


def test_a_broken_contract_is_named_before_the_revision_mismatch(
    config: RotationConfig,
) -> None:
    """Quand les deux gardes mordraient, l'invariant DIRECT parle.

    Le proxy se tait : entre « la révision a bougé » et « cette vue a
    disparu », c'est la seconde qui est actionnable.
    """
    database = FakeDatabase(revision="999", missing=("codex_ticket_v1",))

    with pytest.raises(RotationError) as excinfo:
        run_rotation(
            config,
            database=database,
            privileged_installer=FakePrivilegedInstaller(),
            gateway_probe=FakeGatewayProbe(),
        )

    message = str(excinfo.value)
    assert "codex_ticket_v1" in message
    assert "999" not in message


def test_rollback_still_works_when_a_gateway_view_disappeared(
    config: RotationConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L'échappatoire ne dépend pas plus du contrat que de la révision.

    Jumeau de test_rollback_still_works_when_the_declared_head_no_longer_matches :
    c'est la même raison, et c'est le pire moment pour enfermer l'opérateur
    dehors.
    """
    database = FakeDatabase()
    installer = FakePrivilegedInstaller()
    generated = iter((NEW_BRAIN, NEW_CODEX, NEW_BEARER))
    monkeypatch.setattr(rotation.secrets, "token_hex", lambda _size: next(generated))
    apply_config = replace(
        config,
        apply=True,
        consumers_stopped_confirmed=True,
        rollback_preflight_confirmed=True,
    )
    run_rotation(
        apply_config,
        database=database,
        privileged_installer=installer,
        gateway_probe=FakeGatewayProbe(),
    )

    # Une migration a emporté une vue de la gateway entre l'apply et le rollback.
    database.missing = ("codex_dream_run_v1",)

    result = run_rotation(
        replace(apply_config, apply=False, rollback=True),
        database=database,
        privileged_installer=installer,
        gateway_probe=FakeGatewayProbe(),
    )
    assert result["status"] == "rolled_back"


def test_the_resume_path_refuses_a_broken_gateway_contract(
    config: RotationConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--resume` ne repasse JAMAIS par le préflight.

    Sans cette sonde, on peut ne jamais écrire le contrôle dans
    `_prove_new_generation` sans qu'un seul test bronche — une ligne
    d'implémentation sans témoin est une promesse, pas un clou.
    """
    database = FakeDatabase()
    installer = FakePrivilegedInstaller()
    generated = iter((NEW_BRAIN, NEW_CODEX, NEW_BEARER))
    monkeypatch.setattr(rotation.secrets, "token_hex", lambda _size: next(generated))
    apply_config = replace(
        config,
        apply=True,
        consumers_stopped_confirmed=True,
        rollback_preflight_confirmed=True,
    )
    run_rotation(
        apply_config,
        database=database,
        privileged_installer=installer,
        gateway_probe=FakeGatewayProbe(),
    )

    # Le même resume, contrat INTACT, doit passer : c'est ce contraste qui
    # fait mordre la sonde. Le chemin apply/resume enveloppe toute exception
    # dans un rollback automatique au message uniforme — la garde de révision
    # y perd son diagnostic exactement pareil —, donc asserter le NOM ici
    # tiendrait la nouvelle garde à un standard que l'ancienne ne tient pas.
    # Le nom est prouvé là où il survit : au préflight, par les trois sondes
    # ci-dessus.
    run_rotation(
        replace(apply_config, resume=True),
        database=database,
        privileged_installer=installer,
        gateway_probe=FakeGatewayProbe(),
    )

    database.missing = ("codex_feature_artifact_v1",)

    with pytest.raises(RotationError):
        run_rotation(
            replace(apply_config, resume=True),
            database=database,
            privileged_installer=installer,
            gateway_probe=FakeGatewayProbe(),
        )


def test_gateway_contract_arrays_zip_every_declared_column_to_its_view() -> None:
    """Les deux tableaux parallèles doivent avoir EXACTEMENT la même longueur.

    `unnest($1, $2)` pade le plus court avec NULL. Un désalignement ferait
    remonter `{NULL}` dans les manquants, et `", ".join(...)` exploserait en
    TypeError avalée en « credential cutover failed ». Cette sonde est le seul
    rempart, malgré la trivialité apparente de la fonction.
    """
    views, columns = rotation._gateway_contract_arrays(rotation._CODEX_GATEWAY_CONTRACT)

    assert len(views) == len(columns)
    assert len(views) == sum(len(c) for c in rotation._CODEX_GATEWAY_CONTRACT.values())
    assert set(views) == set(rotation._CODEX_GATEWAY_CONTRACT)
    # Aucune vue déclarée sans colonne : une entrée vide passerait le contrôle
    # d'existence sans jamais vérifier la forme.
    assert all(rotation._CODEX_GATEWAY_CONTRACT[v] for v in rotation._CODEX_GATEWAY_CONTRACT)
    assert list(zip(views, columns, strict=True))[0] == (
        views[0],
        rotation._CODEX_GATEWAY_CONTRACT[views[0]][0],
    )


def test_the_views_tuple_is_derived_from_the_contract_not_retyped() -> None:
    """Une liste recopiée annule la garde — learning 8dc7e042."""
    assert rotation._CODEX_GATEWAY_VIEWS == tuple(rotation._CODEX_GATEWAY_CONTRACT)
    assert len(rotation._CODEX_GATEWAY_VIEWS) == 10


def test_the_contract_proof_binds_every_declared_column_as_codex_ro(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LA sonde qui empêche la livraison entière d'être inerte.

    Sans elle, écrire `fetchrow(_GATEWAY_CONTRACT_PROOF, [], [])` rendrait
    deux tableaux vides — donc jamais aucun manquant, donc un préflight qui
    passe toujours — pendant que TOUS les autres tests de ce fichier restent
    verts : ils passent par FakeDatabase et ne touchent jamais le SQL réel.
    C'est la faute « le paramètre est LIÉ, donc le test survit au retrait du
    WHERE », qui a eu ce dépôt deux fois.
    """
    captured: dict[str, Any] = {}

    class _FakeConnection:
        async def fetchrow(self, statement: str, *args: Any) -> dict[str, list[str]]:
            captured["statement"] = statement
            captured["args"] = args
            return {
                "missing_views": [],
                "missing_columns": [],
                "missing_barriers": [],
                "missing_triggers": [],
            }

        async def close(self) -> None:
            captured["closed"] = True

    async def _fake_connect(_self: Any, role: str, password: str) -> _FakeConnection:
        captured["role"] = role
        captured["password"] = password
        return _FakeConnection()

    monkeypatch.setattr(rotation.AsyncpgCredentialDatabase, "_connect", _fake_connect)
    database = rotation.AsyncpgCredentialDatabase(
        make_url("postgresql+asyncpg://brain:x@localhost:5433/brain")
    )

    assert database.missing_gateway_contract(OLD_CODEX) == ()

    # Le rôle : codex_ro par cohérence avec le reste du préflight. NE PAS y lire
    # une preuve de lisibilité — mesuré (b3331691) : has_table_privilege('codex_ro',
    # 'pg_attribute','SELECT') est vrai et to_regclass est exécutable par public,
    # la requête rend la MÊME réponse exécutée en brain. Seul codex_scope_is_bounded
    # attrape un REVOKE, et c'est une garde antérieure.
    assert captured["role"] == "codex_ro"
    assert captured["password"] == OLD_CODEX
    # Les ARGUMENTS LIÉS, et pas seulement leur existence — les CINQ : les deux
    # tableaux parallèles vue/colonne, puis les clauses (b), (c) et (d) du
    # contrat (barrières et triggers, b3331691).
    expected_views, expected_columns = rotation._gateway_contract_arrays(
        rotation._CODEX_GATEWAY_CONTRACT
    )
    assert captured["args"] == (
        expected_views,
        expected_columns,
        list(rotation._CODEX_SCOPED_BARRIER_VIEWS),
        [name for name, _table in rotation._CODEX_GATEWAY_TRIGGERS],
        [table for _name, table in rotation._CODEX_GATEWAY_TRIGGERS],
    )
    assert len(captured["args"][0]) == sum(
        len(c) for c in rotation._CODEX_GATEWAY_CONTRACT.values()
    )
    assert captured["closed"] is True


class TestContractProofCoversAllFourClauses:
    """b3331691 : la preuve du préflight ne couvrait qu'UNE clause sur quatre.

    Le contrat faisant autorité (`_CODEX_CONTRACT_READY`, câblé sur `/ready`)
    en a QUATRE : existence des dix vues, `security_barrier` sur les sept vues
    scopées, et les deux triggers actifs. Or changer une liste de colonnes
    impose un DROP+CREATE — précisément le geste que la garde surveille — et
    un CREATE sans `WITH (security_barrier=true)` rendait `preflight_ok` puis
    cassait la passerelle APRÈS bascule, en fuyant hors scope au passage.

    Le script n'importe RIEN de `brain_v42` (délibéré : il réécrit les .env
    que `Settings` lit) — la copie est donc inévitable, et c'est CE test qui
    l'empêche d'en être une : lui peut importer les deux côtés.
    """

    def test_the_script_lists_mirror_the_readiness_authority(self) -> None:
        import re

        from brain_v42.codex_gateway.composition import _CODEX_CONTRACT_READY

        authority = " ".join(str(_CODEX_CONTRACT_READY).split())
        arrays = re.findall(r"ARRAY\[(.*?)\]::text\[\]", authority)
        assert len(arrays) >= 2, "la forme de _CODEX_CONTRACT_READY a changé — réaligner ce test"
        authority_views = set(re.findall(r"'(codex_\w+)'", arrays[0]))
        authority_barriers = set(re.findall(r"'(codex_\w+)'", arrays[1]))

        assert set(rotation._CODEX_GATEWAY_VIEWS) == authority_views
        assert set(rotation._CODEX_SCOPED_BARRIER_VIEWS) == authority_barriers
        assert len(rotation._CODEX_SCOPED_BARRIER_VIEWS) == 7

        for trigger_name, table_name in rotation._CODEX_GATEWAY_TRIGGERS:
            assert f"tgname = '{trigger_name}'" in authority
            assert f"to_regclass('public.{table_name}')" in authority
        assert len(rotation._CODEX_GATEWAY_TRIGGERS) == 2

        # Les PRÉDICATS de la preuve sont ceux de l'autorité, au blanc près :
        # même sémantique de barrière, même définition d'« actif ».
        proof = " ".join(rotation._GATEWAY_CONTRACT_PROOF.split())
        assert (
            "'security_barrier=true' = ANY( COALESCE(contract_view.reloptions, ARRAY[]::text[]) )"
            in proof
        )
        assert "tgenabled IN ('O', 'A') AND NOT tgisinternal" in proof
        assert "tgenabled IN ('O', 'A') AND NOT tgisinternal" in authority

    def test_the_column_pairs_agree_with_the_canonical_contract(self) -> None:
        """Les 92 paires cessent d'être une copie : les 9 vues de la 036 sont
        liées à CONTRACT_COLUMNS (leur source de vérité testée en intégration) ;
        la 10e (codex_brain_entity_v1, migration 024, jamais modifiée) est
        épinglée ici même — une vue ajoutée d'un seul côté rougit."""
        from tests.integration.db.test_codex_contract_views_036 import CONTRACT_COLUMNS

        script_contract = {
            view: tuple(columns) for view, columns in rotation._CODEX_GATEWAY_CONTRACT.items()
        }
        assert set(script_contract) == set(CONTRACT_COLUMNS) | {"codex_brain_entity_v1"}
        for view, columns in CONTRACT_COLUMNS.items():
            assert script_contract[view] == tuple(columns), view
        assert script_contract["codex_brain_entity_v1"] == (
            "id",
            "type",
            "title",
            "status",
            "freshness_status",
            "content",
            "project_key",
            "updated_at",
            "superseded_by",
            "merged_into",
        )

    def test_missing_barriers_and_triggers_redden_the_preflight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Le scénario du ticket : colonnes intactes, barrière absente — la
        preuve doit nommer le manquant au lieu de rendre preflight_ok."""
        captured: dict[str, Any] = {}

        class _FakeConnection:
            async def fetchrow(self, statement: str, *args: Any) -> dict[str, list[str]]:
                captured["statement"] = statement
                captured["args"] = args
                return {
                    "missing_views": [],
                    "missing_columns": [],
                    "missing_barriers": ["codex_ticket_v1"],
                    "missing_triggers": ["trg_ticket_participants_immutable ON tickets"],
                }

            async def close(self) -> None:
                captured["closed"] = True

        async def _fake_connect(_self: Any, role: str, password: str) -> _FakeConnection:
            return _FakeConnection()

        monkeypatch.setattr(rotation.AsyncpgCredentialDatabase, "_connect", _fake_connect)
        database = rotation.AsyncpgCredentialDatabase(
            make_url("postgresql+asyncpg://brain:x@localhost:5433/brain")
        )

        missing = database.missing_gateway_contract(OLD_CODEX)

        assert "security_barrier:codex_ticket_v1" in missing
        assert "trigger:trg_ticket_participants_immutable ON tickets" in missing
        # Les nouveaux paramètres sont LIÉS — pas seulement présents dans le SQL.
        assert list(captured["args"][2]) == list(rotation._CODEX_SCOPED_BARRIER_VIEWS)
        assert list(captured["args"][3]) == [
            name for name, _table in rotation._CODEX_GATEWAY_TRIGGERS
        ]
        assert list(captured["args"][4]) == [
            table for _name, table in rotation._CODEX_GATEWAY_TRIGGERS
        ]
