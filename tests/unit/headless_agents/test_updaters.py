"""``ha providers --update``: the vendor updaters and the rollback each one leaves (0.5.2 lot 4b).

No test here ever runs a real vendor updater: every updater is a fake executable
script on a temporary ``PATH``.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from headless_agents import locks, proofs, prove, updaters
from headless_agents.engine import UsageError


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


# ── run_updates: lock, update, release, prove (Task B2) ─────────────────────────

VERSIONS = {
    "claude": "1.0.0 (Claude Code)",
    "codex": "codex-cli 1.0.0",
    "agy": "1.0.0",
    "opencode": "1.0.0",
}

#: A fake CLI. ``--version`` prints ``<name>.version`` and records the environment it
#: was probed with in ``<name>.probed_environ.json`` (to pin what the version probe
#: actually receives). Anything else is its updater, which records what it saw in
#: ``<name>.updated`` (JSON) -- its argv, its own ``os.environ`` (to pin what the
#: updater actually receives), whether the global lock is held by someone else, and,
#: for ``agy``, whether its rollback copy already exists. The state directory and,
#: for agy, the rollback copy are found relative to the script's own directory
#: (``tmp_path/bin``'s sibling ``tmp_path/state``) -- never through an environment
#: variable, since the sanitised environment carries none of the package's own
#: names. Then behaves as ``<name>.behaviour`` says: ``bump`` (default), ``keep``,
#: ``fail``, ``fail-bump`` or ``sleep``.
_FAKE = """#!{python}
import fcntl, json, os, pathlib, subprocess, sys, time
here = pathlib.Path(__file__).resolve().parent
name = pathlib.Path(__file__).name
version = here / (name + ".version")
if sys.argv[1:] == ["--version"]:
    (here / (name + ".probed_environ.json")).write_text(json.dumps(dict(os.environ)))
    print(version.read_text().strip())
    sys.exit(0)
old_version = version.read_text().strip()
seen = {{"args": sys.argv[1:], "environ": dict(os.environ)}}
state = here.parent / "state"
lock = os.open(str(state / "unconfined.lock"), os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
except BlockingIOError:
    seen["lock"] = "blocked"
else:
    seen["lock"] = "free"
if name == "agy":
    seen["watched"] = (state / "rollback" / "agy" / old_version / "agy").exists()
(here / (name + ".updated")).write_text(json.dumps(seen))
behaviour_file = here / (name + ".behaviour")
behaviour = behaviour_file.read_text().strip() if behaviour_file.exists() else "bump"
if behaviour in ("bump", "fail-bump"):
    version.write_text(version.read_text().replace("1.0.0", "2.0.0"))
if behaviour == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (here / (name + ".grandchild")).write_text(str(child.pid))
    (here / (name + ".pid")).write_text(str(os.getpid()))
    time.sleep(60)
sys.exit(1 if behaviour.startswith("fail") else 0)
"""


@dataclass
class _Bin:
    directory: Path

    def install(self, rail: str, version: str) -> Path:
        script = self.directory / rail
        script.write_text(_FAKE.format(python=sys.executable))
        script.chmod(0o755)
        (self.directory / f"{rail}.version").write_text(version)
        return script

    def behave(self, rail: str, behaviour: str) -> None:
        (self.directory / f"{rail}.behaviour").write_text(behaviour)

    def updated(self, rail: str) -> dict[str, Any] | None:
        path = self.directory / f"{rail}.updated"
        return json.loads(path.read_text()) if path.exists() else None

    def probed_environ(self, rail: str) -> dict[str, str] | None:
        path = self.directory / f"{rail}.probed_environ.json"
        return json.loads(path.read_text()) if path.exists() else None


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Bin:
    """Fake CLIs as the WHOLE ``PATH``: no real vendor updater is reachable."""
    built = _Bin(tmp_path / "bin")
    built.directory.mkdir()
    for rail, version in VERSIONS.items():
        built.install(rail, version)
    monkeypatch.setenv("PATH", str(built.directory))
    for rail in VERSIONS:
        assert shutil.which(rail) == str(built.directory / rail)
    return built


@dataclass
class _Proofs:
    """``prove.prove`` replaced by a recorder, sharing one event list with ``say``."""

    events: list[tuple[Any, ...]] = field(default_factory=list)
    outcomes: dict[tuple[str, str], tuple[str, bool]] = field(default_factory=dict)
    record: bool = False

    @property
    def calls(self) -> list[tuple[str, str]]:
        return [(event[1], event[2]) for event in self.events if event[0] == "prove"]


@pytest.fixture
def proved(monkeypatch: pytest.MonkeyPatch) -> _Proofs:
    recorder = _Proofs()

    def fake_prove(rail: str, kind: str, **kwargs: Any) -> prove.Verdict:
        state = kwargs["state"]
        recorder.events.append(("prove", rail, kind, locks.is_free(state / "unconfined.lock")))
        outcome, recorded = recorder.outcomes.get((rail, kind), ("passed", True))
        if recorder.record and recorded:
            version = updaters_probe_version(rail)
            proofs.record_proof(state, rail, version=version, **{kind: outcome == "passed"})
        return prove.Verdict(
            rail,
            kind,
            "v",
            outcome,
            "fake",
            recorded,
            prove.planned_runs(rail, kind),  # type: ignore[arg-type]
        )

    monkeypatch.setattr(prove, "prove", fake_prove)
    monkeypatch.setattr(prove, "running_from_checkout", lambda: False)
    return recorder


def updaters_probe_version(rail: str) -> str:
    """What the fake on ``PATH`` answers to ``--version`` now."""
    path = shutil.which(rail)
    assert path is not None
    return Path(f"{path}.version").read_text().strip()


def _update(
    tmp_path: Path,
    proved: _Proofs,
    rails: tuple[str, ...] = tuple(VERSIONS),
    *,
    check: bool = False,
    prove_after: bool = True,
    wait: float | None = None,
    environ: dict[str, str] | None = None,
) -> list[updaters.UpdateRow]:
    return updaters.run_updates(
        rails,
        state=tmp_path / "state",
        home=tmp_path / "home",
        environ=environ if environ is not None else {"PATH": os.environ["PATH"]},
        wait=locks.AdmissionWait(wait),
        check=check,
        prove_after=prove_after,
        models={"claude": "m", "codex": "m", "opencode": "m"},
        say=lambda text: proved.events.append(("say", text)),
    )


def _row(rows: list[updaters.UpdateRow], rail: str) -> updaters.UpdateRow:
    (found,) = [row for row in rows if row.rail == rail]
    return found


_HOLD_GLOBAL = """
import sys, time
from pathlib import Path
from headless_agents.locks import AdmissionWait, admit_global, held, Rank
state, mode, ready = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
if mode == "exclusive-raw":
    with held(state / "unconfined.lock", rank=Rank.UNCONFINED, exclusive=True, wait=None,
              what="the unconfined lock"):
        ready.write_text("held")
        time.sleep(60)
else:
    with admit_global(state, exclusive=False, wait=AdmissionWait(None)):
        ready.write_text("held")
        time.sleep(60)
"""


@pytest.fixture
def hold_global(tmp_path: Path) -> Iterator[Callable[[str], None]]:
    """Hold the global lock from another process: ``exclusive-raw`` or a running run."""
    started: list[subprocess.Popen[bytes]] = []

    def hold(mode: str) -> None:
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        ready = tmp_path / f"held-{mode}"
        started.append(
            subprocess.Popen([sys.executable, "-c", _HOLD_GLOBAL, str(state), mode, str(ready)])
        )
        limit = time.monotonic() + 10
        while not ready.exists():
            assert time.monotonic() < limit and started[-1].poll() is None
            time.sleep(0.01)

    yield hold
    for process in started:
        process.kill()
        process.wait()


def test_check_runs_no_updater_takes_no_lock_and_reports_unknown(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs, hold_global: Callable[[str], None]
) -> None:
    hold_global("exclusive-raw")
    started = time.monotonic()
    rows = _update(tmp_path, proved, check=True)
    assert time.monotonic() - started < 3, "--check waited for a lock"
    assert all(fake_bin.updated(rail) is None for rail in VERSIONS), "an updater ran"
    assert proved.calls == []
    for row in rows:
        assert row.old_version == VERSIONS[row.rail]
        assert (row.new_version, row.exit_code, row.log) == (None, None, None)
        assert row.note == "update available: unknown (no vendor dry run)"
        assert row.status == "checked"
        assert row.updater == (
            str(fake_bin.directory / row.rail),
            *updaters.UPDATERS[row.rail].args,
        )
    # What an update would leave to return to: the version installed now.
    assert _row(rows, "claude").rollback_command == "claude install 1.0.0"
    assert _row(rows, "opencode").rollback_command == "opencode upgrade 1.0.0"


def test_updaters_run_while_the_global_lock_is_held_exclusively(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs
) -> None:
    rows = _update(tmp_path, proved)
    for rail in VERSIONS:
        seen = fake_bin.updated(rail)
        assert seen is not None and seen["lock"] == "blocked", rail
        assert seen["args"] == list(updaters.UPDATERS[rail].args)
    assert all(row.exit_code == 0 for row in rows)


def test_a_running_run_blocks_the_update_until_wait_expires(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs, hold_global: Callable[[str], None]
) -> None:
    hold_global("shared-run")
    started = time.monotonic()
    with pytest.raises(UsageError, match=r"--wait 1 s expired.*nothing updated"):
        _update(tmp_path, proved, wait=1.0)
    assert 0.9 <= time.monotonic() - started < 3
    assert all(fake_bin.updated(rail) is None for rail in VERSIONS)


def test_without_wait_the_default_bound_applies(
    tmp_path: Path,
    fake_bin: _Bin,
    proved: _Proofs,
    hold_global: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(locks, "LOCK_WAIT_SECONDS", 0.3)
    hold_global("shared-run")
    started = time.monotonic()
    with pytest.raises(UsageError, match="runs still running.*nothing updated"):
        _update(tmp_path, proved)
    assert 0.25 <= time.monotonic() - started < 3
    assert all(fake_bin.updated(rail) is None for rail in VERSIONS)


def test_proofs_run_after_the_lock_is_released_and_only_for_changed_versions(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs
) -> None:
    fake_bin.behave("codex", "keep")
    fake_bin.behave("agy", "keep")
    rows = _update(tmp_path, proved)
    assert [(event[1], event[2], event[3]) for event in proved.events if event[0] == "prove"] == [
        ("claude", "isolation", True),
        ("opencode", "isolation", True),
        ("opencode", "confinement", True),
    ], "claude's confinement is unprovable; every proof runs with the global lock free"
    assert _row(rows, "claude").new_version == "2.0.0 (Claude Code)"
    assert _row(rows, "codex").new_version == "codex-cli 1.0.0"
    assert _row(rows, "codex").verdicts == ()
    assert [(row.rail, row.status) for row in rows] == [
        ("claude", "updated"),
        ("codex", "unchanged"),
        ("agy", "unchanged"),
        ("opencode", "updated"),
    ]


def test_the_mode_is_read_on_the_version_installed_after_the_proofs(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs
) -> None:
    proved.record = True
    rows = _update(tmp_path, proved, ("opencode",))
    assert _row(rows, "opencode").mode == "parallel"


def test_no_prove_proves_nothing_and_reports_the_mode(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs
) -> None:
    rows = _update(tmp_path, proved, ("claude",), prove_after=False)
    assert proved.calls == []
    row = _row(rows, "claude")
    assert row.new_version == "2.0.0 (Claude Code)"
    assert row.mode == "refused"
    assert not any(event[0] == "say" and "tokens" in event[1] for event in proved.events)


def test_a_failed_updater_is_reported_and_the_version_reprobed(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs
) -> None:
    fake_bin.behave("codex", "fail-bump")
    kept = tmp_path / "home/.codex/packages/standalone/releases/1.0.0-x86_64/bin/codex"
    kept.parent.mkdir(parents=True)
    kept.write_text("binary")
    rows = _update(tmp_path, proved, ("codex",))
    row = _row(rows, "codex")
    assert (row.exit_code, row.new_version) == (1, "codex-cli 2.0.0")
    assert row.status == "failed"
    assert proved.calls == [("codex", "isolation"), ("codex", "confinement")]
    assert row.rollback_path == kept
    assert row.log is not None and row.log.is_file()
    assert stat.S_IMODE(row.log.stat().st_mode) == 0o600


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:  # a zombie no init has reaped yet is gone too
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].startswith("Z")
    except (OSError, IndexError):
        return True


def test_an_updater_past_its_timeout_is_killed_with_its_group(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updaters, "UPDATE_TIMEOUT_SECONDS", 0.5)
    fake_bin.behave("opencode", "sleep")
    started = time.monotonic()
    rows = _update(tmp_path, proved, ("opencode",))
    assert time.monotonic() - started < 10
    row = _row(rows, "opencode")
    assert row.exit_code is None and row.note is not None and "timed out" in row.note
    assert row.status == "timed out"
    for name in ("opencode.pid", "opencode.grandchild"):
        pid = int((fake_bin.directory / name).read_text())
        limit = time.monotonic() + 5
        while not _gone(pid):
            assert time.monotonic() < limit, f"{name} {pid} survived the timeout"
            time.sleep(0.05)
    assert proved.calls == [], "an unchanged version is not proven"


def test_a_failed_proof_reports_the_rollback_that_exists(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs
) -> None:
    kept = tmp_path / "home/.local/share/claude/versions/1.0.0"
    kept.parent.mkdir(parents=True)
    kept.write_text("binary")
    proved.outcomes[("claude", "isolation")] = ("failed", True)
    rows = _update(tmp_path, proved, ("claude",))
    row = _row(rows, "claude")
    assert (row.rollback_path, row.rollback_command) == (kept, "claude install 1.0.0")


def test_a_passing_update_reports_no_rollback(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs
) -> None:
    kept = tmp_path / "home/.local/share/claude/versions/1.0.0"
    kept.parent.mkdir(parents=True)
    kept.write_text("binary")
    row = _row(_update(tmp_path, proved, ("claude",)), "claude")
    assert (row.rollback_path, row.rollback_command) == (None, None)


def test_the_updater_is_the_exact_probed_path(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = _Bin(tmp_path / "bin2")
    second.directory.mkdir()
    second.install("codex", "codex-cli 1.0.0")
    monkeypatch.setenv("PATH", f"{fake_bin.directory}:{second.directory}")
    rows = _update(tmp_path, proved, ("codex",))
    assert fake_bin.updated("codex") is not None
    assert second.updated("codex") is None
    assert _row(rows, "codex").updater[0] == str(fake_bin.directory / "codex")


def test_the_updater_environment_is_sanitised_never_the_raw_operator_environ(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs
) -> None:
    """The updater subprocess gets the package's shared child-environment
    allowlist (``capability.scoped_environment``), not the raw parent environ:
    a Claude Code session marker or an unrelated credential must never reach a
    vendor's update binary, which this package neither audits nor sandboxes."""
    environ = {
        "PATH": os.environ["PATH"],
        "HOME": "/should-not-be-used",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "SOME_SERVICE_API_KEY": "super-secret-value",
    }
    _update(tmp_path, proved, ("agy",), environ=environ)
    seen = fake_bin.updated("agy")
    assert seen is not None
    child_environ = seen["environ"]
    assert "CLAUDECODE" not in child_environ
    assert "CLAUDE_CODE_ENTRYPOINT" not in child_environ
    assert "SOME_SERVICE_API_KEY" not in child_environ
    assert child_environ["PATH"] == os.environ["PATH"]
    assert child_environ["HOME"] == str(tmp_path / "home")


@pytest.mark.parametrize(
    ("rail", "variable"),
    [("codex", "CODEX_HOME"), ("claude", "CLAUDE_CONFIG_DIR")],
)
def test_a_vendor_home_variable_reaches_only_its_own_rails_updater_and_probe(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs, rail: str, variable: str
) -> None:
    """PR #243 review round 2, finding 2: CODEX_HOME is codex's own documented
    state directory (providers.codex.CHILD_ENV_PASSTHROUGH); CLAUDE_CONFIG_DIR is
    claude's own (providers.claude.CHILD_ENV_PASSTHROUGH). Losing either from the
    matching rail's sanitised environment would run its updater and its probe
    against the DEFAULT home/config instead of the one the operator declared,
    silently targeting the wrong installation; leaking it to another rail is the
    opposite mistake."""
    value = str(tmp_path / f"custom-{variable.lower()}")
    environ = {"PATH": os.environ["PATH"], variable: value}
    _update(tmp_path, proved, environ=environ)
    for other in VERSIONS:
        updated = fake_bin.updated(other)
        probed = fake_bin.probed_environ(other)
        assert updated is not None and probed is not None, other
        if other == rail:
            assert updated["environ"].get(variable) == value
            assert probed.get(variable) == value
        else:
            assert variable not in updated["environ"], other
            assert variable not in probed, other


def test_agy_is_copied_aside_before_its_update(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs
) -> None:
    copy = updaters.agy_copy(tmp_path / "state", "1.0.0")
    old = tmp_path / "state/rollback/agy/0.9.0/agy"
    old.parent.mkdir(parents=True)
    old.write_text("an older copy")
    proved.outcomes[("agy", "isolation")] = ("failed", True)
    rows = _update(tmp_path, proved, ("agy",))
    seen = fake_bin.updated("agy")
    assert seen is not None and seen["watched"] is True, "the copy must exist before agy update"
    assert copy.read_bytes() == (fake_bin.directory / "agy").read_bytes()
    assert os.access(copy, os.X_OK)
    assert not old.parent.exists(), "only the copy of the version replaced is kept"
    assert _row(rows, "agy").rollback_path == copy


def test_agy_is_not_updated_without_its_rollback_copy(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs
) -> None:
    fake_bin.install("agy", "an agy that prints no version number")
    rows = _update(tmp_path, proved, ("agy",))
    row = _row(rows, "agy")
    assert fake_bin.updated("agy") is None
    assert row.exit_code is None and row.note is not None and "no rollback copy" in row.note
    assert row.status == "not updated"
    assert proved.calls == []


def test_a_development_install_proves_nothing_and_says_so(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prove, "running_from_checkout", lambda: True)
    rows = _update(tmp_path, proved, ("opencode",))
    assert proved.calls == []
    row = _row(rows, "opencode")
    assert row.new_version == "2.0.0"
    assert row.note is not None and "not proven: development install" in row.note


def test_proving_is_announced_before_the_first_provider_run(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs
) -> None:
    _update(tmp_path, proved, ("codex", "opencode"))
    kinds = [event[0] for event in proved.events]
    announcement = next(
        index
        for index, event in enumerate(proved.events)
        if event[0] == "say" and "this spends provider tokens" in event[1]
    )
    assert announcement < kinds.index("prove")
    total = sum(prove.planned_runs(rail, kind) for rail, kind in proved.calls)  # type: ignore[arg-type]
    assert f"{total} provider runs" in proved.events[announcement][1]


def test_a_rail_that_is_not_installed_is_skipped_without_an_updater(
    tmp_path: Path, fake_bin: _Bin, proved: _Proofs
) -> None:
    (fake_bin.directory / "agy").unlink()
    rows = _update(tmp_path, proved)
    row = _row(rows, "agy")
    assert (row.old_version, row.exit_code, row.log) == (None, None, None)
    assert row.note is not None and row.note.startswith("not installed")
    assert row.status == "not installed"
    assert fake_bin.updated("claude") is not None
