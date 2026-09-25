"""Quarantines: the widest scope a fired tripwire reaches, published and checked (spec 0.5.0 §3.8.5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from headless_agents import quarantine

_RUN = "20260925T000000-aaaaaaaa"


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    common = tmp_path / "repo" / ".git"
    return {
        "worktree": tmp_path / "wt",
        "git_dir": common / "worktrees" / "wt",
        "common_dir": common,
        "home": tmp_path / "home",
    }


def _scope(
    layout: dict[str, Path], *paths: Path | str, environ: dict[str, str] | None = None
) -> str:
    return quarantine.widest_scope(
        [str(p) for p in paths],
        worktree=layout["worktree"],
        git_dir=layout["git_dir"],
        common_dir=layout["common_dir"],
        home=layout["home"],
        environ=environ or {},
    )


def test_the_worktree_git_file_is_the_lineage(layout: dict[str, Path]) -> None:
    assert _scope(layout, layout["worktree"] / ".git") == "lineage"


def test_the_worktree_git_dir_is_the_lineage(layout: dict[str, Path]) -> None:
    assert _scope(layout, layout["git_dir"] / "config.worktree") == "lineage"
    assert _scope(layout, layout["git_dir"] / "hooks" / "pre-commit") == "lineage"


def test_a_hooks_path_inside_the_worktree_is_the_lineage(layout: dict[str, Path]) -> None:
    assert _scope(layout, layout["worktree"] / ".husky" / "pre-commit") == "lineage"


def test_the_common_dir_config_is_the_repository(layout: dict[str, Path]) -> None:
    assert _scope(layout, layout["common_dir"] / "config") == "repository"
    assert _scope(layout, layout["common_dir"] / "hooks" / "post-checkout") == "repository"


def test_the_operator_gitconfig_is_the_operator(layout: dict[str, Path]) -> None:
    assert _scope(layout, layout["home"] / ".gitconfig") == "operator"


def test_the_xdg_git_config_is_the_operator(layout: dict[str, Path], tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    assert _scope(layout, xdg / "git" / "config", environ={"XDG_CONFIG_HOME": str(xdg)}) == (
        "operator"
    )


def test_a_hooks_path_outside_the_repository_is_the_operator(
    layout: dict[str, Path], tmp_path: Path
) -> None:
    assert _scope(layout, tmp_path / "shared-hooks" / "pre-commit") == "operator"


def test_the_widest_scope_wins(layout: dict[str, Path]) -> None:
    assert (
        _scope(layout, layout["worktree"] / ".git", layout["common_dir"] / "config") == "repository"
    )
    assert (
        _scope(layout, layout["common_dir"] / "config", layout["home"] / ".gitconfig") == "operator"
    )


def test_a_lexically_escaping_path_is_not_inside(layout: dict[str, Path]) -> None:
    assert _scope(layout, f"{layout['worktree']}/../elsewhere/x") == "operator"


def test_check_is_clear_without_quarantines(tmp_path: Path, layout: dict[str, Path]) -> None:
    assert quarantine.check(tmp_path / "state", layout["common_dir"]) is None


def test_a_repository_quarantine_refuses_that_repository_only(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    state = tmp_path / "state"
    quarantine.publish(
        state,
        "repository",
        reason="tripwire",
        run_id=_RUN,
        paths=[str(layout["common_dir"] / "config")],
        common_dir=layout["common_dir"],
    )
    refusal = quarantine.check(state, layout["common_dir"])
    assert refusal is not None and "repository" in refusal and _RUN in refusal
    assert quarantine.check(state, tmp_path / "other" / ".git") is None
    assert quarantine.check(state, None) is None


def test_the_operator_quarantine_refuses_everywhere_and_first(
    tmp_path: Path, layout: dict[str, Path]
) -> None:
    state = tmp_path / "state"
    quarantine.publish(
        state,
        "repository",
        reason="tripwire",
        run_id=_RUN,
        paths=[],
        common_dir=layout["common_dir"],
    )
    quarantine.publish(
        state, "operator", reason="unfinalized_write", run_id=_RUN, paths=[], common_dir=None
    )
    refusal = quarantine.check(state, layout["common_dir"])
    assert refusal is not None and refusal.startswith("operator quarantine")
    assert quarantine.check(state, None) is not None


def test_a_corrupt_quarantine_file_refuses(tmp_path: Path, layout: dict[str, Path]) -> None:
    state = tmp_path / "state"
    path = quarantine.quarantine_path(state, "repository", layout["common_dir"])
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    refusal = quarantine.check(state, layout["common_dir"])
    assert refusal is not None and "unreadable" in refusal


def test_the_first_quarantine_is_kept(tmp_path: Path, layout: dict[str, Path]) -> None:
    state = tmp_path / "state"
    for reason in ("first", "second"):
        quarantine.publish(state, "operator", reason=reason, run_id=_RUN, paths=[], common_dir=None)
    refusal = quarantine.check(state, None)
    assert refusal is not None and "first" in refusal and "second" not in refusal


def test_a_lineage_scope_is_not_a_quarantine_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lineage"):
        quarantine.publish(tmp_path, "lineage", reason="x", run_id=_RUN, paths=[], common_dir=None)


def test_a_repository_quarantine_needs_its_common_dir(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="common dir"):
        quarantine.publish(
            tmp_path, "repository", reason="x", run_id=_RUN, paths=[], common_dir=None
        )


def test_active_lists_every_quarantine_the_operators_first(tmp_path: Path) -> None:
    state = tmp_path / "state"
    common = tmp_path / "repo" / ".git"
    common.mkdir(parents=True)
    quarantine.publish(
        state, "repository", reason="tripwire", run_id="r1", paths=["x"], common_dir=common
    )
    quarantine.publish(
        state, "operator", reason="stale unconfined intent", run_id="r2", paths=[], common_dir=None
    )
    found = quarantine.active(state)
    assert [(q["scope"], q["run_id"], q["readable"]) for q in found] == [
        ("operator", "r2", True),
        ("repository", "r1", True),
    ]


def test_active_lists_an_unreadable_quarantine(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "quarantine").mkdir(parents=True)
    (state / "quarantine" / "operator.json").write_text("{not json")
    (entry,) = quarantine.active(state)
    assert entry["readable"] is False and "does not parse" in str(entry["reason"])


def test_active_without_a_quarantine_is_empty(tmp_path: Path) -> None:
    assert quarantine.active(tmp_path / "state") == []
