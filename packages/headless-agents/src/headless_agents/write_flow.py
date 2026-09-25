"""A write, from admission to publication (spec 0.5.0 §3.8.3).

:func:`run_write_step` runs the nine steps of §3.8.3 for a role write run,
each a function of its own, in this order:

1. admission, without git -- the lineage registry lock, then the lineage
   locks in ascending owner order, then every state check under them;
2. intent -- the new lineage's state, with its pending write, created before
   any mutation; the registry lock released;
3. preparation -- the only git before the provider, hooks off;
4. start point -- the branch tip, recorded after preparation;
5. the step -- the role's chain, on a writable worktree, the tripwire armed;
6. tripwire -- fired: quarantine, lineage compromised, no git at all;
7. agent commits -- a moved ``HEAD`` or branch tip: every new commit
   recorded ``made_by: agent``, lineage compromised;
8. engine commit -- the repository's hooks run for it only; every commit
   that appeared is attributed whatever ``git commit`` returned;
9. publication -- provenance, then the lineage state in one rename (the
   single point where the write becomes final).

The caller (:func:`headless_agents.engine.execute`) holds the lifecycle lock
and the unconfined lock (§3.8.2) around the whole call and writes the report.
:func:`_crash_after` is called between steps: tests inject a crash there.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from . import lineage as lineages
from . import locks, provenance, quarantine
from .git_tripwire import Tripwire, resolve_git_dir
from .gitops import git
from .lineage import LineageState, PendingWrite
from .locks import LockTimeout, Rank, held, is_free
from .profile import Workspace
from .provenance import MadeBy
from .repo import RepoIdentity
from .result import RunResult
from .state import Unknown, publish, read_optional

if TYPE_CHECKING:
    from .engine import Plan

#: The exit code of a write that changed nothing (0.4.0, unchanged).
NO_CHANGE_EXIT_CODE: Final = 5
COMMIT_LOG: Final = "commit.log"
PATCH_FILE: Final = "change.patch"
UNCONFINED_INTENT: Final = "unconfined-intent.json"
UNCONFINED_WRITERS: Final = "unconfined-writers.json"


class WriteRefused(Exception):  # noqa: N818 - a refusal, not a crash
    """Admission refused the write: exit ``2``, nothing ran, no git."""


@dataclass(frozen=True)
class WriteOutcome:
    exit_code: int
    status: str
    failure_reason: str | None
    commits: tuple[tuple[str, MadeBy], ...]
    branch: str | None
    base: str | None
    head: str | None
    final: RunResult | None


RunLinks = Callable[[Workspace, Path], RunResult]


def _crash_after(step: str) -> None:
    """A test hook: monkeypatched to raise between two steps; does nothing in production."""


@dataclass
class _Write:
    """What one write knows, step after step."""

    plan: Plan
    run_id: str
    run_dir: Path
    state: Path
    identity: RepoIdentity
    start: Path
    unconfined: bool
    say: Callable[[str], None]
    locks: ExitStack
    worktree: Path = field(init=False)
    branch: str = field(init=False)
    git_dir: Path | None = None
    reflog_start: tuple[bytes | None, ...] = ()
    lineage: LineageState | None = None

    def __post_init__(self) -> None:
        self.worktree = self.run_dir / "wt"
        self.branch = f"ha/{self.run_id}"

    @property
    def environ(self) -> Mapping[str, str]:
        return self.plan.environment

    def git(self, root: Path, args: Sequence[str], *, hooks: bool = False) -> tuple[int, str, str]:
        result = git(root, args, self.environ, state=self.state, hooks=hooks)
        return result.returncode, result.stdout, result.stderr

    def save(self, lineage: LineageState) -> None:
        self.lineage = lineage
        lineages.save(self.state, lineage)

    @property
    def current(self) -> LineageState:
        assert self.lineage is not None, "the intent creates the lineage"
        return self.lineage


# ── step 1: admission, without git ─────────────────────────────────────────


def _inside(path: Path, root: Path) -> bool:
    normal = os.path.normpath(os.path.abspath(path))
    top = os.path.normpath(os.path.abspath(root))
    return normal == top or normal.startswith(top.rstrip(os.sep) + os.sep)


def _sources(state: Path, start: Path) -> list[str]:
    """Every lineage whose recorded worktree contains ``start`` (§3.8.2)."""
    found = []
    for owner in lineages.owners(state):
        try:
            recorded = lineages.load(state, owner).worktree
        except Unknown:
            continue
        if _inside(start, recorded):
            found.append(owner)
    return found


def check_unconfined_intent(state: Path, run_id: str) -> None:
    """An intent found by a holder of the unconfined lock has lost its writer (§3.8.2)."""
    path = state / UNCONFINED_INTENT
    if not path.exists() and not path.is_symlink():
        return
    try:
        document = read_optional(path) or {}
        named = document.get("run_id")
    except Unknown:
        named = None
    quarantine.publish(
        state,
        "operator",
        reason="stale_unconfined_intent",
        run_id=str(named) if isinstance(named, str) else run_id,
        paths=[str(path)],
        common_dir=None,
    )
    raise WriteRefused(
        f"a stale unconfined intent ({path}) names a dead unconfined write: operator "
        "quarantine published; nothing ran"
    )


def check_repository(state: Path, common: Path, *, own: str) -> None:
    """No lineage of the repository unknown, and no stale pending write in any (§3.8.3 step 1).

    A stale pending write compromises its lineage (``unfinalized_write``) and
    publishes the repository quarantine -- the operator's too when that write
    was unconfined. ``own`` is skipped: the caller holds its lock.
    """
    for owner in lineages.of_repository(state, common):
        if owner == own:
            continue
        try:
            other = lineages.load(state, owner)
        except Unknown as exc:
            raise WriteRefused(f"lineage {owner} is unknown ({exc}); nothing ran") from None
        if other.pending is not None and is_free(lineages.lineage_lock(state, owner)):
            lineages.save(state, replace(other, compromised="unfinalized_write"))
            quarantine.publish(
                state,
                "repository",
                reason="unfinalized_write",
                run_id=other.pending.run_id,
                paths=[],
                common_dir=common,
            )
            if other.pending.unconfined:
                quarantine.publish(
                    state,
                    "operator",
                    reason="unfinalized_write",
                    run_id=other.pending.run_id,
                    paths=[],
                    common_dir=None,
                )
            raise WriteRefused(
                f"lineage {owner} holds the unfinished write of run {other.pending.run_id}: "
                "it is compromised (unfinalized_write) and the repository quarantined; "
                "nothing ran"
            )


def _check_sources(state: Path, sources: Sequence[str]) -> None:
    """Every source lineage (§3.8.2) known, not compromised, with no pending write."""
    for owner in sources:
        try:
            source = lineages.load(state, owner)
        except Unknown as exc:
            raise WriteRefused(f"lineage {owner} is unknown ({exc}); nothing ran") from None
        if source.compromised is not None:
            raise WriteRefused(
                f"--repo is inside lineage {owner}, compromised ({source.compromised}); nothing ran"
            )
        if source.pending is not None:
            raise WriteRefused(
                f"--repo is inside lineage {owner}, which holds a pending write; nothing ran"
            )


def _admit(write: _Write, registry: ExitStack) -> None:
    """Step 1: every lock first, in the §3.8.2 order, then every state check under them."""
    state = write.state
    registry.enter_context(
        held(
            lineages.registry_lock(state),
            rank=Rank.LINEAGE_REGISTRY,
            exclusive=True,
            wait=locks.LOCK_WAIT_SECONDS,
            what="the lineage registry lock",
        )
    )
    sources = _sources(state, write.start)
    for owner in sorted({write.run_id, *sources}):
        own = owner == write.run_id
        write.locks.enter_context(
            held(
                lineages.lineage_lock(state, owner),
                rank=Rank.LINEAGE,
                exclusive=own,
                wait=locks.LOCK_WAIT_SECONDS,
                what=f"the lineage lock of {owner}",
                key=owner,
            )
        )
    refusal = quarantine.check(state, write.identity.common_dir)
    if refusal is not None:
        raise WriteRefused(f"{refusal}; nothing ran")
    check_unconfined_intent(state, write.run_id)
    check_repository(state, write.identity.common_dir, own=write.run_id)
    _check_sources(state, sources)


# ── step 2: intent ─────────────────────────────────────────────────────────


def _intent(write: _Write) -> None:
    providers = write.plan.role.providers
    write.lineage = LineageState(
        owner=write.run_id,
        repository=write.identity.work_tree,
        common_dir=write.identity.common_dir,
        worktree=write.worktree,
        branch=write.branch,
        base=None,
        members={write.run_id: "running"},
        pending=PendingWrite(
            run_id=write.run_id,
            providers=tuple(providers),
            unconfined=write.unconfined,
            start_tip=None,
            start_reflog=None,
        ),
        compromised=None,
    )
    lineages.create(write.state, write.lineage)
    if write.unconfined:
        _publish_unconfined_intent(write)


def _publish_unconfined_intent(write: _Write) -> None:
    """``unconfined-intent.json``, and this write appended to ``unconfined-writers.json``.

    The intent exists only while its writer holds the unconfined lock
    exclusively; the writers' list is never cleared by ``ha`` (§3.8.4).
    """
    record: dict[str, object] = {
        "run_id": write.run_id,
        "repository": str(write.identity.work_tree),
        "providers": list(write.plan.role.providers),
    }
    publish(write.state / UNCONFINED_INTENT, record)
    path = write.state / UNCONFINED_WRITERS
    try:
        document = read_optional(path) or {"writers": []}
    except Unknown:
        document = {"writers": [], "unreadable_before": True}
    writers = document.get("writers")
    listed = list(writers) if isinstance(writers, list) else []
    publish(path, {**document, "writers": [*listed, record]})


# ── step 3: preparation ────────────────────────────────────────────────────


class _PreparationFailed(Exception):
    pass


def _prepare(write: _Write) -> str:
    """Resolve the base and add the worktree; the base commit."""
    base_ref = write.plan.request.base or "HEAD"
    repository = write.identity.work_tree
    code, out, err = write.git(repository, ["rev-parse", "--verify", f"{base_ref}^{{commit}}"])
    if code != 0:
        raise _PreparationFailed(f"cannot resolve --base {base_ref!r}: {err.strip()}")
    base = out.strip()
    code, _, err = write.git(
        repository, ["worktree", "add", "-q", "-b", write.branch, str(write.worktree), base]
    )
    if code != 0:
        raise _PreparationFailed(f"git worktree add failed: {err.strip()}")
    write.git_dir = resolve_git_dir(write.worktree)
    write.save(replace(write.current, base=base))
    return base


def _tip(write: _Write) -> str | None:
    code, out, _ = write.git(
        write.worktree, ["rev-parse", "--verify", f"refs/heads/{write.branch}"]
    )
    return out.strip() if code == 0 else None


def _head(write: _Write) -> str | None:
    code, out, _ = write.git(write.worktree, ["rev-parse", "--verify", "HEAD"])
    return out.strip() if code == 0 else None


# ── step 4: start point ────────────────────────────────────────────────────


def _reflog_files(write: _Write) -> tuple[Path, ...]:
    """The worktree's ``HEAD`` log and the branch's log, where git appends every move.

    Read as files, not through ``git reflog``: an emptied ``HEAD`` log makes
    ``git reflog show HEAD`` of a linked worktree fall back to another log,
    so a count through git cannot tell a rewrite (codex review of #208, r2).
    """
    git_dir = write.git_dir or write.worktree / ".git"
    return (
        git_dir / "logs" / "HEAD",
        write.identity.common_dir / "logs" / "refs" / "heads" / write.branch,
    )


def _read_log(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _start_point(write: _Write, tip: str) -> None:
    """Step 4: the branch tip and, for an unconfined write, the reflogs' start.

    The logs' bytes are kept for step 7 (they only ever grow); the lineage
    records the ``HEAD`` log's length.
    """
    pending = write.current.pending
    assert pending is not None, "the intent records the pending write"
    start_reflog: int | None = None
    if write.unconfined:
        write.reflog_start = tuple(_read_log(path) for path in _reflog_files(write))
        head_log = write.reflog_start[0]
        start_reflog = len(head_log) if head_log is not None else None
    write.save(
        replace(
            write.current,
            pending=replace(pending, start_tip=tip, start_reflog=start_reflog),
        )
    )


def _reflog_gained(write: _Write, tip: str) -> tuple[bool, bool, list[str]]:
    """``(moved, rewritten, commits)``: what the reflogs gained since the start point.

    ``rewritten`` when a log is gone, unreadable, or no longer begins with the
    bytes it held at the start point: commits may have appeared that no
    witness can name. ``commits`` are the new object ids of the appended
    entries, oldest first.
    """
    commits: list[str] = []
    for path, before in zip(_reflog_files(write), write.reflog_start, strict=True):
        now = _read_log(path)
        if before is None or now is None or not now.startswith(before):
            return True, True, []
        for line in now[len(before) :].decode("utf-8", "replace").splitlines():
            fields = line.split(" ", 2)
            if len(fields) >= 2 and fields[1] != tip and fields[1] not in commits:
                commits.append(fields[1])
    return bool(commits), False, commits


# ── steps 6 to 8 ───────────────────────────────────────────────────────────


def _new_commits(write: _Write, start: str, tips: Sequence[str | None]) -> list[str]:
    """Every commit reachable from ``tips`` and not from ``start``, oldest first."""
    found: list[str] = []
    for tip in dict.fromkeys(t for t in tips if t is not None and t != start):
        code, out, _ = write.git(write.worktree, ["rev-list", "--reverse", f"{start}..{tip}"])
        for sha in out.split() if code == 0 else [tip]:
            if sha not in found:
                found.append(sha)
    return found


def _branch_reflog(write: _Write) -> list[tuple[str, str]]:
    """``(sha, message)`` of the branch's reflog, oldest first."""
    code, out, _ = write.git(
        write.worktree, ["reflog", "show", "--format=%H %gs", f"refs/heads/{write.branch}"]
    )
    if code != 0:
        return []
    entries = []
    for line in reversed(out.splitlines()):
        sha, _, message = line.partition(" ")
        entries.append((sha, message))
    return entries


def _commit(
    write: _Write, message: str, start: str, step_dir: Path
) -> tuple[str | None, list[tuple[str, MadeBy]], str | None]:
    """Step 8: ``(failure_reason, commits, head)`` of the engine's commit."""
    before = len(_branch_reflog(write))
    code, out, err = write.git(write.worktree, ["add", "-A"])
    if code == 0:
        code, out, err = write.git(write.worktree, ["commit", "-q", "-m", message], hooks=True)
    step_dir.mkdir(parents=True, exist_ok=True)
    (step_dir / COMMIT_LOG).write_text(out + err, encoding="utf-8", errors="replace")
    tip, head = _tip(write), _head(write)
    new_entries = _branch_reflog(write)[before:]
    engine_sha = next(
        (sha for sha, entry in new_entries if entry == f"commit: {message.splitlines()[0]}"),
        None,
    )
    shas = _new_commits(write, start, [tip, head])
    for sha, _ in new_entries:
        if sha not in shas and sha != start:
            shas.append(sha)
    commits: list[tuple[str, MadeBy]] = [
        (sha, "engine" if sha == engine_sha else "hook") for sha in shas
    ]
    if engine_sha is not None and engine_sha not in shas:
        commits.insert(0, (engine_sha, "engine"))
    if any(made_by == "hook" for _, made_by in commits):
        return "hook_committed", commits, head
    if code != 0 or engine_sha is None:
        return "hook_refused", commits, head
    return None, commits, head


# ── step 9: publication ────────────────────────────────────────────────────


def _publish(
    write: _Write,
    *,
    status: str,
    commits: Sequence[tuple[str, MadeBy]],
    compromised: str | None,
) -> None:
    # Every commit carries every link of the role: the engine's commit holds the
    # agent's work, and the vendor rule reads it from here (§3.8.4 step 4, §3.10).
    providers = write.plan.role.providers
    for sha, made_by in commits:
        try:
            provenance.record(
                write.state,
                sha,
                run_id=write.run_id,
                lineage=write.run_id,
                made_by=made_by,
                providers=providers,
            )
        except FileExistsError:
            pass
    current = write.current
    write.save(
        replace(
            current,
            members={**current.members, write.run_id: status},
            pending=None,
            compromised=compromised or current.compromised,
        )
    )
    _crash_after("lineage_published")
    if write.unconfined:
        (write.state / UNCONFINED_INTENT).unlink(missing_ok=True)


def _compromise(write: _Write, reason: str) -> None:
    """The lineage compromised, its pending write LEFT in place (steps 3 and 6)."""
    current = write.current
    write.save(
        replace(current, compromised=reason, members={**current.members, write.run_id: "failed"})
    )


def _tampered(result: RunResult, tripwire: Tripwire | None) -> tuple[str, ...]:
    raw = (result.workspace or {}).get("git_tampered")
    reported = tuple(str(path) for path in raw) if isinstance(raw, list) else ()
    own = tripwire.tampered() if tripwire is not None else ()
    return tuple(sorted(set(reported) | set(own)))


def _outcome(
    code: int,
    status: str,
    reason: str | None,
    write: _Write,
    *,
    commits: Sequence[tuple[str, MadeBy]] = (),
    head: str | None = None,
    final: RunResult | None = None,
) -> WriteOutcome:
    base = write.lineage.base if write.lineage is not None else None
    return WriteOutcome(
        exit_code=code,
        status=status,
        failure_reason=reason,
        commits=tuple(commits),
        branch=write.branch,
        base=base,
        head=head,
        final=final,
    )


def run_write_step(
    plan: Plan,
    *,
    run_id: str,
    run_dir: Path,
    state: Path,
    identity: RepoIdentity,
    start: Path,
    step_dir: Path,
    run_links: RunLinks,
    say: Callable[[str], None],
    unconfined: bool = False,
) -> WriteOutcome:
    """§3.8.3 for one role write run; :class:`WriteRefused` when admission refuses."""
    with ExitStack() as locks:
        write = _Write(
            plan=plan,
            run_id=run_id,
            run_dir=run_dir,
            state=state,
            identity=identity,
            start=start,
            unconfined=unconfined,
            say=say,
            locks=locks,
        )
        with ExitStack() as registry:
            try:
                _admit(write, registry)
            except LockTimeout as exc:
                raise WriteRefused(f"{exc}; nothing ran") from None
            _intent(write)
            _crash_after("intent")
        # The registry lock is released: the lineage exists with its intent.
        try:
            base = _prepare(write)
        except _PreparationFailed as exc:
            _publish(write, status="failed", commits=(), compromised=None)
            say(f"{exc}; nothing ran")
            return _outcome(1, "failed", "preparation_failed", write)
        _crash_after("preparation")
        tip = _tip(write)
        if tip != base:
            _compromise(write, "preparation_moved_head")
            say(f"the branch {write.branch} is not at the base after preparation")
            return _outcome(1, "failed", "preparation_moved_head", write, head=tip)
        _start_point(write, tip)
        _crash_after("start_point")

        # nosec B604: ``shell`` is a Workspace capability flag, not a subprocess argument.
        workspace = Workspace(path=write.worktree, write=True, shell=plan.role.shell)  # nosec B604
        tripwire = Tripwire.arm(workspace, home=plan.request.home, environ=plan.environment)
        final = run_links(workspace, step_dir)
        _crash_after("step")

        tampered = _tampered(final, tripwire)
        if tampered:
            scope = quarantine.widest_scope(
                tampered,
                worktree=write.worktree,
                git_dir=write.git_dir or write.worktree / ".git",
                common_dir=identity.common_dir,
                home=plan.request.home,
                environ=plan.environment,
            )
            if scope != "lineage":
                quarantine.publish(
                    state,
                    scope,
                    reason="tripwire",
                    run_id=run_id,
                    paths=tampered,
                    common_dir=identity.common_dir,
                )
            _compromise(write, "tripwire")
            for path in tampered:
                say(f"git tripwire: {path} changed during the run")
            say(
                f"no git command was run; the worktree is kept for inspection at "
                f"{write.worktree}. Do not run git inside it."
            )
            return _outcome(1, "failed", "tripwire", write, final=final)

        head, moved_tip = _head(write), _tip(write)
        reflog_moved, rewritten, reflog_commits = (
            _reflog_gained(write, tip) if unconfined else (False, False, [])
        )
        if rewritten:
            # No witness can name what appeared: never published as final. The
            # pending write and the intent stay, so the next admission finds
            # them stale and quarantines the operator (§3.8.3 steps 1 and 6).
            found = _new_commits(write, tip, [head, moved_tip])
            for sha in found:
                try:
                    provenance.record(
                        state,
                        sha,
                        run_id=run_id,
                        lineage=run_id,
                        made_by="agent",
                        providers=plan.role.providers,
                    )
                except FileExistsError:
                    pass
            _compromise(write, "reflog_rewritten")
            say(
                "the worktree's reflog was rewritten during an unconfined write: the lineage "
                "is compromised and left uncertain; recover it by hand"
            )
            commits_found: list[tuple[str, MadeBy]] = [(sha, "agent") for sha in found]
            return _outcome(
                1,
                "failed",
                "reflog_rewritten",
                write,
                commits=commits_found,
                head=head,
                final=final,
            )
        if head != tip or moved_tip != tip or reflog_moved:
            found = _new_commits(write, tip, [head, moved_tip])
            found += [sha for sha in reflog_commits if sha not in found]
            commits: list[tuple[str, MadeBy]] = [(sha, "agent") for sha in found]
            _publish(write, status="failed", commits=commits, compromised="agent_moved_head")
            say("the agent moved HEAD or the branch: nothing committed, the lineage compromised")
            return _outcome(
                1, "failed", "agent_moved_head", write, commits=commits, head=head, final=final
            )

        code, out, err = write.git(write.worktree, ["status", "--porcelain"])
        if code != 0:
            _compromise(write, "status_failed")
            say(f"git status failed in {write.worktree}: {err.strip()}")
            return _outcome(1, "failed", "status_failed", write, head=head, final=final)
        failed_step = final.exit_code != 0
        if not out.strip():
            status = "failed" if failed_step else "no_change"
            _publish(write, status=status, commits=(), compromised=None)
            if failed_step:
                return _outcome(1, "failed", "step_failed", write, head=head, final=final)
            say(f"no change in {write.worktree}; nothing committed")
            return _outcome(NO_CHANGE_EXIT_CODE, "no_change", None, write, head=head, final=final)

        model = final.model_reported or final.model or "unknown"
        verb = "residue" if failed_step else "implement"
        message = f"chore(ha): {run_id} {verb} via {final.provider}/{model}"
        reason, commits, head = _commit(write, message, tip, step_dir)
        _crash_after("commit")
        if reason is not None:
            _publish(write, status="failed", commits=commits, compromised=reason)
            say(
                f"the commit was refused or a hook committed ({reason}): see "
                f"{step_dir / COMMIT_LOG}; the lineage is compromised"
            )
            return _outcome(1, "failed", reason, write, commits=commits, head=head, final=final)
        # The patch before the publication: after the lineage rename only the report is
        # left to rebuild (§3.8.3 step 9), and change.patch cannot be rebuilt from it.
        code, patch, _ = write.git(write.worktree, ["diff", "--binary", base, "HEAD"])
        (run_dir / PATCH_FILE).write_text(patch, encoding="utf-8", errors="replace")
        status = "failed" if failed_step else "committed"
        _publish(write, status=status, commits=commits, compromised=None)
        if failed_step:
            return _outcome(
                1, "failed", "step_failed", write, commits=commits, head=head, final=final
            )
        return _outcome(0, "committed", None, write, commits=commits, head=head, final=final)


# ── ha clean of a write run (plan Task 21) ────────────────────────────────


def clean_write(
    *,
    run_id: str,
    run_dir: Path,
    owner: str,
    state: Path,
    environ: Mapping[str, str],
    say: Callable[[str], None],
    forget: Callable[[], None],
    cleaned: Callable[[], None],
) -> int:
    """``ha clean`` of a lineage member, under the §3.8.2 locks (spec §3.9).

    The caller holds the run's lifecycle lock and the unconfined lock shared.
    Here: the lineage registry lock shared, then the lineage lock exclusive;
    under them, the admission checks of §3.8.3 step 1; the registry lock
    released before the first git command. No git at all -- exit ``1``,
    naming the reason -- when a quarantine covers the repository or the
    operator, or the lineage is unknown, compromised or holds a pending
    write. Otherwise the worktree is removed, the run dir deleted, the branch,
    the lineage state and the provenance kept. A run whose lineage state was
    never written and whose directory holds no worktree never started: it is
    forgotten.
    """
    with ExitStack() as held_locks:
        with ExitStack() as registry:
            registry.enter_context(
                held(
                    lineages.registry_lock(state),
                    rank=Rank.LINEAGE_REGISTRY,
                    exclusive=False,
                    wait=locks.LOCK_WAIT_SECONDS,
                    what="the lineage registry lock",
                )
            )
            held_locks.enter_context(
                held(
                    lineages.lineage_lock(state, owner),
                    rank=Rank.LINEAGE,
                    exclusive=True,
                    wait=locks.LOCK_WAIT_SECONDS,
                    what=f"the lineage lock of {owner}",
                    key=owner,
                )
            )
            path = lineages.lineage_path(state, owner)
            if not path.exists() and not path.is_symlink() and not (run_dir / "wt").exists():
                shutil.rmtree(run_dir, ignore_errors=True)
                forget()
                say(f"{run_id} never started: forgotten")
                return 0
            try:
                current = lineages.load(state, owner)
                refusal = quarantine.check(state, current.common_dir)
                if refusal is not None:
                    raise WriteRefused(refusal)
                check_unconfined_intent(state, run_id)
                check_repository(state, current.common_dir, own=owner)
            except (Unknown, WriteRefused) as exc:
                say(f"{exc}: nothing cleaned, no git command run")
                return 1
            if current.compromised is not None or current.pending is not None:
                reason = current.compromised or "a pending write"
                say(
                    f"lineage {owner} is uncertain ({reason}): nothing cleaned, no git command "
                    "run; inspect it and recover it by hand"
                )
                return 1
        # The registry lock is released: the first git command comes now.
        if current.worktree.exists():
            result = git(
                current.repository,
                ["worktree", "remove", "--force", str(current.worktree)],
                environ,
                state=state,
            )
            if result.returncode != 0:
                say(f"git worktree remove failed: {result.stderr.strip()}; nothing cleaned")
                return 1
        shutil.rmtree(run_dir, ignore_errors=True)
        cleaned()
        say(f"{run_id} cleaned: worktree removed; branch {current.branch} kept")
        return 0


__all__ = [
    "COMMIT_LOG",
    "NO_CHANGE_EXIT_CODE",
    "PATCH_FILE",
    "UNCONFINED_INTENT",
    "UNCONFINED_WRITERS",
    "WriteOutcome",
    "WriteRefused",
    "check_repository",
    "check_unconfined_intent",
    "clean_write",
    "run_write_step",
]
