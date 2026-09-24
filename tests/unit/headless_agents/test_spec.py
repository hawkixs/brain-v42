"""``RunSpec.run_dir``: one directory per run, fixed log names, explicit paths win."""

from __future__ import annotations

from pathlib import Path

import pytest

from headless_agents.spec import RUN_DIR_LOG_NAMES, RunSpec


class TestWithRunDirDefaults:
    def test_without_run_dir_the_spec_is_returned_unchanged(self, tmp_path: Path) -> None:
        spec = RunSpec(prompt="P", report_log=tmp_path / "r.log")
        assert spec.with_run_dir_defaults() is spec

    def test_every_unset_log_takes_its_fixed_name_inside_run_dir(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "runs" / "r1"
        spec = RunSpec(prompt="P", run_dir=run_dir).with_run_dir_defaults()
        assert spec.report_log == run_dir / "report.log"
        assert spec.events_log == run_dir / "events.jsonl"
        assert spec.stderr_log == run_dir / "stderr.log"
        assert spec.raw_log == run_dir / "raw.log"

    def test_an_explicit_log_path_wins_over_run_dir(self, tmp_path: Path) -> None:
        explicit = tmp_path / "elsewhere" / "final.txt"
        spec = RunSpec(
            prompt="P", run_dir=tmp_path / "r1", report_log=explicit
        ).with_run_dir_defaults()
        assert spec.report_log == explicit
        assert spec.events_log == tmp_path / "r1" / "events.jsonl"

    def test_the_original_spec_is_left_as_it_was(self, tmp_path: Path) -> None:
        spec = RunSpec(prompt="P", run_dir=tmp_path / "r1")
        spec.with_run_dir_defaults()
        assert spec.report_log is None

    def test_the_fixed_names_are_the_documented_ones(self) -> None:
        assert dict(RUN_DIR_LOG_NAMES) == {
            "report_log": "report.log",
            "events_log": "events.jsonl",
            "stderr_log": "stderr.log",
            "raw_log": "raw.log",
        }


class TestRunDirValidation:
    def test_a_run_dir_resolving_to_the_filesystem_root_is_refused(self) -> None:
        with pytest.raises(ValueError, match="run_dir"):
            RunSpec(prompt="P", run_dir=Path("/"))

    def test_dot_resolves_to_a_non_empty_name_and_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        spec = RunSpec(prompt="P", run_dir=Path("."))
        assert spec.run_dir == Path(".")

    def test_a_named_run_dir_is_accepted(self, tmp_path: Path) -> None:
        spec = RunSpec(prompt="P", run_dir=tmp_path / "runs" / "r1")
        assert spec.run_dir == tmp_path / "runs" / "r1"

    def test_without_run_dir_nothing_is_validated(self) -> None:
        RunSpec(prompt="P")
