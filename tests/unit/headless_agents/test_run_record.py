"""What every rail does after its process exits: read the answer, record the run."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from headless_agents.result import RunResult
from headless_agents.run_record import answer_text, record, run_id_of
from headless_agents.spec import RunSpec


def _result(**overrides: object) -> RunResult:
    fields: dict[str, object] = {
        "exit_code": 0,
        "provider": "codex",
        "model": "m",
        "report_path": None,
        "events_log": None,
        "tokens": None,
        "duration_seconds": 0.5,
        "tool_call_completed": False,
    }
    fields.update(overrides)
    return RunResult(**fields)  # type: ignore[arg-type]


class TestAnswerText:
    def test_an_answer_that_is_json_comes_back_verbatim(self, tmp_path: Path) -> None:
        path = tmp_path / "report.log"
        path.write_text('{"result": "pass", "score": 3}\n', encoding="utf-8")
        assert answer_text(path, exit_code=0) == '{"result": "pass", "score": 3}\n'

    def test_a_failed_run_has_no_answer_even_with_a_report(self, tmp_path: Path) -> None:
        path = tmp_path / "report.log"
        path.write_text("half an answer", encoding="utf-8")
        assert answer_text(path, exit_code=1) is None
        assert answer_text(path, exit_code=3) is None
        assert answer_text(path, exit_code=124) is None

    def test_an_absent_or_blank_file_is_no_answer(self, tmp_path: Path) -> None:
        blank = tmp_path / "blank.log"
        blank.write_text(" \n\t\n", encoding="utf-8")
        assert answer_text(None, exit_code=0) is None
        assert answer_text(tmp_path / "absent.log", exit_code=0) is None
        assert answer_text(blank, exit_code=0) is None

    def test_offset_skips_what_a_previous_run_left_in_a_reused_log(self, tmp_path: Path) -> None:
        path = tmp_path / "raw.log"
        path.write_text("PREVIOUS RUN\n", encoding="utf-8")
        offset = path.stat().st_size
        with path.open("a", encoding="utf-8") as stream:
            stream.write("THIS RUN\n")
        assert answer_text(path, exit_code=0, offset=offset) == "THIS RUN\n"

    def test_undecodable_bytes_are_replaced_never_raised(self, tmp_path: Path) -> None:
        path = tmp_path / "report.log"
        path.write_bytes(b"ok \xff\n")
        assert answer_text(path, exit_code=0) == "ok �\n"


class TestRunIdOf:
    def test_is_the_name_of_the_run_directory(self, tmp_path: Path) -> None:
        assert run_id_of(RunSpec(prompt="P", run_dir=tmp_path / "20260923-a1")) == "20260923-a1"

    def test_is_none_without_a_run_directory(self) -> None:
        assert run_id_of(RunSpec(prompt="P")) is None

    def test_dot_is_named_by_the_current_directorys_resolved_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert run_id_of(RunSpec(prompt="P", run_dir=Path("."))) == tmp_path.name

    def test_a_symlinked_run_directory_keeps_the_name_of_the_link(self, tmp_path: Path) -> None:
        # ``runs/latest -> runs/2026-09-24T01``: a reader listing run
        # directories sees ``latest``; naming the run after the link target
        # would give one run two ids depending on how it was reached.
        (tmp_path / "2026-09-24T01").mkdir()
        (tmp_path / "latest").symlink_to(tmp_path / "2026-09-24T01")
        assert run_id_of(RunSpec(prompt="P", run_dir=tmp_path / "latest")) == "latest"


class TestRecord:
    def test_without_run_dir_nothing_is_written(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = _result()
        assert record(RunSpec(prompt="P"), result) is result
        assert list(tmp_path.iterdir()) == []

    def test_writes_the_schema_1_dict_into_a_run_dir_it_creates(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "runs" / "r1"
        result = _result(text="déjà ✓", run_id="r1")
        assert record(RunSpec(prompt="P", run_dir=run_dir), result) is result
        written = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert written == result.to_dict()
        assert sorted(path.name for path in run_dir.iterdir()) == ["result.json"]

    def test_run_dir_under_a_regular_file_does_not_raise_and_notes_the_failure(
        self, tmp_path: Path
    ) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        stderr_log = tmp_path / "stderr.log"
        spec = RunSpec(prompt="P", run_dir=blocker / "run1", stderr_log=stderr_log)
        result = _result()
        assert record(spec, result) is result
        assert not (blocker / "run1").exists()
        note = stderr_log.read_text(encoding="utf-8")
        assert "NotADirectoryError" in note
        assert "ENOTDIR" in note

    def test_read_only_run_dir_does_not_raise_and_leaves_no_result_file(
        self, tmp_path: Path
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("root bypasses directory permissions")
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        run_dir.chmod(0o500)
        try:
            spec = RunSpec(prompt="P", run_dir=run_dir)
            result = _result()
            assert record(spec, result) is result
            assert not (run_dir / "result.json").exists()
            assert not (run_dir / ".result.json.partial").exists()
        finally:
            run_dir.chmod(0o700)

    def test_result_json_is_owner_only(self, tmp_path: Path) -> None:
        # Pinned on purpose (follow-up review of PR #197): mkstemp creates the
        # temporary file 0600 and the rename keeps it, so result.json -- the
        # run's text, model and paths -- is readable by its owner only. Every
        # reader (``ha runs``, the caller) is that same user.
        run_dir = tmp_path / "run1"
        record(RunSpec(prompt="P", run_dir=run_dir), _result())
        assert (run_dir / "result.json").stat().st_mode & 0o777 == 0o600

    def test_a_partial_file_it_did_not_create_is_never_deleted(self, tmp_path: Path) -> None:
        # Independent review of PR #197, finding 2: a fixed partial name meant a
        # failed write deleted a ``.result.json.partial`` this invocation never
        # created -- a read-only leftover, or a concurrent writer's.
        if os.geteuid() == 0:
            pytest.skip("root bypasses file permissions")
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        foreign = run_dir / ".result.json.partial"
        foreign.write_text("someone else's", encoding="utf-8")
        foreign.chmod(0o400)
        result = _result()
        assert record(RunSpec(prompt="P", run_dir=run_dir), result) is result
        assert foreign.read_text(encoding="utf-8") == "someone else's"
        assert json.loads((run_dir / "result.json").read_text(encoding="utf-8")) == (
            result.to_dict()
        )

    def test_a_failed_rename_removes_only_its_own_temporary_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        foreign = run_dir / ".result.json.partial"
        foreign.write_text("someone else's", encoding="utf-8")
        stderr_log = tmp_path / "stderr.log"

        def _refuse(source: object, destination: object) -> None:
            raise PermissionError(13, "refused")

        monkeypatch.setattr(os, "replace", _refuse)
        result = _result()
        spec = RunSpec(prompt="P", run_dir=run_dir, stderr_log=stderr_log)
        assert record(spec, result) is result
        assert sorted(path.name for path in run_dir.iterdir()) == [".result.json.partial"]
        assert foreign.read_text(encoding="utf-8") == "someone else's"
        assert "PermissionError" in stderr_log.read_text(encoding="utf-8")

    def test_run_dir_being_a_file_does_not_raise(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run1"
        run_dir.write_text("i am a file, not a run directory", encoding="utf-8")
        spec = RunSpec(prompt="P", run_dir=run_dir)
        result = _result()
        assert record(spec, result) is result
        assert run_dir.is_file()
