"""The vendor rule: who wrote each commit of a range, and whether a reviewer shares a vendor
with them (spec 0.5.0 §3.8.4 steps 4-5, §3.8.6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from headless_agents import provenance
from headless_agents.state import Unknown, publish
from headless_agents.vendor import (
    AttributedCommit,
    VendorCheck,
    VendorRefused,
    attribute,
    check_independence,
)

SHA_1 = "1" * 40
SHA_2 = "2" * 40
SHA_3 = "3" * 40
RUN = "20260926T100000-aaaaaaaa"
OTHER = "20260926T110000-bbbbbbbb"


def _record(state: Path, sha: str, providers: list[str], made_by: str = "engine") -> None:
    provenance.record(state, sha, run_id=RUN, lineage=RUN, made_by=made_by, providers=providers)  # type: ignore[arg-type]


def _writers(state: Path, *providers: list[str]) -> None:
    publish(
        state / "unconfined-writers.json",
        {"writers": [{"run_id": OTHER, "repository": "/r", "providers": p} for p in providers]},
    )


def test_a_recorded_commit_takes_its_recorded_providers(tmp_path: Path) -> None:
    _record(tmp_path, SHA_1, ["opencode", "codex"])
    (commit,) = attribute(tmp_path, [(SHA_1, "chore(ha): x implement via opencode/m")])
    assert commit == AttributedCommit(
        sha=SHA_1, run_id=RUN, made_by="engine", providers=("opencode", "codex")
    )


def test_a_commit_without_provenance_is_hand_written_when_no_unconfined_write_ran(
    tmp_path: Path,
) -> None:
    (commit,) = attribute(tmp_path, [(SHA_2, "fix: a typo")])
    assert commit == AttributedCommit(sha=SHA_2, run_id=None, made_by="hand", providers=())


def test_a_commit_without_provenance_is_attributed_to_every_unconfined_writer(
    tmp_path: Path,
) -> None:
    """§3.8.4 step 4: once an unconfined write ran, nothing is presumed hand-written."""
    _writers(tmp_path, ["claude"], ["opencode", "claude"])
    (commit,) = attribute(tmp_path, [(SHA_2, "fix: a typo")])
    assert commit == AttributedCommit(
        sha=SHA_2, run_id=None, made_by="unknown", providers=("claude", "opencode")
    )


@pytest.mark.parametrize("subject", ["chore(ha): 2026 implement via codex/m", "chore(ha):"])
def test_a_chore_ha_commit_without_provenance_refuses(tmp_path: Path, subject: str) -> None:
    """A 0.4.0 write run or a lost state directory: no legacy fallback (§3.8.4 step 4)."""
    with pytest.raises(VendorRefused, match=rf"{SHA_3[:12]}.*chore\(ha\).*no provenance"):
        attribute(tmp_path, [(SHA_3, subject)])


def test_an_unreadable_provenance_record_refuses(tmp_path: Path) -> None:
    (tmp_path / "provenance").mkdir()
    (tmp_path / "provenance" / f"{SHA_1}.json").write_text("{not json")
    with pytest.raises(VendorRefused, match="unknown"):
        attribute(tmp_path, [(SHA_1, "anything")])


@pytest.mark.parametrize(
    "change",
    [
        {"providers": "codex"},
        {"providers": []},
        {"providers": ["codex", ""]},
        {"providers": ["codex", 7]},
        {"providers": None},
        {"made_by": "hand"},
        {"made_by": None},
        {"made_by": []},
        {"made_by": {}},
        {"run_id": None},
        {"run_id": ""},
        {"run_id": 7},
    ],
)
def test_a_malformed_provenance_record_refuses(tmp_path: Path, change: dict[str, object]) -> None:
    """A readable record that is not one ``ha`` writes is unknown, never empty (§3.8.1)."""
    publish(
        tmp_path / "provenance" / f"{SHA_1}.json",
        {
            "sha": SHA_1,
            "run_id": RUN,
            "lineage": RUN,
            "made_by": "engine",
            "providers": ["codex"],
            **change,
        },
    )
    with pytest.raises(VendorRefused, match="malformed"):
        attribute(tmp_path, [(SHA_1, "chore(ha): x implement via codex/m")])


def test_an_unreadable_writers_list_refuses(tmp_path: Path) -> None:
    """Unknown is never empty (§3.8.1): an unreadable list cannot presume a hand."""
    (tmp_path / "unconfined-writers.json").write_text("{not json")
    with pytest.raises(VendorRefused, match="unconfined-writers.json"):
        attribute(tmp_path, [(SHA_2, "fix: a typo")])


@pytest.mark.parametrize(
    "document",
    [
        {"writers": [{"run_id": OTHER, "repository": "/r", "providers": []}]},
        {"writers": [{"run_id": OTHER, "repository": "/r", "providers": ["claude", ""]}]},
        {"writers": [{"repository": "/r", "providers": ["claude"]}]},
        {"writers": [{"run_id": "", "repository": "/r", "providers": ["claude"]}]},
        {"writers": ["claude"]},
        {"writers": {"claude": 1}},
        {},
    ],
)
def test_a_malformed_writers_list_refuses(tmp_path: Path, document: dict[str, object]) -> None:
    """Codex review of PR A: a writer with no provider made the union empty, and a commit
    without provenance was then presumed hand-written although an unconfined write ran."""
    publish(tmp_path / "unconfined-writers.json", document)
    with pytest.raises(VendorRefused, match="malformed"):
        attribute(tmp_path, [(SHA_2, "fix: a typo")])


def test_a_writers_list_rebuilt_after_it_was_unreadable_refuses(tmp_path: Path) -> None:
    """A write that found the list unreadable starts a new one and marks it: the writers it
    lost are unknown, so no commit without provenance can be presumed hand-written."""
    publish(
        tmp_path / "unconfined-writers.json",
        {
            "writers": [{"run_id": OTHER, "repository": "/r", "providers": ["claude"]}],
            "unreadable_before": True,
        },
    )
    with pytest.raises(VendorRefused, match="unreadable"):
        attribute(tmp_path, [(SHA_2, "fix: a typo")])


def test_independent_reviewers_pass_and_the_check_says_who_wrote_what(tmp_path: Path) -> None:
    _record(tmp_path, SHA_1, ["opencode", "codex"])
    commits = attribute(tmp_path, [(SHA_1, "chore(ha): x"), (SHA_2, "docs: by hand")])
    check = check_independence(commits, {"reviewer-agy": ("agy",), "reviewer-claude": ("claude",)})
    assert check.authors == ("codex", "opencode")
    assert check.to_document() == {
        "commits": [
            {"sha": SHA_1, "run_id": RUN, "made_by": "engine", "providers": ["opencode", "codex"]},
            {"sha": SHA_2, "run_id": None, "made_by": "hand", "providers": []},
        ],
        "authors": ["codex", "opencode"],
        "reviewers": {"reviewer-agy": ["agy"], "reviewer-claude": ["claude"]},
    }


def test_a_reviewer_sharing_any_link_with_an_author_is_refused_naming_all_three(
    tmp_path: Path,
) -> None:
    """Every link of the reviewer's chain counts (§3.8.4 step 5)."""
    _record(tmp_path, SHA_1, ["opencode", "codex"])
    commits = attribute(tmp_path, [(SHA_1, "chore(ha): x")])
    with pytest.raises(VendorRefused, match=rf"reviewer-pair.*codex.*{SHA_1[:12]}"):
        check_independence(commits, {"reviewer-pair": ("agy", "codex")})


def test_a_hand_written_commit_constrains_nothing(tmp_path: Path) -> None:
    commits = attribute(tmp_path, [(SHA_2, "docs: by hand")])
    check = check_independence(commits, {"reviewer-codex": ("codex",)})
    assert check.authors == ()


def test_a_check_round_trips_through_its_document() -> None:
    check = VendorCheck(
        commits=(
            AttributedCommit(sha=SHA_1, run_id=RUN, made_by="agent", providers=("claude",)),
            AttributedCommit(sha=SHA_2, run_id=None, made_by="hand", providers=()),
            AttributedCommit(sha=SHA_3, run_id=None, made_by="unknown", providers=("agy",)),
        ),
        authors=("agy", "claude"),
        reviewers={"r": ("codex",)},
    )
    assert VendorCheck.from_document(check.to_document(), where="x") == check


def _commit(**change: object) -> dict[str, object]:
    return {"sha": SHA_1, "run_id": RUN, "made_by": "engine", "providers": ["claude"], **change}


@pytest.mark.parametrize(
    ("commits", "authors", "reviewers"),
    [
        ([_commit()], [], {"r": ["codex"]}),
        ([_commit()], ["claude", "agy"], {"r": ["codex"]}),
        ([_commit(made_by="hand", run_id=None)], ["claude"], {"r": ["codex"]}),
        ([_commit(made_by="hand", providers=[])], [], {"r": ["codex"]}),
        ([_commit(run_id=None)], ["claude"], {"r": ["codex"]}),
        ([_commit(providers=[])], [], {"r": ["codex"]}),
        ([_commit(made_by="unknown", run_id=None, providers=[])], [], {"r": ["codex"]}),
        ([_commit()], ["claude"], {"r": ["codex", "claude"]}),
        ([_commit()], ["claude"], {}),
        ([_commit()], ["claude"], {"r": []}),
    ],
)
def test_a_check_ha_could_not_have_made_is_unknown(
    commits: list[object], authors: list[str], reviewers: dict[str, object]
) -> None:
    """Codex review of PR A, round 2: every field well-typed is not enough -- the authors are
    the union of the commits' providers, each commit's fields are an attribution ``ha``
    makes, and every reviewer is independent of them."""
    document = {"commits": commits, "authors": authors, "reviewers": reviewers}
    with pytest.raises(Unknown, match="where"):
        VendorCheck.from_document(document, where="where")


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"commits": "x", "authors": [], "reviewers": {}},
        {"commits": [{"sha": "nope"}], "authors": [], "reviewers": {}},
        {"commits": [], "authors": [1], "reviewers": {}},
        {"commits": [], "authors": [], "reviewers": {"r": "codex"}},
        {
            "commits": [{"sha": "1" * 40, "run_id": None, "made_by": [], "providers": []}],
            "authors": [],
            "reviewers": {},
        },
    ],
)
def test_a_malformed_check_is_unknown(document: dict[str, object]) -> None:
    with pytest.raises(Unknown, match="where"):
        VendorCheck.from_document(document, where="where")
