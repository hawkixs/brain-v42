"""The vendor rule: no reviewer shares a vendor with the code's author (spec 0.5.0 §3.8.4).

Pure decisions over the state directory, no git: the review flow lists the
commits of ``<merge-base>..<head>`` and their subjects, then calls
:func:`attribute` and :func:`check_independence` under its locks.

- A commit with provenance takes its recorded providers.
- A ``chore(ha):`` commit without provenance -- a 0.4.0 write run, a lost state
  directory, a subject typed by hand -- refuses the review: a 0.4.0 chain that
  succeeded on its first link leaves no trace of the links it declared.
- Any other commit without provenance is hand-written (``made_by: hand``, no
  provider) only while ``unconfined-writers.json`` records no unconfined write;
  otherwise it is attributed to the union of their providers (``made_by:
  unknown``), because ``ha`` cannot see everything such a write did (§3.8.0).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, get_args

from . import provenance
from .state import Unknown, read_optional
from .write_flow import UNCONFINED_WRITERS

#: The subject prefix of every commit ``ha`` makes (§3.6).
HA_SUBJECT: Final = "chore(ha):"
#: What a check records for each commit: the three of provenance, ``unknown`` for an
#: unconfined writer's possible commit, ``hand`` for a presumed hand-written one.
MADE_BY: Final = frozenset({"engine", "agent", "hook", "unknown", "hand"})
#: What a provenance record may say: the three of a recorded commit, and ``unknown``.
_RECORDED_MADE_BY: Final = frozenset(get_args(provenance.MadeBy))
_SHA: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class VendorRefused(Exception):  # noqa: N818 - a refusal, not a crash
    """The review cannot prove independence: exit ``2``, naming why (§3.8.4)."""


@dataclass(frozen=True)
class AttributedCommit:
    sha: str
    #: The run that recorded it; ``None`` without provenance.
    run_id: str | None
    made_by: str
    providers: tuple[str, ...]

    def to_document(self) -> dict[str, object]:
        return {
            "sha": self.sha,
            "run_id": self.run_id,
            "made_by": self.made_by,
            "providers": list(self.providers),
        }


@dataclass(frozen=True)
class VendorCheck:
    """What a review proved before its reviewers started (§3.8.6), ``vendor_check`` in reports."""

    commits: tuple[AttributedCommit, ...]
    #: The union of the providers attributed to the range, sorted.
    authors: tuple[str, ...]
    #: Every reviewer role with the providers of every link of its chain.
    reviewers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def to_document(self) -> dict[str, object]:
        return {
            "commits": [commit.to_document() for commit in self.commits],
            "authors": list(self.authors),
            "reviewers": {role: list(providers) for role, providers in self.reviewers.items()},
        }

    @classmethod
    def from_document(cls, document: Mapping[str, object], *, where: str) -> VendorCheck:
        """The check ``document`` states; :class:`Unknown` when it is not one ``ha`` writes."""

        def texts(value: object) -> tuple[str, ...]:
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise Unknown(f"{where}: vendor check is malformed")
            return tuple(value)

        raw_commits, raw_reviewers = document.get("commits"), document.get("reviewers")
        if not isinstance(raw_commits, list) or not isinstance(raw_reviewers, dict):
            raise Unknown(f"{where}: vendor check is malformed")
        commits = []
        for raw in raw_commits:
            if not isinstance(raw, dict):
                raise Unknown(f"{where}: vendor check is malformed")
            sha, run_id, made_by = raw.get("sha"), raw.get("run_id"), raw.get("made_by")
            if (
                not isinstance(sha, str)
                or not _SHA.fullmatch(sha)
                or not (run_id is None or isinstance(run_id, str))
                or not isinstance(made_by, str)
                or made_by not in MADE_BY
            ):
                raise Unknown(f"{where}: vendor check is malformed")
            commits.append(
                AttributedCommit(
                    sha=sha,
                    run_id=run_id,
                    made_by=str(made_by),
                    providers=texts(raw.get("providers")),
                )
            )
        authors = texts(document.get("authors"))
        reviewers = {str(role): texts(providers) for role, providers in raw_reviewers.items()}
        return cls(commits=tuple(commits), authors=authors, reviewers=reviewers)


def _unconfined_providers(state: Path) -> tuple[str, ...]:
    """The union of the providers of every recorded unconfined write; ``()`` for none."""
    path = state / UNCONFINED_WRITERS
    try:
        document = read_optional(path)
    except Unknown as exc:
        raise VendorRefused(
            f"{path} is unknown ({exc}): a commit without provenance cannot be presumed "
            "hand-written; recover the file by hand"
        ) from None
    if document is None:
        return ()
    writers = document.get("writers")
    if not isinstance(writers, list):
        raise VendorRefused(f"{path} is malformed: recover it by hand")
    found: list[str] = []
    for writer in writers:
        providers = writer.get("providers") if isinstance(writer, dict) else None
        if not isinstance(providers, list) or not all(isinstance(p, str) for p in providers):
            raise VendorRefused(f"{path} is malformed: recover it by hand")
        found.extend(p for p in providers if p not in found)
    return tuple(found)


def _recorded(sha: str, record: Mapping[str, object]) -> AttributedCommit:
    """The commit a provenance record states; :class:`VendorRefused` when it is not one ``ha``
    writes -- a record read as no provider would let an author's vendor review its code, and
    unknown is never empty (§3.8.1)."""
    run_id, made_by, providers = (
        record.get("run_id"),
        record.get("made_by"),
        record.get("providers"),
    )
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(made_by, str)
        or made_by not in _RECORDED_MADE_BY
        or not isinstance(providers, list)
        or not providers
        or not all(isinstance(p, str) and p for p in providers)
    ):
        raise VendorRefused(
            f"the provenance record of {sha[:12]} is malformed: its authors cannot be proven; "
            "recover it by hand"
        )
    return AttributedCommit(
        sha=sha, run_id=run_id, made_by=str(made_by), providers=tuple(providers)
    )


def attribute(state: Path, commits: Sequence[tuple[str, str]]) -> tuple[AttributedCommit, ...]:
    """Every ``(sha, subject)`` of the range attributed (§3.8.4 step 4), in the given order."""
    writers: tuple[str, ...] | None = None
    attributed = []
    for sha, subject in commits:
        try:
            record = provenance.lookup(state, sha)
        except Unknown as exc:
            raise VendorRefused(f"the provenance of {sha[:12]} is unknown ({exc})") from None
        if record is not None:
            attributed.append(_recorded(sha, record))
            continue
        if subject.startswith(HA_SUBJECT):
            raise VendorRefused(
                f"commit {sha[:12]} says {HA_SUBJECT} but has no provenance (a 0.4.0 write run, "
                "a lost state directory, or a subject typed by hand): its authors cannot be proven"
            )
        if writers is None:
            writers = _unconfined_providers(state)
        made_by = "unknown" if writers else "hand"
        attributed.append(
            AttributedCommit(sha=sha, run_id=None, made_by=made_by, providers=writers)
        )
    return tuple(attributed)


def check_independence(
    commits: Sequence[AttributedCommit], reviewers: Mapping[str, Sequence[str]]
) -> VendorCheck:
    """The check, or :class:`VendorRefused` naming the reviewer, provider and commit (step 5).

    The judge is not constrained: only the reviewers are passed here.
    """
    authors: list[str] = []
    for commit in commits:
        authors.extend(p for p in commit.providers if p not in authors)
    for role, providers in reviewers.items():
        for provider in providers:
            if provider in authors:
                sha = next(c.sha for c in commits if provider in c.providers)
                raise VendorRefused(
                    f"reviewer {role} runs {provider}, which wrote commit {sha[:12]} of the "
                    "reviewed range: a reviewer must not share a vendor with the code's author"
                )
    return VendorCheck(
        commits=tuple(commits),
        authors=tuple(sorted(authors)),
        reviewers={role: tuple(providers) for role, providers in reviewers.items()},
    )


__all__ = [
    "AttributedCommit",
    "HA_SUBJECT",
    "MADE_BY",
    "VendorCheck",
    "VendorRefused",
    "attribute",
    "check_independence",
]
