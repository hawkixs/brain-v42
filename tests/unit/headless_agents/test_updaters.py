"""``ha providers --update``: the vendor updaters and the rollback each one leaves (0.5.2 lot 4b).

No test here ever runs a real vendor updater: every updater is a fake executable
script on a temporary ``PATH``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from headless_agents import proofs, updaters


def test_every_cli_rail_declares_its_measured_updater() -> None:
    table = {rail: (u.rail, u.args, u.check_args) for rail, u in updaters.UPDATERS.items()}
    assert table == {
        "claude": ("claude", ("update",), None),
        "codex": ("codex", ("update",), None),
        "agy": ("agy", ("update",), None),
        "opencode": ("opencode", ("upgrade",), None),
    }
    assert set(updaters.UPDATERS) == set(proofs.CLI_RAILS)
    assert updaters.UPDATE_TIMEOUT_SECONDS == 600.0


def test_the_updater_table_is_not_a_fingerprinted_source() -> None:
    """Editing a fingerprinted file makes every installed isolation proof of that rail
    stale: the updater table lives apart from them on purpose."""
    fingerprinted = {name for names in proofs._ISOLATION_SOURCE_FILES.values() for name in names}  # noqa: SLF001
    assert "updaters.py" not in fingerprinted
    assert not any(name.endswith("updaters.py") for name in fingerprinted)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("2.1.283 (Claude Code)", "2.1.283"),
        ("codex-cli 0.156.0", "0.156.0"),
        ("1.2.11", "1.2.11"),
        ("1.18.30", "1.18.30"),
        ("garbage", None),
        ("", None),
        (None, None),
    ],
)
def test_semver_reads_each_measured_version_string(version: str | None, expected: str) -> None:
    assert updaters.semver(version) == expected


def test_rollback_names_only_a_path_that_exists(tmp_path: Path) -> None:
    home, state = tmp_path / "home", tmp_path / "state"
    # Nothing kept anywhere: no path, and a command only where the vendor has one.
    assert updaters.rollback("claude", "2.1.283 (Claude Code)", home, state) == (
        None,
        "claude install 2.1.283",
    )
    assert updaters.rollback("codex", "codex-cli 0.156.0", home, state) == (None, None)
    assert updaters.rollback("agy", "1.2.11", home, state) == (None, None)
    assert updaters.rollback("opencode", "1.18.30", home, state) == (
        None,
        "opencode upgrade 1.18.30",
    )

    claude = home / ".local/share/claude/versions/2.1.283"
    claude.parent.mkdir(parents=True)
    claude.write_text("binary")
    assert updaters.rollback("claude", "2.1.283 (Claude Code)", home, state) == (
        claude,
        "claude install 2.1.283",
    )

    releases = home / ".codex/packages/standalone/releases"
    codex = releases / "0.156.0-x86_64-unknown-linux-musl/bin/codex"
    codex.parent.mkdir(parents=True)
    codex.write_text("binary")
    assert updaters.rollback("codex", "codex-cli 0.156.0", home, state) == (codex, None)
    other = releases / "0.156.0-aarch64-unknown-linux-musl/bin/codex"
    other.parent.mkdir(parents=True)
    other.write_text("binary")
    assert updaters.rollback("codex", "codex-cli 0.156.0", home, state) == (None, None), (
        "two candidates: naming one would be a guess"
    )

    # agy keeps no previous binary: its rollback is the copy --update made (default 3).
    agy = state / "rollback/agy/1.2.11/agy"
    agy.parent.mkdir(parents=True)
    agy.write_text("binary")
    assert updaters.rollback("agy", "1.2.11", home, state) == (agy, None)


@pytest.mark.parametrize("rail", ["claude", "codex", "agy", "opencode"])
def test_an_unparsable_version_has_no_rollback(tmp_path: Path, rail: str) -> None:
    assert updaters.rollback(rail, "garbage", tmp_path, tmp_path) == (None, None)
    assert updaters.rollback(rail, None, tmp_path, tmp_path) == (None, None)


def test_updaters_never_record_a_proof() -> None:
    """Trust chain (spec 0.5.2 §4): only ``headless_agents.prove.prove`` records a proof.
    The update path may CALL it, never record one itself -- not by name, not by
    attribute, not through a string handed to ``getattr``."""
    source = Path(updaters.__file__).read_text(encoding="utf-8")
    offending = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id == "record_proof":
            offending.append(f"name at line {node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr == "record_proof":
            offending.append(f"attribute at line {node.lineno}")
        elif isinstance(node, ast.ImportFrom) and any(
            alias.name == "record_proof" for alias in node.names
        ):
            offending.append(f"import at line {node.lineno}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "record_proof" in node.value
        ):
            offending.append(f"string at line {node.lineno}")
    assert offending == []
