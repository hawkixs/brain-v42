"""Update the provider CLIs, then re-prove what changed (spec 0.5.2 §3.4, ``ha providers --update``).

WHY A MODULE OF ITS OWN. Each CLI rail's vendor ships its own updater (measured
2026-09-27 from each ``--help``; no updater was run to measure them). The table could
have lived beside each rail in ``providers/*.py``, as the spec words it -- but those
files are the rails' fingerprinted isolation source (``proofs._ISOLATION_SOURCE_FILES``):
editing one would make every installed isolation proof of that rail stale. Here it
moves nothing.

TRUST CHAIN. Updating is not proving: this module may CALL
:func:`headless_agents.prove.prove`, which alone records a proof, and never records one
itself (pinned by a test).

ROLLBACK is reported, never performed (a spec non-goal), and a path is named only when
it exists at report time:

- claude keeps its previous versions (``~/.local/share/claude/versions/<v>``) and
  reinstalls one with ``claude install <v>``; Claude Code also updates itself, which may
  undo a rollback (spec Q2).
- codex keeps its previous releases (``~/.codex/packages/standalone/releases/<v>-*``);
  there is no command: ``~/.local/bin/codex`` is repointed at the kept release by hand.
- agy keeps nothing, so ``--update`` copies its binary aside first, to
  ``<state>/rollback/agy/<v>/agy`` (lot 4 plan, orchestrator default 3).
- opencode keeps nothing either, but reinstalls a version: ``opencode upgrade <v>``.

ORDER, and nothing else (:func:`run_updates`): take the global lock EXCLUSIVELY, so no
run executes while a binary changes (it honours ``--wait``); per rail, probe the
version, run the updater -- the exact path the probe measured, never a bare name --
and probe again, even after a failed updater; release the lock; only then prove the
rails whose version changed, so runs on the unchanged rails resume meanwhile, and runs
on an updated rail stay refused until its new version is proven: fail-closed by
construction. ``--check`` runs nothing and takes no lock: no vendor has a dry run
(measured), so it reports ``unknown``.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from . import locks, proof_state, prove
from .engine import UsageError, executable_for
from .registry import Probe, probe
from .state import ensure_dir


@dataclass(frozen=True)
class Updater:
    rail: str
    #: Appended to the executable path the probe measured -- never to a bare name.
    args: tuple[str, ...]
    #: A vendor dry run; ``None`` for all four today (measured): ``--check`` then
    #: reports ``unknown``. A vendor that gains one is a one-line change here.
    check_args: tuple[str, ...] | None


UPDATERS: Final[Mapping[str, Updater]] = {
    "claude": Updater("claude", ("update",), None),
    "codex": Updater("codex", ("update",), None),
    "agy": Updater("agy", ("update",), None),
    "opencode": Updater("opencode", ("upgrade",), None),
}

#: An updater downloads a release: ten minutes, then its whole process group is killed.
UPDATE_TIMEOUT_SECONDS: Final = 600.0

_SEMVER: Final = re.compile(r"\d+\.\d+\.\d+")


def semver(version: str | None) -> str | None:
    """The first ``X.Y.Z`` of a rail's ``--version`` line, or ``None``.

    Only this ever reaches a path or a command: digits and dots, nothing a vendor's
    output could turn into another directory.
    """
    if not version:
        return None
    match = _SEMVER.search(version)
    return match.group(0) if match else None


def agy_copy(state: Path, version: str) -> Path:
    """Where ``--update`` keeps the agy binary of ``version`` (a semver) before updating."""
    return state / "rollback" / "agy" / version / "agy"


def rollback(
    rail: str, old_version: str | None, home: Path, state: Path
) -> tuple[Path | None, str | None]:
    """What would return ``rail`` to ``old_version``: a path that exists now, and the
    vendor's command when it has one. ``(None, None)`` for a version that does not parse.
    """
    version = semver(old_version)
    if version is None:
        return None, None
    if rail == "claude":
        kept = home / ".local" / "share" / "claude" / "versions" / version
        return (kept if kept.exists() else None), f"claude install {version}"
    if rail == "codex":
        releases = home / ".codex" / "packages" / "standalone" / "releases"
        found = sorted(releases.glob(f"{version}-*/bin/codex"))
        # Two builds of one version (two architectures): naming one would be a guess.
        return (found[0] if len(found) == 1 else None), None
    if rail == "agy":
        copy = agy_copy(state, version)
        return (copy if copy.is_file() else None), None
    if rail == "opencode":
        return None, f"opencode upgrade {version}"
    return None, None


# ── run_updates ─────────────────────────────────────────────────────────────


#: What happened to a rail: ``checked`` (``--check``), ``not installed``, ``not updated``
#: (refused before its updater ran), ``updated``, ``unchanged``, ``failed`` (the updater
#: exited non-zero or did not start, or the rail was gone after it) or ``timed out``.
Status = Literal[
    "checked", "not installed", "not updated", "updated", "unchanged", "failed", "timed out"
]


@dataclass(frozen=True)
class UpdateRow:
    """One rail's update, as ``ha providers --update`` reports it."""

    rail: str
    status: Status
    old_version: str | None
    #: Probed after the updater, even a failed one; ``None`` under ``--check``.
    new_version: str | None
    #: The argv run -- or that would run, under ``--check``.
    updater: tuple[str, ...]
    #: ``None``: no updater ran, or it was killed at its timeout (``note`` says which).
    exit_code: int | None
    log: Path | None
    verdicts: tuple[prove.Verdict, ...]
    #: The mode the engine leaves the rail in now (``proof_state.rail_state``).
    mode: str
    rollback_path: Path | None
    rollback_command: str | None
    note: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "rail": self.rail,
            "status": self.status,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "updater": list(self.updater),
            "exit_code": self.exit_code,
            "log": None if self.log is None else str(self.log),
            "verdicts": [verdict.to_dict() for verdict in self.verdicts],
            "mode": self.mode,
            "rollback_path": None if self.rollback_path is None else str(self.rollback_path),
            "rollback_command": self.rollback_command,
            "note": self.note,
        }


@dataclass(frozen=True)
class _Attempt:
    rail: str
    status: Status
    old_version: str | None
    new_version: str | None
    updater: tuple[str, ...]
    exit_code: int | None
    log: Path | None
    note: str | None
    ran: bool

    @property
    def changed(self) -> bool:
        return self.new_version is not None and self.new_version != self.old_version


def _probe(rail: str, home: Path, environ: Mapping[str, str]) -> Probe:
    return probe(rail, executable=executable_for(rail, home), environ=environ)


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """The updater and everything it started: it leads its own process group."""
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5.0)


def _run_updater(
    argv: tuple[str, ...], log: Path, environ: Mapping[str, str]
) -> tuple[int | None, str | None, bool]:
    """The updater's exit code, or ``None`` and why, and whether it timed out; its
    output goes to ``log`` (0600)."""
    ensure_dir(log.parent)
    descriptor = os.open(
        log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        process = subprocess.Popen(  # nosec B603 - the probed path of a declared updater
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=descriptor,
            stderr=subprocess.STDOUT,
            env=dict(environ),
            start_new_session=True,
        )
    except OSError as exc:
        return None, f"the updater did not start: {exc}", False
    finally:
        os.close(descriptor)
    try:
        return process.wait(timeout=UPDATE_TIMEOUT_SECONDS), None, False
    except subprocess.TimeoutExpired:
        _kill_group(process)
        return (
            None,
            f"timed out after {UPDATE_TIMEOUT_SECONDS:g} s: its process group was killed",
            True,
        )
    except BaseException:
        # Ctrl-C: the updater dies with us, never left running under a released lock.
        _kill_group(process)
        raise


def _copy_agy_aside(executable: str, version: str | None, state: Path) -> str | None:
    """Keep the agy binary about to be replaced (orchestrator default 3); why not, or
    ``None`` once kept. Only the copy of the version being replaced is kept: agy is
    a 220 MB binary (measured)."""
    kept = semver(version)
    if kept is None:
        return f"no rollback copy: its version {version!r} does not parse"
    target = agy_copy(state, kept)
    temporary = target.with_name(f".agy.{os.getpid()}.tmp")
    try:
        ensure_dir(target.parent)
        shutil.copy2(executable, temporary)
        os.replace(temporary, target)
    except OSError as exc:
        with suppress(OSError):
            os.unlink(temporary)
        return f"no rollback copy: {exc}"
    for other in target.parent.parent.iterdir():
        if other != target.parent:
            shutil.rmtree(other, ignore_errors=True)
    return None


def _update_one(
    rail: str,
    *,
    logs: Path,
    state: Path,
    home: Path,
    environ: Mapping[str, str],
    say: Callable[[str], None],
) -> _Attempt:
    before = _probe(rail, home, environ)
    if not before.available:
        return _Attempt(
            rail=rail,
            status="not installed",
            old_version=None,
            new_version=None,
            updater=(),
            exit_code=None,
            log=None,
            note=f"not installed: {before.detail}",
            ran=False,
        )
    argv = (before.detail, *UPDATERS[rail].args)
    if rail == "agy":
        refusal = _copy_agy_aside(before.detail, before.version, state)
        if refusal is not None:
            return _Attempt(
                rail=rail,
                status="not updated",
                old_version=before.version,
                new_version=before.version,
                updater=argv,
                exit_code=None,
                log=None,
                note=f"not updated: {refusal}",
                ran=False,
            )
    say(f"updating {rail} ({before.version or 'version unknown'}): {' '.join(argv)}")
    log = logs / f"{rail}.log"
    exit_code, note, timed_out = _run_updater(argv, log, environ)
    after = _probe(rail, home, environ)
    if not after.available:
        gone = f"unavailable after the update: {after.detail}"
        note = gone if note is None else f"{note}; {gone}"
    status: Status
    if timed_out:
        status = "timed out"
    elif exit_code != 0 or not after.available:
        status = "failed"
    else:
        status = "updated" if after.version != before.version else "unchanged"
    return _Attempt(
        rail=rail,
        status=status,
        old_version=before.version,
        new_version=after.version if after.available else None,
        updater=argv,
        exit_code=exit_code,
        log=log,
        note=note,
        ran=True,
    )


def _update_all(
    rails: Sequence[str],
    *,
    state: Path,
    home: Path,
    environ: Mapping[str, str],
    wait: locks.AdmissionWait,
    say: Callable[[str], None],
) -> list[_Attempt]:
    logs = (
        state
        / "updates"
        / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    )
    with ExitStack() as held:
        try:
            held.enter_context(locks.admit_global(state, exclusive=True, wait=wait))
        except locks.LockTimeout:
            if wait.seconds is not None:
                raise UsageError(
                    f"--wait {wait.seconds:g} s expired: runs still running; nothing updated"
                ) from None
            raise UsageError(
                "runs still running after the bound: an update waits for none of them; "
                "nothing updated"
            ) from None
        return [
            _update_one(rail, logs=logs, state=state, home=home, environ=environ, say=say)
            for rail in rails
        ]


def _prove_changed(
    changed: Sequence[_Attempt],
    *,
    state: Path,
    home: Path,
    environ: Mapping[str, str],
    models: Mapping[str, str],
    say: Callable[[str], None],
) -> dict[str, list[prove.Verdict]]:
    """Prove each changed rail, after the lock is released: isolation, then confinement
    where it can be proven. Announced before the first provider run."""
    pairs = [
        (attempt.rail, kind, attempt.new_version)
        for attempt in changed
        for kind in prove.KINDS
        if not (kind == "confinement" and attempt.rail in proof_state.UNPROVABLE_CONFINEMENT)
    ]
    for rail, kind, version in pairs:
        model = f"model {models[rail]}" if models.get(rail) else f"the model {rail} chooses"
        say(
            f"prove {rail} {kind}: {prove.planned_runs(rail, kind)} provider runs on "
            f"{version} ({model})"
        )
    total = sum(prove.planned_runs(rail, kind) for rail, kind, _ in pairs)
    say(
        f"proving {len(changed)} rails whose version changed: {total} provider runs; "
        "this spends provider tokens"
    )
    verdicts: dict[str, list[prove.Verdict]] = {}
    root = prove.proof_root(home)
    try:
        for rail, kind, _ in pairs:
            verdicts.setdefault(rail, []).append(
                prove.prove(
                    rail,
                    kind,
                    model=models.get(rail, ""),
                    state=state,
                    home=home,
                    environ=environ,
                    root=root,
                )
            )
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return verdicts


def _settled(verdicts: Sequence[prove.Verdict]) -> bool:
    return all(verdict.outcome == "passed" and verdict.recorded for verdict in verdicts)


def run_updates(
    rails: Sequence[str],
    *,
    state: Path,
    home: Path,
    environ: Mapping[str, str],
    wait: locks.AdmissionWait,
    check: bool,
    prove_after: bool,
    models: Mapping[str, str],
    say: Callable[[str], None],
) -> list[UpdateRow]:
    """Update ``rails`` and re-prove the ones whose version changed (see the module
    docstring for the order). ``models`` gives each rail the model its proofs run on.
    Refuses (:class:`UsageError`) before any updater runs when the global lock is not
    obtained within ``wait``.
    """
    if check:
        rows = []
        for rail in rails:
            found = _probe(rail, home, environ)
            version = found.version if found.available else None
            path, command = rollback(rail, version, home, state)
            rows.append(
                UpdateRow(
                    rail=rail,
                    status="checked" if found.available else "not installed",
                    old_version=version,
                    new_version=None,
                    updater=(found.detail, *UPDATERS[rail].args) if found.available else (),
                    exit_code=None,
                    log=None,
                    verdicts=(),
                    mode=proof_state.rail_state(state, rail, version).mode,
                    rollback_path=path,
                    rollback_command=command,
                    note="update available: unknown (no vendor dry run)"
                    if found.available
                    else f"not installed: {found.detail}",
                )
            )
        return rows

    attempts = _update_all(rails, state=state, home=home, environ=environ, wait=wait, say=say)
    changed = [attempt for attempt in attempts if attempt.changed]
    verdicts: dict[str, list[prove.Verdict]] = {}
    unproven: str | None = None
    if changed and prove_after:
        refusal = prove.checkout_refusal(environ)
        if refusal is None:
            verdicts = _prove_changed(
                changed, state=state, home=home, environ=environ, models=models, say=say
            )
        else:
            unproven = f"not proven: development install ({refusal})"

    rows = []
    for attempt in attempts:
        # The mode on the version installed NOW, the one the engine will probe.
        now = _probe(attempt.rail, home, environ)
        mode = proof_state.rail_state(
            state, attempt.rail, now.version if now.available else None
        ).mode
        proven = tuple(verdicts.get(attempt.rail, ()))
        note = attempt.note
        if attempt.changed and unproven is not None:
            note = unproven if note is None else f"{note}; {unproven}"
        failed = (
            (attempt.ran and attempt.exit_code != 0)
            or (attempt.ran and attempt.new_version is None)
            or not _settled(proven)
        )
        path, command = (
            rollback(attempt.rail, attempt.old_version, home, state) if failed else (None, None)
        )
        rows.append(
            UpdateRow(
                rail=attempt.rail,
                status=attempt.status,
                old_version=attempt.old_version,
                new_version=attempt.new_version,
                updater=attempt.updater,
                exit_code=attempt.exit_code,
                log=attempt.log,
                verdicts=proven,
                mode=mode,
                rollback_path=path,
                rollback_command=command,
                note=note,
            )
        )
    return rows


__all__ = [
    "UPDATERS",
    "UPDATE_TIMEOUT_SECONDS",
    "Status",
    "UpdateRow",
    "Updater",
    "agy_copy",
    "rollback",
    "run_updates",
    "semver",
]
