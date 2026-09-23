from pathlib import Path

import pytest

from headless_agents.capability import INVALID_USAGE_EXIT_CODE
from headless_agents.context import ContextBundle, ContextFile
from headless_agents.profile import CapabilityProfile, Workspace
from headless_agents.spec import RunSpec
from headless_agents.workspace import (
    argv_prompt_or_refusal,
    prepend,
    rail_preamble,
    workspace_of,
    workspace_summary,
)


def _file(tmp_path: Path, scope: str, text: str) -> ContextFile:
    return ContextFile(
        source=tmp_path / f"{scope}.md",
        scope=scope,
        content=text,  # type: ignore[arg-type]
        size_bytes=len(text),
        sha256="0" * 64,
    )


def test_both_workspaces_is_refused(tmp_path: Path) -> None:
    spec = RunSpec(
        prompt="p",
        workspace=tmp_path,
        profile=CapabilityProfile(workspace=Workspace(path=tmp_path)),
    )
    with pytest.raises(ValueError, match="both"):
        workspace_of(spec)


def test_no_workspace_no_context_means_no_preamble() -> None:
    assert rail_preamble(RunSpec(prompt="p"), tools_note="x") == ""


def test_read_only_preamble_names_path_and_carries_repository(tmp_path: Path) -> None:
    bundle = ContextBundle(
        level="full", files=(_file(tmp_path, "repository", "REPO"), _file(tmp_path, "user", "USER"))
    )
    spec = RunSpec(
        prompt="p", context=bundle, profile=CapabilityProfile(workspace=Workspace(path=tmp_path))
    )
    preamble = rail_preamble(spec, tools_note="read tools")
    assert str(tmp_path) in preamble and "read tools" in preamble
    assert "REPO" in preamble and "USER" in preamble


def test_write_preamble_leaves_repository_to_the_file(tmp_path: Path) -> None:
    bundle = ContextBundle(
        level="full", files=(_file(tmp_path, "repository", "REPO"), _file(tmp_path, "user", "USER"))
    )
    spec = RunSpec(
        prompt="p",
        context=bundle,
        profile=CapabilityProfile(workspace=Workspace(path=tmp_path, write=True)),
    )
    preamble = rail_preamble(spec, tools_note="edit tools")
    assert "USER" in preamble and "REPO" not in preamble


def test_prepend_is_identity_without_preamble() -> None:
    assert prepend("", "the task") == "the task"


def test_prepend_wraps_the_task() -> None:
    assert prepend("PRE", "the task") == "PRE\n\n<task>\nthe task\n</task>"


def test_argv_refusal_counts_bytes_not_chars() -> None:
    assert argv_prompt_or_refusal("é" * 3, 6) is None
    assert (
        argv_prompt_or_refusal("é" * 4, 6) == "prompt with context too long for argv: 8 bytes > 6"
    )


def test_invalid_usage_code() -> None:
    assert INVALID_USAGE_EXIT_CODE == 2


def test_summary(tmp_path: Path) -> None:
    assert workspace_summary(None) is None
    assert workspace_summary(Workspace(path=tmp_path, write=True)) == {
        "path": str(tmp_path),
        "write": True,
        "shell": False,
    }
