"""Safety defaults for standalone data maintenance scripts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from scripts import regen_embeddings, rotate_neo4j_credential


def test_regen_embeddings_requires_explicit_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["regen_embeddings.py"])
    assert not regen_embeddings.parse_args().apply


def test_regen_embeddings_accepts_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["regen_embeddings.py", "--apply"])
    assert regen_embeddings.parse_args().apply


def test_migration_defaults_to_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from brain_v42.scripts import migrate_neo4j_to_pg

    monkeypatch.setattr(sys, "argv", ["migrate_neo4j_to_pg.py"])
    args = migrate_neo4j_to_pg.parse_args()
    assert not args.apply


def test_migration_password_comes_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from brain_v42.scripts import migrate_neo4j_to_pg

    monkeypatch.setenv("NEO4J_PASSWORD", "from-environment")
    monkeypatch.setattr(sys, "argv", ["migrate_neo4j_to_pg.py"])
    assert migrate_neo4j_to_pg.parse_args().neo4j_password is None


@pytest.mark.parametrize("option", ["--neo4j-password", "--neo4j-pass"])
@pytest.mark.parametrize("form", ["separate", "equals"])
def test_migration_refuses_neo4j_password_argv_forms(
    monkeypatch: pytest.MonkeyPatch, option: str, form: str
) -> None:
    from brain_v42.scripts import migrate_neo4j_to_pg

    argv = (
        ["migrate_neo4j_to_pg.py", f"{option}=secret"]
        if form == "equals"
        else ["migrate_neo4j_to_pg.py", option, "secret"]
    )
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit):
        migrate_neo4j_to_pg.parse_args()


@pytest.mark.parametrize("module", ["migration", "regen"])
@pytest.mark.parametrize(
    "url",
    ["postgresql://user:secret@localhost/db", "postgresql+asyncpg://u:p%40ss@localhost/db"],
)
def test_scripts_reject_password_in_postgres_url_argv(
    monkeypatch: pytest.MonkeyPatch, module: str, url: str
) -> None:
    if module == "migration":
        from brain_v42.scripts import migrate_neo4j_to_pg as script
    else:
        script = regen_embeddings
    monkeypatch.setattr(sys, "argv", ["script.py", "--postgres-url", url])
    with pytest.raises(SystemExit):
        script.parse_args()


def test_passwordless_postgres_url_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    from brain_v42.scripts import migrate_neo4j_to_pg

    monkeypatch.setattr(
        sys, "argv", ["script.py", "--postgres-url", "postgresql://user@localhost/db"]
    )
    assert migrate_neo4j_to_pg.parse_args().postgres_url == "postgresql://user@localhost/db"


def test_credential_rotator_timeout_is_a_safe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        rotate_neo4j_credential, "_trusted_repository_command_assets", lambda _path: True
    )

    def timeout_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        raise subprocess.TimeoutExpired(cmd="docker", timeout=kwargs["timeout"])

    with pytest.raises(rotate_neo4j_credential.RotationError):
        rotate_neo4j_credential._run_command(
            timeout_runner,
            ["docker", "inspect"],
            "safe timeout failure",
            environment={},
            working_directory=Path(__file__).resolve().parents[2],
        )

    assert calls[0]["timeout"] == 60


def test_dry_run_flags_default_without_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    from brain_v42.scripts import migrate_neo4j_to_pg

    monkeypatch.setattr(sys, "argv", ["script.py"])
    assert migrate_neo4j_to_pg.parse_args().apply is False
    monkeypatch.setattr(sys, "argv", ["script.py"])
    assert regen_embeddings.parse_args().apply is False
    assert "args.dry_run = not args.apply" in Path(regen_embeddings.__file__).read_text()
    assert "args.dry_run = not args.apply" in Path(migrate_neo4j_to_pg.__file__).read_text()
