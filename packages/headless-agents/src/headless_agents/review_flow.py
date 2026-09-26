"""A review's state and git, from its locks to its cleanup (spec 0.5.0 §3.5, §3.8.4, §3.8.6).

:func:`prepare` runs steps 1-6 of §3.8.4 and step 1 of §3.5, in this order:

1. locks, without git -- the lineage registry lock shared, then the lock of
   every lineage of the repository (and of any lineage whose worktree holds
   ``--repo``) shared, in ascending owner order; the quarantines and a stale
   unconfined intent checked under them, before any git;
2. uncertainty refuses -- a lineage unknown, compromised, or holding a pending
   write; a pending write seen under its lock held shared has lost its writer
   (a live writer holds it exclusively), so it is handled as §3.8.5 says;
3. the head pinned -- ``--run``'s lineage tip and base, or ``--head`` and
   ``--base`` (default ``origin/HEAD``); then the merge base;
4. every commit of ``<merge-base>..<head>`` attributed (:mod:`.vendor`);
5. independence checked for every reviewer role;
6. the check written once; the lineage and registry locks released -- the
   caller keeps the lifecycle and unconfined locks to the end (§3.8.2) --
   then ``change.patch`` and the detached worktree on the pinned head.

:func:`finish` writes the result once, when a verdict was read, **before**
the cleanup, so a failed cleanup never loses a verdict (§3.8.4 step 6).
Every git command runs through :func:`headless_agents.gitops.git`, hooks off.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from . import lineage as lineages
from . import locks, quarantine, reviews
from .git_tripwire import GitTampered
from .gitops import git
from .lineage import LineageState
from .locks import LockTimeout, Rank, held
from .repo import RepoIdentity
from .state import Unknown
from .templates import Verdict
from .vendor import VendorCheck, VendorRefused, attribute, check_independence
from .write_flow import (
    PATCH_FILE,
    WriteRefused,
    check_unconfined_intent,
    source_lineages,
    unfinalized,
)

#: The exit code of a review whose verdict asks for changes (§3.5, §3.9).
CHANGES_EXIT_CODE: Final = 6
#: A review's base when neither ``--base`` nor ``--run`` names one (§3.5).
DEFAULT_BASE: Final = "origin/HEAD"
WORKTREE: Final = "wt"


class ReviewRefused(Exception):  # noqa: N818 - a refusal, not a crash
    """The review cannot start: exit ``2``, no agent ran."""


@dataclass(frozen=True)
class Prepared:
    """What a review reads, pinned before its reviewers start."""

    head: str
    merge_base: str
    patch: str
    check: VendorCheck
    worktree: Path


def _git(
    identity: RepoIdentity, args: Sequence[str], environ: Mapping[str, str], state: Path
) -> tuple[int, str, str]:
    try:
        result = git(identity.work_tree, args, environ, state=state)
    except GitTampered as exc:
        raise ReviewRefused(f"{exc}; nothing ran") from None
    return result.returncode, result.stdout, result.stderr


def _resolve(
    identity: RepoIdentity, ref: str, environ: Mapping[str, str], state: Path
) -> str | None:
    code, out, _ = _git(
        identity, ["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], environ, state
    )
    return out.strip() if code == 0 and out.strip() else None


def _admitted(state: Path, owners: Sequence[str]) -> dict[str, LineageState]:
    """Every lineage, known, not compromised, holding no pending write (§3.8.4 step 2)."""
    found: dict[str, LineageState] = {}
    for owner in owners:
        try:
            current = lineages.load(state, owner)
        except Unknown as exc:
            raise ReviewRefused(f"lineage {owner} is unknown ({exc}); nothing ran") from None
        if current.compromised is not None:
            raise ReviewRefused(
                f"lineage {owner} is compromised ({current.compromised}): no review of its "
                "repository can prove who wrote what; nothing ran"
            )
        if current.pending is not None:
            # Held shared by us, the lock is free of any writer: this write is dead.
            raise ReviewRefused(str(unfinalized(state, current, current.common_dir))) from None
        found[owner] = current
    return found


def _range(
    identity: RepoIdentity,
    environ: Mapping[str, str],
    state: Path,
    *,
    head_ref: str | None,
    base_ref: str | None,
    reviewed: LineageState | None,
) -> tuple[str, str]:
    """``(head, base)`` as commits: pinned once, and only now (§3.8.4 step 3)."""
    if reviewed is not None:
        head = _resolve(identity, f"refs/heads/{reviewed.branch}", environ, state)
        if head is None:
            raise ReviewRefused(
                f"the branch {reviewed.branch} of lineage {reviewed.owner} no longer exists: "
                "nothing to review; nothing ran"
            )
        if reviewed.base is None:
            raise ReviewRefused(f"lineage {reviewed.owner} records no base; nothing ran")
        return head, reviewed.base
    head_name = head_ref or "HEAD"
    head = _resolve(identity, head_name, environ, state)
    if head is None:
        raise ReviewRefused(f"--head {head_name} does not resolve to a commit; nothing ran")
    base_name = base_ref or DEFAULT_BASE
    base = _resolve(identity, base_name, environ, state)
    if base is None:
        raise ReviewRefused(
            f"--base {base_name} does not resolve to a commit; name the base with --base; "
            "nothing ran"
        )
    return head, base


def prepare(
    *,
    run_id: str,
    run_dir: Path,
    state: Path,
    identity: RepoIdentity,
    start: Path,
    environ: Mapping[str, str],
    head_ref: str | None,
    base_ref: str | None,
    reviewed_lineage: str | None,
    reviewers: Mapping[str, Sequence[str]],
) -> Prepared:
    """Steps 1-6 of §3.8.4, then the patch and the detached worktree; :class:`ReviewRefused`."""
    common = identity.common_dir
    with ExitStack() as stack:
        try:
            stack.enter_context(
                held(
                    lineages.registry_lock(state),
                    rank=Rank.LINEAGE_REGISTRY,
                    exclusive=False,
                    wait=locks.LOCK_WAIT_SECONDS,
                    what="the lineage registry lock",
                )
            )
            owners = sorted(
                set(lineages.of_repository(state, common)) | set(source_lineages(state, start))
            )
            for owner in owners:
                stack.enter_context(
                    held(
                        lineages.lineage_lock(state, owner),
                        rank=Rank.LINEAGE,
                        key=owner,
                        exclusive=False,
                        wait=locks.LOCK_WAIT_SECONDS,
                        what=f"the lineage lock of {owner}",
                    )
                )
        except LockTimeout as exc:
            raise ReviewRefused(f"{exc}: a write holds it; nothing ran") from None
        # Step 1 checks under the locks: a write that finished while this review waited
        # may have quarantined the repository or left an unconfined intent.
        refusal = quarantine.check(state, common)
        if refusal is not None:
            raise ReviewRefused(f"{refusal}; nothing ran")
        try:
            check_unconfined_intent(state, run_id)
        except WriteRefused as exc:
            raise ReviewRefused(str(exc)) from None
        admitted = _admitted(state, owners)
        reviewed = None
        if reviewed_lineage is not None:
            reviewed = admitted.get(reviewed_lineage)
            if reviewed is None:
                raise ReviewRefused(
                    f"--run: lineage {reviewed_lineage} does not belong to "
                    f"{identity.work_tree}; nothing ran"
                )
        head, base = _range(
            identity, environ, state, head_ref=head_ref, base_ref=base_ref, reviewed=reviewed
        )
        code, out, err = _git(identity, ["merge-base", base, head], environ, state)
        if code != 0 or not out.strip():
            raise ReviewRefused(f"{base[:12]} and {head[:12]} have no merge base; nothing ran")
        merge_base = out.strip()
        code, patch, err = _git(identity, ["diff", "--binary", merge_base, head], environ, state)
        if code != 0:
            raise ReviewRefused(f"git diff failed: {err.strip()}; nothing ran")
        if not patch.strip():
            raise ReviewRefused(
                f"the diff from {merge_base[:12]} to {head[:12]} is empty: nothing to review"
            )
        code, log, err = _git(
            identity,
            ["log", "--reverse", "--format=%H%x00%s", f"{merge_base}..{head}"],
            environ,
            state,
        )
        if code != 0:
            raise ReviewRefused(f"git log failed: {err.strip()}; nothing ran")
        commits = [tuple(line.split("\x00", 1)) for line in log.splitlines() if line]
        try:
            check = check_independence(
                attribute(state, [(sha, subject) for sha, subject in commits]), reviewers
            )
        except VendorRefused as exc:
            raise ReviewRefused(f"{exc}; nothing ran") from None
        reviews.write_check(state, run_id, check)
    # The lineage and registry locks are released: the pinned commit no write can change.
    (run_dir / PATCH_FILE).write_text(patch, encoding="utf-8", errors="replace")
    worktree = run_dir / WORKTREE
    code, _, err = _git(
        identity, ["worktree", "add", "-q", "--detach", str(worktree), head], environ, state
    )
    if code != 0:
        raise ReviewRefused(f"git worktree add failed: {err.strip()}; nothing ran")
    return Prepared(head=head, merge_base=merge_base, patch=patch, check=check, worktree=worktree)


def remove_worktree(
    worktree: Path, identity: RepoIdentity, environ: Mapping[str, str], state: Path
) -> str | None:
    """Remove the detached worktree through git; the reason when it cannot."""
    refusal = quarantine.check(state, identity.common_dir)
    if refusal is not None:
        return refusal
    try:
        code, _, err = _git(
            identity, ["worktree", "remove", "--force", str(worktree)], environ, state
        )
    except ReviewRefused as exc:
        return str(exc)
    return None if code == 0 else (err.strip() or "git worktree remove failed")


def finish(
    *,
    run_id: str,
    state: Path,
    identity: RepoIdentity,
    environ: Mapping[str, str],
    prepared: Prepared,
    verdict: Verdict | None,
    text: str | None,
) -> dict[str, object]:
    """The result, written once when a verdict was read, then the cleanup (§3.8.4 step 6)."""
    if verdict is not None:
        reviews.write_result(
            state,
            run_id,
            head=prepared.head,
            verdict=verdict,
            text=text or "",
            check=prepared.check,
        )
    reason = remove_worktree(prepared.worktree, identity, environ, state)
    return {"status": "done"} if reason is None else {"status": "failed", "reason": reason}


__all__ = [
    "CHANGES_EXIT_CODE",
    "DEFAULT_BASE",
    "Prepared",
    "ReviewRefused",
    "WORKTREE",
    "finish",
    "prepare",
    "remove_worktree",
]
