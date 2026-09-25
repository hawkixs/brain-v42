"""Lineage state and per-commit provenance (spec 0.5.0 §3.8.1, §3.8.3)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from headless_agents import lineage, provenance
from headless_agents.lineage import LineageState, PendingWrite
from headless_agents.state import Unknown

_OWNER = "20260925T000000-aaaaaaaa"


def _lineage(tmp_path: Path, owner: str = _OWNER, common: str = "repo/.git") -> LineageState:
    return LineageState(
        owner=owner,
        repository=tmp_path / "repo",
        common_dir=tmp_path / common,
        worktree=tmp_path / "wt" / owner,
        branch=f"ha/{owner}",
        base="0" * 40,
        members={owner: "running"},
        pending=None,
        compromised=None,
    )


def test_paths_live_in_the_state(tmp_path: Path) -> None:
    assert lineage.lineage_path(tmp_path, _OWNER) == tmp_path / "lineages" / f"{_OWNER}.json"
    assert lineage.lineage_lock(tmp_path, _OWNER) == tmp_path / "lineages" / f"{_OWNER}.lock"
    assert lineage.registry_lock(tmp_path) == tmp_path / "lineages.lock"


def test_a_lineage_round_trips(tmp_path: Path) -> None:
    state = tmp_path / "state"
    pending = PendingWrite(
        run_id=_OWNER, providers=("codex",), unconfined=False, start_tip="1" * 40, start_reflog=3
    )
    original = replace(_lineage(tmp_path), pending=pending)
    lineage.create(state, original)
    assert lineage.load(state, _OWNER) == original
    saved = replace(original, pending=None, members={_OWNER: "committed"}, compromised="x")
    lineage.save(state, saved)
    assert lineage.load(state, _OWNER) == saved


def test_create_twice_raises(tmp_path: Path) -> None:
    state = tmp_path / "state"
    lineage.create(state, _lineage(tmp_path))
    with pytest.raises(FileExistsError):
        lineage.create(state, _lineage(tmp_path))


def test_a_lineage_naming_another_owner_is_unknown(tmp_path: Path) -> None:
    state = tmp_path / "state"
    lineage.create(state, _lineage(tmp_path))
    path = lineage.lineage_path(state, _OWNER)
    document = json.loads(path.read_text())
    document["owner"] = "20260925T000000-bbbbbbbb"
    path.write_text(json.dumps(document))
    with pytest.raises(Unknown):
        lineage.load(state, _OWNER)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.pop("members"),
        lambda d: d.update(members=["x"]),
        lambda d: d.update(pending={"run_id": 3}),
        lambda d: d.update(branch=None),
    ],
)
def test_a_malformed_lineage_is_unknown(tmp_path: Path, mutation: object) -> None:
    state = tmp_path / "state"
    lineage.create(state, _lineage(tmp_path))
    path = lineage.lineage_path(state, _OWNER)
    document = json.loads(path.read_text())
    mutation(document)  # type: ignore[operator]
    path.write_text(json.dumps(document))
    with pytest.raises(Unknown):
        lineage.load(state, _OWNER)


def test_a_missing_lineage_is_unknown(tmp_path: Path) -> None:
    with pytest.raises(Unknown):
        lineage.load(tmp_path, _OWNER)


def test_of_repository_groups_worktrees_of_one_repository(tmp_path: Path) -> None:
    state = tmp_path / "state"
    b, a, other = (
        "20260925T000000-bbbbbbbb",
        "20260925T000000-aaaaaaaa",
        "20260925T000000-cccccccc",
    )
    lineage.create(state, _lineage(tmp_path, b))
    lineage.create(state, _lineage(tmp_path, a))
    lineage.create(state, _lineage(tmp_path, other, common="other/.git"))
    assert lineage.of_repository(state, tmp_path / "repo/.git") == [a, b]


def test_of_repository_includes_an_unreadable_lineage(tmp_path: Path) -> None:
    """It will read as Unknown and refuse: never silently skipped."""
    state = tmp_path / "state"
    lineage.create(state, _lineage(tmp_path))
    broken = "20260925T000000-dddddddd"
    lineage.lineage_path(state, broken).write_text("{not json")
    assert lineage.of_repository(state, tmp_path / "repo/.git") == [_OWNER, broken]


def test_of_repository_without_lineages_is_empty(tmp_path: Path) -> None:
    assert lineage.of_repository(tmp_path, tmp_path / "repo/.git") == []


def test_a_provenance_record_is_written_once(tmp_path: Path) -> None:
    sha = "a" * 40
    provenance.record(
        tmp_path, sha, run_id=_OWNER, lineage=_OWNER, made_by="engine", providers=["codex"]
    )
    assert provenance.lookup(tmp_path, sha) == {
        "sha": sha,
        "run_id": _OWNER,
        "lineage": _OWNER,
        "made_by": "engine",
        "providers": ["codex"],
    }
    with pytest.raises(FileExistsError):
        provenance.record(
            tmp_path, sha, run_id=_OWNER, lineage=_OWNER, made_by="agent", providers=[]
        )
    assert provenance.lookup(tmp_path, "b" * 40) is None


def test_provenance_refuses_what_is_not_a_sha(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a commit sha"):
        provenance.record(
            tmp_path, "../x", run_id=_OWNER, lineage=_OWNER, made_by="engine", providers=[]
        )
